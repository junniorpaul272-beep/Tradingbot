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
    detect_bos_impulse,
    INVALIDATION_RETRACE, HTF_STRUCTURE_WING, HTF_EMA_SLOPE_BARS,
    HTF_CONSOLIDATION_ATR_MULT, HTF_EMA_FLAT_THRESHOLD,
    PROMOTION_MIN_BREAK_COUNT, SESSION_WINDOWS_UTC,
    REGIME_SHIFT_ENABLED, REGIME_SHIFT_SHORT_PERIOD, REGIME_SHIFT_LONG_PERIOD,
    REGIME_SHIFT_THRESHOLD, REGIME_SHIFT_OPEN_WARMUP,
    POST_SPIKE_COOLDOWN_BASE, POST_SPIKE_COOLDOWN_SCALE, POST_SPIKE_COOLDOWN_MAX,
    ATR_MIN_PIPS, ATR_WARN_PIPS,
    FIB_ZONE_NEAR, FIB_ZONE_FAR,
    SWEEP_LOOKBACK_CANDLES,
    OB_MIN_DISPLACEMENT_ATR_MULT, OB_OPPOSING_LOOKBACK_CANDLES,
    FVG_LOOKBACK_CANDLES, FVG_MIN_SIZE_ATR_MULT, FVG_MAX_AGE_CANDLES,
    SR_ZONE_ATR_MULT, SR_MIN_CONTAINMENT_BARS, SR_FLIP_COOLDOWN_BARS,
    SR_MAX_FLIPS, SR_MAX_AGE_CANDLES,
    ZONE_TOLERANCE_PIPS, ENGULF_TOLERANCE_PIPS, ATR_ENGULF_MIN,
    ENGULF_CLOSE_LOCATION_MIN, SWING_LOOKBACK_15,
    BOS_15M_BREAK_BUFFER_ATR_MULT, LEG_MATCH_TOLERANCE_PIPS, FRACTAL_WING,
    classify_conviction, SL_ATR_MULT_COMPRESSED, SL_VOL_SPIKE_RATIO, SL_ATR_MULT,
    PHASE_EXHAUSTION_MIN_BREAK_COUNT, PHASE_EXHAUSTION_EMA_DIST_ATR_MULT,
    PHASE_EXHAUSTION_EXIT_FRACTION,
    PHASE_HISTORY_MAX_LEN, MEASURED_MOVE_BUCKETS,
    PHASE_VOLATILITY_HINT_LOOKBACK_BARS, PHASE_VOLATILITY_HINT_THRESHOLD,
    FAILURE_RISK_APPROACHING_FRACTION, EXPECTED_NEXT_EVENT_MAP,
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
    always shows exactly which swing points justify the current bias.

    CONTRACT (per chat): origin is this 1H leg's FOUNDING swing point —
    from detect_bos_impulse()'s impulse_start, so it does NOT move on a
    same-direction continuation. macro_swing_low/macro_swing_high are the
    dominant 1H leg's origin-to-extreme range, which is the correct,
    intentional anchor for HTF fib zones (Tier 2) and HTF structure
    (Tier 3) — do NOT read these as "the latest 1H swing point." extreme
    DOES update every scan the leg survives (see check_leg_anchor_
    survival above) — it's the origin side that's fixed for the leg's
    lifetime, not the pair as a whole."""
    if direction == "BULLISH":
        swing_low, swing_high = origin, extreme
    else:
        swing_high, swing_low = origin, extreme
    return {
        "macro_swing_low": swing_low,
        "macro_swing_high": swing_high,
        "macro_swing_confirmed_at": datetime.now(timezone.utc).isoformat(),
    }


def _campaign_updates(state, bias, leg_origin, leg_extreme, leg_origin_time, new_leg):
    """
    PURE. Maintains the "directional campaign" — a persistent object that
    spans MULTIPLE continuation legs in the same direction, unlike
    macro_leg_origin/extreme (see _macro_swing_updates above), which
    resets on EVERY confirmed leg, continuation or reversal alike. Per
    the friend's framework (2026-08-12 chat): "the useful question is
    what structural move are we currently living inside," not how far
    back some swing from 200 candles ago sits — so this deliberately
    resets on genuine direction changes, not on a fixed lookback.

    One comparison covers every path that can change the held direction
    (a genuine 1H CHoCH, a 15M stale-bias promotion into the opposite
    side, or an ordinary same-direction continuation) — no separate
    was_choch branching needed here:
      - state["campaign_direction"] != bias (or None — bootstrap): a NEW
        campaign starts at this leg's origin.
      - state["campaign_direction"] == bias: the EXISTING campaign
        extends — origin/initial_impulse untouched, only the extreme
        (and, on a genuinely new leg, continuation_count) move.

    new_leg=True: called from a freshly-confirmed leg (continuation or
        reversal) — bumps continuation_count on an extend, resets it to
        1 on a new campaign.
    new_leg=False: called from ordinary leg-anchor SURVIVAL (price
        extended further without a new confirmed break) — only ever
        pushes current_extreme further in the campaign's own direction;
        never touches continuation_count/origin/initial_impulse. If the
        survival check somehow disagrees with the held campaign
        direction (shouldn't happen in practice), this just no-ops —
        the next genuinely NEW leg will resync it, and forcing a resync
        from a read-only survival check isn't this helper's job.

    OBSERVATION LAYER ONLY (per chat) — nothing reads campaign_* to gate
    activation, sizing, phase transitions, or any other live decision.
    Feeds compute_campaign_extension() below for the Market Thesis and
    EXP4/shadow study only.
    """
    campaign_dir = state.get("campaign_direction")

    if campaign_dir != bias:
        if not new_leg:
            return {}
        initial_pips = abs(leg_extreme - leg_origin) / PIP_SIZE
        if initial_pips <= 0:
            return {}
        return {
            "campaign_direction":             bias,
            "campaign_origin":                leg_origin,
            "campaign_origin_time":           leg_origin_time,
            "campaign_initial_impulse_pips":  round(initial_pips, 1),
            "campaign_current_extreme":       leg_extreme,
            "campaign_continuation_count":    1,
        }

    current_extreme = state.get("campaign_current_extreme")
    if current_extreme is None:
        current_extreme = leg_extreme
    new_extreme = max(current_extreme, leg_extreme) if bias == "BULLISH" \
        else min(current_extreme, leg_extreme)

    updates = {"campaign_current_extreme": new_extreme}
    if new_leg:
        updates["campaign_continuation_count"] = state.get("campaign_continuation_count", 1) + 1
    return updates


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

    CONTRACT: macro_bias is a HELD direction, not a fresh per-scan
    indicator reading — it only changes on a confirmed 1H BOS/CHoCH (or
    the stale-promotion path above), so it can and often does stay
    identical across many consecutive scans even as price moves. Owner:
    this function, exclusively — nowhere else in the codebase should
    write state["macro_bias"]/state["macro_bias_confirmed"] directly.
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
            # Added 2026-08-25, per chat (gap surfaced investigating why
            # prior_macro_leg goes stale for many continuations in a row):
            # detect_bos_impulse() was already computing latest_swing_origin
            # (the swing point behind THIS leg's most recent break,
            # continuation included) but nothing persisted it — it was
            # computed and thrown away every scan. WorldState fix, per this
            # file's own rule ("if Brain needs a fact WorldState doesn't
            # have, fix WorldState") — this is purely additive, read by
            # nothing else in the bias path above, zero risk to
            # macro_bias_confirmed/macro_leg_origin/macro_leg_extreme.
            "macro_leg_latest_swing_origin": bos_1h.get("latest_swing_origin"),
        })
        updates.update(_macro_swing_updates(bias, bos_1h["impulse_start"], bos_1h["impulse_end"]))
        updates.update(_campaign_updates(
            state, bias, bos_1h["impulse_start"], bos_1h["impulse_end"], origin_time, new_leg=True))
        return bias, updates

    survived, anchor_updates = check_leg_anchor_survival(state, "macro_leg", df_1h_x)
    if survived:
        updates.update(anchor_updates)
        bias = state.get("macro_leg_direction")
        updates["macro_bias_confirmed"] = bias
        updates["macro_bias_stale"] = False
        # Surviving leg may have extended its own extreme (see
        # check_leg_anchor_survival above) without a fresh confirmed
        # break — push the campaign's own extreme along with it, same
        # direction, no new leg (continuation_count untouched).
        survived_extreme = anchor_updates.get("macro_leg_extreme", state.get("macro_leg_extreme"))
        if bias in ("BULLISH", "BEARISH") and survived_extreme is not None:
            updates.update(_campaign_updates(
                state, bias, state.get("macro_leg_origin"), survived_extreme,
                state.get("macro_leg_origin_time"), new_leg=False))
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
                _promo_origin_time = updates.get("leg15_origin_time", state.get("leg15_origin_time"))
                updates.update(_campaign_updates(
                    state, bias, early_bos["impulse_start"], early_bos["impulse_end"],
                    _promo_origin_time, new_leg=True))
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


class TransitionCause(Enum):
    """
    WHY the phase resolved the way it did — per chat with Vally's
    collaborator. Fixed taxonomy, not free text, so it can be grouped/
    counted later (Similarity Engine, Phase 2). Every member is read off
    a value compute_market_phase() already computes; see
    classify_transition_cause() below. UNKNOWN is a real, legitimate
    value — returned when the current inputs don't confidently match any
    other member, rather than guessing one.
    """
    FRESH_BOS             = "FRESH_BOS"             # new same-direction dominant leg, no flip
    CHOCH                 = "CHOCH"                 # new dominant leg IS a direction flip
    SWEEP_RECLAIM         = "SWEEP_RECLAIM"         # MANIPULATION overlay — swept boundary, reclaimed
    BIAS_FLIP             = "BIAS_FLIP"             # macro_bias itself changed vs the prior scan
    EMA_EXHAUSTION        = "EMA_EXHAUSTION"        # aging trigger fired (break_count and/or EMA distance
                                                     # — which one is recorded separately as aging_reason,
                                                     # not split into two enum members; see MarketPhase)
    VOLATILITY_EXPANSION  = "VOLATILITY_EXPANSION"
    VOLATILITY_COLLAPSE   = "VOLATILITY_COLLAPSE"
    FAILED_CONTINUATION   = "FAILED_CONTINUATION"   # bias_stale — leg invalidated, nothing fresh replaced it
    UNKNOWN                = "UNKNOWN"


@dataclass
class MarketPhase:
    phase: Phase
    macro_bias: str          # BULLISH / BEARISH / CONSOLIDATION — direction phase applies to
    age_bars: int             # consecutive scans this exact phase has held
    history: tuple            # last PHASE_HISTORY_MAX_LEN phase values before this one
    narrative: str            # human-readable summary for logs/Telegram
    extension: Optional[dict] = None   # measured-move dict from compute_measured_move_extension(), or None
    # ---- additive fields for the Market Thesis Engine (Phase 1, per chat)
    # — all default None so nothing that constructs a MarketPhase without
    # them (there is no such caller today, but keeping the pattern used
    # everywhere else in this file: origin_idx/was_choch on
    # detect_bos_impulse, extension above) breaks. -----------------------
    transition_cause: Optional[TransitionCause] = None
    aging_reason: Optional[str] = None      # "break_count" / "ema_distance" / "break_count+ema_distance" / None
    break_count: Optional[int] = None       # the 1H leg's own break_count, exposed for thesis/failure-risk text
    dist_in_atr: Optional[float] = None     # price's distance from EMA_100 in ATR multiples, same reason
    swept_boundary: Optional[str] = None    # "high" / "low" / None — which swing MANIPULATION swept
    volatility_hint: Optional[str] = None   # "expanding" / "contracting" / "flat" / None — 1H, independent recompute


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


def capture_prior_continuation_snapshot(prev_direction, prev_latest_swing_origin, prev_extreme):
    """
    PURE. Same snapshot-before-overwrite pattern as capture_prior_leg_
    snapshot() above, but answers a DIFFERENT question — added 2026-08-25
    per chat, to fill a gap surfaced investigating a campaign 31
    continuations deep where prior_macro_leg had gone stale.

    WHY THIS IS A SEPARATE FIELD, NOT A FIX TO prior_macro_leg: prior_
    macro_leg is, by design, the current swing's FOUNDING origin — it
    does NOT move on a same-direction continuation (see detect_bos_
    impulse()'s own contract on impulse_start), and that's the CORRECT
    behavior for its two existing consumers: compute_measured_move_
    extension()'s AB=CD comparison and relate_current_leg_to_context()'s
    reversal-confirmation check both need the founding origin (the level
    that must be reclaimed to invalidate the WHOLE current structure),
    not the most recent continuation's own anchor. Repurposing it would
    break both of those. So capturing this as a NEW, additive fact
    instead — "the leg immediately before THIS continuation" — rather
    than changing what prior_macro_leg means.

    Caller (scanner_live.py) calls this ONLY when it detects
    campaign_continuation_count is about to advance — i.e. a genuinely
    new continuation (or founding) leg has just been confirmed — passing
    the OLD leg's direction/latest_swing_origin/extreme from BEFORE this
    scan's compute_macro_bias() overwrote macro_leg_latest_swing_origin/
    macro_leg_extreme with the new continuation's own values.

    Returns {} when there's no previous continuation to snapshot (first
    continuation this field has ever tracked, or a state.json with no
    history yet) — never fabricates, same discipline as capture_prior_
    leg_snapshot() above.
    """
    if prev_latest_swing_origin is None or prev_extreme is None:
        return {}
    return {
        "prior_continuation_leg_direction": prev_direction,
        "prior_continuation_leg_origin": prev_latest_swing_origin,
        "prior_continuation_leg_extreme": prev_extreme,
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


def compute_campaign_extension(state, now_utc):
    """
    PURE. Reads the persistent "directional campaign" state (see
    _campaign_updates() above) and returns an observation-only summary —
    the friend's "long-horizon maturity lens" (2026-08-12 chat), distinct
    from and complementary to compute_measured_move_extension()'s
    leg-to-leg "short-horizon rhythm lens" just above. Both are kept;
    neither supersedes the other.

    Returns None until a campaign has actually been observed to start
    (state.json reset, or the very first leg since this feature
    shipped) — same "never fabricate a ratio from a missing prior"
    discipline as compute_measured_move_extension().

    OBSERVATION LAYER ONLY, deliberately NOT wired into anything else
    yet — not phase transitions, not risk bands, not trade gates, not
    expected events, not conviction. Per chat: "first make the bot
    capable of seeing the footprint, then measure whether that footprint
    matters, only later let it influence interpretation." That evidence-
    gathering step belongs to EXP4/shadow, not here — resist the urge to
    wire a threshold ("3x = exhausted") straight into this function or
    its caller; markets don't work that mechanically, and the whole
    point of keeping this a separate field is to not repeat that mistake.
    """
    direction              = state.get("campaign_direction")
    origin                 = state.get("campaign_origin")
    origin_time            = state.get("campaign_origin_time")
    initial_impulse_pips   = state.get("campaign_initial_impulse_pips")
    current_extreme        = state.get("campaign_current_extreme")
    continuation_count     = state.get("campaign_continuation_count")

    if None in (direction, origin, origin_time, initial_impulse_pips,
                current_extreme, continuation_count):
        return None
    if initial_impulse_pips <= 0:
        return None

    total_travel_pips = abs(current_extreme - origin) / PIP_SIZE
    extension_multiple = total_travel_pips / initial_impulse_pips

    age_hours = None
    try:
        origin_ts = pd.Timestamp(origin_time)
        now_ts = pd.Timestamp(now_utc)
        if origin_ts.tzinfo is not None and now_ts.tzinfo is None:
            now_ts = now_ts.tz_localize("UTC")
        elif origin_ts.tzinfo is None and now_ts.tzinfo is not None:
            origin_ts = origin_ts.tz_localize("UTC")
        age_hours = round((now_ts - origin_ts).total_seconds() / 3600, 1)
    except Exception:
        age_hours = None

    return {
        "direction":              direction,
        "origin":                 origin,
        "origin_time":            origin_time,
        "initial_impulse_pips":   initial_impulse_pips,
        "current_extreme":        current_extreme,
        "total_travel_pips":      round(total_travel_pips, 1),
        "extension_multiple":     round(extension_multiple, 2),
        "continuation_count":     continuation_count,
        "age_hours":              age_hours,
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


def classify_transition_cause(phase, macro_bias, prev_macro_bias, bias_stale,
                               bos_now, volatility_hint):
    """
    PURE. Labels WHY `phase` resolved the way it did this scan — see
    TransitionCause for the fixed taxonomy (per chat with Vally's
    collaborator). Every argument is a value compute_market_phase()
    already has in scope at the point it calls this; nothing here does
    new detection. Re-evaluated fresh each scan (a snapshot property of
    the CURRENT phase, same as Phase itself), not a one-time "this is the
    exact bar it started" event flag — consistent with how age_bars/
    history already treat phase transitions at the phase-VALUE level.

    Returns UNKNOWN, a real and legitimate value, when nothing here
    confidently explains the phase — better than guessing one.
    """
    if phase == Phase.MANIPULATION:
        return TransitionCause.SWEEP_RECLAIM

    if phase == Phase.EXHAUSTION:
        if bias_stale:
            return TransitionCause.FAILED_CONTINUATION
        return TransitionCause.EMA_EXHAUSTION

    if phase == Phase.EXPANSION:
        if bos_now is not None and bos_now.get("was_choch"):
            return TransitionCause.CHOCH
        if bos_now is not None:
            return TransitionCause.FRESH_BOS
        return TransitionCause.UNKNOWN

    # phase == Phase.TRANSITION — a sweep can't have happened here: the
    # caller already promotes phase to MANIPULATION whenever one did,
    # which is caught above.
    if prev_macro_bias is not None and macro_bias != prev_macro_bias:
        return TransitionCause.BIAS_FLIP
    if volatility_hint == "contracting":
        return TransitionCause.VOLATILITY_COLLAPSE
    if volatility_hint == "expanding":
        return TransitionCause.VOLATILITY_EXPANSION
    return TransitionCause.UNKNOWN


def compute_market_phase(df_1h, macro_bias, bias_stale, facts, state, extension=None,
                          prev_macro_bias=None, prev_phase=None):
    """
    PURE. See module-level docstring above. `facts` must be the
    MarketFacts instance already built for this scan (reused for its
    swing_high/swing_low and has_liquidity_sweep() — no new detection).
    `extension` is the (optional) dict from compute_measured_move_extension()
    — passed in rather than computed here so this function stays free of
    any assumption about WHEN the caller snapshots the prior leg.
    `prev_macro_bias` is macro_bias_confirmed AS OF BEFORE this scan's
    compute_macro_bias() call overwrote it — caller snapshots this the
    same way it already snapshots the prior leg for capture_prior_leg_
    snapshot(). Optional/defaults to None (BIAS_FLIP just won't be
    detectable that scan) so this stays backward compatible with any
    other call site.
    `prev_phase` is state["market_phase"] AS OF BEFORE this scan
    overwrites it (last scan's Phase.value, lowercase) — same before/
    after snapshot pattern as prev_macro_bias. Optional; None just means
    the EXHAUSTION exit-hysteresis below has nothing to compare against,
    so it behaves like a fresh boot. See PHASE_EXHAUSTION_EXIT_FRACTION
    in scanner_common.py for why this exists — without it, a leg sitting
    right on PHASE_EXHAUSTION_EMA_DIST_ATR_MULT flips EXPANSION<->
    EXHAUSTION every scan purely from intra-candle price noise, with no
    real regime change (confirmed live, 2026-08-26: EXPANSION at 3:00,
    EXHAUSTION at 3:05, nothing structurally different between them).

    CONTRACT: the returned phase (EXPANSION/EXHAUSTION/TRANSITION/
    MANIPULATION) describes THIS scan's macro_bias's leg specifically —
    it is not a general "market regime" label independent of direction.
    If macro_bias flips, phase is being asked about a different leg
    entirely, not a continuation of the same one re-labeled.

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

    # Exposed on MarketPhase regardless of which branch below fires — stay
    # None unless the matched-leg branch (the only one with a trustworthy
    # break_count/EMA-distance reading for THIS leg) actually sets them.
    # Deliberately NOT populated from a mismatched/stale bos_now in the
    # other branches — that would be describing a leg other than the one
    # the phase label is actually about.
    break_count = None
    dist_in_atr = None
    aging_reason = None

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
        # HYSTERESIS (2026-08-26, per chat — see PHASE_EXHAUSTION_EXIT_
        # FRACTION docstring in scanner_common.py). If we were ALREADY
        # EXHAUSTION last scan, don't revert to EXPANSION just because
        # dist_in_atr/break_count ticked back under the plain entry
        # threshold — that's exactly the noise this fix targets. Require
        # dropping to PHASE_EXHAUSTION_EXIT_FRACTION of the threshold
        # before reverting. Entering EXHAUSTION fresh (prev_phase was
        # EXPANSION/None/anything else) is UNCHANGED — still the plain
        # threshold, so this makes the label sticky on the way out, not
        # easier to trigger on the way in.
        was_exhaustion = prev_phase == Phase.EXHAUSTION.value
        break_threshold = (
            PHASE_EXHAUSTION_MIN_BREAK_COUNT * PHASE_EXHAUSTION_EXIT_FRACTION
            if was_exhaustion else PHASE_EXHAUSTION_MIN_BREAK_COUNT
        )
        dist_threshold = (
            PHASE_EXHAUSTION_EMA_DIST_ATR_MULT * PHASE_EXHAUSTION_EXIT_FRACTION
            if was_exhaustion else PHASE_EXHAUSTION_EMA_DIST_ATR_MULT
        )
        if break_count >= break_threshold:
            aging_reason = "break_count"
        if dist_in_atr >= dist_threshold:
            aging_reason = "ema_distance" if aging_reason is None else "break_count+ema_distance"
        phase = Phase.EXHAUSTION if aging_reason is not None else Phase.EXPANSION

    # MANIPULATION overlay: only meaningful while otherwise reading as
    # TRANSITION — a sweep-and-reclaim against the standing macro swing
    # boundary, using the exact sweep detector Tier 1/3 already gate on
    # (facts.has_liquidity_sweep), not a new detector.
    swept_high = swept_low = False
    if phase == Phase.TRANSITION:
        swept_high = facts.swing_high is not None and facts.has_liquidity_sweep(facts.swing_high)
        swept_low  = facts.swing_low is not None and facts.has_liquidity_sweep(facts.swing_low)
        if swept_high or swept_low:
            phase = Phase.MANIPULATION
    swept_boundary = "high" if swept_high else "low" if swept_low else None

    # Independent 1H volatility comparison (see PHASE_VOLATILITY_HINT_* in
    # scanner_common.py for why this is its own recompute rather than a
    # call into MIN's compute_market_state()).
    volatility_hint = None
    atr_series_1h = df_1h_x["ATR_1H"].dropna()
    lookback = PHASE_VOLATILITY_HINT_LOOKBACK_BARS
    if len(atr_series_1h) > lookback and not pd.isna(atr_now) and atr_now != 0:
        atr_then = atr_series_1h.iloc[-1 - lookback]
        if atr_then and not pd.isna(atr_then) and atr_then != 0:
            change = (atr_now - atr_then) / atr_then
            volatility_hint = (
                "expanding" if change > PHASE_VOLATILITY_HINT_THRESHOLD
                else "contracting" if change < -PHASE_VOLATILITY_HINT_THRESHOLD
                else "flat"
            )

    transition_cause = classify_transition_cause(
        phase, macro_bias, prev_macro_bias, bias_stale, bos_now, volatility_hint,
    )

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
        # ---- Market Thesis Engine additions (Phase 1, per chat) — mirror
        # of the fields on MarketPhase below, persisted so /thesis can
        # render the LAST computed thesis from state.json without needing
        # a fresh scan at the exact moment someone asks.
        "market_thesis_transition_cause": transition_cause.value,
        "market_thesis_aging_reason":     aging_reason,
        "market_thesis_break_count":      break_count,
        "market_thesis_dist_in_atr":      dist_in_atr,
        "market_thesis_swept_boundary":   swept_boundary,
        "market_thesis_volatility_hint":  volatility_hint,
    }

    narrative = _build_phase_narrative(macro_bias, phase, age_bars, history, extension=extension)

    result = MarketPhase(
        phase=phase, macro_bias=macro_bias, age_bars=age_bars,
        history=tuple(history), narrative=narrative, extension=extension,
        transition_cause=transition_cause, aging_reason=aging_reason,
        break_count=break_count, dist_in_atr=dist_in_atr,
        swept_boundary=swept_boundary, volatility_hint=volatility_hint,
    )
    return result, updates


def classify_failure_risk(phase, aging_reason, break_count, dist_in_atr, low_atr_warning):
    """
    PURE. Rule-based LOW/MEDIUM/HIGH label — per chat, deliberately NOT a
    fabricated percentage ("Failure Risk: 83%" implies precision this bot
    has no basis for). Every input is already computed by
    compute_market_phase()/MarketContext; this only buckets and narrates.
    Returns (band, reasons) — reasons is always populated, so a HIGH read
    is exactly as auditable as a LOW one.
    """
    if phase not in (Phase.EXPANSION, Phase.EXHAUSTION):
        return "N/A", ["No confirmed directional leg to assess yet."]

    if phase == Phase.EXHAUSTION:
        reasons = []
        if aging_reason and "break_count" in aging_reason:
            reasons.append(f"{break_count} breaks into this leg (aging threshold)")
        if aging_reason and "ema_distance" in aging_reason:
            reasons.append(f"{dist_in_atr:.1f} ATR from EMA_100 (exhaustion threshold)")
        if low_atr_warning:
            reasons.append("volatility below the warning floor")
        if not reasons:
            reasons.append("leg held-over/stale — founding structure already invalidated")
        return "HIGH", reasons

    # phase == Phase.EXPANSION
    reasons = []
    if break_count is not None and break_count >= max(
            1, round(PHASE_EXHAUSTION_MIN_BREAK_COUNT * FAILURE_RISK_APPROACHING_FRACTION)):
        reasons.append(
            f"{break_count} breaks into this leg — approaching the "
            f"{PHASE_EXHAUSTION_MIN_BREAK_COUNT}-break aging threshold"
        )
    if dist_in_atr is not None and dist_in_atr >= PHASE_EXHAUSTION_EMA_DIST_ATR_MULT * FAILURE_RISK_APPROACHING_FRACTION:
        reasons.append(
            f"{dist_in_atr:.1f} ATR from EMA_100 — approaching the "
            f"{PHASE_EXHAUSTION_EMA_DIST_ATR_MULT} ATR exhaustion threshold"
        )
    if low_atr_warning:
        reasons.append("volatility below the warning floor — thin conditions can fail continuation")
    if reasons:
        return "MEDIUM", reasons
    return "LOW", ["fresh leg, break count and EMA distance both well inside the aging thresholds"]


@dataclass
class MarketThesis:
    """
    One-scan market narrative — per chat with Vally's collaborator
    ("Why did it work / why did it fail", not "did it work"). Assembled
    entirely from MarketPhase/MarketContext/MarketFacts fields that are
    ALREADY computed each scan; detects nothing new. NOT a live gate — no
    tier reads this. Confidence is deliberately a fixed label, never a
    synthesized number — same reasoning as failure_risk staying banded
    instead of a fabricated percentage.

    Does NOT include Primary/Alternative/Competing Cases — still not
    built. Case File actual-vs-expected resolution tracking, however, IS
    covered — not here, but in format_scenario_summary() / `/legobs
    scenario` (min_scanner.py), which buckets resolved legs by
    (phase, transition_cause, extension) and reports the empirical fate
    split next to this dataclass's own expected_next_event text for that
    same (phase, cause) pair. Update this note if that report moves.

    CONTRACT on expected_next_event: this is a STATED possibility (often
    deliberately disjunctive, e.g. "CHoCH or range"), never a scored
    probability — do not build logic that treats it as a single
    deterministic prediction to grade right/wrong; that reintroduces the
    exact fabricated-precision problem confidence/failure_risk were
    designed to avoid.
    """
    current_state: str
    transition_narrative: str
    trend_health: str
    evidence: list
    weaknesses: list
    expected_next_event: Optional[str]
    failure_risk: str
    failure_risk_reasons: list
    invalidation: str
    # ---- additive field (Phase 1b, per chat — "cheap wins" pass): 15M
    # texture the thesis was missing entirely. compute_market_state()/
    # classify_regime() were already computed every scan for leg_obs
    # formation tagging (min_scanner.py) — this just gives the thesis the
    # same read. Bundled as one dict (same pattern as MarketPhase.extension)
    # rather than exploded into ~8 new scalar fields. None until a caller
    # populates it — nothing constructing a MarketThesis without it breaks.
    mtf_15m: Optional[dict] = None
    # ---- additive field (Phase 2a, per chat — "What Changed" layer): fixed
    # set of bullets diffing THIS scan's snapshot against the one persisted
    # last scan. See classify_thesis_delta() below. None only if the caller
    # didn't pass `prev` (shouldn't happen from scan(), which always does —
    # kept optional so nothing constructing MarketThesis directly breaks).
    delta: Optional[list] = None
    # ---- additive field (Phase 2c, per chat): 5M texture, same bundled-
    # dict pattern as mtf_15m — see compute_5m_read() above.
    mtf_5m: Optional[dict] = None
    # ---- additive field (Phase 2d, per chat — Structural Interpretation,
    # Layer 4 of the memo): the narrative stitch. Built ENTIRELY from
    # fields already on this dataclass (evidence/weaknesses/delta/mtf_5m/
    # timeline) — detects nothing new, same "no second engine" discipline
    # as everywhere else in the Market Thesis Engine. None if the caller
    # didn't pass a `timeline` (build_market_thesis()'s `timeline` param
    # is optional — a thesis is still valid without Market Story context).
    narrative: Optional[str] = None
    confidence: str = "Research only"
    # ---- additive field (Campaign Extension, per chat 2026-08-12 — the
    # friend's "long-horizon maturity lens"): a persistent directional
    # campaign spanning MULTIPLE continuation legs, distinct from
    # MarketPhase.extension (leg-to-leg AB=CD, resets every leg). See
    # _campaign_updates()/compute_campaign_extension() above. Bundled
    # dict, same pattern as mtf_15m/mtf_5m. None until a campaign has
    # actually been observed (state.json reset, or first leg since this
    # shipped) — never fabricated from a missing origin.
    #
    # DELIBERATELY OBSERVATION-ONLY: not read by failure_risk, not read
    # by any gate, not given a threshold anywhere in this codebase. Per
    # chat: "first make the bot capable of seeing the footprint, then
    # measure whether that footprint matters, only later let it
    # influence interpretation" — that's EXP4/shadow's job, not this
    # dataclass's. Resist wiring a magic number here later without that
    # evidence step.
    campaign: Optional[dict] = None
    # ---- additive fields (2026-08-26, per chat — human-facing /understand
    # and /thesis): prose renderings of evidence/weaknesses above, built
    # in lockstep with them (see build_market_thesis()'s evidence/
    # weaknesses block) so they can never list a different set of facts.
    # None-safe defaults so nothing constructing MarketThesis directly
    # breaks; callers should treat these as parallel to evidence/
    # weaknesses, never as a replacement computed independently.
    evidence_prose: Optional[list] = None
    weaknesses_prose: Optional[list] = None


def compute_5m_read(facts):
    """
    Market State, 5M layer (Phase 2c, per chat — Layer 1 of the memo: the
    thesis already had 1H (Phase) and 15M (mtf_15m) texture but no 5M read
    at all). PURE — built the exact same way bos_15m() is: same
    detect_bos_impulse primitive, same FRACTAL_WING, one timeframe down,
    off facts.df_5m which is already in scope. "Mostly free" per the plan.

    Deliberately does NOT reuse min_scanner.py's _exp2_bos_5m() —
    scanner_observation.py doesn't import from min_scanner.py (that would
    invert the module dependency direction the whole codebase uses) — so
    this recomputes with the identical window (tail(60)) and wing
    instead. Same twin-computation caveat as volatility_state vs
    volatility_hint elsewhere in this file: if EXP2 and this ever
    disagree, it's because one changed its tuning and the other didn't,
    not a bug in either.

    Returns a flat dict, ALWAYS present with every key, values None on no
    read — never raises. Caller (build_market_thesis) wraps this in the
    same try/except as mtf_15m.
    """
    bos5 = detect_bos_impulse(facts.df_5m.tail(60), wing=FRACTAL_WING)
    if bos5 is None:
        return {
            "m5_direction":            None,
            "m5_was_choch":            None,
            "m5_break_count":          None,
            "m5_relationship_to_15m":  None,
            "m5_relationship_to_htf":  None,
        }

    direction = bos5["direction"]

    bos15 = facts.bos_15m()
    relationship_to_15m = (
        ("aligned" if direction == bos15["direction"] else "countertrend")
        if bos15 is not None else None
    )

    relationship_to_htf = (
        ("aligned" if direction == facts.macro_bias else "countertrend")
        if facts.macro_bias in ("BULLISH", "BEARISH") else None
    )

    return {
        "m5_direction":            direction,
        "m5_was_choch":            bool(bos5.get("was_choch", False)),
        "m5_break_count":          bos5.get("break_count"),
        "m5_relationship_to_15m":  relationship_to_15m,
        "m5_relationship_to_htf":  relationship_to_htf,
    }


def classify_thesis_delta(prev, new):
    """
    PURE. Diffs THIS scan's thesis/phase snapshot (`new`) against the one
    persisted last scan (`prev`) — the "What Changed" / "Since last scan"
    layer of the Market Thesis Engine (per chat with Vally's collaborator;
    see the memo's Layer 3). Same discipline as classify_transition_cause():
    a FIXED set of comparisons on fields that already exist, never
    freeform text and never a diff of every field on the thesis — only
    the handful actually meaningful to a human reading /thesis.

    `prev` / `new` are same-shaped dicts — see build_market_thesis()
    (the only caller) for the exact keys. `prev` is None (or has no
    current_state yet) on the very first scan after a fresh state.json —
    returns a single bootstrap line rather than fabricating a comparison
    against nothing.
    """
    if not prev or prev.get("current_state") is None:
        return ["First thesis this run — no prior scan to compare against."]

    bullets = []

    if prev.get("current_state") != new["current_state"]:
        bullets.append(f"Phase shifted: {prev.get('current_state')} → {new['current_state']}")

    prev_bc, new_bc = prev.get("break_count"), new.get("break_count")
    if prev_bc is not None and new_bc is not None and new_bc != prev_bc:
        direction = "up" if new_bc > prev_bc else "down"
        bullets.append(f"Leg break count {direction}: {prev_bc} → {new_bc}")

    if new.get("transition_cause") is not None and prev.get("transition_cause") != new.get("transition_cause"):
        bullets.append(f"Transition cause: {prev.get('transition_cause') or '?'} → {new['transition_cause']}")

    if prev.get("failure_risk") != new["failure_risk"]:
        bullets.append(f"Failure risk: {prev.get('failure_risk') or '?'} → {new['failure_risk']}")

    prev_evidence, new_evidence = set(prev.get("evidence") or []), set(new.get("evidence") or [])
    for gained in sorted(new_evidence - prev_evidence):
        bullets.append(f"+ evidence: {gained}")
    for lost in sorted(prev_evidence - new_evidence):
        bullets.append(f"- evidence: {lost}")

    prev_weak, new_weak = set(prev.get("weaknesses") or []), set(new.get("weaknesses") or [])
    for gained in sorted(new_weak - prev_weak):
        bullets.append(f"+ weakness: {gained}")
    for lost in sorted(prev_weak - new_weak):
        bullets.append(f"- weakness: {lost}")

    if prev.get("expected_next_event") != new.get("expected_next_event"):
        bullets.append(
            f"Expected next event: {prev.get('expected_next_event') or 'none'} → "
            f"{new.get('expected_next_event') or 'none'}"
        )

    prev_vol = (prev.get("mtf_15m") or {}).get("volatility_state")
    new_vol = (new.get("mtf_15m") or {}).get("volatility_state")
    if new_vol is not None and prev_vol != new_vol:
        bullets.append(f"15M volatility regime: {prev_vol or '?'} → {new_vol}")

    if not bullets:
        bullets.append("No material change since last scan.")

    return bullets


MEASURED_MOVE_BUCKET_LABELS = {
    "UNDER_100":    "under 100% of prior",
    "EXT_100_150":  "100-150% of prior",
    "EXT_150_200":  "150-200% of prior",
    "EXT_OVER_200": "over 200% of prior",
}


def stitch_narrative(transition_narrative, mtf_5m, delta, timeline, extension=None, campaign=None):
    """
    PURE. Structural Interpretation — Layer 4 of the memo ("what sequence
    of observable events best explains the current structure?"). Stitches
    fields the thesis ALREADY computed into short sentences using a
    FIXED, enumerated set of template branches — per chat: "the
    intelligence should live in the structured market model, not the
    prose," explicitly NOT an LLM-style freeform essay. Every sentence is
    either copied straight from an existing field or built from a small
    branch keyed off a field that already exists; nothing here infers,
    predicts, or detects anything new.

    `mtf_5m` / `delta` / `timeline` / `extension` / `campaign` are each
    optional and independently None-safe — this degrades gracefully to
    just `transition_narrative` if all are unavailable (e.g. this scan's
    5M read failed, or no prior leg / campaign exists yet).
    `timeline` is the CURRENT open leg_obs record's event list (Facet
    from min_scanner.py's run_leg_observation, passed in READ-ONLY by
    the caller) — a separate pass from build_market_thesis() itself, so
    it can legitimately be missing/stale by up to one scan; that's fine,
    this is narrative colour, not a live gate.

    `extension` (compute_measured_move_extension()'s dict, via
    phase_result.extension) and `campaign` (compute_campaign_extension())
    were already being computed and persisted every scan but never
    reached this function — the gap the friend's 2026-08-12 review
    flagged (the scan log showed "leg ext: 82% of prior" while the
    thesis text said nothing about it). Deliberately kept purely
    DESCRIPTIVE here — state the ratio/bucket/travel, do not editorialize
    ("shows weakness", "is bearish") — same "never fabricate a threshold
    ahead of the evidence" discipline as compute_measured_move_extension()
    and compute_campaign_extension()'s own docstrings: the P(reversal) by
    (phase, bucket) read belongs to `/legobs scenario`, not to prose
    generated before that evidence exists.
    """
    parts = [transition_narrative]

    if extension is not None:
        bucket_label = MEASURED_MOVE_BUCKET_LABELS.get(extension['bucket'], extension['bucket'])
        parts.append(
            f"This leg has reached {extension['ratio']*100:.0f}% of the prior leg's "
            f"displacement ({extension['current_leg_pips']}p vs "
            f"{extension['prior_leg_pips']}p prior — {bucket_label})."
        )

    if mtf_5m and mtf_5m.get("m5_direction"):
        direction_word = mtf_5m["m5_direction"].title()
        rel = mtf_5m.get("m5_relationship_to_htf")
        if rel == "aligned":
            sentence = f"5M is trading {direction_word.lower()}, aligned with the higher timeframe."
        elif rel == "countertrend":
            sentence = (f"5M has turned {direction_word.lower()}, against the higher-timeframe "
                        f"direction — could be corrective or an early warning, not yet distinguishable.")
        else:
            sentence = f"5M is currently {direction_word.lower()}."
        if mtf_5m.get("m5_was_choch"):
            sentence += " (fresh 5M CHoCH)"
        parts.append(sentence)

    if delta:
        bootstrap_lines = (
            "No material change since last scan.",
            "First thesis this run — no prior scan to compare against.",
        )
        material = [d for d in delta if d not in bootstrap_lines]
        if material:
            parts.append("Since last scan: " + "; ".join(material[:3]) + ".")
        elif delta and delta[0] == "No material change since last scan.":
            parts.append("No material change since last scan.")

    if campaign is not None:
        parts.append(
            f"Campaign: {campaign['continuation_count']} continuation leg(s) "
            f"since {campaign['origin']:.5f} ({campaign['age_hours']}h ago), "
            f"{campaign['total_travel_pips']}p travelled — "
            f"{campaign['extension_multiple']}x the initial impulse."
        )

    if timeline:
        recap = "; ".join(ev.get("detail", "?") for ev in timeline[-3:])
        if recap:
            parts.append(f"Recent leg history: {recap}.")

    return " ".join(parts)


def build_market_thesis(phase_result, ctx, facts, macro_bias, bias_stale, state, prev=None,
                         timeline=None, now_utc=None):
    """
    PURE. Market Thesis Engine, Phase 1 (replaces the Advisory Council —
    per chat, Advisory's block-analysis/ATR-suitability pieces already
    have their own standalone commands, so nothing there is lost; its
    Sharpe line, which had no other command surface, gets folded into
    whichever caller renders this — see format_market_thesis() in
    min_scanner.py — only when a tier is actually live that scan).

    `phase_result` is the MarketPhase returned by compute_market_phase()
    THIS scan. `ctx` is the MarketContext from evaluate_market_context().
    `facts` is the same MarketFacts instance both were built from.

    `prev` (Phase 2a) is the caller's snapshot of LAST scan's thesis
    fields, captured BEFORE this scan's compute_market_phase()/
    build_market_thesis() calls overwrite state — see scanner_live.py's
    scan() for exactly where it's captured and why it can't just be
    re-read from `state` in here (compute_market_phase()'s own
    apply_state_updates() already overwrites market_thesis_break_count
    et al. before this function runs). Passed straight through to
    classify_thesis_delta(); build_market_thesis() does no diffing itself.

    `now_utc` (Campaign Extension, per chat 2026-08-12) feeds
    compute_campaign_extension()'s age_hours calculation — optional,
    same "None if the caller doesn't pass it" discipline as `timeline`,
    so nothing constructing a thesis directly (tests, REPL) breaks.
    """
    phase = phase_result.phase
    cause = phase_result.transition_cause
    break_count = phase_result.break_count
    dist_in_atr = phase_result.dist_in_atr
    direction_label = {"BULLISH": "Bullish", "BEARISH": "Bearish", "CONSOLIDATION": "Neutral"}.get(
        macro_bias, macro_bias)

    # ---- current state / transition narrative ---------------------------
    if phase == Phase.MANIPULATION:
        boundary = phase_result.swept_boundary or "a swing boundary"
        current_state = "Manipulation"
        transition_narrative = f"Liquidity swept the standing swing {boundary} and reclaimed — no confirmed directional leg yet."
    elif phase == Phase.TRANSITION:
        current_state = "Consolidation" if macro_bias == "CONSOLIDATION" else "Transition"
        if cause == TransitionCause.BIAS_FLIP:
            transition_narrative = f"Bias flipped to {direction_label} — no fresh leg confirmed yet."
        elif cause == TransitionCause.VOLATILITY_COLLAPSE:
            transition_narrative = "Range compressing — no fresh displacement to confirm a leg."
        elif cause == TransitionCause.VOLATILITY_EXPANSION:
            transition_narrative = "Volatility expanding but structure hasn't confirmed a leg yet."
        else:
            transition_narrative = "No confirmed directional leg this scan."
    else:
        current_state = f"{direction_label} {phase.value.title()}"
        if cause == TransitionCause.CHOCH:
            transition_narrative = f"{phase.value.title()} began after a CHoCH flip to {direction_label}."
        elif cause == TransitionCause.SWEEP_RECLAIM:
            boundary = phase_result.swept_boundary or "the standing swing"
            transition_narrative = f"{phase.value.title()} began after reclaim of liquidity swept at the swing {boundary}."
        elif cause == TransitionCause.FRESH_BOS:
            transition_narrative = f"{phase.value.title()} continuing on fresh {direction_label} BOS."
        elif cause == TransitionCause.FAILED_CONTINUATION:
            transition_narrative = "Leg is held-over/stale — the structure that founded this direction already invalidated."
        elif cause == TransitionCause.EMA_EXHAUSTION:
            transition_narrative = f"{phase.value.title()} — leg aged past the break-count/EMA-distance threshold while still notionally intact."
        else:
            transition_narrative = f"{phase.value.title()} — cause not confidently classified this scan."

    # ---- trend health ------------------------------------------------------
    if phase == Phase.EXPANSION:
        trend_health = (
            f"Healthy — {break_count} break(s) into this leg, no exhaustion signal yet."
            if break_count is not None else "Healthy."
        )
    elif phase == Phase.EXHAUSTION:
        reason_txt = {
            "break_count": "break-count",
            "ema_distance": "EMA-distance",
            "break_count+ema_distance": "break-count and EMA-distance",
        }.get(phase_result.aging_reason, "held-over/stale")
        trend_health = (
            f"Aging — {reason_txt} exhaustion trigger, {break_count} break(s) into this leg."
            if break_count is not None else f"Aging — {reason_txt}."
        )
    else:
        trend_health = "No active leg to assess."

    # ---- evidence / weaknesses ----------------------------------------------
    # ADDED (2026-08-26, per chat — human-facing /understand and /thesis):
    # evidence_prose / weaknesses_prose are the SAME facts as evidence/
    # weaknesses above, just phrased as a clause that reads naturally
    # mid-sentence instead of a labeled dev-facing fragment (e.g.
    # "Very-low-ATR warning — 3.5p, below the 4p floor" becomes
    # "volatility is very low, at 3.5 pips against a 4 pip floor"). Built
    # in lockstep with evidence/weaknesses (same if-branch, appended
    # together) specifically so the two can never drift apart — there is
    # exactly one place that decides WHETHER a fact is evidence/weakness,
    # this only decides how it reads. The raw versions are kept for
    # dev/audit surfaces and for delta-diffing (classify_thesis_delta()
    # compares the raw strings); the prose versions are for anything
    # meant to be read by a human as a sentence.
    evidence = []
    weaknesses = []
    evidence_prose = []
    weaknesses_prose = []
    if macro_bias in ("BULLISH", "BEARISH"):
        evidence.append(f"HTF bias: {macro_bias}")
        evidence_prose.append(f"the higher-timeframe bias is {macro_bias.lower()}")
    if facts.has_fresh_bos_aligned_with_bias():
        evidence                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          