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
from datetime import datetime, timezone, timedelta

from scanner_common import (
    BASE_DIR, PAIR, PIP_SIZE, RR_RATIO, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    atomic_write_json, send_telegram, atr, _json_default,
    is_forex_weekend, check_data_freshness, data_looks_sane,
    apply_state_updates, load_state, save_state, load_stats, save_stats,
    DIAGNOSTIC_MODE,
    CONVICTION_MANAGEMENT_BANDS, classify_conviction,
    LIVE_SIZE_MULT_DEFAULT,
    SL_ATR_MULT, SL_MIN_PIPS,
    MAX_RISK_ATR_MULT, MAX_RISK_PIPS,
    TRADE_STATUS_UPDATE_MINUTES,
    RESULT_TRACKING_ENABLED, STATS_SUMMARY_EVERY,
    THESIS_UPDATE_TZ_OFFSET_HOURS, THESIS_UPDATE_HOURS_LOCAL,
    LIVE_TRADE_LOG_FILE, TIER_PRIORITY,
    JOURNAL_MAX_ENTRIES,
    load_bias_ab_log, save_bias_ab_log, load_markov_data, save_markov_data,
    HTF_BIAS_MIN_BARS, ATR_WARN_PIPS, SWING_LOOKBACK_15, ATR_MIN_PIPS,
    SCAN_LOCK_FILE, SCAN_LOCK_MAX_AGE_SEC,
    atr_histogram_bucket,
)
from scanner_observation import (
    compute_macro_bias, compute_macro_bias_shadow_old_rule,
    compute_market_phase, capture_prior_leg_snapshot, compute_measured_move_extension,
    build_market_thesis, stitch_signal_narrative, build_market_intent,
    evaluate_market_context, MarketFacts, build_structure_digest,
    compute_leg_id, _same_leg, get_leg_owner, bias_to_side,
    TierResult, TIER_REGISTRY,
    compute_signal_timeline_reset, compute_signal_timeline_updates,
    format_timeline_diagnostics, build_diagnostic_report, new_diagnostic,
    diag_set, _gate_stale_bias, sl_multiplier_for_context,
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
    format_tier_block_analysis, format_market_thesis, format_ic_report,
    format_conviction_audit,
    format_policy_lab_report,
    format_regime_performance,
    format_failure_investigation, format_shadow_detail, format_shadow_leaderboard,
    format_atr_suitability_table, format_markov_report, format_markov_line,
    format_leg_obs_summary, format_leg_obs_status, format_calibration_report,
    format_scenario_summary,
    format_case_detail, format_recent_cases, format_bias_ab_summary,
    compute_evidence, format_evidence_note,
    load_market_intent_state, save_market_intent_state,
    run_market_intent_tracking, format_market_intent_report,
    format_bank_home, format_bank_performance, format_bank_account_detail,
    format_accounts_list, use_account_profile, set_account_profile,
    delete_account_profile, format_position_size_line,
    build_world_state, format_world_state,
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
        # AUDIT FIX (2026-08-17): without this, Twelve Data returns
        # candle "datetime" strings in whatever its default response
        # timezone is for this symbol — NOT guaranteed to be UTC. The
        # line below (`pd.to_datetime(df["datetime"], utc=True)`) then
        # LABELS that raw string as UTC without converting it, so any
        # non-UTC default silently offsets every candle timestamp —
        # and everything downstream that inherits it: shadow trade
        # resolved_at, bank_transactions resolved_at, bars_open counts.
        # Forcing UTC here removes the ambiguity at the source instead
        # of trusting an unverified default.
        "timezone": "UTC",
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

    # evaluated_tiers_this_scan — WorldState 1B (per chat, review approved
    # 2026-08-19). Tracks every tier_label _run() actually calls THIS
    # scan, so the digest built in _scan_once() can honestly distinguish
    # "genuinely not evaluated" from "evaluated and didn't activate" —
    # per review, that distinction must come from real evaluation, not be
    # inferred. Purely additive bookkeeping: one .append() alongside each
    # existing _run() call. Does not read any state, does not affect any
    # `if`, does not change what gets returned or who wins — nothing
    # about arbitration itself changes because this list exists.
    evaluated = []

    def _run(tier_label):
        evaluated.append(tier_label)
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
        locked = TierResult(activated=True, fired=False, tier_label=owner["tier"],
                             reason=f"{owner['tier']} owns this leg as FIRED — untouchable, not re-evaluated")
        locked.evaluated_tiers_this_scan = evaluated  # always [] on this branch — nothing ran
        return locked

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
                    result.evaluated_tiers_this_scan = evaluated
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
        result.evaluated_tiers_this_scan = evaluated
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
            result.evaluated_tiers_this_scan = evaluated
            return result

    stats["no_leg_owner"] = stats.get("no_leg_owner", 0) + 1
    unclaimed = TierResult(reason="no tier activated on this leg")
    unclaimed.evaluated_tiers_this_scan = evaluated
    return unclaimed


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
def build_live_tier_digest(result, risk_result=None):
    """
    PURE. WorldState 1B (per chat, review approved 2026-08-19) —
    STRICTLY OBSERVATIONAL. Called only after evaluate_rule_of_law() and
    (if reached) apply_risk_gate_and_finalize() have already fully
    decided everything this scan; nothing here feeds back into any
    decision, and nothing upstream reads this function's output. See
    world_state_schema.md for the full field-by-field review history.

    `decision`:
      "trade_already_open"       — owner leg is FIRED from an earlier
                                    scan, untouchable, genuinely not
                                    re-evaluated this scan (see
                                    evaluate_rule_of_law()'s hard
                                    short-circuit). NOT in the original
                                    4-value design from chat — found
                                    while tracing the real control flow.
                                    Distinguished structurally (zero
                                    tiers actually run this scan, per
                                    evaluated_tiers_this_scan) rather than
                                    by parsing `reason` text.
      "not_activated"             — evaluated, no setup found.
      "activated_not_fired"       — setup existed; this tier's OWN logic
                                    (pre-risk-gate) didn't hand off to
                                    Trade Management.
      "fired_risk_gate_passed"    — tier fired, risk gate passed, a real
                                    trade opened.
      "fired_risk_gate_rejected"  — tier fired, risk gate blocked it.

    risk_gate_reason is populated ONLY for the two risk-gate outcomes
    above — None everywhere else. Never fabricated (review check #3):
    absence of a risk-gate reason means the risk gate genuinely never
    ran, not "no reason available."

    evaluated_tiers_this_scan comes straight from
    evaluate_rule_of_law()'s own bookkeeping (review check #4) — any
    tier NOT in that list simply has no entry here. Never inferred.
    """
    evaluated_this_scan = getattr(result, "evaluated_tiers_this_scan", None) or []

    if risk_result is None:
        if not evaluated_this_scan and result.activated:
            decision = "trade_already_open"
        elif not result.activated:
            decision = "not_activated"
        else:
            decision = "activated_not_fired"
        risk_gate_reason = None
    else:
        decision = ("fired_risk_gate_passed" if risk_result.get("risk_gate_pass")
                    else "fired_risk_gate_rejected")
        risk_gate_reason = risk_result.get("risk_gate_reason")

    return {
        "tier_label": result.tier_label,
        "activated": result.activated,
        "decision": decision,
        "reason": result.reason,
        "risk_gate_reason": risk_gate_reason,
        "evaluated_tiers_this_scan": evaluated_this_scan,
    }


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
    doesn't run through Phase 3).

    LIVE CALL SITE (per chat, 2026-08-11): the live fire path now always
    passes conviction=None here — conviction score no longer drives live
    management. target_r falls back to RR_RATIO (already-existing
    fallback below); size_mult falls back to LIVE_SIZE_MULT_DEFAULT
    (1.0, matching the already-flattened CONVICTION_MANAGEMENT_BANDS
    value) instead of None so the Telegram message doesn't print
    "None x base risk". partial_r/breakeven_r stay None — no partial, no
    breakeven move, until real data supports specific numbers."""
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
        "size_mult": conviction.get("size_mult") if conviction else LIVE_SIZE_MULT_DEFAULT,
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
        f"📉 ATR data invalid:     `{stats.get('atr_invalid', 0)}` ({pct(stats.get('atr_invalid', 0))})",
        f"🕒 Outside session:      `{stats.get('session_skip', 0)}` ({pct(stats.get('session_skip', 0))})",
        f"⚡ Regime shift skip:    `{stats.get('regime_shift_skip', 0)}` ({pct(stats.get('regime_shift_skip', 0))})",
        f"🕳 No leg owner:         `{stats.get('no_leg_owner', 0)}` ({pct(stats.get('no_leg_owner', 0))})",
        f"⬆️ Ownership upgrades:   `{stats.get('ownership_upgrades', 0)}`",
        f"🏛 Tier 1 (POI):         `{stats.get('tier1_signals', 0)}`",
        f"🏛 Tier 2 (Fib):         `{stats.get('tier2_signals', 0)}`",
        f"🏛 Tier 3 (Structure):   `{stats.get('tier3_signals', 0)}`",
        f"🛑 Risk-gate suppressed: `{stats.get('risk_gate_suppressed', 0)}`",
        f"🚨 Signals sent:         `{stats.get('signals_sent', 0)}` ({pct(stats.get('signals_sent', 0))})",
        f"　　⚠️ very-low-ATR:      `{stats.get('signals_very_low_atr', 0)}`",
        f"　　🟡 low-ATR:           `{stats.get('signals_low_atr', 0)}`",
        "─────────────────────",
        f"🏆 Win rate:             `{win_rate}`",
        f"📐 Expectancy:           `{exp_str}`",
        "─────────────────────",
    ]

    hist = stats.get("atr_histogram")
    if hist:
        lines.append(format_atr_histogram(stats, compact=True))
        lines.append("_Full breakdown: /atrhist_")
        lines.append("─────────────────────")

    if n > 20:
        if stats.get("atr_invalid", 0) / n > 0.1:
            lines.append("⚠️ _>10% of scans have unusable ATR data — check the data feed._")
        elif stats.get("no_leg_owner", 0) / n > 0.5:
            lines.append("⚠️ _Legs rarely find an owner — tier gates may be too strict for current conditions._")
        elif total_results >= 10 and wins / total_results < 0.35:
            lines.append("⚠️ _Win rate below 35% over 10+ trades — review tier entry logic._")
        else:
            lines.append("✅ _Funnel behaving as expected._")

    pending = stats.get("pending_confirmation")
    if pending:
        lines.append(
            f"⏳ *Awaiting confirmation:* {pending['tier_label']} `{pending['direction']}` "
            f"@ `{pending['entry']:.5f}` ({pending.get('risk_band', '?')} risk) — "
            "reply /taken or /skip."
        )

    if RESULT_TRACKING_ENABLED:
        lines.append("_Send /win or /loss to log the last trade result._")

    return "\n".join(lines)


def _atr_histogram_sorted_items(hist):
    """Sort bucket labels ('0-1p', '1-2p', ..., '10p+') in numeric order
    by their lower bound, so display order is always ascending regardless
    of dict insertion order."""
    def sort_key(label):
        return int(label.split("-")[0].rstrip("p+"))
    return sorted(hist.items(), key=lambda kv: sort_key(kv[0]))


def format_atr_histogram(stats, compact=False):
    """
    Pure display of stats["atr_histogram"] — a scan-level tally of every
    ATR reading (in pips) bucketed at 1p width, incremented once per scan
    regardless of what else happened that scan (skipped, gated, signal
    fired, etc). Zero gating impact; this is purely descriptive of what
    volatility regime scans have actually been running in.

    compact=True returns a single inline line (used inside /stats);
    compact=False returns the full multi-line /atrhist report.
    """
    hist = stats.get("atr_histogram") or {}
    if not hist:
        return "📊 _No ATR histogram data yet — accumulates one tally per scan._"

    items = _atr_histogram_sorted_items(hist)
    total = sum(count for _, count in items)

    if compact:
        return "📊 ATR hist: " + "   ".join(f"{label}: {count}" for label, count in items)

    lines = [
        "",
        "📊 *ATR Histogram — scan-level, pure counting*",
        f"_{total} scans tallied_",
        "─────────────────────",
    ]
    max_count = max(count for _, count in items) if items else 0
    for label, count in items:
        pct = f"{count/total*100:.1f}%" if total else "—"
        bar_len = int(round((count / max_count) * 20)) if max_count else 0
        bar = "█" * bar_len
        lines.append(f"`{label:>6}` {bar} `{count}` ({pct})")
    lines.append("─────────────────────")
    lines.append(
        f"_Labels: <{ATR_MIN_PIPS}p = very low, {ATR_MIN_PIPS}-{ATR_WARN_PIPS}p = low "
        "(see fired-signal tags in /stats). Purely descriptive — never gates a scan._"
    )
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
    On-demand answer for /trade (ported from V6, extended per chat
    2026-08-11). Four cases:
      1. A live trade is open — same content as the periodic ping.
      2. A signal fired but is awaiting /taken or /skip confirmation.
      3. No trade open, but the last one closed (auto SL/TP or manual
         /win|/loss) — say which level was hit / which result, and pips.
      4. Never had a trade this session.
    """
    active = stats.get("active_trade")
    if active:
        return format_trade_status(active, current_price, now_utc)

    pending = stats.get("pending_confirmation")
    if pending:
        risk_band = pending.get("risk_band", "?")
        risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(risk_band, "⚪")
        reasons = pending.get("risk_reasons") or []
        reasons_lines = "\n".join(f"• {r}" for r in reasons)
        recommendation = pending.get("risk_recommendation")
        recommendation_line = f"🧭 _{recommendation}_\n" if recommendation else ""
        return (
            f"⏳ *Awaiting confirmation* — {pending.get('tier_label','?')} "
            f"`{pending.get('direction','?')}` @ `{pending.get('entry', 0):.5f}`\n"
            f"{risk_emoji} Risk-at-hand: `{risk_band}`\n"
            f"{reasons_lines}\n"
            + recommendation_line +
            f"_Fired: {pending.get('opened_at_display','?')}_\n\n"
            "❓ Reply /taken or /skip — no reply by the next scan = assumed taken."
        )

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
def _rejected_leg_id(side, facts, tier_anchor):
    """Same stable leg-identity anchor every rejected-live record uses —
    swing high/low + a STABLE tier anchor (never a live-computed score;
    see log_rejected_live_to_queue's own docstring for why that mattered).
    Factored out (per chat, 2026-08-16) so log_risk_gate_rejection_to_
    queue() below reuses the identical format instead of a second,
    driftable copy of it."""
    return "{}|{:.5f}|{:.5f}|{}".format(side, facts.swing_high, facts.swing_low, tier_anchor)


def _write_rejected_live_record(record):
    """Appends ONE record to REJECTED_LIVE_QUEUE_FILE. Factored out of
    log_rejected_live_to_queue (per chat, 2026-08-16) so
    log_risk_gate_rejection_to_queue() below shares the exact same write
    path — including the default=_json_default fix (see the comment this
    carries forward) — instead of a second copy that could silently drift
    back to the same numpy-bool crash this one already fixed once."""
    try:
        with open(REJECTED_LIVE_QUEUE_FILE, "a") as f:
            # BUG FIX (found live in prod, GH Actions run #453, 2026-08-12):
            # this was a raw json.dumps(record) with no `default=`, so any
            # numpy bool/int/float slipping into `tags` (peek.activated/
            # peek.fired are numpy bools straight off pandas/numpy
            # comparisons, not native Python bool) crashed with "Object of
            # type bool is not JSON serializable" — see _json_default's
            # own docstring for why that error text is misleading on
            # numpy>=2.0. Every OTHER writer in this codebase already goes
            # through atomic_write_json (which defaults to _json_default)
            # or passes default=_json_default explicitly — this was the
            # one write path that didn't. Silently ate this scan's
            # rejected-live record every time it fired (caught by the
            # try/except around each call site below), not a crash, so it
            # went unnoticed until someone actually read the Action logs.
            f.write(json.dumps(record, default=_json_default) + "\n")
    except Exception as e:
        print("[REJECTED-LIVE QUEUE ERROR] " + str(e))


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
        # "fired" added (per chat, 2026-08-12) so format_tier_block_analysis()
        # can actually tell WATCHING (activated, fired=False — rejection
        # candle not confirmed yet) apart from a tier whose OWN raw peek
        # says it structurally would fire this scan. NOTE: this peek does
        # NOT go through evaluate_rule_of_law's ownership arbitration or
        # _gate_stale_bias — so peek.fired=True here means "this tier's
        # mandatory conditions + trigger are satisfied in isolation," not
        # "this tier was the live winner." A tier can show fired=True here
        # while the actual live_result belongs to a different (higher-
        # priority) tier that scan; see _blocked_by_stale_bias below for
        # the one case where "this tier's peek says fire, but live said no"
        # IS fully attributable to a specific, known cause.
        checks[tier_label] = {"activated": peek.activated, "fired": peek.fired, "reason": peek.reason}

    # NOTE: ATR pip level is no longer a hard gate — blocked_by_atr now
    # only means the ATR reading itself was unusable (NaN/0), which is
    # rare. A thin-but-valid ATR reading no longer blocks anything; use
    # the _atr_very_low/_atr_low tags below instead to see whether a
    # rejected scan happened during thin volatility.
    blocked_by_atr = not ctx.atr_ok
    blocked_by_session = not ctx.session_active
    blocked_by_post_spike = ctx.post_spike_active
    # Same live-fire gate _gate_stale_bias applies inside evaluate_rule_of_
    # law — recorded directly here (not inferred from live_result.reason
    # text) so format_tier_block_analysis() has an unambiguous signal, the
    # same way _blocked_by_atr/_blocked_by_session/_blocked_by_post_spike
    # already do for their own gates.
    blocked_by_stale_bias = bool(state.get("macro_bias_stale"))

    side = bias_to_side(facts.macro_bias)
    entry = float(facts.last_candle_5m()["Close"])
    generic_sl_distance = max(SL_ATR_MULT * facts.current_atr_5m(), SL_MIN_PIPS * PIP_SIZE)
    sl_raw = entry - generic_sl_distance if side == "BUY" else entry + generic_sl_distance

    # Same stable leg-identity anchor the original used post-audit-fix —
    # swing high/low + a STABLE anchor, NOT a rounded entry/timestamp that
    # drifts every scan (that was the dedup-defeating bug fixed in the
    # monolith). CORRECTION (found investigating repeated rejected-live
    # entries showing up 5 minutes apart in production): live_result.reason
    # is NOT actually stable — classify_conviction() embeds the live
    # numeric score directly in the reason text ("TIER_3_STRUCTURE
    # conviction 45 < minimum 55..."), and that score drifts scan-to-scan
    # with price even when the underlying setup hasn't fundamentally
    # changed. Using `reason` in leg_id meant a score wobble (45 -> 46 ->
    # 45) produced a NEW leg_id every scan, silently defeating
    # drain_rejected_live_queue's drained_leg_ids dedup (exact-string-
    # match) — reproducing the exact symptom the "stable anchor" comment
    # above was trying to prevent. Fixed: use live_result.tier_label
    # instead (a constant string like "TIER_3_STRUCTURE", not a live-
    # computed number). Also stable for the market-context-blocked call
    # site below, whose placeholder TierResult leaves tier_label at its
    # default (None) regardless of ctx_reason's specific wording — falls
    # back to "NO_TIER_ACTIVATED" for that case and the true "no tier
    # activated on this leg" fallthrough in evaluate_rule_of_law. Full
    # reason text still goes in `note` below for human-readable context —
    # it's just no longer part of the identity key.
    tier_anchor = live_result.tier_label or "NO_TIER_ACTIVATED"
    leg_id = _rejected_leg_id(side, facts, tier_anchor)

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
            "_blocked_by_stale_bias": blocked_by_stale_bias,
            "_atr_very_low": ctx.very_low_atr_warning,
            "_atr_low": ctx.low_atr_warning,
            "_blocked_by_risk_gate_width": False,   # see log_risk_gate_rejection_to_queue — this function never covers that case
            **checks,
        },
    }
    _write_rejected_live_record(record)


# =========================================================================
# RISK-GATE WIDTH REJECTIONS — per chat, 2026-08-16. log_rejected_live_to_
# queue() above ONLY runs when a tier never structurally fired at all
# (called before apply_risk_gate_and_finalize even runs — see _scan_once).
# A tier that DID fire but got REJECTED for being too wide (MAX_RISK_
# ATR_MULT / MAX_RISK_PIPS) was never queued anywhere — the specific
# population the friend's Structural Stop Engine discussion is about had
# ZERO shadow evidence behind it. This is the fix: queue the REAL
# structural stop that got rejected (not a generic ATR*1.5 guess), tagged
# distinctly, so EXPE_REJECTED_LIVE can finally answer "what would have
# happened if this gate hadn't fired" with actual resolved R-multiples —
# per the friend's own point 8, "shadow the whole thing before letting it
# influence anything live." Live behavior is completely unchanged by
# this — the gate still rejects exactly as before; this only adds
# visibility into what it's rejecting.
# =========================================================================
def log_risk_gate_rejection_to_queue(facts, ctx, result, prospective_entry,
                                       prospective_sl, risk_gate_reason, now_utc):
    """Queues ONE risk-gate width rejection with the REAL prospective
    entry/stop that was actually tested against MAX_RISK_ATR_MULT/
    MAX_RISK_PIPS — i.e. result.entry and sl_final exactly as passed into
    apply_risk_gate_and_finalize() at the call site in _scan_once, not a
    re-derived approximation. Same queue file, same downstream consumer
    (drain_rejected_live_queue -> build_shadow_setup, unmodified) — this
    only changes what gets written in, not how it's read.

    result: the winning TierResult from evaluate_rule_of_law (already
    confirmed .fired=True by the caller — this function doesn't re-check).
    """
    side = result.direction
    risk_pips = abs(prospective_entry - prospective_sl) / PIP_SIZE
    atr_ratio = (risk_pips / ctx.current_atr_pips) if ctx.current_atr_pips else None

    leg_id = _rejected_leg_id(side, facts, result.tier_label or "NO_TIER_ACTIVATED")

    record = {
        "queued_at": now_utc.isoformat(),
        "leg_id": leg_id,
        "side": side,
        "entry": prospective_entry,
        "sl_raw": prospective_sl,   # the REAL, buffered, structurally-wide stop — not a generic fallback
        "atr_pips": ctx.current_atr_pips,
        "note": "Live risk gate suppressed this trade — " + (risk_gate_reason or "risk too wide"),
        "tags": {
            "_blocked_by_risk_gate_width": True,
            "tier_label": result.tier_label,
            "risk_pips": round(risk_pips, 1),
            "atr_ratio": round(atr_ratio, 2) if atr_ratio is not None else None,
        },
    }
    _write_rejected_live_record(record)


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
    "`/taken` — confirm you took a fired signal\n"
    "`/skip` — pass on a fired signal (releases the leg)\n"
    "`/win` `/loss` — log the current signal's result\n"
    "`/confirm` — confirm a pending signal\n"
    "`/undo` — undo the last logged result\n"
    "`/trade` — show current open trade\n\n"
    "*Stats & journal*\n"
    "`/stats` — summary stats\n"
    "`/journal` — trade journal\n"
    "`/last` — last signal + diagnostics\n"
    "`/bias` `/biasab` — macro bias / bias A-B comparison\n"
    "`/thesis` — Market Thesis Engine: current-scan narrative\n\n"
    "*Bank & accounts*\n"
    "`/bank` — bank report (home)\n"
    "`/bank performance` — aggregate across all accounts\n"
    "`/bank <n>` — drill into one account\n"
    "`/accounts` — list your account profiles\n"
    "`/use <name>` — switch active account\n"
    "`/setaccount <name> <balance> [risk%]` — add/update an account\n"
    "`/delaccount <name>` — remove an account\n\n"
    "*Shadow research pipeline*\n"
    "`/shadow` — full experiment summary\n"
    "`/shadow exp1`..`/shadow exp8` — one experiment (exp4 = Policy Lab)\n"
    "`/shadow rejected` — rejected-live detail\n"
    "`/shadow recent` — last 10 resolved shadow trades\n"
    "`/shadow blocked tier1|tier2|tier3` — tier block analysis\n"
    "`/shadow ic tier1|tier2|tier3` — IC report\n"
    "`/shadow conviction tier1|tier2|tier3` — conviction audit (score buckets, "
    "gate value, per-factor attribution)\n"
    "`/shadow policy tier1|tier2|tier3` — Policy Lab (conviction vs risk-at-hand, "
    "sweep on/off, by risk band)\n"
    "`/shadow regime tier1|tier2|tier3` — win%/avgR/PF split by market phase\n"
    "`/shadow investigate <trade_id>` — failure investigation\n"
    "`/leaderboard` — experiments ranked by average R\n\n"
    "*Diagnostics*\n"
    "`/atrbands` — ATR band suitability (win-rate by band)\n"
    "`/atrhist` — scan-level ATR histogram (pure counts, no outcomes)\n"
    "`/markov` — Markov transition / regime state\n"
    "`/legobs` — per-leg order block tracking (`summary`, `scenario`)\n"
    "`/calibration` — calibration checks\n"
    "`/cases` — case studies\n\n"
    "*World State (Phase 1 — plumbing/debug view)*\n"
    "`/world` — unified read of what Live/MIN/Shadow/Bank currently know, "
    "plus source freshness\n\n"
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

    Runs at the START of _scan_once(), before this pass's own data
    fetch/facts/thesis/intent computation — deliberate, so a later
    fetch failure can never silently drop a /win, /undo, /trade, etc.
    (per the comment at the call site).

    /thesis and /marketintent are pure reads, not mutations, and used
    to be answered right here too — which meant they only ever saw
    LAST cycle's already-committed state.json, one full pass stale at
    best, and "No scan data yet" whenever the previous run crashed
    before reaching git-commit (as the _fvg_cache bug was doing).
    Per chat, 2026-08-17: those two are now returned as
    `deferred_report_cmds` instead of being answered immediately.
    The caller answers them later in _scan_once(), right after THIS
    pass's own market_intent_*/market_thesis_* state is finalized —
    see the call site there for why that answering point is placed
    where it is (before evaluate_rule_of_law(), so a downstream crash
    can't cause a deferred command to be dropped without a reply).
    """
    deferred_report_cmds = []

    if not RESULT_TRACKING_ENABLED:
        return stats, deferred_report_cmds

    url    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = stats.get("last_update_id", 0) + 1

    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 2}, timeout=5).json()
    except Exception:
        return stats, deferred_report_cmds

    if not resp.get("ok") or not resp.get("result"):
        return stats, deferred_report_cmds

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

            elif cmd in ("/taken", "taken"):
                pending = stats.get("pending_confirmation")
                if not pending:
                    send_telegram("_Nothing pending — no signal awaiting confirmation._")
                    print("  [TAKEN] No pending_confirmation to promote.")
                else:
                    stats["active_trade"] = {k: v for k, v in pending.items()
                                              if k not in ("risk_band", "risk_reasons",
                                                            "awaiting_confirmation_since")}
                    stats.pop("pending_confirmation", None)
                    send_telegram(
                        f"✅ *Confirmed taken* — {pending['tier_label']} "
                        f"`{pending['direction']}` @ `{pending['entry']:.5f}`. Now tracking."
                    )
                    print(f"  [TAKEN] Confirmed — {pending['tier_label']} {pending['direction']} "
                          f"@ {pending['entry']:.5f}.")

            elif cmd in ("/skip", "skip"):
                pending = stats.get("pending_confirmation")
                if not pending:
                    send_telegram("_Nothing pending — no signal awaiting confirmation._")
                    print("  [SKIP] No pending_confirmation to discard.")
                else:
                    stats.pop("pending_confirmation", None)
                    stats.setdefault("skipped_signals", [])
                    stats["skipped_signals"].append({
                        "tier_label":  pending["tier_label"],
                        "direction":   pending["direction"],
                        "entry":       pending["entry"],
                        "risk_band":   pending.get("risk_band"),
                        "fired_at":    pending.get("opened_at"),
                        "skipped_at":  now_str,
                    })
                    stats["skipped_signals"] = stats["skipped_signals"][-JOURNAL_MAX_ENTRIES:]
                    if state is not None:
                        release_leg(state, "signal skipped via /skip — not taken")
                    send_telegram(
                        f"⏭️ *Skipped* — {pending['tier_label']} `{pending['direction']}` "
                        f"@ `{pending['entry']:.5f}`. Leg released, scanner free to look "
                        "for the next setup."
                    )
                    print(f"  [SKIP] Discarded — {pending['tier_label']} {pending['direction']} "
                          f"@ {pending['entry']:.5f}.")

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

                elif arg.startswith("conviction") or arg.startswith("convaudit"):
                    prefix = "conviction" if arg.startswith("conviction") else "convaudit"
                    tier_arg = arg[len(prefix):].strip()
                    tier_map = {"tier1": "TIER_1_POI", "1": "TIER_1_POI",
                                "tier2": "TIER_2_FIB", "2": "TIER_2_FIB",
                                "tier3": "TIER_3_STRUCTURE", "3": "TIER_3_STRUCTURE"}
                    tier_label = tier_map.get(tier_arg)
                    if not tier_label:
                        send_telegram("🔎 _Usage: `/shadow conviction tier1`, `tier2`, or `tier3`._")
                    else:
                        try:
                            send_telegram(format_conviction_audit(tier_label))
                        except Exception as e:
                            send_telegram(f"🔎 _Conviction audit error: {e}_")

                elif arg.startswith("policy") or arg.startswith("policylab"):
                    prefix = "policylab" if arg.startswith("policylab") else "policy"
                    tier_arg = arg[len(prefix):].strip()
                    tier_map = {"tier1": "TIER_1_POI", "1": "TIER_1_POI",
                                "tier2": "TIER_2_FIB", "2": "TIER_2_FIB",
                                "tier3": "TIER_3_STRUCTURE", "3": "TIER_3_STRUCTURE"}
                    tier_label = tier_map.get(tier_arg)
                    if not tier_label:
                        send_telegram("🧪 _Usage: `/shadow policy tier1`, `tier2`, or `tier3`._")
                    else:
                        try:
                            send_telegram(format_policy_lab_report(tier_label))
                        except Exception as e:
                            send_telegram(f"🧪 _Policy Lab report error: {e}_")

                elif arg.startswith("regime"):
                    tier_arg = arg[len("regime"):].strip()
                    tier_map = {"tier1": "TIER_1_POI", "1": "TIER_1_POI",
                                "tier2": "TIER_2_FIB", "2": "TIER_2_FIB",
                                "tier3": "TIER_3_STRUCTURE", "3": "TIER_3_STRUCTURE"}
                    tier_label = tier_map.get(tier_arg)
                    if not tier_label:
                        send_telegram("🌦️ _Usage: `/shadow regime tier1`, `tier2`, or `tier3`._")
                    else:
                        try:
                            send_telegram(format_regime_performance(tier_label))
                        except Exception as e:
                            send_telegram(f"🌦️ _Regime performance error: {e}_")

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
                        "Try: `/shadow`, `/shadow exp1`, `exp2`, `exp3`, "
                        "`exp4`..`exp8`, "
                        "`/shadow rejected`, `/shadow blocked tier1|tier2|tier3`, "
                        "`/shadow ic tier1|tier2|tier3`, "
                        "`/shadow conviction tier1|tier2|tier3`, "
                        "`/shadow policy tier1|tier2|tier3`, "
                        "`/shadow regime tier1|tier2|tier3`, "
                        "`/shadow recent`, or `/leaderboard`."
                    )

            elif cmd in ("/leaderboard", "leaderboard"):
                board = format_shadow_leaderboard(load_shadow_stats())
                send_telegram(board or "🏆 _Nothing logged yet to rank._")

            elif cmd in ("/atrbands", "atrbands", "/atr", "atr"):
                table = format_atr_suitability_table()
                send_telegram(table or "📊 _No EXP7_TIER_ATR trades resolved yet — check back after a few hundred scans._")

            elif cmd in ("/atrhist", "atrhist", "/atrhistogram"):
                send_telegram(format_atr_histogram(stats))

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
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         