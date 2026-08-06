"""
scanner_observation.py
=======================
The shared OBSERVATION layer: macro bias, market context, and market
facts (structure/order-block/liquidity-sweep/fib/FVG detection).

Both scanner_live.py (for the real-time trade decision) and
min_scanner.py (to rebuild the same `facts`/`ctx` objects the
Experimental Lab and friends need) import this module. Everything
here is PURE with respect to `state` — functions return
(result, updates) and the CALLER decides whether to persist the
updates via apply_state_updates(). scanner_live.py is the only
caller that ever actually saves those updates back to state.json;
min_scanner.py can call the exact same functions against a
read-only load_state() snapshot to reconstruct facts without ever
writing to live state.
"""

import numpy as np
import pandas as pd
from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from scanner_common import (
    PIP_SIZE, _REMOVE, apply_state_updates, atr, close_location,
    find_all_fractals, detect_bos_impulse,
    INVALIDATION_RETRACE, HTF_STRUCTURE_WING, HTF_EMA_SLOPE_BARS,
    HTF_CONSOLIDATION_ATR_MULT, HTF_EMA_FLAT_THRESHOLD,
    PROMOTION_MIN_BREAK_COUNT, SESSION_WINDOWS_UTC,
    REGIME_SHIFT_ENABLED, REGIME_SHIFT_SHORT_PERIOD, REGIME_SHIFT_LONG_PERIOD,
    REGIME_SHIFT_THRESHOLD, REGIME_SHIFT_OPEN_WARMUP,
    POST_SPIKE_COOLDOWN_BASE, POST_SPIKE_COOLDOWN_SCALE, POST_SPIKE_COOLDOWN_MAX,
    ATR_MIN_PIPS, ATR_WARN_PIPS,
    FIB_ZONE_NEAR, FIB_ZONE_FAR, FIB_SCORE_MIN_FRACTION,
    SWEEP_LOOKBACK_CANDLES, SWEEP_MAX_DISTANCE_PIPS,
    OB_MIN_DISPLACEMENT_ATR_MULT, OB_OPPOSING_LOOKBACK_CANDLES,
    FVG_LOOKBACK_CANDLES, FVG_MIN_SIZE_ATR_MULT, FVG_MAX_AGE_CANDLES,
    ZONE_TOLERANCE_PIPS, ENGULF_TOLERANCE_PIPS, ATR_ENGULF_MIN,
    ENGULF_CLOSE_LOCATION_MIN, SWING_LOOKBACK_15, STATE_MEMORY_MAX_DRIFT_PIPS,
    BOS_15M_BREAK_BUFFER_ATR_MULT, LEG_MATCH_TOLERANCE_PIPS, FRACTAL_WING,
    classify_conviction, SL_ATR_MULT_COMPRESSED, SL_VOL_SPIKE_RATIO, SL_ATR_MULT,
    PHASE_EXHAUSTION_MIN_BREAK_COUNT, PHASE_EXHAUSTION_EMA_DIST_ATR_MULT,
    PHASE_HISTORY_MAX_LEN, MEASURED_MOVE_BUCKETS,
)


def check_leg_anchor_survival(state, prefix, window_df, invalidation_retrace=INVALIDATION_RETRACE):
    """
    PURE version of what used to be `_refresh_leg_anchor` — reads a
    standing leg anchor (state[prefix+"_direction"/"_origin"/"_extreme"])
    and checks it directly against window_df's High/Low, WITHOUT
    mutating state. Returns (survived: bool, updates: dict) — apply via
    apply_state_updates(). This is what distinguishes "the origin
    fractal scrolled out of the fetch window" from "the leg actually
    broke," without the caller losing visibility into what changed.
    """
    anchor_dir     = state.get(prefix + "_direction")
    anchor_origin  = state.get(prefix + "_origin")
    anchor_extreme = state.get(prefix + "_extreme")
    anchor_time    = state.get(prefix + "_origin_time")

    if (anchor_dir not in ("BULLISH", "BEARISH") or anchor_origin is None or
            anchor_extreme is None or anchor_time is None):
        return False, {}

    try:
        anchor_ts = pd.Timestamp(anchor_time)
        observed = window_df.loc[window_df.index > anchor_ts]
    except Exception:
        return False, {}

    if observed.empty:
        return True, {}

    window_low  = observed["Low"].min()
    window_high = observed["High"].max()

    if anchor_dir == "BULLISH":
        new_extreme      = max(anchor_extreme, window_high)
        leg_range        = new_extreme - anchor_origin
        origin_violated  = window_low <= anchor_origin
        retrace_violated = leg_range > 0 and (new_extreme - window_low) / leg_range >= invalidation_retrace
    else:
        new_extreme      = min(anchor_extreme, window_low)
        leg_range        = anchor_origin - new_extreme
        origin_violated  = window_high >= anchor_origin
        retrace_violated = leg_range > 0 and (window_high - new_extreme) / leg_range >= invalidation_retrace

    if origin_violated or retrace_violated:
        return False, {
            prefix + "_direction": _REMOVE,
            prefix + "_origin": _REMOVE,
            prefix + "_extreme": _REMOVE,
            prefix + "_origin_time": _REMOVE,
        }

    return True, {prefix + "_extreme": new_extreme}


def _promotion_confirmed(break_count, df_1h, direction, min_break_count=PROMOTION_MIN_BREAK_COUNT):
    """
    Gates whether a 15M break is strong enough to promote/override a
    STALE 1H hold. Only meaningful while bias is already stale. Pure.
      1. break_count >= min_break_count (confirmed follow-through, not
         just the single reversal wick that founded it)
      2. EMA agreement — 1H close on the correct side of EMA_100
    """
    if break_count < min_break_count:
        return False, f"break_count {break_count} < {min_break_count} required"

    df_1h = df_1h.copy()
    df_1h["EMA_100"] = df_1h["Close"].ewm(span=100, adjust=False).mean()
    close_now = df_1h["Close"].iloc[-1]
    ema_now = df_1h["EMA_100"].iloc[-1]

    ema_agrees = (direction == "BULLISH" and close_now > ema_now) or \
                 (direction == "BEARISH" and close_now < ema_now)
    if not ema_agrees:
        return False, "1H close disagrees with EMA_100"

    return True, f"break_count {break_count} >= {min_break_count} and EMA agrees"


def _macro_swing_updates(direction, origin, extreme):
    """PURE helper — builds the state-update dict for the auditable 1H
    swing points backing the current confirmed bias, so state.json
    always shows exactly which swing points justify the current bias."""
    if direction == "BULLISH":
        swing_low, swing_high = origin, extreme
    else:
        swing_high, swing_low = origin, extreme
    return {
        "macro_swing_low": swing_low,
        "macro_swing_high": swing_high,
        "macro_swing_confirmed_at": datetime.now(timezone.utc).isoformat(),
    }


def compute_macro_bias(df_1h, df_15m, state):
    """
    PURE. The single authority for direction. Returns (bias, updates) —
    `state` is read-only here; the caller applies `updates` via
    apply_state_updates(). Returns "BULLISH", "BEARISH", or
    "CONSOLIDATION".

    Flip gate: ONLY a confirmed 1H BOS/CHoCH can change the held
    direction. A bare EMA_100 cross with no matching structural break
    does not flip bias.

    Also owns stale-bias promotion end-to-end: if the confirmed 1H leg
    has gone stale (invalidated with nothing fresh to replace it), a
    strong enough 15M BOS/CHoCH can promote a new direction — folded in
    here (rather than left for a tier or scan() to reach back into bias
    bookkeeping) so Macro Bias is the ONE place direction gets decided,
    full stop.
    """
    updates = {}
    df_1h_x = df_1h.copy()
    df_1h_x["EMA_100"] = df_1h_x["Close"].ewm(span=100, adjust=False).mean()
    df_1h_x["ATR_1H"] = atr(df_1h_x, period=14)

    close_now = df_1h_x["Close"].iloc[-1]
    ema_now = df_1h_x["EMA_100"].iloc[-1]
    atr_now = df_1h_x["ATR_1H"].iloc[-1]
    confirmed = state.get("macro_bias_confirmed")

    is_flat = False
    if not (pd.isna(atr_now) or atr_now == 0 or len(df_1h_x) <= HTF_EMA_SLOPE_BARS):
        ema_then = df_1h_x["EMA_100"].iloc[-1 - HTF_EMA_SLOPE_BARS]
        dist_in_atr = abs(close_now - ema_now) / atr_now
        slope_in_atr = abs(ema_now - ema_then) / atr_now
        is_flat = dist_in_atr < HTF_CONSOLIDATION_ATR_MULT and slope_in_atr < HTF_EMA_FLAT_THRESHOLD

    if is_flat:
        updates["macro_bias_confirmed"] = "CONSOLIDATION"
        updates["macro_bias_stale"] = False
        return "CONSOLIDATION", updates

    bos_1h = detect_bos_impulse(df_1h_x, wing=HTF_STRUCTURE_WING)
    if bos_1h is not None:
        bias = bos_1h["direction"]
        origin_idx = bos_1h.get("origin_idx")
        origin_time = (
            df_1h_x.index[origin_idx].isoformat()
            if origin_idx is not None and 0 <= origin_idx < len(df_1h_x) else None
        )
        updates.update({
            "macro_bias_confirmed": bias,
            "macro_bias_stale": False,
            "macro_leg_direction": bias,
            "macro_leg_origin": bos_1h["impulse_start"],
            "macro_leg_extreme": bos_1h["impulse_end"],
            "macro_leg_origin_time": origin_time,
            # Formation story — was this 1H leg a genuine CHoCH (flipped
            # from an opposite-direction dominant) or a fresh continuation?
            # Captured here, at leg-birth, for Forward Observation Facet 2.
            "macro_leg_was_choch": bos_1h.get("was_choch", False),
        })
        updates.update(_macro_swing_updates(bias, bos_1h["impulse_start"], bos_1h["impulse_end"]))
        return bias, updates

    survived, anchor_updates = check_leg_anchor_survival(state, "macro_leg", df_1h_x)
    if survived:
        updates.update(anchor_updates)
        bias = state.get("macro_leg_direction")
        updates["macro_bias_confirmed"] = bias
        updates["macro_bias_stale"] = False
        return bias, updates

    if confirmed in ("BULLISH", "BEARISH"):
        bias = confirmed
        stale = True

        # ── Stale-bias promotion via 15M structure ──────────────────
        early_lookback = df_15m.tail(SWING_LOOKBACK_15)
        early_bos = detect_bos_impulse(
            early_lookback, wing=FRACTAL_WING,
            break_buffer_atr_mult=BOS_15M_BREAK_BUFFER_ATR_MULT,
        )
        if early_bos is None:
            survived_15, anchor_updates_15 = check_leg_anchor_survival(state, "leg15", early_lookback)
            if survived_15:
                updates.update(anchor_updates_15)
                early_bos = {
                    "direction": state.get("leg15_direction"),
                    "impulse_start": state.get("leg15_origin"),
                    "impulse_end": anchor_updates_15.get("leg15_extreme", state.get("leg15_extreme")),
                    "break_count": state.get("leg15_break_count", 1),
                }
        else:
            early_origin_idx = early_bos.get("origin_idx")
            early_origin_time = (
                early_lookback.index[early_origin_idx].isoformat()
                if early_origin_idx is not None and 0 <= early_origin_idx < len(early_lookback) else None
            )
            updates.update({
                "leg15_direction": early_bos["direction"],
                "leg15_origin": early_bos["impulse_start"],
                "leg15_extreme": early_bos["impulse_end"],
                "leg15_origin_time": early_origin_time,
                "leg15_break_count": early_bos["break_count"],
            })

        if early_bos is not None and early_bos.get("direction") not in (None, bias):
            promo_ok, promo_reason = _promotion_confirmed(
                early_bos.get("break_count", 1), df_1h_x, early_bos["direction"])
            if promo_ok:
                bias = early_bos["direction"]
                stale = False
                updates.update({
                    "macro_bias_confirmed": bias,
                    "macro_bias_stale": False,
                    "macro_leg_direction": bias,
                    "macro_leg_origin": early_bos["impulse_start"],
                    "macro_leg_extreme": early_bos["impulse_end"],
                    "macro_leg_origin_time": updates.get(
                        "leg15_origin_time", state.get("leg15_origin_time")),
                    # A 15M promotion replaces a stale 1H direction — it is
                    # NOT a 1H CHoCH (the 1H already pointed this way, just
                    # stale). Forward Observation Facet 2 tracks this as False.
                    "macro_leg_was_choch": False,
                })
                updates.update(_macro_swing_updates(bias, early_bos["impulse_start"], early_bos["impulse_end"]))
                print(f"  [BIAS] 15M BOS ({bias}) reconfirms over stale 1H hold — {promo_reason}")

        if stale:
            updates["macro_bias_stale"] = True
        return bias, updates

    raw_bias = "BULLISH" if close_now > ema_now else "BEARISH"
    updates["macro_bias_confirmed"] = raw_bias
    updates["macro_bias_stale"] = True
    return raw_bias, updates


def compute_macro_bias_shadow_old_rule(df_1h, state):
    """
    SHADOW ONLY — never used for trading decisions. PURE, same
    (result, updates) shape as compute_macro_bias. Mirrors the pre-gate
    behavior: any 1H break, either direction, flips bias immediately (no
    CHoCH+BOS follow-through requirement, no EMA agreement). Reads/
    writes only shadow_-prefixed keys.
    """
    updates = {}
    df_1h_x = df_1h.copy()
    df_1h_x["EMA_100"] = df_1h_x["Close"].ewm(span=100, adjust=False).mean()
    df_1h_x["ATR_1H"] = atr(df_1h_x, period=14)

    close_now = df_1h_x["Close"].iloc[-1]
    ema_now = df_1h_x["EMA_100"].iloc[-1]
    atr_now = df_1h_x["ATR_1H"].iloc[-1]
    confirmed = state.get("shadow_macro_bias_confirmed")

    is_flat = False
    if not (pd.isna(atr_now) or atr_now == 0 or len(df_1h_x) <= HTF_EMA_SLOPE_BARS):
        ema_then = df_1h_x["EMA_100"].iloc[-1 - HTF_EMA_SLOPE_BARS]
        dist_in_atr = abs(close_now - ema_now) / atr_now
        slope_in_atr = abs(ema_now - ema_then) / atr_now
        is_flat = dist_in_atr < HTF_CONSOLIDATION_ATR_MULT and slope_in_atr < HTF_EMA_FLAT_THRESHOLD

    if is_flat:
        updates["shadow_macro_bias_confirmed"] = "CONSOLIDATION"
        return "CONSOLIDATION", updates

    bos_1h = detect_bos_impulse(df_1h_x, wing=HTF_STRUCTURE_WING)
    if bos_1h is not None:
        origin_idx = bos_1h.get("origin_idx")
        origin_time = (
            df_1h_x.index[origin_idx].isoformat()
            if origin_idx is not None and 0 <= origin_idx < len(df_1h_x) else None
        )
        updates["shadow_macro_bias_confirmed"] = bos_1h["direction"]
        updates["shadow_macro_leg_direction"]  = bos_1h["direction"]
        updates["shadow_macro_leg_origin"]     = bos_1h["impulse_start"]
        updates["shadow_macro_leg_extreme"]    = bos_1h["impulse_end"]
        updates["shadow_macro_leg_origin_time"] = origin_time
        return bos_1h["direction"], updates

    survived, anchor_updates = check_leg_anchor_survival(state, "shadow_macro_leg", df_1h_x)
    if survived:
        updates.update(anchor_updates)
        return state.get("shadow_macro_leg_direction"), updates

    if confirmed in ("BULLISH", "BEARISH"):
        return confirmed, updates

    raw_bias = "BULLISH" if close_now > ema_now else "BEARISH"
    updates["shadow_macro_bias_confirmed"] = raw_bias
    return raw_bias, updates



# =========================================================================
# MARKET PHASE — read-only narrative layer. Relabels observations the
# scanner ALREADY makes (macro bias, staleness, break_count, EMA distance,
# liquidity sweeps at the swing boundary) into a phase taxonomy. Detects
# NOTHING new — every input here is already computed by compute_macro_bias
# or MarketFacts. PURE: returns (MarketPhase, updates); caller applies
# updates via apply_state_updates(), same shape as compute_macro_bias.
#
# NOT a live gate. No tier reads this. It exists so a human (Telegram log,
# /shadow drill-down) or later research (MIN) can ask "how old/tired is
# this trend" in one call — see the placeholder warning on
# PHASE_EXHAUSTION_MIN_BREAK_COUNT / PHASE_EXHAUSTION_EMA_DIST_ATR_MULT in
# scanner_common.py before ever wiring this into a live decision.
# =========================================================================
class Phase(Enum):
    EXPANSION    = "expansion"     # trending, fresh leg, not yet extended
    EXHAUSTION   = "exhaustion"    # still trending (or held-over/stale) but aging/extended
    TRANSITION   = "transition"    # no live confirmed leg — CONSOLIDATION or unconfirmed
    MANIPULATION = "manipulation"  # sweep-and-reclaim against the swing boundary inside TRANSITION


@dataclass
class MarketPhase:
    phase: Phase
    macro_bias: str          # BULLISH / BEARISH / CONSOLIDATION — direction phase applies to
    age_bars: int             # consecutive scans this exact phase has held
    history: tuple            # last PHASE_HISTORY_MAX_LEN phase values before this one
    narrative: str            # human-readable summary for logs/Telegram
    extension: Optional[dict] = None   # measured-move dict from compute_measured_move_extension(), or None


def capture_prior_leg_snapshot(prev_direction, prev_origin, prev_extreme):
    """
    PURE. Caller (scanner_live.py) calls this ONLY when it detects
    macro_leg_origin is about to change — i.e. a genuinely new 1H leg is
    replacing the old one, not the same leg's extreme just extending.
    Snapshots the OLD leg (direction/origin/extreme) as "prior_macro_leg_*"
    BEFORE compute_macro_bias()'s update overwrites macro_leg_* with the
    new leg, so compute_measured_move_extension() has an "AB" to compare
    the new "CD" leg against.

    Deliberately called from the OUTSIDE (comparing before/after state)
    rather than from inside compute_macro_bias() itself — this keeps the
    live bias function completely untouched; a bug here cannot alter
    macro_bias_confirmed, macro_leg_*, or anything compute_macro_bias()
    already owns.

    Returns {} when there's no previous leg to snapshot (e.g. the very
    first leg the bot has ever tracked, or a state.json with no leg
    history yet) — never fabricates a value.
    """
    if prev_origin is None or prev_extreme is None:
        return {}
    return {
        "prior_macro_leg_direction": prev_direction,
        "prior_macro_leg_origin": prev_origin,
        "prior_macro_leg_extreme": prev_extreme,
    }


def _measured_move_bucket(ratio):
    for lo, hi, label in MEASURED_MOVE_BUCKETS:
        if lo <= ratio < hi:
            return label
    return MEASURED_MOVE_BUCKETS[-1][2]


def compute_measured_move_extension(state):
    """
    PURE. Classic AB=CD comparison: the CURRENT confirmed 1H leg's length
    vs the leg immediately before it (see capture_prior_leg_snapshot()).
    Reads state only — no dataframes, no recompute.

    Returns None until a prior-leg snapshot actually exists (first leg
    the bot has ever tracked since this feature shipped, or right after
    a state.json reset) — never fabricates a ratio from a missing prior.
    Never a live gate — see MEASURED_MOVE_BUCKETS caveat in
    scanner_common.py; this is tagging/narrative only until MIN's
    scenario report has enough resolved samples per bucket to say
    anything about it.
    """
    cur_origin    = state.get("macro_leg_origin")
    cur_extreme   = state.get("macro_leg_extreme")
    prior_origin  = state.get("prior_macro_leg_origin")
    prior_extreme = state.get("prior_macro_leg_extreme")

    if None in (cur_origin, cur_extreme, prior_origin, prior_extreme):
        return None

    current_len = abs(cur_extreme - cur_origin)
    prior_len = abs(prior_extreme - prior_origin)
    if prior_len <= 0:
        return None

    ratio = current_len / prior_len
    return {
        "ratio": round(ratio, 3),
        "bucket": _measured_move_bucket(ratio),
        "current_leg_pips": round(current_len / PIP_SIZE, 1),
        "prior_leg_pips": round(prior_len / PIP_SIZE, 1),
    }


def _build_phase_narrative(macro_bias, phase, age_bars, history, extension=None):
    direction_label = {
        "BULLISH": "Bull", "BEARISH": "Bear", "CONSOLIDATION": "Neutral",
    }.get(macro_bias, macro_bias)

    if phase in (Phase.TRANSITION, Phase.MANIPULATION):
        label = phase.value.replace("_", " ").title()
    else:
        label = f"{direction_label} {phase.value.title()}"

    story = list(history[-3:]) + [phase.value]
    line = f"{label} ({age_bars} bars) | story: {' -> '.join(story)}"
    if extension is not None:
        line += f" | leg ext: {extension['ratio']*100:.0f}% of prior ({extension['bucket']})"
    return line


def compute_market_phase(df_1h, macro_bias, bias_stale, facts, state, extension=None):
    """
    PURE. See module-level docstring above. `facts` must be the
    MarketFacts instance already built for this scan (reused for its
    swing_high/swing_low and has_liquidity_sweep() — no new detection).
    `extension` is the (optional) dict from compute_measured_move_extension()
    — passed in rather than computed here so this function stays free of
    any assumption about WHEN the caller snapshots the prior leg.

    Independently recomputes EMA_100/ATR_1H/detect_bos_impulse() on
    df_1h rather than threading a new return value through
    compute_macro_bias() — same reasoning as
    compute_macro_bias_shadow_old_rule()'s independent recompute just
    above: keeps this a zero-risk add-on that cannot alter the live bias
    path even if it has a bug.
    """
    df_1h_x = df_1h.copy()
    df_1h_x["EMA_100"] = df_1h_x["Close"].ewm(span=100, adjust=False).mean()
    df_1h_x["ATR_1H"] = atr(df_1h_x, period=14)

    close_now = df_1h_x["Close"].iloc[-1]
    ema_now = df_1h_x["EMA_100"].iloc[-1]
    atr_now = df_1h_x["ATR_1H"].iloc[-1]

    bos_now = detect_bos_impulse(df_1h_x, wing=HTF_STRUCTURE_WING)

    if macro_bias == "CONSOLIDATION":
        phase = Phase.TRANSITION
    elif bias_stale:
        # The leg that founded this direction already invalidated and
        # nothing fresh has replaced it — _gate_stale_bias() already
        # treats this as "held-over, not live," which IS the exhaustion
        # signature: direction nominally intact, structural backing gone.
        phase = Phase.EXHAUSTION
    elif bos_now is None or bos_now["direction"] != macro_bias:
        # Not flagged stale yet, but an independent recompute can't
        # confirm a live matching leg either — conservative default
        # rather than guessing at a phase with no structural backing.
        phase = Phase.TRANSITION
    else:
        break_count = bos_now["break_count"]
        dist_in_atr = (
            abs(close_now - ema_now) / atr_now
            if not (pd.isna(atr_now) or atr_now == 0) else 0.0
        )
        aging = (
            break_count >= PHASE_EXHAUSTION_MIN_BREAK_COUNT or
            dist_in_atr >= PHASE_EXHAUSTION_EMA_DIST_ATR_MULT
        )
        phase = Phase.EXHAUSTION if aging else Phase.EXPANSION

    # MANIPULATION overlay: only meaningful while otherwise reading as
    # TRANSITION — a sweep-and-reclaim against the standing macro swing
    # boundary, using the exact sweep detector Tier 1/3 already gate on
    # (facts.has_liquidity_sweep), not a new detector.
    if phase == Phase.TRANSITION:
        swept_high = facts.swing_high is not None and facts.has_liquidity_sweep(facts.swing_high)
        swept_low  = facts.swing_low is not None and facts.has_liquidity_sweep(facts.swing_low)
        if swept_high or swept_low:
            phase = Phase.MANIPULATION

    prev_phase_str = state.get("market_phase")
    if prev_phase_str == phase.value:
        age_bars = state.get("market_phase_age_bars", 0) + 1
        history = list(state.get("market_phase_history", []))
    else:
        age_bars = 0
        history = list(state.get("market_phase_history", []))
        if prev_phase_str is not None:
            history.append(prev_phase_str)
        history = history[-PHASE_HISTORY_MAX_LEN:]

    updates = {
        "market_phase": phase.value,
        "market_phase_age_bars": age_bars,
        "market_phase_history": history,
    }

    narrative = _build_phase_narrative(macro_bias, phase, age_bars, history, extension=extension)

    result = MarketPhase(
        phase=phase, macro_bias=macro_bias, age_bars=age_bars,
        history=tuple(history), narrative=narrative, extension=extension,
    )
    return result, updates


# =========================================================================
# MARKET CONTEXT — is the environment tradeable right now? PURE: returns
# (ctx, reason, updates); caller applies updates via apply_state_updates.
# =========================================================================
def is_active_session(now_utc, windows=SESSION_WINDOWS_UTC):
    hour = now_utc.hour
    for start, end in windows:
        if start <= hour < end:
            return True
    return False


def detect_regime_shift(df_5m, current_atr, now_utc):
    """Compares short-term ATR (~25min) vs long-term ATR (~4h session
    baseline). Ratio above threshold = a news spike/liquidity event
    shifted the volatility environment. Pure."""
    if not REGIME_SHIFT_ENABLED:
        return False, 0.0, 0.0

    if len(df_5m) < REGIME_SHIFT_LONG_PERIOD + 2:
        return False, 0.0, 0.0

    session_open = now_utc.replace(hour=8, minute=0, second=0, microsecond=0)
    bars_since_open = int((now_utc - session_open).total_seconds() / 300)
    if 0 <= bars_since_open < REGIME_SHIFT_OPEN_WARMUP:
        return False, 0.0, 0.0

    atr_series = atr(df_5m, period=14)
    short_atr = atr_series.rolling(
        REGIME_SHIFT_SHORT_PERIOD, min_periods=REGIME_SHIFT_SHORT_PERIOD
    ).mean().iloc[-1]
    long_atr = atr_series.rolling(
        REGIME_SHIFT_LONG_PERIOD, min_periods=REGIME_SHIFT_LONG_PERIOD // 2
    ).mean().iloc[-1]

    if pd.isna(short_atr) or pd.isna(long_atr) or long_atr == 0:
        return False, 0.0, 0.0

    ratio = short_atr / long_atr
    return ratio >= REGIME_SHIFT_THRESHOLD, ratio, short_atr / PIP_SIZE


def scaled_cooldown_bars(regime_ratio):
    bars = POST_SPIKE_COOLDOWN_BASE + (regime_ratio - REGIME_SHIFT_THRESHOLD) * POST_SPIKE_COOLDOWN_SCALE
    return int(min(max(bars, POST_SPIKE_COOLDOWN_BASE), POST_SPIKE_COOLDOWN_MAX))


class MarketContext:
    """Single bundled read of 'is now tradeable at all,' handed to every
    tier so none of them repeat this work or disagree about it."""
    def __init__(self, atr_ok, current_atr, current_atr_pips,
                 regime_shifted, regime_ratio, post_spike_active, session_active,
                 low_atr_warning=False):
        self.atr_ok = atr_ok
        self.current_atr = current_atr
        self.current_atr_pips = current_atr_pips
        self.regime_shifted = regime_shifted
        self.regime_ratio = regime_ratio
        self.post_spike_active = post_spike_active
        self.session_active = session_active
        # Soft floor only — cleared the ATR_MIN_PIPS hard gate (atr_ok=True)
        # but still below ATR_WARN_PIPS. Never blocks anything; it's a flag
        # the live alert surfaces so a fired signal in thin conditions is
        # clearly labeled, not silently treated the same as a normal one.
        self.low_atr_warning = low_atr_warning

    @property
    def tradeable(self):
        """True only when volatility, session, and cooldown conditions permit trading."""
        return self.atr_ok and self.session_active and not self.post_spike_active


def evaluate_market_context(df_5m, state, now_utc):
    """
    PURE. Builds one MarketContext for this scan. Returns
    (ctx, reason, updates) — updates carries the new
    post_spike_cooldown_remaining value only; caller applies via
    apply_state_updates(). Computes its own ATR series locally rather
    than depending on the caller having attached one to df_5m (Stage 1
    mutated the caller's dataframe to do this — fixed here).
    """
    atr_series = atr(df_5m, period=14)
    current_atr = atr_series.iloc[-1]
    if pd.isna(current_atr) or current_atr == 0:
        return (MarketContext(False, current_atr, 0.0, False, 0.0, False, False,
                               low_atr_warning=False),
                "ATR invalid (NaN/0)", {})

    current_atr_pips = current_atr / PIP_SIZE
    atr_ok = current_atr_pips >= ATR_MIN_PIPS
    # Only meaningful once the hard gate is cleared — a setup that never
    # activates because it's below ATR_MIN_PIPS has nothing to "warn" on.
    low_atr_warning = atr_ok and current_atr_pips < ATR_WARN_PIPS

    regime_shifted, regime_ratio, _short_pips = detect_regime_shift(df_5m, current_atr, now_utc)

    cooldown_remaining = state.get("post_spike_cooldown_remaining", 0)
    if regime_shifted:
        cooldown_remaining = scaled_cooldown_bars(regime_ratio)
    elif cooldown_remaining > 0:
        cooldown_remaining -= 1
    updates = {"post_spike_cooldown_remaining": max(0, cooldown_remaining)}
    post_spike_active = updates["post_spike_cooldown_remaining"] > 0

    session_active = is_active_session(now_utc)

    ctx = MarketContext(
        atr_ok=atr_ok, current_atr=current_atr, current_atr_pips=current_atr_pips,
        regime_shifted=regime_shifted, regime_ratio=regime_ratio,
        post_spike_active=post_spike_active, session_active=session_active,
        low_atr_warning=low_atr_warning,
    )
    if not atr_ok:
        reason = f"ATR {current_atr_pips:.1f}p < {ATR_MIN_PIPS}p floor"
    elif not session_active:
        reason = "outside configured London/New York sessions"
    elif post_spike_active:
        reason = f"post-spike cooldown active ({updates['post_spike_cooldown_remaining']} bars remaining)"
    else:
        reason = None
    return ctx, reason, updates


# =========================================================================
# STRUCTURE PRIMITIVES — pure building blocks the MarketFacts layer
# wraps. None of these decide whether a tier fires.
# =========================================================================
def adaptive_fib_ratio(df_5m, current_atr, near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR, lookback=50):
    """Fib zone gets SHALLOWER as volatility expands relative to its own
    recent average."""
    atr_series = atr(df_5m, period=14)
    avg_atr = atr_series.rolling(lookback, min_periods=10).mean().iloc[-1]
    if pd.isna(avg_atr) or avg_atr == 0 or pd.isna(current_atr):
        return (near + far) / 2

    expansion = current_atr / avg_atr
    expansion = max(0.5, min(2.0, expansion))
    t = (expansion - 0.5) / (2.0 - 0.5)
    return far - (t * (far - near))


def compute_fib_fraction(swing_high, swing_low, extremity, macro_bias,
                          near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR):
    """How deep an entry reached into its own NEAR-FAR band, 0-1."""
    structural_range = swing_high - swing_low
    if structural_range <= 0:
        return 0.0
    if macro_bias == "BULLISH":
        retrace_fraction = (swing_high - extremity) / structural_range
    else:
        retrace_fraction = (extremity - swing_low) / structural_range
    if far <= near:
        return 1.0
    fraction = (retrace_fraction - near) / (far - near)
    return max(0.0, min(1.0, fraction))


def detect_liquidity_sweep(df_5m, df_15m, level, macro_bias,
                            lookback_candles=SWEEP_LOOKBACK_CANDLES):
    """Generic sweep detector against ANY level. Returns (swept, label,
    distance_pips). distance_pips is how far price traded beyond `level`
    before reclaiming it (None when not swept) — added per chat as one
    of the fingerprint facts; the boolean/label behavior is UNCHANGED,
    this only adds a third return value."""
    recent = df_5m.tail(lookback_candles)

    if macro_bias == "BULLISH":
        swept = recent[(recent["Low"] < level) & (recent["Close"] > level)]
        if swept.empty:
            return False, "no sweep", None
        if df_15m.index[-1] + pd.Timedelta(minutes=15) < swept.index[-1] + pd.Timedelta(minutes=5):
            return False, "awaiting 15M confirmation", None
        if df_15m.iloc[-1]["Close"] <= level:
            return False, "15M closed below level", None
        distance_pips = round((level - swept["Low"].min()) / PIP_SIZE, 1)
        return True, "SWEEP CONFIRMED", distance_pips
    else:
        swept = recent[(recent["High"] > level) & (recent["Close"] < level)]
        if swept.empty:
            return False, "no sweep", None
        if df_15m.index[-1] + pd.Timedelta(minutes=15) < swept.index[-1] + pd.Timedelta(minutes=5):
            return False, "awaiting 15M confirmation", None
        if df_15m.iloc[-1]["Close"] >= level:
            return False, "15M closed above level", None
        distance_pips = round((swept["High"].max() - level) / PIP_SIZE, 1)
        return True, "SWEEP CONFIRMED", distance_pips


def detect_order_block(df_15m, bos, atr_series,
                        min_displacement_atr_mult=OB_MIN_DISPLACEMENT_ATR_MULT,
                        opposing_lookback=OB_OPPOSING_LOOKBACK_CANDLES):
    """
    Locates the supply/demand order block behind a confirmed BOS
    impulse: "the candle which led OR is immediately followed by an
    aggressive move which led to a strong BOS displacement." See Stage 1
    for the full method writeup. Returns a dict or None.

    FIX (found during audit): candles are bounded by BOTH price overlap
    AND recency (bos["origin_idx"] onward). Price overlap alone is not
    enough — GBPUSD frequently revisits the same handful of pips over a
    trading day, so an unrelated candle from hours earlier that happens
    to share the current leg's price band was able to win the
    displacement search (its local ATR context could easily make its
    ratio look larger than the real, current displacement candle's).
    `df_15m` passed in here MUST be the same window bos was computed
    from, so bos["origin_idx"] indexes correctly into it — see
    MarketFacts.order_block(), which passes the same lookback slice used
    for bos_15m() for exactly this reason.
    """
    if bos is None:
        return None

    direction = bos["direction"]
    start_price, end_price = bos["impulse_start"], bos["impulse_end"]
    origin_idx = bos.get("origin_idx")
    break_idx = bos.get("break_idx")

    lo_bound, hi_bound = sorted([start_price, end_price])
    price_mask = (df_15m["High"] >= lo_bound) & (df_15m["Low"] <= hi_bound)

    if origin_idx is not None and 0 <= origin_idx < len(df_15m):
        recency_mask = pd.Series(False, index=df_15m.index)
        recency_mask.iloc[origin_idx:] = True
        if break_idx is not None and origin_idx <= break_idx < len(df_15m):
            recency_mask.iloc[break_idx + 1:] = False
        in_leg_mask = price_mask & recency_mask
    else:
        # Defensive fallback only — every live call path now supplies
        # origin_idx. Keeps old price-only behavior rather than
        # silently returning None if it's ever missing.
        in_leg_mask = price_mask

    leg_idx = df_15m.index[in_leg_mask]
    if len(leg_idx) < 2:
        return None
    leg_df = df_15m.loc[leg_idx]
    leg_atr = atr_series.loc[leg_idx]

    candle_range = leg_df["High"] - leg_df["Low"]
    displacement_ratio = candle_range / leg_atr.replace(0, np.nan)
    if displacement_ratio.isna().all():
        return None

    displacement_pos = displacement_ratio.values.argmax()
    displacement_ratio_val = displacement_ratio.iloc[displacement_pos]
    if pd.isna(displacement_ratio_val) or displacement_ratio_val < min_displacement_atr_mult:
        return None

    displacement_idx_label = leg_df.index[displacement_pos]
    displacement_pos_global = df_15m.index.get_loc(displacement_idx_label)

    ob_candle = None
    ob_pos_global = None
    window_start = max(0, displacement_pos_global - opposing_lookback)
    for pos in range(displacement_pos_global - 1, window_start - 1, -1):
        candle = df_15m.iloc[pos]
        is_bear_candle = candle["Close"] < candle["Open"]
        is_bull_candle = candle["Close"] > candle["Open"]
        if direction == "BULLISH" and is_bear_candle:
            ob_candle, ob_pos_global = candle, pos
            break
        if direction == "BEARISH" and is_bull_candle:
            ob_candle, ob_pos_global = candle, pos
            break

    if ob_candle is None and displacement_pos_global > 0:
        ob_pos_global = displacement_pos_global - 1
        ob_candle = df_15m.iloc[ob_pos_global]

    if ob_candle is None:
        return None

    ob_high, ob_low = float(ob_candle["High"]), float(ob_candle["Low"])

    # Real mitigation tracking (per chat — was a TODO stub returning
    # False unconditionally, meaning the same OB could trigger repeat
    # signals as price oscillated around it in a ranging market).
    #
    # An OB counts as MITIGATED if price already traded back into its
    # zone on some PRIOR closed candle, AFTER the displacement leg that
    # defined the OB completed — i.e. this is not price's first return
    # since the OB formed. Scan starts at displacement_pos_global + 1,
    # NOT ob_pos_global + 1: the displacement candle itself routinely
    # opens from right inside/against the OB zone (that's the move AWAY
    # from it) and would otherwise be misread as an immediate return —
    # caught via testing (Case A below), not just by reading the logic.
    #
    # The most recent CLOSED candle is deliberately EXCLUDED from this
    # scan: it may BE the current reaction that has_order_block()/
    # price_in_order_block() are being asked to evaluate right now — a
    # first touch must not be able to mark itself as already-mitigated.
    #
    # df_15m here is already closed-candles-only (fetch_ohlc drops the
    # still-forming bar before this function ever sees the data).
    mitigated = False
    mitigated_at_idx = None
    for pos in range(displacement_pos_global + 1, len(df_15m) - 1):
        candle = df_15m.iloc[pos]
        if candle["High"] >= ob_low and candle["Low"] <= ob_high:
            mitigated = True
            mitigated_at_idx = pos
            break

    return {
        "high": ob_high,
        "low": ob_low,
        "origin_idx": ob_pos_global,
        "displacement_idx": displacement_pos_global,
        "direction": direction,
        "mitigated": mitigated,
        "mitigated_at_idx": mitigated_at_idx,
    }


def detect_fvg(df, direction, lookback=FVG_LOOKBACK_CANDLES):
    """
    Fair Value Gap — classic 3-candle imbalance: candle[i-2] and
    candle[i] don't overlap at all, meaning candle[i-1] traded through a
    range neither neighbor retraced into.

      BULLISH gap: candle[i-2].High < candle[i].Low
      BEARISH gap: candle[i-2].Low  > candle[i].High

    Only returns gaps still OPEN as of the end of `df` — a gap counts as
    closed once a later candle has fully traded back through it.
    Returns a list of {high, low, idx} dicts, oldest first.
    """
    window = df.tail(lookback)
    highs, lows = window["High"].values, window["Low"].values
    n = len(window)
    gaps = []

    for i in range(2, n):
        if direction == "BULLISH":
            if highs[i - 2] < lows[i]:
                gap_low, gap_high = highs[i - 2], lows[i]
                closed = (lows[i + 1:] <= gap_low).any() if i + 1 < n else False
                if not closed:
                    gaps.append({
                        "high": float(gap_high), "low": float(gap_low), "idx": i,
                        "time": window.index[i].isoformat(),
                    })
        else:
            if lows[i - 2] > highs[i]:
                gap_low, gap_high = highs[i], lows[i - 2]
                closed = (highs[i + 1:] >= gap_high).any() if i + 1 < n else False
                if not closed:
                    gaps.append({
                        "high": float(gap_high), "low": float(gap_low), "idx": i,
                        "time": window.index[i].isoformat(),
                    })

    return gaps


def detect_significant_fvg(df_15m, direction, atr_series,
                            lookback=FVG_LOOKBACK_CANDLES,
                            min_size_atr_mult=FVG_MIN_SIZE_ATR_MULT,
                            max_age_candles=FVG_MAX_AGE_CANDLES):
    """
    SHADOW-ONLY. "Not every FVG is important" — this is the quality gate
    that keeps Experiment 3 (POI) from just logging every 3-candle
    imbalance. A gap only counts if BOTH:
      1. size >= min_size_atr_mult * ATR(15m as of the gap's middle
         candle) — a gap smaller than that is noise, not an imbalance
         institutions actually need to come back and fill.
      2. it formed within the last `max_age_candles` candles — an old,
         technically-still-open gap that price has drifted away from is
         a stale reference, not a live one.
    Returns the same list shape as detect_fvg(), each dict additionally
    carrying "size_atr_ratio" and "age_candles" for later logging/analysis.
    """
    window = df_15m.tail(lookback)
    atr_window = atr_series.reindex(window.index)
    n = len(window)
    raw_gaps = detect_fvg(df_15m, direction, lookback=lookback)

    quality_gaps = []
    for gap in raw_gaps:
        age = (n - 1) - gap["idx"]
        if age > max_age_candles:
            continue
        gap_atr = atr_window.iloc[gap["idx"]]
        if pd.isna(gap_atr) or gap_atr == 0:
            continue
        size = gap["high"] - gap["low"]
        size_atr_ratio = size / gap_atr
        if size_atr_ratio < min_size_atr_mult:
            continue
        enriched = dict(gap)
        enriched["size_atr_ratio"] = float(size_atr_ratio)
        enriched["age_candles"] = int(age)
        quality_gaps.append(enriched)

    return quality_gaps


def is_fib_zone_stale(c_spike, swing_high, swing_low, fib_zone, current_price):
    """Post-spike staleness check for a Tier 2 fib zone."""
    structural_range = swing_high - swing_low
    spike_range = c_spike["High"] - c_spike["Low"]

    if spike_range > structural_range:
        return True, (
            f"spike range {spike_range/PIP_SIZE:.1f}p > structural range "
            f"{structural_range/PIP_SIZE:.1f}p"
        )
    if c_spike["Close"] > swing_high:
        return True, f"spike close {c_spike['Close']:.5f} broke above SwH {swing_high:.5f}"
    if c_spike["Close"] < swing_low:
        return True, f"spike close {c_spike['Close']:.5f} broke below SwL {swing_low:.5f}"

    zone_drift = abs(current_price - fib_zone) / structural_range
    if zone_drift > 0.5:
        return True, (
            f"price {abs(current_price - fib_zone)/PIP_SIZE:.1f}p from zone "
            f"(>{structural_range * 0.5 / PIP_SIZE:.1f}p threshold)"
        )
    return False, "zone still valid"


# =========================================================================
# MARKET FACTS LAYER — pure observation. Knows BOS, CHoCH, Order Blocks,
# FVG, liquidity sweeps, swings, fib. Knows NOTHING about trading. Tiers
# ask it questions; it never says "this is a buy." Computed once per
# scan and handed to every tier — if a tier needs new information about
# the market that isn't here, add a fact HERE, never reach into raw OHLC
# from inside a tier.
# =========================================================================
class MarketFacts:
    def __init__(self, df_5m, df_15m, df_1h, macro_bias, swing_high, swing_low, now_utc):
        self.df_5m = df_5m
        self.df_15m = df_15m
        self.df_1h = df_1h
        self.macro_bias = macro_bias
        self.swing_high = swing_high
        self.swing_low = swing_low
        self.now_utc = now_utc

        self._atr_5m_series = atr(df_5m, period=14)
        self._atr_15m_series = atr(df_15m, period=14)

        # lazy caches — computed at most once per instance regardless of
        # how many tiers ask the same question this scan
        self._bos_15m_cache = "UNSET"
        self._ob_cache = "UNSET"

    # ---- raw candle access (still "facts," just at candle grain) ------
    def last_candle_5m(self):
        return self.df_5m.iloc[-1]

    def prev_candle_5m(self):
        return self.df_5m.iloc[-2]

    def current_atr_5m(self):
        return self._atr_5m_series.iloc[-1]

    def current_atr_5m_pips(self):
        return self.current_atr_5m() / PIP_SIZE

    def atr_percentile_15m(self):
        """Where the CURRENT 15m ATR sits within its own recent history
        (0-100). Self-contained — uses only self._atr_15m_series, which
        already exists on every MarketFacts instance, so this needed no
        new plumbing into any call site. Fingerprint fact (per chat),
        not a live gate."""
        series = self._atr_15m_series.dropna()
        if len(series) < 2:
            return None
        current = series.iloc[-1]
        return round((series < current).mean() * 100, 1)

    # ---- structure facts ------------------------------------------------
    def bos_15m(self):
        """Fresh 15M BOS/CHoCH leg, or None."""
        if self._bos_15m_cache == "UNSET":
            lookback = self.df_15m.tail(SWING_LOOKBACK_15)
            self._bos_15m_cache = detect_bos_impulse(
                lookback, wing=FRACTAL_WING,
                break_buffer_atr_mult=BOS_15M_BREAK_BUFFER_ATR_MULT,
            )
        return self._bos_15m_cache

    def has_fresh_bos_aligned_with_bias(self):
        b = self.bos_15m()
        return b is not None and b["direction"] == self.macro_bias

    def has_choch_15m(self):
        """Whether the current 15M leg is a direction FLIP versus the
        immediately preceding leg (a CHoCH), not a same-direction
        follow-through BOS or a fresh first leg."""
        b = self.bos_15m()
        return b is not None and bool(b.get("was_choch", False))

    def has_order_block(self):
        # A MITIGATED OB is treated as if it doesn't exist for live
        # gating purposes (per chat — this is what stops the same OB
        # from re-triggering as price oscillates around it in a ranging
        # market). order_block() itself still returns the raw dict with
        # mitigated=True so research/tags can see it happened — only the
        # live gate treats it as absent.
        ob = self.order_block()
        return ob is not None and not ob.get("mitigated", False)

    def order_block(self):
        if self._ob_cache == "UNSET":
            bos = self.bos_15m()
            if bos is None:
                self._ob_cache = None
            else:
                # Must be the SAME window bos_15m() computed against, so
                # bos["origin_idx"] indexes correctly (see the recency-
                # bounding fix in detect_order_block's docstring).
                lookback = self.df_15m.tail(SWING_LOOKBACK_15)
                atr_lookback = self._atr_15m_series.loc[lookback.index]
                self._ob_cache = detect_order_block(lookback, bos, atr_lookback)
        return self._ob_cache

    # NOTE (AUDIT FIX): a second, dead `price_in_order_block` definition
    # used to sit here. Python silently keeps only the LAST definition of
    # a method with a given name — this one was never actually called;
    # the real, live one is defined once, below, in the boolean-wrappers
    # section, derived from ob_distance_pips(). Two definitions of the
    # same gating method is exactly the kind of thing that can silently
    # change live behavior with no error and no log line — worth a
    # standing habit of grepping `def price_in_` / `def has_` for dupes
    # after any merge.

    # NOTE: FVG is intentionally NOT exposed here. The live bot only ever
    # trades a high-probability Order Block — a demand zone while bias is
    # BULLISH, a supply zone while bias is BEARISH (order_block() above is
    # already bias-locked, since it's derived from the BOS leg that IS the
    # bias). FVG lives exclusively in the Shadow Pipeline's POI experiment,
    # gated by a size/freshness quality filter — see detect_significant_fvg().

    # ---- fib / zone facts (parametrized — a level is a fact input, not
    # a hardcoded field, since different tiers ask about different levels) -
    def fib_zone(self, near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR):
        ratio = adaptive_fib_ratio(self.df_5m, self.current_atr_5m(), near=near, far=far)
        if self.macro_bias == "BULLISH":
            return self.swing_high - ratio * (self.swing_high - self.swing_low)
        else:
            return self.swing_low + ratio * (self.swing_high - self.swing_low)

    def fib_pocket_bounds(self, near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR):
        structural_range = self.swing_high - self.swing_low
        if structural_range <= 0:
            return None
        adaptive_ratio = adaptive_fib_ratio(
            self.df_5m, self.current_atr_5m(), near=near, far=far)
        if self.macro_bias == "BULLISH":
            adaptive_level = self.swing_high - adaptive_ratio * structural_range
            far_level = self.swing_high - far * structural_range
        else:
            adaptive_level = self.swing_low + adaptive_ratio * structural_range
            far_level = self.swing_low + far * structural_range
        return tuple(sorted((adaptive_level, far_level)))

    def price_in_fib_pocket(self, near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR,
                            tolerance_pips=ZONE_TOLERANCE_PIPS):
        # REFACTORED (AUDIT FIX): this is the function that ACTUALLY
        # gates the live Tier 2 (_tier2_fib_evaluate calls this, not
        # price_in_zone — the two were separate, independently-arithmetic
        # zone tests before this fix). Now derives from the UNROUNDED
        # core (_fib_pocket_distance_pips_raw) — deliberately not the
        # public fib_pocket_distance_pips(), since rounding before a
        # threshold comparison can flip a pass/fail at the boundary
        # (see _fib_distance_pips_raw docstring; caught by randomized
        # testing during this audit). Verified empirically identical to
        # the prior overlap test across 200k randomized cases.
        d = self._fib_pocket_distance_pips_raw(near=near, far=far)
        return d is not None and d <= tolerance_pips

    def fib_fraction(self, extremity, near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR):
        return compute_fib_fraction(self.swing_high, self.swing_low, extremity,
                                     self.macro_bias, near, far)

    def price_in_zone(self, level, tolerance_pips=ZONE_TOLERANCE_PIPS):
        # REFACTORED (AUDIT FIX): was independent overlap arithmetic
        # duplicating price_in_fib_pocket()'s pattern. Now reads the
        # UNROUNDED core (_fib_distance_pips_raw) — deliberately NOT the
        # public fib_distance_pips(), since that rounds to 1dp and
        # rounding before a threshold comparison can flip a pass/fail
        # right at the boundary (confirmed by randomized testing).
        # Verified empirically identical to the prior version across
        # 200k randomized cases.
        return self._fib_distance_pips_raw(level) <= tolerance_pips

    def has_liquidity_sweep(self, level):
        swept, _, _ = detect_liquidity_sweep(self.df_5m, self.df_15m, level, self.macro_bias)
        return swept

    def liquidity_sweep_label(self, level):
        _, label, _ = detect_liquidity_sweep(self.df_5m, self.df_15m, level, self.macro_bias)
        return label

    def sweep_distance_pips(self, level):
        """How far price traded beyond `level` before reclaiming it, in
        pips. None if there was no sweep. Fingerprint fact (per chat)."""
        _, _, distance = detect_liquidity_sweep(self.df_5m, self.df_15m, level, self.macro_bias)
        return distance

    # ---- raw continuous measurements (primary methods) ---------------------
    # These are the MEASUREMENTS. The boolean wrappers below each derive
    # from exactly one of these, with the threshold applied as a one-liner,
    # so threshold logic can never drift away from its underlying fact.

    def _fib_distance_pips_raw(self, level):
        """Unrounded core for fib_distance_pips() — see that method for
        the full explanation. Kept unrounded so price_in_zone() can gate
        on the true float distance; rounding here BEFORE a threshold
        comparison is a real bug (caught by randomized testing during
        this audit): a true distance of 1.0489 pips rounds to 1.0 at one
        decimal place, which then wrongly PASSES a tolerance=1 gate that
        should have rejected it. Rounding is only safe at the point a
        number is surfaced for a human/log to read, never before a
        pass/fail comparison."""
        c, c_prev = self.last_candle_5m(), self.prev_candle_5m()
        observed_low = min(c["Low"], c_prev["Low"])
        observed_high = max(c["High"], c_prev["High"])
        if level < observed_low:
            return (observed_low - level) / PIP_SIZE
        if level > observed_high:
            return (level - observed_high) / PIP_SIZE
        return 0.0

    def fib_distance_pips(self, level=None, near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR):
        """MEASUREMENT (Measurement Engine role). Signed distance, in
        pips, from the last TWO 5M candles' touched range (Low/High,
        same window price_in_zone()/price_in_fib_pocket() look at — not
        just the last close, so this genuinely matches what the gate
        tests) to a given `level`.
          0    the level falls inside the range the last two candles
               already touched
          > 0  the level is still that many pips away from the touched
               range (works whether the level sits above or below it)
        `level` defaults to the adaptive fib_zone() when not given, so
        existing callers (fingerprint logging) keep working unchanged.
        Rounded to 1dp for readability — price_in_zone() below uses the
        unrounded core directly, it does not call this method, so
        rounding here can never affect a live gate.
        This is the number your original example was about — before
        this fix, 'price not in HTF fib pocket' carried no measurement
        at all; a miss by 0.5 pips and a miss by 30 pips logged
        identically."""
        if level is None:
            level = self.fib_zone(near=near, far=far)
        return round(self._fib_distance_pips_raw(level), 1)

    def _fib_pocket_distance_pips_raw(self, near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR):
        """Unrounded core for fib_pocket_distance_pips() — see
        _fib_distance_pips_raw() for why this must stay unrounded."""
        bounds = self.fib_pocket_bounds(near=near, far=far)
        if bounds is None:
            return None
        low_bound, high_bound = bounds
        c, c_prev = self.last_candle_5m(), self.prev_candle_5m()
        observed_low = min(c["Low"], c_prev["Low"])
        observed_high = max(c["High"], c_prev["High"])
        if observed_high < low_bound:
            return (low_bound - observed_high) / PIP_SIZE
        if observed_low > high_bound:
            return (observed_low - high_bound) / PIP_SIZE
        return 0.0

    def fib_pocket_distance_pips(self, near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR):
        """MEASUREMENT backing the actual LIVE Tier 2 gate
        (price_in_fib_pocket() — a BAND between the adaptive ratio and
        the fixed FAR retrace, not a single level like fib_zone()).
        Signed distance, in pips, from the last two candles' touched
        range to the nearest edge of that band. 0 = already
        overlapping/inside. None if the swing has no valid range.
        Rounded for readability — price_in_fib_pocket() uses the
        unrounded core directly (see fib_distance_pips for why)."""
        d = self._fib_pocket_distance_pips_raw(near=near, far=far)
        return None if d is None else round(d, 1)

    def ob_penetration_pct(self):
        """How far the last close has penetrated INTO the Order Block,
        as a percentage of the OB's total height.
          < 0%   price is still outside the OB (approaching; gap = tolerance)
          0-100% partial penetration (0 = just touched the near edge)
          100%   close has reached the far edge (mitigation boundary)
          > 100% price has gone through the OB (fully mitigated)
        Returns None when there is no valid un-mitigated OB."""
        ob = self.order_block()
        if ob is None or ob.get("mitigated", False):
            return None
        ob_height = ob["high"] - ob["low"]
        if ob_height <= 0:
            return None
        close = float(self.last_candle_5m()["Close"])
        if self.macro_bias == "BULLISH":
            # Demand zone is below current area; penetration = close
            # moving down into the zone from the high side.
            penetration = ob["high"] - close
        else:
            # Supply zone is above; penetration = close moving up into it.
            penetration = close - ob["low"]
        return round(penetration / ob_height * 100, 1)

    def _ob_distance_pips_raw(self):
        """Unrounded core for ob_distance_pips() — see
        _fib_distance_pips_raw() for why this must stay unrounded (a
        rounded number compared to a threshold can flip a pass/fail
        right at the boundary)."""
        ob = self.order_block()
        if ob is None or ob.get("mitigated", False):
            return None
        c = self.last_candle_5m()
        if c["Low"] > ob["high"]:
            return (c["Low"] - ob["high"]) / PIP_SIZE
        if c["High"] < ob["low"]:
            return (ob["low"] - c["High"]) / PIP_SIZE
        return 0.0

    def ob_distance_pips(self):
        """MEASUREMENT backing price_in_order_block(). Signed distance,
        in pips, from the last 5M candle's range to the order block
        zone. 0.0 = already overlapping. None if there's no live
        (unmitigated) OB. Kept in pips (not %) specifically so the
        tolerance_pips parameter on price_in_order_block() means exactly
        what it always meant — no unit conversion, no behavior drift.
        Rounded for readability — price_in_order_block() uses the
        unrounded core directly, so rounding here can't affect a gate."""
        d = self._ob_distance_pips_raw()
        return None if d is None else round(d, 1)

    def choch_magnitude(self):
        """The impulse leg size of the current 15M BOS/CHoCH in pips.
        Meaningful for any BOS, not only CHoCH — the boolean has_choch_15m()
        is a separate one-liner on the was_choch flag; this is the continuous
        underlying measurement. Returns None if no BOS is present.
        Shadow logger records this even when no tier activated, so a Step-1
        rejection still has a structural size reading, not just 'no CHoCH'."""
        b = self.bos_15m()
        if b is None:
            return None
        return round(abs(b["impulse_end"] - b["impulse_start"]) / PIP_SIZE, 1)

    def atr_ratio_5m_vs_15m(self):
        """Ratio of the current 5M ATR to the current 15M ATR. > 1 means
        5M volatility is running hotter than the 15M baseline (expansion);
        < 1 means 5M is compressed relative to it. Fingerprint fact for
        shadow tagging — not used in any live gate."""
        atr_15m = self._atr_15m_series.dropna()
        if len(atr_15m) < 1:
            return None
        atr_15m_current = atr_15m.iloc[-1]
        atr_5m_current = self.current_atr_5m()
        if atr_15m_current <= 0:
            return None
        return round(atr_5m_current / atr_15m_current, 3)

    # ---- boolean wrappers (one-liners derived from measurements above) ------
    # Each boolean is expressed as a single threshold applied to its raw
    # measurement. There is no duplicate logic: if the threshold changes,
    # only this line changes; the measurement method is the single source of
    # truth for both the pass/fail decision and the shadow-logged number.

    def price_in_order_block(self, tolerance_pips=ZONE_TOLERANCE_PIPS):
        # REFACTORED (AUDIT FIX): the previous version of this docstring
        # claimed to derive from ob_penetration_pct(), but the code body
        # never actually called it — it ran its own independent overlap
        # arithmetic instead (a comment describing behavior the code
        # didn't have). Now genuinely derives from ob_distance_pips(),
        # which is in the same pips unit as tolerance_pips, so behavior
        # is unchanged and there's exactly one calculation backing both
        # this gate and anything that logs the distance. Uses the
        # UNROUNDED core, not the public ob_distance_pips(), for the
        # same boundary-safety reason documented on
        # _fib_distance_pips_raw (caught by randomized testing).
        d = self._ob_distance_pips_raw()
        return d is not None and d <= tolerance_pips

    # ---- candle-quality facts -------------------------------------------
    def rejection_metrics(self, direction=None):
        """MEASUREMENT (Measurement Engine role). Raw numbers behind the
        engulf/rejection check, computed regardless of whether the
        candle actually qualifies — so a FAILED rejection still tells
        you how close it was, instead of collapsing to a bare
        `rejection_candle_present: false`.

          rejection_strength : 0-1 continuous close-location value on
              the side that matters for `direction`. This is the SAME
              number evaluate_tier1/evaluate_tier2 use for scoring once
              a rejection fires — previously they recomputed a
              simplified version of it inline themselves; they should
              now read it from here instead of keeping a second copy.
          body_atr_ratio     : body size / the ATR_ENGULF_MIN threshold.
              >= 1.0 is what the gate requires; 0.85 tells you it was
              close, not just "no."
          engulfs_prior_body / correct_colors / close_location_ok :
              the three other sub-conditions, kept separate so a
              rejected setup's log can show WHICH one actually failed.

        rejection_candle() below is a thin AND over these fields — one
        calculation feeds both the live gate and the shadow log."""
        direction = direction or self.macro_bias
        c_last, c_prev = self.last_candle_5m(), self.prev_candle_5m()
        atr_now = self.current_atr_5m()
        body_last = abs(c_last["Close"] - c_last["Open"])
        loc = close_location(c_last)
        engulf_tol = ENGULF_TOLERANCE_PIPS * PIP_SIZE
        atr_threshold = ATR_ENGULF_MIN * atr_now if atr_now else None

        if direction == "BULLISH":
            color_ok = c_prev["Close"] < c_prev["Open"] and c_last["Close"] > c_last["Open"]
            engulfs = (c_last["Close"] >= c_prev["Open"] - engulf_tol and
                       c_last["Open"]  <= c_prev["Close"] + engulf_tol)
            loc_ok = loc >= ENGULF_CLOSE_LOCATION_MIN
            rejection_strength = loc
        else:
            color_ok = c_prev["Close"] > c_prev["Open"] and c_last["Close"] < c_last["Open"]
            engulfs = (c_last["Close"] <= c_prev["Open"] + engulf_tol and
                       c_last["Open"]  >= c_prev["Close"] - engulf_tol)
            loc_ok = loc <= (1 - ENGULF_CLOSE_LOCATION_MIN)
            rejection_strength = 1 - loc

        # AUDIT FIX: these two are deliberately left UNROUNDED. They feed
        # a threshold comparison in rejection_candle() (body_atr_ratio
        # >= 1.0) and score-bonus comparisons in evaluate_tier1/2
        # (rejection_strength >= 0.7 / 0.55) — rounding before a
        # threshold check can flip the result right at the boundary
        # (e.g. a true ratio of 0.996 rounds to 1.0 and would wrongly
        # pass a >=1.0 gate). This exact bug was caught by randomized
        # testing on the sibling distance-measurement methods during
        # this audit. Full-precision floats are also simply better for
        # the shadow log — round only when formatting for display
        # (e.g. f"{value:.2f}"), never in the stored/returned number.
        return {
            "rejection_strength": float(rejection_strength),
            "body_atr_ratio": (body_last / atr_threshold) if atr_threshold else None,
            "engulfs_prior_body": bool(engulfs),
            "correct_colors": bool(color_ok),
            "close_location_ok": bool(loc_ok),
        }

    def rejection_candle(self, direction=None):
        """Engulf/rejection on the last closed 5M bar: prior candle
        opposite color, last candle correct color, engulfs prior body
        (within tolerance), real body vs ATR, closes in the correct half
        of its own range.

        REFACTORED (AUDIT FIX): now a thin AND over rejection_metrics()'s
        fields instead of its own arithmetic. One near-zero-probability
        behavior note: the original required body_last > atr_threshold
        (strict); this requires body_atr_ratio >= 1.0, i.e. body_last >=
        atr_threshold (non-strict). Only differs on an exact float tie —
        flagged for completeness, not expected to change live behavior."""
        m = self.rejection_metrics(direction)
        return bool(m["engulfs_prior_body"] and m["correct_colors"] and
                    m["close_location_ok"] and (m["body_atr_ratio"] or 0) >= 1.0)

# =========================================================================
# LEG IDENTITY — shared vocabulary for "which 1H leg is this?" used by
# live Rule of Law AND by MIN experiments / Forward Observation to match
# their own records to the same leg. get_leg_owner() is a read-only peek;
# the WRITE side (apply_leg_ownership/release_leg) stays in scanner_live.py
# since only the live bot is allowed to actually claim/release a leg.
# =========================================================================
def compute_leg_id(macro_bias, swing_high, swing_low):
    return "{}|{:.5f}|{:.5f}".format(macro_bias, swing_high, swing_low)


def _same_leg(leg_id_a, leg_id_b, tolerance_pips=LEG_MATCH_TOLERANCE_PIPS):
    if leg_id_a == leg_id_b:
        return True
    try:
        dir_a, hi_a, lo_a = leg_id_a.split("|")
        dir_b, hi_b, lo_b = leg_id_b.split("|")
    except (AttributeError, ValueError):
        return False
    if dir_a != dir_b:
        return False
    tol = tolerance_pips * PIP_SIZE
    return abs(float(hi_a) - float(hi_b)) <= tol and abs(float(lo_a) - float(lo_b)) <= tol


def get_leg_owner(state):
    return state.get("leg_owner")


def bias_to_side(macro_bias):
    """BULLISH/BEARISH (structure vocabulary) -> BUY/SELL (trade vocabulary)."""
    return "BUY" if macro_bias == "BULLISH" else "SELL"


# =========================================================================
# TIERS — pure evaluate(facts, ctx, state, now_utc) -> TierResult functions.
# Shared because BOTH scanner_live.py (Rule of Law arbitration, real
# ownership decisions) AND min_scanner.py (Experiment 7 — Tier ATR Mirror,
# a read-only peek at what all three tiers WOULD say every scan) need to
# call the exact same evaluate() logic. Neither of these functions ever
# writes to `state` itself — see apply_leg_ownership() in scanner_live.py
# for the only place a tier's result actually becomes a state write.
# =========================================================================
def tier_rating_from_score(score):
    if score >= 85:
        return "A"
    if score >= 65:
        return "B"
    return "C"


class TierResult:
    """
    Uniform return shape every tier's evaluate() produces.

    activated:   did this tier's activation trigger fire at all this
                 scan? Independent of `fired` — a tier can be activated
                 and sit in its own WATCHING state for several scans.
    fired:       ready to hand off to Trade Management this scan
    direction / entry / sl_raw: only meaningful if fired. sl_raw is an
                 UN-buffered structural reference (Trade Management
                 applies the shared ATR/SL_MIN buffer + risk gate).
    tier_label:  e.g. "TIER_1_POI"
    score / tier_rating / breakdown: THIS TIER'S OWN scoring — each tier
                 has its own weights/formula, not a shared 0-100 table.
    reason:      human-readable, always populated.
    state_updates: this tier's OWN namespaced state bookkeeping (e.g. a
                 break-retest timing window) as a plain dict, applied by
                 the caller via apply_state_updates() — tiers do not
                 mutate `state` directly, same discipline as Macro Bias
                 and Market Context.
    """
    def __init__(self, activated=False, fired=False, direction=None,
                 entry=None, sl_raw=None, tier_label=None,
                 score=None, tier_rating=None, breakdown=None,
                 reason="", state_updates=None, conviction=None):
        self.activated = activated
        self.fired = fired
        self.direction = direction
        self.entry = entry
        self.sl_raw = sl_raw
        self.tier_label = tier_label
        self.score = score
        self.tier_rating = tier_rating
        self.breakdown = breakdown or {}
        self.reason = reason
        self.state_updates = state_updates or {}
        # conviction: output of classify_conviction() — decision/minimum/
        # band_label/size_mult/target_r/partial_r/breakeven_r/reason.
        # Only populated once a tier's mandatory activation conditions
        # AND its structural fire-trigger (rejection candle / CHoCH) have
        # already passed — conviction never gates activation, only what
        # an activated setup is allowed to do next.
        self.conviction = conviction


# =========================================================================
# DIAGNOSTIC REPORT — ported from V6. V6's version had keys matching its
# own single-pass scoring model (macro_bias/structure/liquidity/
# confirmation/htf_gate/confidence_score/risk_gate/volatility_filter/
# duplicate_check). V3 has no equivalent single scoring pass — it has a
# tier-arbitration model instead — so the keys below are remapped to what
# V3 ACTUALLY evaluates, in the order scan() actually evaluates them:
#
#   macro_bias      — CONSOLIDATION vs directional, and stale/confirmed
#   market_context  — ctx.tradeable (ATR floor, regime shift, session)
#   tier_evaluation — did any tier ACTIVATE on the current leg
#   leg_ownership   — did the activated tier actually FIRE (own leg,
#                     not just WATCHING) this scan
#   risk_gate       — dual ATR/flat-pip risk ceiling
#
# Only gates that were actually reached are populated (None = never
# ran, an earlier gate already stopped the scan) — same "honest record"
# principle as V6's version.
# =========================================================================
def new_diagnostic():
    keys = ["macro_bias", "market_context", "tier_evaluation",
            "leg_ownership", "risk_gate"]
    return {k: None for k in keys}


DIAGNOSTIC_LABELS = {
    "macro_bias":      "Macro bias (1H)",
    "market_context":  "Market context (ATR/session/regime)",
    "tier_evaluation": "Tier evaluation",
    "leg_ownership":   "Leg ownership / fire",
    "risk_gate":       "Risk gate",
}


def diag_set(diag, key, passed, reason=None):
    """Record one gate's outcome. No-op if the key isn't tracked or diag
    is None (DIAGNOSTIC_MODE off)."""
    if diag is not None and key in diag:
        diag[key] = (bool(passed), reason)
    return diag


def build_diagnostic_report(diag, header="No signal this scan"):
    """
    Renders the diag dict into a plain-language record, e.g.:

        No signal this scan

        Because:
        ✓ Macro bias (1H)
        ✓ Market context (ATR/session/regime)
        ✗ Tier evaluation (no tier activated on this leg)

    Console-only (matches V6 — this is a per-scan developer record, not
    something worth pushing to Telegram every time nothing fires).
    """
    lines = [header, "", "Because:"]
    for key in diag:
        entry = diag[key]
        if entry is None:
            continue
        passed, reason = entry
        mark  = "✓" if passed else "✗"
        label = DIAGNOSTIC_LABELS.get(key, key)
        suffix = f" ({reason})" if (not passed and reason) else ""
        lines.append(f"{mark} {label}{suffix}")
    if len(lines) == 3:
        lines.append("_(no gates recorded)_")
    return "\n".join(lines)


# =========================================================================
# SIGNAL TIMELINE — ported from V6's format_timeline_diagnostics. Tracks,
# per LEG (reset whenever leg_id changes — see evaluate_rule_of_law),
# when each confirmation stage was first observed. Populated
# opportunistically from MarketFacts each scan a leg is alive; read back
# via /last once a signal fires.
# =========================================================================
def compute_signal_timeline_reset(state, leg_id):
    """PURE. Same trigger as leg-ownership release: a new 1H leg means a
    new hypothetical setup, so prior stage timestamps stop meaning
    anything. Returns an updates dict (possibly empty) for the caller to
    apply via apply_state_updates — never mutates `state` itself."""
    if state.get("signal_timeline_leg_id") != leg_id:
        return {"signal_timeline_leg_id": leg_id, "signal_timeline": {}}
    return {}


def compute_signal_timeline_updates(facts, timeline, now_utc):
    """
    PURE. `timeline` is read-only (the CURRENT state["signal_timeline"]
    dict, already reset for this leg if needed). Returns only the NEW
    stage keys to merge in — a stage already stamped is never
    overwritten, so re-observing the same True fact next scan is a
    no-op. Caller merges and applies via apply_state_updates(), same
    discipline as every other compute_*() in this file.
    """
    updates = {}

    def _stage(stage_key, label=None):
        at_key = f"{stage_key}_at"
        if timeline.get(at_key) is None and at_key not in updates:
            updates[at_key] = now_utc.isoformat()
            if label is not None:
                updates[f"{stage_key}_label"] = label

    if facts.has_choch_15m() or facts.has_fresh_bos_aligned_with_bias():
        label = "CHoCH" if facts.has_choch_15m() else "BOS"
        _stage("choch_bos", label)

    zone = facts.fib_zone()
    if facts.price_in_fib_pocket():
        _stage("fib_entered")
        if facts.has_liquidity_sweep(zone):
            _stage("sweep")

    if facts.rejection_candle():
        _stage("engulf")

    return updates


def format_timeline_diagnostics(timeline, now_utc):
    """Ported near-verbatim from V6 — compact per-signal stage timing."""
    def _parse(iso):
        try:
            return datetime.fromisoformat(iso) if iso else None
        except Exception:
            return None

    stages = [
        ("CHoCH/BOS",   _parse(timeline.get("choch_bos_at")),   timeline.get("choch_bos_label") or "N/A"),
        ("Fib entered", _parse(timeline.get("fib_entered_at")), None),
        ("Sweep",       _parse(timeline.get("sweep_at")),       None),
        ("Engulf",      _parse(timeline.get("engulf_at")),      None),
        ("Signal sent", now_utc,                                None),
    ]
    anchor = next((t for _, t, _ in stages if t is not None), None)

    lines = []
    for label, ts, tag in stages:
        if ts is None:
            lines.append(f"     {label}: —")
            continue
        lag = f" (+{int((ts - anchor).total_seconds() // 60)}m)" if anchor and ts != anchor else ""
        tag_str = f" [{tag}]" if tag and tag != "N/A" else ""
        lines.append(f"     {label}: {ts.strftime('%H:%M UTC')}{lag}{tag_str}")

    return "🕒 *Timeline:*\n" + "\n".join(lines)


# ---- TIERS (Stage 3 — live) -------------------------------------------------
# Signature is (facts, ctx, state, now_utc) — a tier receives the
# MarketFacts object and MarketContext, and read-only `state` for its own
# historical bookkeeping. It must return a TierResult; any state it wants
# persisted goes in TierResult.state_updates, never a direct mutation.
def _tier1_poi_evaluate(facts, ctx, state, now_utc):
    """
    TIER 1 — Premium POI Reaction.

    The live bot's ONLY point of interest is the Order Block, because
    detect_order_block() is already bias-locked: it's carved out of the
    BOS impulse that defines the current bias, so in a BULLISH leg it can
    only ever be a demand zone, and in a BEARISH leg only ever a supply
    zone. There is no FVG check here on purpose — see the note on
    MarketFacts and FVG_MIN_SIZE_ATR_MULT in CONFIG. If you personally
    want FVG reactions, that lives in the Shadow Pipeline's Experiment 3
    (POI) only, gated by a quality filter.

    Mandatory:
      - facts.has_fresh_bos_aligned_with_bias()  (the leg backing the OB
        is still the live, bias-aligned leg — not a stale/superseded one)
      - facts.has_order_block()            (the zone itself)
      - facts.price_in_order_block()        (price is actually reacting AT it)
      - facts.rejection_candle()            (real rejection, not just a touch)

    Optional (score only, never gates):
      - facts.has_choch_15m()  (a flip-driven OB > a mere continuation OB)
    """
    label = "TIER_1_POI"

    if not facts.has_fresh_bos_aligned_with_bias():
        return TierResult(tier_label=label, reason="no fresh 15M BOS aligned with 1H bias")

    if not facts.has_order_block():
        return TierResult(tier_label=label, reason="no order block behind the current BOS impulse")

    if not facts.price_in_order_block():
        d = facts.ob_distance_pips()
        return TierResult(
            tier_label=label,
            reason=(f"price not in order block — {d} pips away" if d is not None
                     else "price not currently inside the order block zone"),
            breakdown={"ob_distance_pips": d, "ob_penetration_pct": facts.ob_penetration_pct()},
        )

    ob = facts.order_block()
    direction = facts.macro_bias
    side = bias_to_side(direction)
    c_last = facts.last_candle_5m()
    choch = facts.has_choch_15m()
    entry = float(c_last["Close"])
    sl_raw = ob["low"] if side == "BUY" else ob["high"]

    # FIX (found during audit): activated and fired used to be the same
    # condition, meaning this tier went straight from unclaimed to FIRED
    # in one step — no tier could ever be claimed as WATCHING, which
    # silently made the Rule of Law ownership-upgrade mechanism
    # unreachable (nothing to upgrade FROM). Splitting them here: the
    # structural conditions above (aligned BOS + order block + price
    # there) are enough to CLAIM/WATCH the leg; rejection_candle() is
    # the separate trigger that promotes WATCHING to FIRED. This also
    # gives a higher-priority tier a real multi-scan window to steal the
    # leg before this one actually fires.
    if not facts.rejection_candle():
        m = facts.rejection_metrics()
        ratio_str = f"{m['body_atr_ratio']:.2f}" if m['body_atr_ratio'] is not None else "n/a"
        return TierResult(activated=True, fired=False, direction=side,
                           entry=entry, sl_raw=sl_raw, tier_label=label,
                           breakdown={
                               "order_block": True, "rejection_candle": False,
                               "fresh_bos_aligned": True, "choch": choch,
                               # AUDIT FIX: previously a failed rejection
                               # candle logged nothing about how close it
                               # was. rejection_metrics() gives the raw
                               # numbers even on failure.
                               **m,
                           },
                           reason="price inside order block, watching for rejection candle "
                                  f"(strength {m['rejection_strength']:.2f}, body/ATR {ratio_str})")

    # AUDIT FIX: previously recomputed close_location() here independently
    # of rejection_candle()'s own internal calculation — a third copy of
    # the same close-location logic. Now reads the single shared number.
    rejection_strength = facts.rejection_metrics()["rejection_strength"]

    score = 55
    breakdown = {
        "order_block": True, "rejection_candle": True,
        "fresh_bos_aligned": True, "choch": choch,
    }
    if choch:
        score += 25
        breakdown["choch_bonus"] = 25
    if rejection_strength >= 0.7:
        score += 15
        breakdown["strong_rejection_bonus"] = 15
    elif rejection_strength >= 0.55:
        score += 8
        breakdown["rejection_bonus"] = 8

    # sl_raw: UN-buffered structural reference — the far edge of the zone
    # itself. Trade Management applies the shared ATR/SL_MIN buffer.

    # Phase 3 — Execution. Structural conditions + rejection candle are
    # enough to be an ACTIVATED, confirmed Tier 1 setup — they are not
    # enough to FIRE on their own anymore. Conviction decides that.
    conviction = classify_conviction(label, score)
    fire = conviction["decision"] == "FIRE"
    base_reason = "Order block reaction ({} zone){}, rejection strength {:.2f}".format(
        "demand" if side == "BUY" else "supply",
        " + CHoCH" if choch else "", rejection_strength)

    return TierResult(
        activated=True, fired=fire, direction=side,
        entry=entry, sl_raw=sl_raw, tier_label=label,
        score=score, tier_rating=tier_rating_from_score(score),
        breakdown=breakdown, conviction=conviction,
        reason=base_reason + " — " + conviction["reason"],
    )


def _tier2_fib_evaluate(facts, ctx, state, now_utc):
    """
    TIER 2 — HTF Fib Pullback.

    Mandatory:
      - facts.price_in_fib_pocket()             (price has retraced into
        the adaptive fib pocket)
      - facts.rejection_candle()                 (a real reaction there,
        not just a touch-and-continue)

    Optional (score only, never gates):
      - facts.has_fresh_bos_aligned_with_bias()  (BOS "optional toggle" —
        confirms momentum has resumed, but a pure pullback entry doesn't
        need it)
      - facts.has_liquidity_sweep(zone)          (a swept pocket is a
        cleaner reaction than an untouched one)

    Supply/demand (order block) is NOT required — that's Tier 1's job.
    """
    label = "TIER_2_FIB"
    direction = facts.macro_bias
    side = bias_to_side(direction)
    zone = facts.fib_zone()

    if not facts.price_in_fib_pocket():
        d = facts.fib_pocket_distance_pips()
        return TierResult(
            tier_label=label,
            reason=f"price not in HTF fib pocket — {d} pips away" if d is not None
                    else "price not in the HTF fib pocket",
            breakdown={"fib_pocket_distance_pips": d},
        )

    bos_aligned = facts.has_fresh_bos_aligned_with_bias()
    swept = facts.has_liquidity_sweep(zone)
    c_last = facts.last_candle_5m()
    entry = float(c_last["Close"])
    if side == "BUY":
        sl_raw = facts.swing_high - FIB_ZONE_FAR * (facts.swing_high - facts.swing_low)
    else:
        sl_raw = facts.swing_low + FIB_ZONE_FAR * (facts.swing_high - facts.swing_low)

    # Same fix as Tier 1 — see its comment. Price being in the pocket is
    # enough to WATCH; rejection_candle() is the separate FIRE trigger.
    # This is also a closer match to your own framework's "1H locates,
    # 15M confirms, 5M executes" split: arriving at the pocket is the
    # 1H/15M "location" step, the rejection candle is the "confirms" step.
    if not facts.rejection_candle():
        m = facts.rejection_metrics()
        ratio_str = f"{m['body_atr_ratio']:.2f}" if m['body_atr_ratio'] is not None else "n/a"
        return TierResult(activated=True, fired=False, direction=side,
                           entry=entry, sl_raw=sl_raw, tier_label=label,
                           breakdown={
                               "in_fib_zone": True, "rejection_candle": False,
                               "bos_aligned": bos_aligned, "liquidity_sweep": swept,
                               **m,
                           },
                           reason="price in HTF fib pocket, watching for rejection candle "
                                  f"(strength {m['rejection_strength']:.2f}, body/ATR {ratio_str})")

    score = 50
    breakdown = {"in_fib_zone": True, "rejection_candle": True,
                 "bos_aligned": bos_aligned, "liquidity_sweep": swept}
    if bos_aligned:
        score += 15
        breakdown["bos_bonus"] = 15
    if swept:
        score += 20
        breakdown["sweep_bonus"] = 20
    reaction_extremity = c_last["Low"] if side == "BUY" else c_last["High"]
    fraction = facts.fib_fraction(reaction_extremity)
    if fraction >= 0.6:
        score += 10
        breakdown["deep_pullback_bonus"] = 10

    # sl_raw: structural reference is the FAR edge of the fib band (a
    # fixed FIB_ZONE_FAR retrace, not the adaptive zone itself) — if price
    # retraces beyond that, the pullback thesis is simply wrong.

    conviction = classify_conviction(label, score)
    fire = conviction["decision"] == "FIRE"
    base_reason = "HTF fib pullback reaction{}{}".format(
        " + aligned BOS" if bos_aligned else "",
        " + swept" if swept else "")

    return TierResult(
        activated=True, fired=fire, direction=side,
        entry=entry, sl_raw=sl_raw, tier_label=label,
        score=score, tier_rating=tier_rating_from_score(score),
        breakdown=breakdown, conviction=conviction,
        reason=base_reason + " — " + conviction["reason"],
    )


def _tier3_structure_evaluate(facts, ctx, state, now_utc):
    """
    TIER 3 — Structure Confirmation. V2's old "Tier 1 break-retest" logic,
    reframed under its own gate set (lowest priority — only gets a look
    when nothing at Tier 1/2 has already claimed the leg).

    Mandatory:
      - facts.has_choch_15m()   (a genuine flip, not a same-direction
        continuation BOS — this tier trades the FIRST confirmation of a
        new leg, not the fifth)
      - facts.bos_15m() aligned with HTF bias (implied by has_choch_15m()
        returning True only for the live leg, but checked explicitly for
        clarity/robustness)

    Strongly preferred (heavy score weight, not a hard gate — matches the
    "strongly preferred" language in the spec):
      - facts.has_liquidity_sweep(level) against the leg's own origin

    Optional (light score bonus):
      - price already inside the fib pocket (fib is optional here, Tier 2
        owns fib primarily)
    """
    label = "TIER_3_STRUCTURE"

    # NOTE: unlike Tier 1/2, this tier's STRUCTURAL trigger (a CHoCH) is
    # atomic, not a zone to sit and wait in — by the time
    # facts.has_choch_15m() is true, the confirming break has already
    # happened, so there's no structural WATCHING state to pass through.
    # activated is therefore always True here once the mandatory gates
    # pass. `fired`, however, is now gated by conviction (Phase 3) same
    # as Tier 1/2 — a low-conviction Tier 3 setup is REJECTED, not
    # fired, even though the CHoCH event itself already happened. This
    # means Tier 3 CAN now sit as an activated-but-not-fired (REJECTED)
    # owner — it just never sits as WATCHING, since there's nothing
    # structural left to wait for; only conviction stands between it
    # and firing.

    if not facts.has_choch_15m():
        bos_now = facts.bos_15m()
        return TierResult(
            tier_label=label,
            reason="current 15M leg is a continuation BOS, not a CHoCH",
            breakdown={
                "choch_magnitude_pips": facts.choch_magnitude(),
                "leg_direction": bos_now["direction"] if bos_now else None,
            },
        )

    bos = facts.bos_15m()
    if bos is None or bos["direction"] != facts.macro_bias:
        return TierResult(tier_label=label, reason="no 15M structure aligned with 1H bias")

    direction = facts.macro_bias
    side = bias_to_side(direction)
    swept = facts.has_liquidity_sweep(bos["impulse_start"])

    score = 55
    breakdown = {"choch": True, "bos_aligned": True, "liquidity_sweep": swept}
    if swept:
        score += 30
        breakdown["sweep_bonus"] = 30
    else:
        score -= 10
        breakdown["no_sweep_penalty"] = -10

    if facts.price_in_fib_pocket():
        score += 10
        breakdown["fib_confluence_bonus"] = 10

    entry = float(facts.last_candle_5m()["Close"])
    # sl_raw: the leg's own origin fractal — if price trades back through
    # the point the CHoCH originated from, the new structure is invalid.
    sl_raw = bos["impulse_start"]

    conviction = classify_conviction(label, score)
    fire = conviction["decision"] == "FIRE"
    base_reason = "CHoCH/BOS structure confirmation{}".format(" + swept origin" if swept else " (unswept)")

    return TierResult(
        activated=True, fired=fire, direction=side,
        entry=entry, sl_raw=sl_raw, tier_label=label,
        score=score, tier_rating=tier_rating_from_score(score),
        breakdown=breakdown, conviction=conviction,
        reason=base_reason + " — " + conviction["reason"],
    )


TIER_REGISTRY = {
    "TIER_1_POI":       _tier1_poi_evaluate,
    "TIER_2_FIB":        _tier2_fib_evaluate,
    "TIER_3_STRUCTURE":  _tier3_structure_evaluate,
}


# =========================================================================
# LIVE-FIRE / SL SIZING HELPERS — pure functions moved here (out of
# scanner_live.py) because min_scanner.py's Experiment 7 (Tier ATR Mirror)
# needs to apply the exact same stale-bias gate and SL sizing as a
# read-only peek, same reasoning as the tier evaluate() functions above.
# =========================================================================
def _gate_stale_bias(result, state):
    """
    Hard live-fire gate (per chat — real live risk, not just a research
    finding): when the confirmed 1H bias is STALE (the leg that last
    confirmed it has since invalidated and nothing fresh has replaced
    it), a tier may still ACTIVATE or sit WATCHING — leg-ownership
    bookkeeping stays honest either way — but it can never actually
    FIRE a live signal off a held-over, no-longer-confirmed direction.

    MUST be applied to `result` BEFORE the caller decides
    apply_leg_ownership(status="FIRED" if result.fired else "WATCHING").
    Applying it after would still record the leg as FIRED in state for
    a signal that was silently suppressed — permanently freezing that
    leg as "untouchable FIRED" for a trade that never happened. Every
    call site in evaluate_rule_of_law() gates immediately after _run(),
    before any ownership decision — see the three call sites.
    """
    if not (result.fired and state.get("macro_bias_stale")):
        return result
    conviction = dict(result.conviction) if result.conviction else None
    if conviction is not None:
        conviction["decision"] = "REJECT"
        conviction["reason"] = "macro_bias_stale=True — held-over direction, not live-confirmed"
    return TierResult(
        activated=result.activated, fired=False, direction=result.direction,
        entry=result.entry, sl_raw=result.sl_raw, tier_label=result.tier_label,
        score=result.score, tier_rating=result.tier_rating,
        breakdown=result.breakdown, conviction=conviction,
        reason=result.reason + " — BLOCKED: macro_bias_stale=True "
               "(1H bias is a held-over direction, not live-confirmed)",
        state_updates=result.state_updates,
    )


def sl_multiplier_for_context(ctx):
    return SL_ATR_MULT_COMPRESSED if ctx.regime_ratio >= SL_VOL_SPIKE_RATIO else SL_ATR_MULT
