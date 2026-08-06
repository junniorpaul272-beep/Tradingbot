"""
scanner_live.py
================
THE LIVE DECISION CORE. This is the only process that: calls Twelve
Data (fetch_ohlc), arbitrates which tier fires (evaluate_rule_of_law),
claims/releases leg ownership, manages the open trade, and enforces
the risk gate.

Deliberately excluded from this file (by design, not oversight):
  - check_result_commands() / the Telegram command handler — comes
    in a later pass once min_scanner.py exists, since it needs to
    import MIN's read-only report formatters (/shadow, /leaderboard,
    etc.) for instant replies without running any experiments itself.
  - _scan_once()/scan() orchestrator — wired up last.

NOTE ON TIERS: the three tier evaluate() functions and TIER_REGISTRY
live in scanner_observation.py, NOT here — min_scanner.py's Experiment 7
(Tier ATR Mirror) needs to call the exact same evaluate() logic as a
read-only peek every scan, and putting them here would force
min_scanner.py to import scanner_live.py, which in turn imports
min_scanner.py for report formatters below -- a circular import.
This file only keeps the WRITE side: evaluate_rule_of_law() (which
decides using those shared tier functions) and apply_leg_ownership().

NOTE ON EXPERIMENT E: experiment_e_rejected_live has been REPLACED by
log_rejected_live_to_queue() (bottom of this file). This process no
longer writes directly to shadow_state.json/shadow_stats.json at all --
those files are now owned exclusively by min_scanner.py to avoid two
processes racing on the same full-file JSON rewrite. Live only appends
one line to rejected_live_queue.jsonl; MIN drains that queue on its own
next run.

TRIGGER: this scanner is invoked by cron-job.org hitting the GitHub
Actions workflow_dispatch REST endpoint every 5 minutes — NOT GitHub's
native `schedule:` cron (confirmed unreliable/doesn't fire in this
account, hence cron-job.org). If runs stop showing up, check
cron-job.org's dashboard first, not the repo's Actions schedule tab.
"""

import os
import math
import json
import time
import uuid
import requests
import pandas as pd
from datetime import datetime, timezone

from scanner_common import (
    BASE_DIR, PAIR, PIP_SIZE, RR_RATIO, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    DATA_SPIKE_ATR_MULT, atomic_write_json, send_telegram, atr, _json_default,
    close_location, is_forex_weekend, check_data_freshness, data_looks_sane,
    apply_state_updates, _REMOVE, load_state, save_state, load_stats, save_stats,
    DIAGNOSTIC_MODE,
    CONVICTION_MIN_BY_TIER, CONVICTION_MANAGEMENT_BANDS, classify_conviction,
    SL_ATR_MULT, SL_MIN_PIPS, SL_VOL_SPIKE_RATIO, SL_ATR_MULT_COMPRESSED,
    MAX_RISK_ATR_MULT, MAX_RISK_PIPS, WATCHING_EXIT_PIPS,
    TRADE_STATUS_UPDATE_MINUTES, NEUTRAL_WATCH_COOLDOWN_MINUTES,
    NEUTRAL_WATCH_MIN_RETRACE, RESULT_TRACKING_ENABLED, STATS_SUMMARY_EVERY,
    STATE_FILE, STATS_FILE, LIVE_TRADE_LOG_FILE, TIER_PRIORITY,
    LEG_MATCH_TOLERANCE_PIPS, FVG_LOOKBACK_CANDLES, JOURNAL_MAX_ENTRIES,
    load_bias_ab_log, save_bias_ab_log, load_markov_data, save_markov_data,
    HTF_BIAS_MIN_BARS, ATR_WARN_PIPS, SWING_LOOKBACK_15, ATR_MIN_PIPS,
    SCAN_LOCK_FILE, SCAN_LOCK_MAX_AGE_SEC,
)
from scanner_observation import (
    compute_macro_bias, compute_macro_bias_shadow_old_rule,
    compute_market_phase, capture_prior_leg_snapshot, compute_measured_move_extension,
    evaluate_market_context, MarketContext, MarketFacts,
    compute_leg_id, _same_leg, get_leg_owner, bias_to_side,
    TierResult, TIER_REGISTRY,
    compute_signal_timeline_reset, compute_signal_timeline_updates,
    format_timeline_diagnostics, build_diagnostic_report, new_diagnostic,
    diag_set, tier_rating_from_score, _gate_stale_bias, sl_multiplier_for_context,
)
# One-way import for instant Telegram replies only — these are all
# read-only formatters/readers (they read shadow_stats.json/shadow_
# trade_log.jsonl/etc already on disk). Calling them here runs ZERO
# experiments and touches ZERO API. See module docstring + TELEGRAM
# COMMANDS section below for why this is the only safe direction.
from min_scanner import (
    load_shadow_stats, load_leg_obs_state,
    classify_bias_state, record_markov_transition, _SHADOW_ALIASES,
    format_shadow_summary, format_rejected_live_detail, format_shadow_recent,
    format_tier_block_analysis, format_advisory_council, format_ic_report,
    format_failure_investigation, format_shadow_detail, format_shadow_leaderboard,
    format_atr_suitability_table, format_markov_report, format_markov_line,
    format_leg_obs_summary, format_leg_obs_status, format_calibration_report,
    format_scenario_summary,
    format_case_detail, format_recent_cases, format_bias_ab_summary,
    compute_evidence, format_evidence_note,
)

# scanner_live.py is the ONLY file that reads this secret.
TWELVE_DATA_KEY = os.environ["TWELVE_DATA_KEY"]

CANDLE_CACHE_FILE = os.path.join(BASE_DIR, "candle_cache.json")
REJECTED_LIVE_QUEUE_FILE = os.path.join(BASE_DIR, "rejected_live_queue.jsonl")


# =========================================================================
# CANDLE CACHE — writer. MUST match min_scanner.py's _deserialize_cached_df/
# load_candle_cache exactly: reset_index() first (plain df.to_dict(orient=
# "records") silently DROPS a DataFrame's index — the candle timestamps
# live in the index, not a column), index column explicitly renamed to
# "datetime", THEN to_dict(orient="records"). Called once per live scan,
# right after the three fetch_ohlc calls below.
# =========================================================================
def write_candle_cache(df_5m, df_15m, df_1h, now_utc):
    def _serialize(df):
        out = df.reset_index()
        out.columns = ["datetime"] + list(df.columns)
        return out.to_dict(orient="records")

    try:
        atomic_write_json(CANDLE_CACHE_FILE, {
            "cached_at": now_utc.isoformat(),
            "5min":  _serialize(df_5m),
            "15min": _serialize(df_15m),
            "1h":    _serialize(df_1h),
        }, default=str)
    except Exception as e:
        print("[CANDLE CACHE WRITE ERROR] " + str(e))


# =========================================================================
# DATA LAYER — the only place in the whole split that calls Twelve Data.
# =========================================================================
_LAST_GOOD_OHLC = {}
OHLC_FETCH_RETRIES = 3
OHLC_FALLBACK_MAX_AGE_MIN = 30


def _fallback_ohlc(interval):
    cached = _LAST_GOOD_OHLC.get(interval)
    if cached is None:
        return None
    df, fetched_at = cached
    age_min = (time.time() - fetched_at) / 60.0
    if age_min > OHLC_FALLBACK_MAX_AGE_MIN:
        print(f"[FETCH FALLBACK] {interval}: last-good dataframe is {age_min:.1f} min old "
              f"(> {OHLC_FALLBACK_MAX_AGE_MIN} min cap) — refusing to use it, returning None")
        return None
    print(f"[FETCH FALLBACK] {interval}: all retries failed — using last-good dataframe "
          f"from {age_min:.1f} min ago")
    return df


def fetch_ohlc(interval, outputsize=200):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": PAIR,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_KEY,
        "format": "JSON",
    }

    resp = None
    for attempt in range(OHLC_FETCH_RETRIES):
        is_last_attempt = attempt == OHLC_FETCH_RETRIES - 1
        try:
            resp = requests.get(url, params=params, timeout=15).json()
        except Exception as e:
            if is_last_attempt:
                print(f"[FETCH ERROR] {interval} (all {OHLC_FETCH_RETRIES} attempts failed): {e}")
                return _fallback_ohlc(interval)
            backoff = 2 ** attempt  # 1s, 2s, 4s
            print(f"[FETCH RETRY] {interval} attempt {attempt + 1}/{OHLC_FETCH_RETRIES} "
                  f"failed ({e}); retrying in {backoff}s")
            time.sleep(backoff)
            continue

        if "values" not in resp:
            msg = resp.get("message") or resp.get("code") or "Unknown error"
            if is_last_attempt:
                print(f"[API ERROR] {interval} (all {OHLC_FETCH_RETRIES} attempts failed): {msg}")
                return _fallback_ohlc(interval)
            backoff = 2 ** attempt
            print(f"[API RETRY] {interval} attempt {attempt + 1}/{OHLC_FETCH_RETRIES}: "
                  f"{msg}; retrying in {backoff}s")
            time.sleep(backoff)
            continue

        break  # got a response with "values" — proceed to parse below
    else:
        # Loop exhausted without an explicit return (shouldn't happen given
        # the is_last_attempt branches above, but keep this as a hard
        # backstop rather than let `resp` be used unset).
        return _fallback_ohlc(interval)

    df = pd.DataFrame(resp["values"])
    df.index = pd.to_datetime(df["datetime"], utc=True)
    df = df[["open", "high", "low", "close"]].rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close"
    }).astype(float).sort_index()

    df = df.iloc[:-1]
    _LAST_GOOD_OHLC[interval] = (df, time.time())
    return df



# =========================================================================
# LEG OWNERSHIP — WRITE side. Only this process may claim/release a leg.
# (get_leg_owner, the read side, lives in scanner_observation.py.)
# =========================================================================
def apply_leg_ownership(state, decision):
    """
    The ONLY function that mutates state["leg_owner"]. Every place that
    used to call claim_leg()/release_leg() directly now builds a
    decision dict and passes it here — one auditable choke point for
    every ownership change, and every change is logged.

    decision: {"action": "claim", "tier": ..., "leg_id": ..., "status": ...,
               "upgraded": bool}
           or {"action": "release", "reason": ...}
    """
    if decision["action"] == "release":
        owner = state.get("leg_owner")
        if owner:
            print(f"  [RULE OF LAW] Released {owner.get('tier')} ownership of leg "
                  f"{owner.get('leg_id')} — {decision.get('reason','')}")
        state.pop("leg_owner", None)
    elif decision["action"] == "claim":
        state["leg_owner"] = {
            "leg_id": decision["leg_id"],
            "tier": decision["tier"],
            "status": decision["status"],
            "upgraded": decision.get("upgraded", False),
            "claimed_at": datetime.now(timezone.utc).isoformat(),
        }


def release_leg(state, reason=""):
    """Thin convenience wrapper around apply_leg_ownership for call
    sites outside the Rule of Law module itself (e.g. manage_active_trade
    releasing ownership when a trade closes)."""
    apply_leg_ownership(state, {"action": "release", "reason": reason})



# =========================================================================
# RULE OF LAW — arbitration only. Uses the shared tier evaluate() functions
# and _gate_stale_bias() from scanner_observation.py, but this is the only
# place their results actually become a state write (via apply_leg_ownership
# above).
# =========================================================================
def evaluate_rule_of_law(facts, ctx, state, stats, now_utc):
    """
    The arbitration layer. Returns the winning tier's TierResult (or an
    empty TierResult if nothing activated). Every state mutation this
    function needs goes through apply_leg_ownership() / apply_state_updates()
    — nothing here pokes state["leg_owner"] directly.

    Ownership rules:
      1. Compute leg_id from the current confirmed 1H swing. If the
         existing owner's leg_id no longer matches (a genuinely new 1H
         leg formed), release immediately regardless of which tier held it.
      2. If a tier still owns this leg:
           - status == "FIRED": untouchable. Only the owner is evaluated.
             (In practice manage_active_trade() already freezes the whole
             scan before this runs once a trade is open — this branch
             mainly covers the brief window between an entry firing and
             the trade being recorded.)
           - status == "WATCHING" and not yet upgraded: every
             STRICTLY-HIGHER-priority tier is evaluated first. The first
             one that activates STEALS ownership (an "upgrade") and is
             returned immediately — lower/equal-priority tiers, including
             the current owner, are not re-checked this scan.
           - No higher tier activated (or the leg was already upgraded
             once): fall through to re-evaluating the current owner only.
      3. Leg is unclaimed: walk TIER_PRIORITY top to bottom, first
         activation claims it. Lower-priority tiers are never evaluated
         once a higher one has already activated.
      4. Nothing activates: leg stays unclaimed, counted in stats.
    """
    leg_id = compute_leg_id(facts.macro_bias, facts.swing_high, facts.swing_low)
    owner = get_leg_owner(state)

    if owner is not None and not _same_leg(owner["leg_id"], leg_id):
        apply_leg_ownership(state, {"action": "release", "reason": "1H leg changed"})
        owner = None

    # Timeline resets whenever the underlying leg changes, same trigger as
    # leg ownership above — a new leg means a new hypothetical setup, so
    # stage timestamps from the previous leg are no longer meaningful.
    # PURE compute + apply_state_updates(), same discipline as every
    # other section of this function.
    apply_state_updates(state, compute_signal_timeline_reset(state, leg_id))
    timeline_now = state.get("signal_timeline", {})
    new_stages = compute_signal_timeline_updates(facts, timeline_now, now_utc)
    if new_stages:
        merged_timeline = dict(timeline_now)
        merged_timeline.update(new_stages)
        apply_state_updates(state, {"signal_timeline": merged_timeline})

    def _run(tier_label):
        result = TIER_REGISTRY[tier_label](facts, ctx, state, now_utc)
        apply_state_updates(state, result.state_updates)
        return result

    if owner is not None and owner["status"] == "FIRED":
        # Hard short-circuit — a FIRED owner is never re-evaluated, by
        # anyone, for any reason. In practice manage_active_trade()
        # already freezes the whole scan before this function runs again
        # once a trade is genuinely open, so this branch only guards
        # against leg_owner and stats["active_trade"] ever desyncing —
        # but if that happens, silently downgrading a FIRED owner back to
        # WATCHING (which re-running its stub would do, since a stub has
        # no memory of "I already fired") would be worse than a no-op.
        return TierResult(activated=True, fired=False, tier_label=owner["tier"],
                           reason=f"{owner['tier']} owns this leg as FIRED — untouchable, not re-evaluated")

    if owner is not None:
        owner_priority = TIER_PRIORITY.index(owner["tier"])
        can_upgrade = owner["status"] == "WATCHING" and not owner.get("upgraded", False)

        if can_upgrade:
            for tier_label in TIER_PRIORITY[:owner_priority]:
                result = _run(tier_label)
                result = _gate_stale_bias(result, state)
                if result.activated:
                    apply_leg_ownership(state, {
                        "action": "claim", "tier": tier_label, "leg_id": leg_id,
                        "status": "WATCHING",
                        "upgraded": True,
                    })
                    stats["ownership_upgrades"] = stats.get("ownership_upgrades", 0) + 1
                    print(f"  [RULE OF LAW] {tier_label} UPGRADES ownership from "
                          f"{owner['tier']} — {result.reason}")
                    return result

        result = _run(owner["tier"])
        result = _gate_stale_bias(result, state)
        if not result.activated:
            apply_leg_ownership(state, {
                "action": "release",
                "reason": f"{owner['tier']} deactivated — {result.reason}",
            })
        else:
            apply_leg_ownership(state, {
                "action": "claim", "tier": owner["tier"], "leg_id": leg_id,
                "status": "WATCHING",
                "upgraded": owner.get("upgraded", False),
            })
        return result

    # Unclaimed leg — priority walk, first activation wins.
    for tier_label in TIER_PRIORITY:
        result = _run(tier_label)
        result = _gate_stale_bias(result, state)
        if result.activated:
            apply_leg_ownership(state, {
                "action": "claim", "tier": tier_label, "leg_id": leg_id,
                "status": "WATCHING",
                "upgraded": False,
            })
            print(f"  [RULE OF LAW] {tier_label} claims leg {leg_id} — {result.reason}")
            return result

    stats["no_leg_owner"] = stats.get("no_leg_owner", 0) + 1
    return TierResult(reason="no tier activated on this leg")

# =========================================================================
# LIVE TRADE LOG — append-only record of the bot's own real trades. NOTE:
# this sits inside what LOOKS like "MIN territory" in the original file's
# line numbers, but both call sites (manage_active_trade below, and the
# /win /loss handler in check_result_commands) are live-only, so it stays
# here rather than in min_scanner.py.
# =========================================================================
def _append_live_trade_log(active, outcome, exit_price, r_achieved, closed_at, close_method):
    """Permanently appends ONE resolved live trade to LIVE_TRADE_LOG_FILE
    (append mode — this file is never truncated or rewritten). Called from
    both the auto-close path (SL/TP detected by manage_active_trade) and
    the manual-close path (/win, /loss commands).

    Fields mirror _append_shadow_trade_log where applicable so both logs
    can be read with the same tooling:
      pair, direction, entry_price, exit_price, sl, tp,
      opened_at, closed_at, outcome, r_achieved, target_r,
      tier_label, score, tier_rating, close_method.
    """
    target_r = active.get("target_r")
    record = {
        "pair":         PAIR,
        "direction":    active.get("direction"),
        "entry_price":  active.get("entry"),
        "exit_price":   exit_price,        # float for auto-close; None for manual
        "sl":           active.get("sl"),
        "tp":           active.get("tp"),
        "opened_at":    active.get("opened_at"),
        "closed_at":    closed_at,         # ISO string
        "outcome":      outcome,           # "WIN" / "LOSS"
        "r_achieved":   round(r_achieved, 2),
        "target_r":     target_r,
        "tier_label":   active.get("tier_label"),
        "score":        active.get("score"),
        "tier_rating":  active.get("tier_rating"),
        "close_method": close_method,      # "auto" | "manual"
    }
    try:
        with open(LIVE_TRADE_LOG_FILE, "a") as f:
            f.write(json.dumps(record, default=_json_default) + "\n")
    except Exception as e:
        print("[LIVE TRADE LOG ERROR] " + str(e))


# =========================================================================
# TRADE MANAGEMENT — shared regardless of which tier fired.
# =========================================================================
def apply_risk_gate_and_finalize(prospective_entry, prospective_sl, direction,
                                   current_atr, stats, score, tier_label,
                                   conviction=None):
    """Shared final stage for every tier: dual risk ceiling (ATR-relative
    + flat pip cap), then trade field assembly. Every tier goes through
    identical risk discipline.

    conviction: output of classify_conviction(), if provided. target_r
    from the conviction band REPLACES the flat RR_RATIO for TP placement
    — a CONSERVATIVE-band setup gets a nearer target than a FULL-band
    one. Falls back to RR_RATIO if conviction wasn't supplied (keeps this
    function callable the old way, e.g. from experiment/shadow code that
    doesn't run through Phase 3)."""
    target_r = conviction["target_r"] if conviction and conviction.get("target_r") else RR_RATIO
    if direction == "BUY":
        prospective_risk = prospective_entry - prospective_sl
    else:
        prospective_risk = prospective_sl - prospective_entry

    if not math.isfinite(prospective_risk) or prospective_risk <= 0:
        stats["risk_gate_suppressed"] = stats.get("risk_gate_suppressed", 0) + 1
        return {
            "fired": False,
            "risk_gate_pass": False,
            "risk_gate_reason": "stop must be finite and on the loss side of entry",
        }

    risk_ceiling_atr  = MAX_RISK_ATR_MULT * current_atr
    risk_ceiling_pips = MAX_RISK_PIPS * PIP_SIZE
    risk_too_wide = (prospective_risk > risk_ceiling_atr or prospective_risk > risk_ceiling_pips)

    if risk_too_wide:
        stats["risk_gate_suppressed"] += 1
        breach = []
        if prospective_risk > risk_ceiling_atr:
            breach.append("{:.1f}p > {:.1f}p ATR cap ({}x)".format(
                prospective_risk / PIP_SIZE, risk_ceiling_atr / PIP_SIZE, MAX_RISK_ATR_MULT))
        if prospective_risk > risk_ceiling_pips:
            breach.append("{:.1f}p > {}p flat cap".format(prospective_risk / PIP_SIZE, MAX_RISK_PIPS))
        return {"fired": False, "risk_gate_pass": False, "risk_gate_reason": "; ".join(breach)}

    risk_pips   = prospective_risk / PIP_SIZE
    reward_pips = (target_r * prospective_risk) / PIP_SIZE
    tp = (prospective_entry + target_r * prospective_risk if direction == "BUY"
          else prospective_entry - target_r * prospective_risk)
    return {
        "fired": True,
        "entry": prospective_entry, "sl": prospective_sl, "tp": tp,
        "risk_pips": risk_pips, "reward_pips": reward_pips,
        "risk_gate_pass": True, "risk_gate_reason": None,
        "tier_label": tier_label, "score": score,
        "target_r": target_r,
        "size_mult": conviction.get("size_mult") if conviction else None,
        "partial_r": conviction.get("partial_r") if conviction else None,
        "breakeven_r": conviction.get("breakeven_r") if conviction else None,
        "band_label": conviction.get("band_label") if conviction else None,
    }


def _last_signal_context(stats):
    """Pulls the most recently sent signal's context for journal
    enrichment. Returns "?" placeholders if nothing has fired yet."""
    return {
        "signal":      stats.get("last_journal_signal",      "?"),
        "entry":       stats.get("last_journal_entry",       "?"),
        "tier_label":  stats.get("last_journal_tier_label",  "?"),
        "score":       stats.get("last_journal_score",       "?"),
        "tier_rating": stats.get("last_journal_tier_rating", "?"),
        "signal_time": stats.get("last_journal_time",        "?"),
    }


def format_stats_summary(stats):
    """
    Funnel breakdown using V3's ACTUAL stats keys (tier1/2/3_signals,
    ownership_upgrades, no_leg_owner, risk_gate_suppressed) — V6's keys
    (watching_alerts/fib_reached/pattern_passed) don't exist in V3's
    tier-arbitration model, so this is a re-derivation, not a copy.
    """
    n = stats["total_scans"]
    if n == 0:
        return "No scans recorded yet."

    def pct(val):
        return f"{val/n*100:.1f}%" if n > 0 else "—"

    first = stats.get("first_scan", "?")
    last  = stats.get("last_scan",  "?")

    wins, losses = stats.get("wins", 0), stats.get("losses", 0)
    total_results = wins + losses
    win_rate = f"{wins}/{total_results} ({wins/total_results*100:.0f}%)" if total_results > 0 else "no results yet"

    realized_values = [
        float(j["realized_r"])
        for j in stats.get("journal", [])
        if j.get("realized_r") is not None
    ]
    if realized_values:
        expectancy = sum(realized_values) / len(realized_values)
        exp_str = f"{expectancy:+.2f}R per trade (n={len(realized_values)})"
    else:
        exp_str = "— (needs auto-tracked realized R)"

    lines = [
        "",
        "📊 *SMC Scanner V3 — Funnel Stats*",
        f"_Period: {first} → {last}_",
        "─────────────────────",
        f"🔍 Total scans:          `{n}`",
        f"➖ Consolidation skip:   `{stats.get('consolidation_skip', 0)}` ({pct(stats.get('consolidation_skip', 0))})",
        f"📉 ATR too low:          `{stats.get('atr_too_low', 0)}` ({pct(stats.get('atr_too_low', 0))})",
        f"🕒 Outside session:      `{stats.get('session_skip', 0)}` ({pct(stats.get('session_skip', 0))})",
        f"⚡ Regime shift skip:    `{stats.get('regime_shift_skip', 0)}` ({pct(stats.get('regime_shift_skip', 0))})",
        f"🕳 No leg owner:         `{stats.get('no_leg_owner', 0)}` ({pct(stats.get('no_leg_owner', 0))})",
        f"⬆️ Ownership upgrades:   `{stats.get('ownership_upgrades', 0)}`",
        f"🏛 Tier 1 (POI):         `{stats.get('tier1_signals', 0)}`",
        f"🏛 Tier 2 (Fib):         `{stats.get('tier2_signals', 0)}`",
        f"🏛 Tier 3 (Structure):   `{stats.get('tier3_signals', 0)}`",
        f"🛑 Risk-gate suppressed: `{stats.get('risk_gate_suppressed', 0)}`",
        f"🚨 Signals sent:         `{stats.get('signals_sent', 0)}` ({pct(stats.get('signals_sent', 0))})",
        "─────────────────────",
        f"🏆 Win rate:             `{win_rate}`",
        f"📐 Expectancy:           `{exp_str}`",
        "─────────────────────",
    ]

    if n > 20:
        if stats.get("atr_too_low", 0) / n > 0.3:
            lines.append("⚠️ _>30% of scans skipped on ATR — ATR_MIN_PIPS may be too high for current regime._")
        elif stats.get("no_leg_owner", 0) / n > 0.5:
            lines.append("⚠️ _Legs rarely find an owner — tier gates may be too strict for current conditions._")
        elif total_results >= 10 and wins / total_results < 0.35:
            lines.append("⚠️ _Win rate below 35% over 10+ trades — review tier entry logic._")
        else:
            lines.append("✅ _Funnel behaving as expected._")

    if RESULT_TRACKING_ENABLED:
        lines.append("_Send /win or /loss to log the last trade result._")

    return "\n".join(lines)

def check_trade_closed(active, c_last):
    """Assume SL if a single candle touches both levels (worse outcome,
    not a claim about intrabar sequencing)."""
    direction = active["direction"]
    sl, tp = active["sl"], active["tp"]
    high, low = c_last["High"], c_last["Low"]

    if direction == "BUY":
        hit_sl, hit_tp = low <= sl, high >= tp
    else:
        hit_sl, hit_tp = high >= sl, low <= tp

    if hit_sl:
        return "LOSS"
    if hit_tp:
        return "WIN"
    return None


def format_trade_status(active, current_price, now_utc):
    direction = active["direction"]
    entry, sl, tp = active["entry"], active["sl"], active["tp"]
    sign = 1 if direction == "BUY" else -1

    pl_pips      = (current_price - entry) * sign / PIP_SIZE
    dist_tp_pips = (tp - current_price) * sign / PIP_SIZE
    dist_sl_pips = (current_price - sl) * sign / PIP_SIZE

    opened_at = datetime.fromisoformat(active["opened_at"])
    elapsed   = now_utc - opened_at
    total_min = int(elapsed.total_seconds() // 60)
    hours, minutes = divmod(total_min, 60)
    time_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"

    pl_emoji = "🟢" if pl_pips >= 0 else "🔴"
    dir_emoji = "📈" if direction == "BUY" else "📉"

    return (
        "📊 *Trade Active — GBPUSD*\n\n"
        f"{dir_emoji} *Direction:* `{direction}` | *Tier:* `{active.get('tier_label','?')}`\n"
        f"📍 *Entry:* `{entry:.5f}`  →  *Now:* `{current_price:.5f}`\n"
        f"{pl_emoji} *P/L:* `{pl_pips:+.1f} pips`\n"
        f"🎯 *Distance to TP:* `{dist_tp_pips:.1f} pips`\n"
        f"🛡 *Distance to SL:* `{dist_sl_pips:.1f} pips`\n"
        f"⏱ *Time in trade:* `{time_str}`\n"
        "─────────────────────\n"
        f"_Signal-time score: {active.get('score', '?')} "
        f"({active.get('tier_rating', '?')}) — frozen, not recomputed._"
    )


def format_trade_query_response(stats, current_price, now_utc):
    """
    On-demand answer for /trade (ported from V6). Three cases:
      1. A live trade is open — same content as the periodic ping.
      2. No trade open, but the last one closed (auto SL/TP or manual
         /win|/loss) — say which level was hit / which result, and pips.
      3. Never had a trade this session.
    """
    active = stats.get("active_trade")
    if active:
        return format_trade_status(active, current_price, now_utc)

    last_closed = stats.get("last_closed_trade")
    if last_closed:
        hit    = last_closed.get("hit", "?")
        pips   = last_closed.get("pips")
        result = last_closed.get("result", "?")
        icon   = "✅" if result == "WIN" else "❌"
        try:
            pips_str = f"{pips:+.1f} pips"
        except (TypeError, ValueError):
            pips_str = "?"
        return (
            f"📭 *No trade in session* — {hit} hit {icon}\n"
            f"{last_closed.get('direction','?')} @ `{last_closed.get('entry','?')}` → "
            f"`{last_closed.get('exit','?')}` ({pips_str})\n"
            f"_Closed: {last_closed.get('closed_at','?')}_"
        )

    return "📭 *No trade in session.*"


def manage_active_trade(stats, state, df_5m, now_utc):
    """Runs BEFORE Macro Bias / Market Context / Rule of Law. If a trade
    is open, this is the ONLY thing scan() does this cycle. Returns True
    if a trade is open. Releases leg ownership on close either way."""
    active = stats.get("active_trade")
    if not active:
        return False

    c_last = df_5m.iloc[-1]
    last_checked = active.get("last_checked_candle")
    if last_checked:
        try:
            relevant = df_5m.loc[df_5m.index > pd.Timestamp(last_checked)]
        except Exception:
            relevant = df_5m.tail(1)
    else:
        relevant = df_5m.tail(1)

    outcome = None
    outcome_candle_time = None
    for candle_time, candle in relevant.iterrows():
        candidate = check_trade_closed(active, candle)
        if candidate is not None:
            outcome = candidate
            outcome_candle_time = candle_time
            break

    if outcome is not None:
        exit_price = active["tp"] if outcome == "WIN" else active["sl"]
        sign = 1 if active["direction"] == "BUY" else -1
        pl_pips = (exit_price - active["entry"]) * sign / PIP_SIZE

        if outcome == "WIN":
            stats["wins"] = stats.get("wins", 0) + 1
        else:
            stats["losses"] = stats.get("losses", 0) + 1

        icon = "✅" if outcome == "WIN" else "❌"
        send_telegram(
            f"{icon} *Trade closed — {outcome}* (auto-detected, GBPUSD)\n\n"
            f"📍 *Entry:* `{active['entry']:.5f}`  →  *Exit:* `{exit_price:.5f}`\n"
            f"{'🟢' if pl_pips >= 0 else '🔴'} *P/L:* `{pl_pips:+.1f} pips`\n"
            f"_Tier: {active.get('tier_label','?')} | "
            f"Signal-time score: {active.get('score', '?')}_"
        )
        print(f"  [TRADE] Auto-closed as {outcome} @ {exit_price:.5f} ({pl_pips:+.1f} pips).")

        # ── Journal entry (ported from V6) — auto-close logs exactly like
        # a manual /win or /loss so /journal shows a complete history
        # regardless of how each trade was closed.
        journal = stats.setdefault("journal", [])
        journal.append({
            "time":            active.get("opened_at_display", "?"),
            "logged_at":       now_utc.strftime("%Y-%m-%d %H:%M UTC"),
            "result":          outcome,
            "note":            "auto-closed (SL/TP hit)",
            "signal":          active.get("direction", "?"),
            "entry":           f"{active['entry']:.5f}" if "entry" in active else "?",
            "tier_label":      active.get("tier_label", "?"),
            "score":           active.get("score", "?"),
            "tier_rating":     active.get("tier_rating", "?"),
            "target_r":        active.get("target_r"),
            "realized_r":      active.get("target_r") if outcome == "WIN" else -1.0,
        })
        stats["journal"] = journal[-JOURNAL_MAX_ENTRIES:]
        # Snapshot for /trade — same shape manual /win|/loss produces.
        stats["last_closed_trade"] = {
            "direction":  active.get("direction", "?"),
            "entry":      f"{active['entry']:.5f}" if "entry" in active else "?",
            "exit":       f"{exit_price:.5f}",
            "result":     outcome,
            "hit":        "TP" if outcome == "WIN" else "SL",
            "pips":       pl_pips,
            "closed_at":  now_utc.strftime("%Y-%m-%d %H:%M UTC"),
            "closed_candle_time": outcome_candle_time.isoformat() if outcome_candle_time is not None else None,
            "opened_at_display": active.get("opened_at_display", "?"),
        }
        realized_r = active.get("target_r", 3.0) if outcome == "WIN" else -1.0
        _append_live_trade_log(
            active,
            outcome=outcome,
            exit_price=exit_price,
            r_achieved=realized_r,
            closed_at=now_utc.isoformat(),
            close_method="auto",
        )
        stats.pop("active_trade", None)
        stats["_trade_closed_this_scan"] = True
        release_leg(state, f"trade closed ({outcome})")
        return False

    if not relevant.empty:
        active["last_checked_candle"] = relevant.index[-1].isoformat()

    last_update = active.get("last_update_sent_at")
    send_update = True
    if last_update:
        try:
            gap = now_utc - datetime.fromisoformat(last_update)
            send_update = gap.total_seconds() >= TRADE_STATUS_UPDATE_MINUTES * 60
        except ValueError:
            send_update = True

    if send_update:
        send_telegram(format_trade_status(active, c_last["Close"], now_utc))
        active["last_update_sent_at"] = now_utc.isoformat()
        stats["active_trade"] = active
        print("  [TRADE] Status update sent — trade still open.")
    else:
        print("  [TRADE] Still open — skipping status ping.")

    return True


# =========================================================================
# REJECTED-LIVE QUEUE — replaces experiment_e_rejected_live's direct writes
# to shadow_state.json/shadow_stats.json. This process only ever appends
# the raw facts of a rejection here; min_scanner.py is the one that turns
# each queued line into a proper EXPE_REJECTED_LIVE shadow setup (via its
# own build_shadow_setup/log_shadow_setup) and clears the queue. Keeps
# both processes' full-file JSON rewrites (shadow_state.json/
# shadow_stats.json) to a single owner.
# =========================================================================
def log_rejected_live_to_queue(facts, ctx, state, live_result, now_utc):
    """Every time the live bot says NO, queue WHY (per-tier mandatory-
    condition breakdown) plus enough raw facts (side/entry/generic SL) for
    min_scanner.py to build a hypothetical EXPE_REJECTED_LIVE setup later,
    the same way experiment_e_rejected_live used to do inline."""
    if live_result.fired:
        return  # bot said YES this scan — nothing to queue

    checks = {}
    for tier_label, tier_fn in TIER_REGISTRY.items():
        peek = tier_fn(facts, ctx, state, now_utc)  # read-only peek — state_updates discarded
        checks[tier_label] = {"activated": peek.activated, "reason": peek.reason}

    # Same distinction as the original: a tier can peek as structurally
    # activated even on a scan where the GLOBAL ATR floor blocked the whole
    # scan before Rule of Law ever ran — flag it so min_scanner.py can tell
    # "blocked by ATR floor" apart from "blocked by its own conviction score".
    blocked_by_atr = not ctx.atr_ok
    blocked_by_session = not ctx.session_active
    blocked_by_post_spike = ctx.post_spike_active

    side = bias_to_side(facts.macro_bias)
    entry = float(facts.last_candle_5m()["Close"])
    generic_sl_distance = max(SL_ATR_MULT * facts.current_atr_5m(), SL_MIN_PIPS * PIP_SIZE)
    sl_raw = entry - generic_sl_distance if side == "BUY" else entry + generic_sl_distance

    # Same stable leg-identity anchor the original used post-audit-fix —
    # swing high/low + reason, NOT a rounded entry/timestamp that drifts
    # every scan (that was the dedup-defeating bug fixed in the monolith).
    leg_id = "{}|{:.5f}|{:.5f}|{}".format(side, facts.swing_high, facts.swing_low, live_result.reason)

    record = {
        "queued_at": now_utc.isoformat(),
        "leg_id": leg_id,
        "side": side,
        "entry": entry,
        "sl_raw": sl_raw,
        "atr_pips": ctx.current_atr_pips,
        "note": "Live bot took no action this scan — " + (live_result.reason or "no tier activated"),
        "tags": {
            "_blocked_by_atr": blocked_by_atr,
            "_blocked_by_session": blocked_by_session,
            "_blocked_by_post_spike": blocked_by_post_spike,
            **checks,
        },
    }
    try:
        with open(REJECTED_LIVE_QUEUE_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print("[REJECTED-LIVE QUEUE ERROR] " + str(e))


# =========================================================================
# TELEGRAM COMMANDS — the ONLY Telegram poller in the whole split (see
# module docstring: getUpdates is a single global queue per bot token,
# two independent pollers would race). MIN-owned commands below import
# their formatters straight from min_scanner.py and call them
# synchronously — this reads already-written shadow_stats.json/logs, it
# does NOT run any experiment or touch Twelve Data.
# =========================================================================
HELP_TEXT = (
    "🤖 *Available commands*\n\n"
    "*Trade logging*\n"
    "`/win` `/loss` — log the current signal's result\n"
    "`/confirm` — confirm a pending signal\n"
    "`/undo` — undo the last logged result\n"
    "`/trade` — show current open trade\n\n"
    "*Stats & journal*\n"
    "`/stats` — summary stats\n"
    "`/journal` — trade journal\n"
    "`/last` — last signal + diagnostics\n"
    "`/bias` `/biasab` — macro bias / bias A-B comparison\n\n"
    "*Shadow research pipeline*\n"
    "`/shadow` — full experiment summary\n"
    "`/shadow exp1`..`/shadow exp7` — one experiment\n"
    "`/shadow rejected` — rejected-live detail\n"
    "`/shadow recent` — last 10 resolved shadow trades\n"
    "`/shadow blocked tier1|tier2|tier3` — tier block analysis\n"
    "`/shadow advisory tier1|tier2|tier3` — Advisory Council report\n"
    "`/shadow ic tier1|tier2|tier3` — IC report\n"
    "`/shadow investigate <trade_id>` — failure investigation\n"
    "`/leaderboard` — experiments ranked by average R\n\n"
    "*Diagnostics*\n"
    "`/atrbands` — ATR band suitability\n"
    "`/markov` — Markov transition / regime state\n"
    "`/legobs` — per-leg order block tracking (`summary`, `scenario`)\n"
    "`/calibration` — calibration checks\n"
    "`/cases` — case studies\n\n"
    "`/help` — show this list"
)
# NOTE: keep this in sync by hand whenever a command is added/renamed in
# check_result_commands() below — there's no single source of truth this
# derives from automatically. If this list ever drifts from the real
# dispatch chain, that's a doc bug, not a functional one (the bot still
# works), but worth checking the two together on future edits.


def check_result_commands(stats, state=None):
    """
    Polls Telegram for result/journal commands (ported from V6, field
    names re-mapped to V3):
      /win [note], /loss [note]  — log a result, with 3 safeguards:
        1. per-signal cooldown (no double-logging the same signal)
        2. /undo — unconditional reversal of the last journal entry
        3. /confirm — override a flip within 60s of the last log
      /undo, /confirm, /stats, /trade, /shadow (+ exp1..exp7/rejected/recent),
      /leaderboard, /atrbands, /biasab, /bias, /journal, /last
    """
    if not RESULT_TRACKING_ENABLED:
        return stats

    url    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = stats.get("last_update_id", 0) + 1

    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 2}, timeout=5).json()
    except Exception:
        return stats

    if not resp.get("ok") or not resp.get("result"):
        return stats

    stats.setdefault("journal", [])

    def _log_result(stats, result, note, now_str, sig_time):
        entry_ctx = _last_signal_context(stats)
        active = stats.get("active_trade")
        target_r = active.get("target_r") if active else None
        journal_entry = {
            "time":      sig_time,
            "logged_at": now_str,
            "result":    result,
            "note":      note or "—",
            "target_r":  target_r,
            "realized_r": None,
            **entry_ctx,
        }
        stats["journal"].append(journal_entry)
        stats["journal"] = stats["journal"][-JOURNAL_MAX_ENTRIES:]
        if result == "WIN":
            stats["wins"] = stats.get("wins", 0) + 1
        else:
            stats["losses"] = stats.get("losses", 0) + 1
        stats["result_logged_for_signal"] = sig_time
        if active:
            stats["last_closed_trade"] = {
                "direction":  active.get("direction", "?"),
                "entry":      f"{active['entry']:.5f}" if "entry" in active else "?",
                "exit":       "manual",
                "result":     result,
                "hit":        "manual /win" if result == "WIN" else "manual /loss",
                "pips":       None,
                "closed_at":  now_str,
                "opened_at_display": active.get("opened_at_display", "?"),
            }
            if state is not None:
                release_leg(state, f"manual trade closed ({result})")
            stats["_trade_closed_this_scan"] = True
            realized_r = active.get("target_r", 3.0) if result == "WIN" else -1.0
            _append_live_trade_log(
                active,
                outcome=result,
                exit_price=None,   # manual close — no exact price known
                r_achieved=realized_r,
                closed_at=now_utc_dt.isoformat(),
                close_method="manual",
            )
        stats.pop("active_trade", None)
        return stats

    def _undo_last(stats):
        j = stats.get("journal", [])
        if not j:
            return None
        last = j.pop()
        if last.get("result") == "WIN":
            stats["wins"] = max(0, stats.get("wins", 0) - 1)
        elif last.get("result") == "LOSS":
            stats["losses"] = max(0, stats.get("losses", 0) - 1)
        stats["result_logged_for_signal"] = None
        stats.pop("pending_confirm", None)
        return last

    for update in resp["result"]:
        update_id = update.get("update_id", 0)
        stats["last_update_id"] = max(stats.get("last_update_id", 0), update_id)

        msg     = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        raw     = msg.get("text", "").strip()

        if chat_id != TELEGRAM_CHAT_ID:
            continue

        cmd  = raw.split()[0].lower() if raw else ""
        note = raw[len(cmd):].strip() if len(raw) > len(cmd) else ""

        now_utc_dt = datetime.now(timezone.utc)
        now_str    = now_utc_dt.strftime("%Y-%m-%d %H:%M UTC")
        sig_time   = stats.get("last_journal_time", "?")

        try:
            if cmd in ("/win", "win", "/w", "/loss", "loss", "/l"):
                result = "WIN" if cmd in ("/win", "win", "/w") else "LOSS"

                locked_sig = stats.get("result_logged_for_signal")
                if locked_sig is not None and sig_time != "?" and locked_sig == sig_time:
                    last_entry = stats["journal"][-1] if stats.get("journal") else None
                    is_flip    = last_entry is not None and last_entry.get("result") != result
                    within_60s = False
                    if last_entry is not None:
                        try:
                            last_logged = datetime.strptime(
                                last_entry.get("logged_at", ""), "%Y-%m-%d %H:%M UTC"
                            ).replace(tzinfo=timezone.utc)
                            within_60s = (now_utc_dt - last_logged).total_seconds() <= 60
                        except ValueError:
                            within_60s = False

                    if is_flip and within_60s:
                        stats["pending_confirm"] = {"cmd": cmd, "note": note, "sig_time": sig_time}
                        send_telegram(
                            f"⚠️ *A result was just logged for this signal* "
                            f"(`{last_entry.get('result')}`). Send /confirm to override with `{result}`."
                        )
                    else:
                        prior   = last_entry.get("result") if last_entry else "?"
                        prior_t = last_entry.get("logged_at") if last_entry else "?"
                        send_telegram(
                            f"🚫 *Result already logged for this signal* (`{prior}` at `{prior_t}`).\n"
                            f"Use /undo to reverse it, then log again — or wait for the next signal."
                        )
                    print(f"  [RESULT] Blocked duplicate {result} for signal @ {sig_time}.")
                    continue

                stats = _log_result(stats, result, note, now_str, sig_time)
                wins, losses = stats.get("wins", 0), stats.get("losses", 0)
                total = wins + losses
                wr    = f"{wins/total*100:.0f}%" if total > 0 else "—"
                icon  = "✅" if result == "WIN" else "❌"
                note_line = f"\n📝 _{note}_" if note else ""
                send_telegram(
                    f"{icon} *{result} logged*{note_line}\n"
                    f"Running record: `{wins}W / {losses}L` ({wr} win rate)\n"
                    f"_Send /journal to see recent entries._"
                )
                print(f"  [RESULT] {result} logged. Note: '{note}'. Running: {wins}W / {losses}L")

            elif cmd in ("/confirm", "confirm"):
                pending = stats.get("pending_confirm")
                if not pending:
                    send_telegram("_Nothing pending to confirm._")
                    continue
                _undo_last(stats)
                result = "WIN" if pending["cmd"] in ("/win", "win", "/w") else "LOSS"
                stats = _log_result(stats, result, pending["note"], now_str, pending["sig_time"])
                stats.pop("pending_confirm", None)
                wins, losses = stats.get("wins", 0), stats.get("losses", 0)
                total = wins + losses
                wr    = f"{wins/total*100:.0f}%" if total > 0 else "—"
                send_telegram(
                    f"🔁 *Result overridden* — now logged as `{result}`.\n"
                    f"Running record: `{wins}W / {losses}L` ({wr} win rate)"
                )
                print(f"  [RESULT] Override confirmed — now {result}. Running: {wins}W / {losses}L")

            elif cmd in ("/undo", "undo"):
                last = _undo_last(stats)
                if last is None:
                    send_telegram("_Nothing to undo — journal is empty._")
                else:
                    wins, losses = stats.get("wins", 0), stats.get("losses", 0)
                    total = wins + losses
                    wr    = f"{wins/total*100:.0f}%" if total > 0 else "—"
                    send_telegram(
                        f"↩️ *Undone* — removed `{last.get('result','?')}` logged at "
                        f"`{last.get('logged_at', last.get('time','?'))}`.\n"
                        f"Running record: `{wins}W / {losses}L` ({wr} win rate)"
                    )
                print("  [RESULT] /undo — reverted last entry.")

            elif cmd in ("/stats", "stats"):
                send_telegram(format_stats_summary(stats))

            elif cmd in ("/trade", "trade"):
                # df_5m isn't fetched yet at this point (commands are
                # processed before the data fetch so they're never dropped
                # by a fetch failure) — defer to scan(), right after df_5m
                # is available, same pattern as V6.
                stats["_pending_trade_query"] = True

            elif cmd in ("/shadow", "shadow"):
                shadow_stats = load_shadow_stats()
                arg = note.strip().lower()  # text after the command, e.g. "/shadow exp3" -> "exp3"

                if not arg:
                    summary = format_shadow_summary(shadow_stats)
                    send_telegram(summary or "🔬 _Shadow pipeline has no logged experiments yet._")

                elif arg in ("rejected", "reject", "live"):
                    send_telegram(format_rejected_live_detail(shadow_stats))

                elif arg in ("recent", "last10"):
                    send_telegram(format_shadow_recent() or "🕒 _No resolved shadow trades yet._")

                elif arg.startswith("blocked"):
                    tier_arg = arg[len("blocked"):].strip()
                    tier_map = {"tier1": "TIER_1_POI", "1": "TIER_1_POI",
                                "tier2": "TIER_2_FIB", "2": "TIER_2_FIB",
                                "tier3": "TIER_3_STRUCTURE", "3": "TIER_3_STRUCTURE"}
                    tier_label = tier_map.get(tier_arg)
                    if not tier_label:
                        send_telegram("🔬 _Usage: `/shadow blocked tier1`, `tier2`, or `tier3`._")
                    else:
                        send_telegram(format_tier_block_analysis(tier_label))

                elif arg.startswith("advisory"):
                    tier_arg = arg[len("advisory"):].strip()
                    tier_map = {"tier1": "TIER_1_POI", "1": "TIER_1_POI",
                                "tier2": "TIER_2_FIB", "2": "TIER_2_FIB",
                                "tier3": "TIER_3_STRUCTURE", "3": "TIER_3_STRUCTURE"}
                    tier_label = tier_map.get(tier_arg)
                    if not tier_label:
                        send_telegram("🏛 _Usage: `/shadow advisory tier1`, `tier2`, or `tier3`._")
                    else:
                        try:
                            send_telegram(format_advisory_council(tier_label))
                        except Exception as e:
                            send_telegram(f"🏛 _Advisory Council error: {e}_")

                elif arg.startswith("ic"):
                    tier_arg = arg[len("ic"):].strip()
                    tier_map = {"tier1": "TIER_1_POI", "1": "TIER_1_POI",
                                "tier2": "TIER_2_FIB", "2": "TIER_2_FIB",
                                "tier3": "TIER_3_STRUCTURE", "3": "TIER_3_STRUCTURE"}
                    tier_label = tier_map.get(tier_arg)
                    if not tier_label:
                        send_telegram("📈 _Usage: `/shadow ic tier1`, `tier2`, or `tier3`._")
                    else:
                        try:
                            send_telegram(format_ic_report(tier_label))
                        except Exception as e:
                            send_telegram(f"📈 _IC report error: {e}_")

                elif arg.startswith("investigate"):
                    trade_id_arg = arg[len("investigate"):].strip()
                    if not trade_id_arg:
                        send_telegram("🕵️ _Usage: `/shadow investigate <trade_id>` — "
                                       "find an id via `/shadow recent`._")
                    else:
                        try:
                            send_telegram(format_failure_investigation(trade_id_arg))
                        except Exception as e:
                            send_telegram(f"🕵️ _Failure Investigation error: {e}_")

                elif arg in _SHADOW_ALIASES:
                    key = _SHADOW_ALIASES[arg]
                    if key == "EXPE_REJECTED_LIVE":
                        send_telegram(format_rejected_live_detail(shadow_stats))
                    else:
                        send_telegram(format_shadow_detail(shadow_stats, key))

                else:
                    send_telegram(
                        f"🔬 _Don't recognize '{arg}'._\n"
                        "Try: `/shadow`, `/shadow exp1`..`/shadow exp7`, "
                        "`/shadow rejected`, `/shadow blocked tier1|tier2|tier3`, "
                        "`/shadow recent`, or `/leaderboard`."
                    )

            elif cmd in ("/leaderboard", "leaderboard"):
                board = format_shadow_leaderboard(load_shadow_stats())
                send_telegram(board or "🏆 _Nothing logged yet to rank._")

            elif cmd in ("/atrbands", "atrbands", "/atr", "atr"):
                table = format_atr_suitability_table()
                send_telegram(table or "📊 _No EXP7_TIER_ATR trades resolved yet — check back after a few hundred scans._")

            elif cmd in ("/markov", "markov", "/regime", "regime"):
                _state = load_state()
                _current_state = classify_bias_state(
                    _state.get("macro_bias_confirmed", "CONSOLIDATION"),
                    _state.get("macro_bias_stale", False),
                )
                send_telegram(format_markov_report(load_markov_data(), _current_state))

            elif cmd in ("/legobs", "legobs"):
                # /legobs          — current open leg record (Facet 1/2/3 live status)
                # /legobs summary  — distribution of all resolved legs
                # /legobs scenario — P(reversal) bucketed by phase + measured-move extension
                _obs = load_leg_obs_state()
                _note = note.strip().lower()
                if _note in ("summary", "hist", "history"):
                    send_telegram(format_leg_obs_summary())
                elif _note in ("scenario", "scenarios", "forecast"):
                    send_telegram(format_scenario_summary())
                else:
                    send_telegram(format_leg_obs_status(_obs, now_utc_dt))

            elif cmd in ("/calibration", "calibration", "/calib", "calib"):
                # Validation Engine: calibration check across all probability gates
                # (Markov transition probs, conviction bands, evidence posteriors).
                try:
                    send_telegram(format_calibration_report())
                except Exception as e:
                    send_telegram(f"🎯 _Calibration report error: {e}_")

            elif cmd in ("/cases", "cases", "/case", "case"):
                # Failure Investigation Bureau: /cases for recent summaries,
                # /case <n> for one specific case's full detail.
                try:
                    arg = note.strip()
                    if arg.isdigit():
                        send_telegram(format_case_detail(int(arg)))
                    else:
                        send_telegram(format_recent_cases())
                except Exception as e:
                    send_telegram(f"🕵️ _Case report error: {e}_")

            elif cmd in ("/biasab", "biasab"):
                send_telegram(format_bias_ab_summary(load_bias_ab_log()))

            elif cmd in ("/bias", "bias"):
                state        = load_state()
                confirmed    = state.get("macro_bias_confirmed", "?")
                stale        = state.get("macro_bias_stale", False)
                swing_high   = state.get("macro_swing_high")
                swing_low    = state.get("macro_swing_low")
                confirmed_at = state.get("macro_swing_confirmed_at", "?")
                leg_dir      = state.get("macro_leg_direction", "?")
                owner        = get_leg_owner(state)
                lines = [
                    "🧭 *1H Bias — live state*",
                    "─────────────────────",
                    f"Confirmed: `{confirmed}`" + (" ⚠️ STALE" if stale else ""),
                    f"Anchored leg direction: `{leg_dir}`",
                ]
                if swing_high is not None and swing_low is not None:
                    lines.append(f"Swing H/L: `{swing_high:.5f}` / `{swing_low:.5f}`")
                    lines.append(f"Confirmed at: `{confirmed_at}`")
                else:
                    lines.append("Swing H/L: _no anchor — nothing has confirmed this bias yet_")
                if owner:
                    lines.append(
                        f"Leg owner: `{owner.get('tier','?')}` — `{owner.get('status','?')}`"
                        + (" (upgraded)" if owner.get("upgraded") else "")
                    )
                else:
                    lines.append("Leg owner: _unclaimed_")
                if stale:
                    lines.append(
                        "\n_STALE means the leg that last confirmed this bias has "
                        "since invalidated (origin break or 78.6%+ retrace) and "
                        "nothing fresh has replaced it yet. The direction shown is "
                        "a hold-over, not a live confirmation._"
                    )
                send_telegram("\n".join(lines))

            elif cmd in ("/journal", "journal", "/j"):
                entries = stats.get("journal", [])
                if not entries:
                    send_telegram("📓 _No journal entries yet. Log trades with /win or /loss._")
                else:
                    recent = list(reversed(entries[-10:]))
                    lines  = ["📓 *Trade Journal — Last 10 entries*", "─────────────────────"]
                    for e in recent:
                        icon    = "✅" if e.get("result") == "WIN" else "❌"
                        sig     = e.get("signal", "?")
                        entry_p = e.get("entry", "?")
                        tier    = e.get("tier_label", "?")
                        score_v = e.get("score", "?")
                        note_t  = e.get("note", "—")
                        trade_time = e.get("time", "?")
                        lines.append(
                            f"{icon} `{trade_time}`\n"
                            f"   {sig} @ {entry_p} | {tier} | score {score_v}\n"
                            f"   📝 _{note_t}_"
                        )
                    send_telegram("\n".join(lines))

            elif cmd in ("/last", "last"):
                ctx = _last_signal_context(stats)
                if ctx and ctx.get("signal") != "?":
                    timeline_line = ""
                    tl = stats.get("last_journal_timeline")
                    if tl:
                        try:
                            timeline_line = "\n\n" + format_timeline_diagnostics(tl, datetime.now(timezone.utc))
                        except Exception:
                            timeline_line = ""
                    send_telegram(
                        f"🔁 *Last signal sent:*\n"
                        f"Direction: `{ctx.get('signal','?')}`\n"
                        f"Entry: `{ctx.get('entry','?')}`\n"
                        f"Tier: `{ctx.get('tier_label','?')}` — `{ctx.get('tier_rating','?')}`\n"
                        f"Score: `{ctx.get('score','?')}`\n"
                        f"Time: `{ctx.get('signal_time','?')}`"
                        + timeline_line
                    )
                else:
                    send_telegram("_No signal recorded yet this session._")

            elif cmd in ("/help", "help", "/commands", "commands", "/start"):
                send_telegram(HELP_TEXT)

            elif cmd:
                # AUDIT FIX: previously any unrecognized command at this
                # level (outside /shadow, which already had its own
                # fallback) was silently swallowed — no reply, no log line,
                # nothing. Someone fat-fingering `/stat` instead of `/stats`
                # would just get silence and have no idea whether the bot
                # was even running. Now it says so and points at /help.
                send_telegram(f"🤖 _Don't recognize '{cmd}'._ Send `/help` to see all commands.")
        except Exception as e:
            # AUDIT FIX: every branch above this point used to be able to fail
            # silently -- an uncaught exception here would propagate out of
            # check_result_commands(), get swallowed by the bare
            # "except Exception as e: print(...)" around its call site in
            # _scan_once(), and the person would just see nothing on Telegram
            # for the command they sent (e.g. /shadow exp3 crashing inside
            # format_shadow_detail with no reply at all). Now any command
            # failure is reported back to Telegram with the real exception,
            # instead of vanishing into the console log only.
            print("[COMMAND ERROR] " + cmd + " -- " + type(e).__name__ + ": " + str(e))
            try:
                send_telegram(
                    f"\u26a0\ufe0f _Command `{cmd}` failed: {type(e).__name__}: {e}_\n"
                    "_(Logged to console -- this is a bug, not you.)_"
                )
            except Exception:
                pass

    return stats


# =========================================================================
# MAIN SCAN — ties everything above together. Differences from the
# original monolith's _scan_once(), all deliberate (see SPLIT_STATUS.md
# for the full reasoning on each):
#
#   1. write_candle_cache() runs right after the three fetch_ohlc calls
#      — this is the ONLY way min_scanner.py ever sees market data.
#   2. Forward Observation (run_leg_observation) is GONE from here —
#      moved entirely to min_scanner.py's own run_min_pass(), which
#      doesn't depend on live's cadence to stay correct.
#   3. The Shadow Pipeline (run_shadow_pipeline / experiments 1-7) is
#      GONE from here too — same reason, now min_scanner.py's job.
#   4. experiment_e_rejected_live is GONE — replaced by
#      log_rejected_live_to_queue(), called from BOTH the "market
#      context blocked" early-return AND after Rule of Law, mirroring
#      the original's two run_shadow_pipeline call sites (each of which
#      used to call experiment_e_rejected_live internally either way).
#   5. Markov recording STAYS here (moved back after finding it's
#      explicitly a one-transition-per-scan-cadence model — see
#      min_scanner.py's comment at the same spot for why it doesn't
#      live there instead).
# =========================================================================
def _scan_once():
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%H:%M UTC")
    print("\n[" + now_str + "] Scan starting...")

    stats = load_stats()
    state = load_state()

    # ── Telegram commands — processed BEFORE the data fetch so a fetch
    # failure never silently drops a /win, /undo, /trade, etc. ───────────
    try:
        stats = check_result_commands(stats, state)
    except Exception as e:
        print("[COMMANDS ERROR] " + str(e))

    save_stats(stats)
    save_state(state)

    owner_at_start = get_leg_owner(state)
    if (owner_at_start is not None and owner_at_start.get("status") == "FIRED" and
            not stats.get("active_trade")):
        apply_leg_ownership(state, {
            "action": "claim",
            "tier": owner_at_start["tier"],
            "leg_id": owner_at_start["leg_id"],
            "status": "WATCHING",
            "upgraded": owner_at_start.get("upgraded", False),
        })
        save_state(state)

    stats["total_scans"] += 1
    if stats["first_scan"] is None:
        stats["first_scan"] = now_utc.strftime("%Y-%m-%d")
    stats["last_scan"] = now_utc.strftime("%Y-%m-%d %H:%M")

    diag = new_diagnostic() if DIAGNOSTIC_MODE else None

    if is_forex_weekend(now_utc):
        print("Forex market is closed for the weekend. Skipping market evaluation.")
        save_stats(stats)
        save_state(state)
        return

    outputsize_5m = 100
    active_for_fetch = stats.get("active_trade")
    if active_for_fetch and active_for_fetch.get("opened_at"):
        try:
            elapsed_bars = int(
                (now_utc - datetime.fromisoformat(active_for_fetch["opened_at"])).total_seconds() / 300)
            outputsize_5m = min(5000, max(100, elapsed_bars + 20))
        except Exception:
            outputsize_5m = 100

    # ── Fetch data — the ONLY place in the whole split that calls Twelve Data ─
    df_5m  = fetch_ohlc("5min",  outputsize=outputsize_5m)
    df_15m = fetch_ohlc("15min", outputsize=SWING_LOOKBACK_15 + 10)
    df_1h  = fetch_ohlc("1h",    outputsize=HTF_BIAS_MIN_BARS + 20)

    if df_5m is None or df_15m is None or df_1h is None:
        print("Data fetch failed. Exiting.")
        save_stats(stats)
        return

    fresh_5m  = check_data_freshness(df_5m,  5,  "5min",  now_utc)
    fresh_15m = check_data_freshness(df_15m, 15, "15min", now_utc)
    fresh_1h  = check_data_freshness(df_1h,  60, "1h",    now_utc)
    if not (fresh_5m and fresh_15m and fresh_1h):
        print("Data freshness check failed — exiting without trading on stale bars.")
        save_stats(stats)
        return

    if not (data_looks_sane(df_5m, "5min") and
            data_looks_sane(df_15m, "15min") and
            data_looks_sane(df_1h, "1h")):
        print("Data sanity check failed. Skipping this run.")
        save_stats(stats)
        return

    # ── Write the candle cache — min_scanner.py's ONLY source of market
    # data. Written even if we return early below (bias CONSOLIDATION,
    # market context blocked, etc.) — MIN needs candles for ATR-mirror/
    # Forward Observation work regardless of what live decides. ──────────
    write_candle_cache(df_5m, df_15m, df_1h, now_utc)

    if stats.get("_pending_trade_query"):
        current_price = df_5m["Close"].iloc[-1]
        send_telegram(format_trade_query_response(stats, current_price, now_utc))
        stats["_pending_trade_query"] = False

    trade_is_open = manage_active_trade(stats, state, df_5m, now_utc)
    save_state(state)
    if trade_is_open:
        save_stats(stats)
        return
    if stats.pop("_trade_closed_this_scan", False):
        save_stats(stats)
        return

    if len(df_1h) < HTF_BIAS_MIN_BARS:
        print(f"Only {len(df_1h)} 1H bars. Need {HTF_BIAS_MIN_BARS}. Skipping.")
        save_stats(stats)
        return

    # ── snapshot the leg about to potentially be replaced, so a genuine
    # leg-replacement can be compared against it for the measured-move
    # (AB=CD) check below — captured BEFORE compute_macro_bias() runs,
    # since that's the only call that ever overwrites macro_leg_*. ──────
    _prev_leg_origin    = state.get("macro_leg_origin")
    _prev_leg_extreme   = state.get("macro_leg_extreme")
    _prev_leg_direction = state.get("macro_leg_direction")

    # ── MACRO BIAS (pure) ─────────────────────────────────────────────────
    macro_bias, bias_updates = compute_macro_bias(df_1h, df_15m, state)
    apply_state_updates(state, bias_updates)
    bias_stale = state.get("macro_bias_stale", False)

    # ── measured-move bookkeeping (pure, additive, zero risk to the bias
    # path above — reads/writes only new prior_macro_leg_* keys). Only
    # fires when the origin actually changed this scan, i.e. a genuinely
    # NEW leg replaced the old one (same-leg extreme extension doesn't
    # count). Works regardless of WHICH internal branch of
    # compute_macro_bias() caused the change (fresh BOS, 15M promotion,
    # etc.) since it just compares before/after. ──────────────────────────
    if state.get("macro_leg_origin") != _prev_leg_origin:
        apply_state_updates(state, capture_prior_leg_snapshot(
            _prev_leg_direction, _prev_leg_origin, _prev_leg_extreme))

    shadow_bias, shadow_updates = compute_macro_bias_shadow_old_rule(df_1h, state)
    apply_state_updates(state, shadow_updates)
    bias_agree = (shadow_bias == macro_bias)
    if not bias_agree:
        print(f"  [SHADOW] live={macro_bias}{' STALE' if bias_stale else ''} | "
              f"old-rule={shadow_bias} | DIVERGE")

    try:
        bias_ab_log = load_bias_ab_log()
        bias_ab_log.append({
            "time":        now_utc.strftime("%Y-%m-%d %H:%M UTC"),
            "live_bias":   macro_bias,
            "shadow_bias": shadow_bias,
            "agree":       bias_agree,
            "price":       float(df_5m["Close"].iloc[-1]),
        })
        save_bias_ab_log(bias_ab_log)
    except Exception as e:
        print("[BIAS AB LOG ERROR] " + str(e))

    save_state(state)

    # ── MARKET EVOLUTION (Markov) — every scan, before CONSOLIDATION early-
    # return. Deliberately LIVE-ONLY (not min_scanner.py too): record_markov_
    # transition() is a "one scan-to-scan transition per call" model — running
    # it from two independent schedules would double-count/corrupt the
    # transition counts, not just race on the file write. min_scanner.py only
    # ever reads markov_state.json read-only (see its run_min_pass()). ─────
    try:
        current_bias_state = classify_bias_state(macro_bias, bias_stale)
        markov_data = load_markov_data()
        record_markov_transition(state, markov_data, current_bias_state)
        save_markov_data(markov_data)
        save_state(state)  # persist the just-updated markov_last_state too
    except Exception as e:
        print("[MARKOV ERROR] " + str(e))

    if macro_bias == "CONSOLIDATION":
        stats["consolidation_skip"] += 1
        save_stats(stats)
        diag_set(diag, "macro_bias", False, "CONSOLIDATION — no directional edge")
        print("  1H Bias: CONSOLIDATION — no directional edge. No tier is evaluated.")
        if diag is not None:
            print(build_diagnostic_report(diag))
        return
    diag_set(diag, "macro_bias", True,
             "STALE — hold-over direction, no live 1H break backing it" if bias_stale else None)

    swing_high = state.get("macro_swing_high")
    swing_low  = state.get("macro_swing_low")
    if swing_high is None or swing_low is None:
        print("  No confirmed 1H swing points yet. Skipping.")
        save_stats(stats)
        return

    # ── MARKET CONTEXT (pure) ─────────────────────────────────────────────
    ctx, ctx_reason, ctx_updates = evaluate_market_context(df_5m, state, now_utc)
    apply_state_updates(state, ctx_updates)
    save_state(state)

    print(
        "  1H Bias: {}{} | ATR: {:.1f}p{} | Regime shift: {} (ratio {:.2f}) | "
        "Post-spike cooldown: {} | Session active: {}".format(
            macro_bias, " (STALE)" if bias_stale else "",
            ctx.current_atr_pips, " (LOW-ATR WARNING)" if ctx.low_atr_warning else "",
            ctx.regime_shifted, ctx.regime_ratio,
            ctx.post_spike_active, ctx.session_active,
        )
    )

    # ── MARKET FACTS (pure observation, built once, shared by every tier) ─
    facts = MarketFacts(df_5m, df_15m, df_1h, macro_bias, swing_high, swing_low, now_utc)

    # ── MARKET PHASE (pure, read-only narrative layer — NOT a live gate) ──
    # Relabels macro bias / staleness / break_count / EMA distance /
    # swing-boundary sweeps (all already computed above) into a phase
    # taxonomy for logging and future research. No tier reads this.
    try:
        extension = compute_measured_move_extension(state)
        phase_result, phase_updates = compute_market_phase(
            df_1h, macro_bias, bias_stale, facts, state, extension=extension)
        apply_state_updates(state, phase_updates)
        save_state(state)
        print(f"  Market Phase: {phase_result.narrative}")
    except Exception as e:
        print("[PHASE ERROR] " + str(e))

    if not ctx.tradeable:
        if not ctx.atr_ok:
            stats["atr_too_low"] += 1
        elif ctx.post_spike_active:
            stats["regime_shift_skip"] += 1
        else:
            stats["session_skip"] = stats.get("session_skip", 0) + 1
        diag_set(diag, "market_context", False, ctx_reason)
        print(f"  NO TRADE — {ctx_reason}. No tier is evaluated live.")

        try:
            log_rejected_live_to_queue(
                facts, ctx, state,
                TierResult(reason="Market context blocked: " + ctx_reason), now_utc)
        except Exception as e:
            print("[REJECTED-LIVE QUEUE ERROR] " + str(e))

        save_stats(stats)
        if diag is not None:
            print(build_diagnostic_report(diag))
        return
    diag_set(diag, "market_context", True)

    # ── RULE OF LAW (arbitration, including ownership upgrades) ──────────
    result = evaluate_rule_of_law(facts, ctx, state, stats, now_utc)
    save_state(state)

    print(f"  [RULE OF LAW] {result.reason}")
    if result.conviction is not None:
        print(f"  [CONVICTION] {result.tier_label} score={result.score} "
              f"minimum={result.conviction['minimum']} decision={result.conviction['decision']} "
              f"band={result.conviction['band_label']}")
        if result.breakdown:
            print(f"  [CONVICTION BREAKDOWN] {result.breakdown}")
    diag_set(diag, "tier_evaluation", result.activated, None if result.activated else result.reason)

    # ── EVIDENCE ENGINE — read-only, tier-isolated, imported from
    # min_scanner.py (same one-way pattern as the command formatters). ────
    evidence = None
    if result.activated and result.tier_label:
        try:
            evidence = compute_evidence(result.tier_label, result.breakdown)
        except Exception as e:
            print("[EVIDENCE ERROR] " + str(e))
            evidence = None
        if evidence:
            print(f"  [EVIDENCE] {result.tier_label}: n={evidence['n']} "
                  f"win_rate={evidence['win_rate']}% avg_r={evidence['avg_r']} "
                  f"strength={evidence['strength']}")

    if evidence and not result.fired and result.activated:
        leg_key = compute_leg_id(facts.macro_bias, facts.swing_high, facts.swing_low)
        note_key = f"{result.tier_label}|{leg_key}"
        if state.get("evidence_last_note_key") != note_key:
            state["evidence_last_note_key"] = note_key
            save_state(state)
            note = format_evidence_note(result.tier_label, evidence)
            if note:
                send_telegram("🔬 *RESEARCH NOTE — Rule of Law said REJECT*\n\n" + note)

    # ── Queue this rejection (if any) for MIN's next pass, and periodically
    # ping the shadow dashboard — both just read what min_scanner.py has
    # already logged, no experiments run here. ───────────────────────────
    try:
        log_rejected_live_to_queue(facts, ctx, state, result, now_utc)
        if stats["total_scans"] % STATS_SUMMARY_EVERY == 0:
            summary = format_shadow_summary(load_shadow_stats())
            if summary:
                send_telegram(summary)
    except Exception as e:
        print("[REJECTED-LIVE QUEUE ERROR] " + str(e))

    if not result.fired:
        save_stats(stats)
        diag_set(diag, "leg_ownership", False,
                  result.reason if result.activated else "no tier activated")
        if diag is not None:
            print(build_diagnostic_report(diag))
        return
    diag_set(diag, "leg_ownership", True)

    # ── TRADE MANAGEMENT ──────────────────────────────────────────────────
    sl_mult = sl_multiplier_for_context(ctx)
    sl_buffer = max(sl_mult * ctx.current_atr, SL_MIN_PIPS * PIP_SIZE)
    sl_final = (result.sl_raw - sl_buffer if result.direction == "BUY"
                else result.sl_raw + sl_buffer)

    risk_result = apply_risk_gate_and_finalize(
        result.entry, sl_final, result.direction, ctx.current_atr,
        stats, result.score, result.tier_label,
        conviction=result.conviction,
    )

    if not risk_result["fired"]:
        print(f"  [RISK GATE] Suppressed — {risk_result['risk_gate_reason']}")
        save_stats(stats)
        diag_set(diag, "risk_gate", False, risk_result["risk_gate_reason"])
        if diag is not None:
            print(build_diagnostic_report(diag, header="Signal suppressed at risk gate"))
        return
    diag_set(diag, "risk_gate", True)

    stats_before_signal = dict(stats)
    stats["signals_sent"] += 1
    tier_counter_key = {
        "TIER_1_POI": "tier1_signals",
        "TIER_2_FIB": "tier2_signals",
        "TIER_3_STRUCTURE": "tier3_signals",
    }.get(result.tier_label)
    if tier_counter_key:
        stats[tier_counter_key] = stats.get(tier_counter_key, 0) + 1

    stats["active_trade"] = {
        "direction":   result.direction,
        "entry":       risk_result["entry"],
        "sl":          risk_result["sl"],
        "tp":          risk_result["tp"],
        "score":       result.score,
        "tier_rating": result.tier_rating,
        "tier_label":  result.tier_label,
        "opened_at":   now_utc.isoformat(),
        "opened_at_display": now_utc.strftime("%Y-%m-%d %H:%M UTC"),
        "last_update_sent_at": None,
        "last_checked_candle": df_5m.index[-1].isoformat(),
        "band_label":   risk_result["band_label"],
        "target_r":     risk_result["target_r"],
        "size_mult":    risk_result["size_mult"],
        "partial_r":    risk_result["partial_r"],
        "breakeven_r":  risk_result["breakeven_r"],
    }

    signal_time = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    stats["last_journal_signal"]      = result.direction
    stats["last_journal_entry"]       = f"{risk_result['entry']:.5f}"
    stats["last_journal_tier_label"]  = result.tier_label
    stats["last_journal_score"]       = result.score
    stats["last_journal_tier_rating"] = result.tier_rating
    stats["last_journal_time"]        = signal_time
    stats["last_journal_timeline"]    = dict(state.get("signal_timeline", {}))
    stats["result_logged_for_signal"] = None

    timeline_line = ""
    tl = stats.get("last_journal_timeline")
    if tl:
        try:
            timeline_line = "\n\n" + format_timeline_diagnostics(tl, now_utc)
        except Exception:
            timeline_line = ""

    direction_emoji = "📈" if result.direction == "BUY" else "📉"
    partial_line = (f"🎯 *Partial:* `{risk_result['partial_r']}R`\n"
                     if risk_result["partial_r"] else "")
    be_line = (f"🔒 *Breakeven at:* `{risk_result['breakeven_r']}R`\n"
               if risk_result["breakeven_r"] else "")
    evidence_line = ("\n\n" + format_evidence_note(result.tier_label, evidence)) if evidence else ""
    low_atr_line = (
        f"⚠️ *Low-ATR warning:* ATR `{ctx.current_atr_pips:.1f}p` is below the "
        f"`{ATR_WARN_PIPS}p` comfort floor (hard gate is `{ATR_MIN_PIPS}p`) — "
        "size/manage accordingly\n"
        if ctx.low_atr_warning else ""
    )
    try:
        _markov_note = format_markov_line(load_markov_data(), classify_bias_state(macro_bias, bias_stale))
        markov_line = (_markov_note + "\n") if _markov_note else ""
    except Exception:
        markov_line = ""
    telegram_ok = send_telegram(
        "🚨 *SMC SIGNAL — GBPUSD* 🚨\n\n"
        f"{direction_emoji} *Action:* `{result.direction}`\n"
        f"🏛 *Tier:* `{result.tier_label}` — `{result.tier_rating}`\n"
        f"🧠 *Conviction:* `{result.score}` — `{risk_result['band_label']}`\n"
        f"📊 *Bias:* `{macro_bias}` (1H structure)\n"
        "─────────────────────\n"
        f"📍 *Entry:* `{risk_result['entry']:.5f}`\n"
        f"🛡 *Stop:*  `{risk_result['sl']:.5f}` _({risk_result['risk_pips']:.1f} pips)_\n"
        f"🏆 *Target:* `{risk_result['tp']:.5f}` _({risk_result['reward_pips']:.1f} pips)_\n"
        f"⚖️ *RR:* `1:{risk_result['target_r']}`\n"
        f"📏 *Suggested size:* `{risk_result['size_mult']}x base risk`\n"
        + low_atr_line
        + partial_line + be_line
        + timeline_line
        + evidence_line
        + ("\n" + markov_line if markov_line else "")
    )
    if not telegram_ok:
        stats.clear()
        stats.update(stats_before_signal)
        print("  [SIGNAL] Telegram delivery failed. Trade was not activated.")
        save_stats(stats)
        save_state(state)
        return

    owner = get_leg_owner(state)
    apply_leg_ownership(state, {
        "action": "claim",
        "tier": result.tier_label,
        "leg_id": compute_leg_id(facts.macro_bias, facts.swing_high, facts.swing_low),
        "status": "FIRED",
        "upgraded": owner.get("upgraded", False) if owner else False,
    })
    save_stats(stats)
    save_state(state)


def scan():
    """Single-instance lock (stale locks older than SCAN_LOCK_MAX_AGE_SEC
    are cleared automatically) so an overlapping Cronjob.com trigger can't
    run two live scans concurrently against the same state.json."""
    lock_path = os.path.abspath(SCAN_LOCK_FILE)
    lock_fd = None
    for _ in range(2):
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(lock_fd, f"{os.getpid()}|{datetime.now(timezone.utc).isoformat()}".encode("utf-8"))
            break
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock_path)
            except OSError:
                age = 0
            if age > SCAN_LOCK_MAX_AGE_SEC:
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
                continue
            print("Another scanner process is already running. Skipping this invocation.")
            return

    if lock_fd is None:
        print("Could not acquire the scanner lock. Skipping this invocation.")
        return

    try:
        _scan_once()
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        try:
            os.remove(lock_path)
        except OSError:
            pass


if __name__ == "__main__":
    scan()
