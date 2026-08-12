"""
min_scanner.py
===============
MARKET INTELLIGENCE NETWORK — runs as its OWN process, own cron/
cron-job.org schedule, and never calls Twelve Data. It reads:
  - candle_cache.json      (written by scanner_live.py each live scan)
  - rejected_live_queue.jsonl  (raw rejection facts queued by live)
  - state.json              (read-only snapshot — MIN never writes it)
  - markov_transitions.json (read-only, for the /markov report — this
                              file is scanner_live.py's, not MIN's; see
                              run_min_pass()'s own comment on why Markov
                              recording deliberately stays live-only)

and OWNS (sole writer of):
  - shadow_state.json, shadow_stats.json, shadow_trade_log.jsonl
  - leg_obs_state.json, leg_obs_log.jsonl, failure_case_log.jsonl

NOTE: bias_ab_log.json and markov_transitions.json are scanner_live.py's,
not MIN's, despite living in the same directory — see the ownership
notes above and the "MARKET EVOLUTION (Markov)" comment inside
run_min_pass() below for why (double-counting risk from running a
scan-to-scan transition model on two independent schedules, not just a
file-write race).

TRIGGER: this scanner is invoked by cron-job.org hitting the GitHub
Actions workflow_dispatch REST endpoint, on a schedule offset ~2-5
minutes from scanner_live.py's own cron-job.org trigger (see
workflows/min-scan.yml) — NOT GitHub's native `schedule:` cron.

If candle_cache.json is missing or stale (weekend, or live scanner
hasn't run recently), the live-data-dependent experiments (EXP1-7,
Forward Observation) are simply skipped for that pass — everything
log-only (Evidence Engine, Failure Investigation Bureau, Advisory
Council, Markov/IC reports, all /shadow dashboard commands) still
works fine, since those only ever read shadow_trade_log.jsonl and the
state files already on disk.

FILE ORDERING RULE (read this before adding a new function anywhere):
This file ends with `if __name__ == "__main__": run_min_pass()`, which
executes immediately when the script runs standalone (exactly how the
GitHub Action invokes it). Since Python runs top-to-bottom, ANY def
placed after that guard does not exist yet when it fires. This already
happened once — Experiment 8 (CISD) was written below the guard and
silently NameError'd on every single pass until caught. New experiments,
helpers, or dispatch entries always go ABOVE `def run_min_pass():`. See
the banner comment directly above the `if __name__` guard for the full
story.
"""

import os
import json
import math
import time
import uuid
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

from scanner_common import (
    BASE_DIR, PIP_SIZE,
    atomic_write_json, send_telegram, atr, _json_default,
    load_state, load_markov_data, apply_state_updates,
    HTF_BIAS_MIN_BARS,
    SHADOW_STATE_FILE, SHADOW_STATS_FILE, SHADOW_TRADE_LOG_FILE,
    SHADOW_TRADE_LOG_WRITE_RETRIES, SHADOW_TRADE_LOG_WRITE_RETRY_DELAY_SEC,
    SHADOW_METHODOLOGY_VERSION, SHADOW_MAX_PENDING_PER_EXPERIMENT,
    SHADOW_MAX_PENDING_BARS, SHADOW_SEEN_LEG_CAP,
    LEG_OBS_STATE_FILE, LEG_OBS_LOG_FILE,
    FAILURE_CASE_LOG_FILE,
    ATR_MIN_PIPS, ATR_WARN_PIPS, ATR_SUITABILITY_BAND_WIDTH_PIPS,
    BAYES_CI_LEVEL, BAYES_PRIOR_ALPHA, BAYES_PRIOR_BETA,
    BOS_15M_BREAK_BUFFER_ATR_MULT,
    CALIBRATION_BUCKETS, CALIBRATION_MIN_N,
    CASE_EXCLUDED_TAGS, CASE_MIN_GROUP_N,
    CONVICTION_MANAGEMENT_BANDS, CONVICTION_MIN_BY_TIER, classify_conviction,
    EVIDENCE_MATCH_THRESHOLD, EVIDENCE_MIN_N, EVIDENCE_STRENGTH_BANDS,
    EXPECTED_NEXT_EVENT_MAP,
    FRACTAL_WING, FVG_MAX_AGE_CANDLES, FVG_MIN_SIZE_ATR_MULT,
    LEG_OBS_CLOSED_MAX, LEG_OBS_METHODOLOGY_VERSION, LEG_OBS_TIMELINE_MAX,
    MARKOV_PRIOR_ALPHA, MARKOV_STATES,
    MAX_RISK_ATR_MULT, MAX_RISK_PIPS, RR_RATIO, SESSION_WINDOWS_UTC,
    SL_MIN_PIPS, SWING_LOOKBACK_15,
    TIER_EVIDENCE_KEYS, TIER_NUMBER, TIER_PRIORITY, ZONE_TOLERANCE_PIPS,
)
from scanner_observation import (
    compute_macro_bias, evaluate_market_context, MarketFacts,
    compute_leg_id, _same_leg, bias_to_side, TIER_REGISTRY,
    detect_bos_impulse, detect_significant_fvg,
    sl_multiplier_for_context,
    compute_measured_move_extension,
    get_leg_owner,
    compute_market_state, classify_regime,
)

CANDLE_CACHE_FILE = os.path.join(BASE_DIR, "candle_cache.json")
REJECTED_LIVE_QUEUE_FILE = os.path.join(BASE_DIR, "rejected_live_queue.jsonl")


# =========================================================================
# INTELLIGENCE DATABASE — persistence. MIN is the sole writer of these
# three files now (shadow_state.json, shadow_stats.json,
# shadow_trade_log.jsonl) — scanner_live.py only ever reads/queues.
# =========================================================================
def load_shadow_state():
    try:
        with open(SHADOW_STATE_FILE, "r") as f:
            state = json.load(f)
        if state.get("methodology_version") != SHADOW_METHODOLOGY_VERSION:
            return {"pending": [], "last_leg": {}, "seen_legs": {}, "drained_leg_ids": [],
                    "methodology_version": SHADOW_METHODOLOGY_VERSION}
        state.setdefault("drained_leg_ids", [])  # backfill for state saved before this field existed
        # backfill for state saved before the bounded seen-set dedup fix (audit
        # fix — see SHADOW_SEEN_LEG_CAP). "last_leg" is left in place, unread by
        # new code, so nothing already on disk gets orphaned by the switch.
        state.setdefault("seen_legs", {})
        return state
    except Exception:
        return {"pending": [], "last_leg": {}, "seen_legs": {}, "drained_leg_ids": [],
                "methodology_version": SHADOW_METHODOLOGY_VERSION}


def save_shadow_state(shadow_state):
    try:
        atomic_write_json(SHADOW_STATE_FILE, shadow_state)
    except Exception as e:
        print("[SHADOW STATE SAVE ERROR] " + str(e))


_SHADOW_STATS_EXPERIMENT_KEYS = [
    "EXP1_STRUCTURE", "EXP2_FIB", "EXP3_POI", "EXP4_POLICY_LAB",
    "EXP5_ABLATION", "EXP6_ALT_BIAS", "EXP7_TIER_ATR", "EXP8_CISD",
    "EXPE_REJECTED_LIVE",
]
# EXP4_LIQUIDITY REMOVED (per chat, 2026-08-11): 0 fires across 1,821
# resolved shadow trades. Root-caused to a structural window collision,
# not genuine rarity — detect_liquidity_sweep() requires a 15M candle
# close AFTER the 5M sweep candle to confirm (0-15 min delay), while
# experiment_4_liquidity only ever looked at df_5m.tail(SWEEP_LOOKBACK_
# CANDLES) (a 15-min window) for the sweep to still be "recent." Those
# two windows are the same order of magnitude and directly opposed: in
# the worst case the sweep candle ages out of the lookback at almost
# exactly the moment 15M confirmation becomes available. Never
# confirmed against live data before removal — if this experiment is
# revisited, redesign the confirmation check to key off the swing LEVEL
# (has this level been swept-and-reclaimed since it became the current
# macro extreme, with no candle count limit) rather than a fixed
# candle-count lookback. Old EXP4_LIQUIDITY entries left as-is in
# shadow_stats.json / shadow_trade_log.jsonl (harmless, all-zero,
# untouched going forward) rather than scrubbed.
#
# EXP4 SLOT REUSED as EXP4_POLICY_LAB (per chat, 2026-08-11): the old
# key logged zero trades ever, so reusing the slot orphans nothing.
# Answers two questions the conviction-retirement change (see
# scanner_common.py CONVICTION_MIN_BY_TIER / classify_tierN_risk)
# left open with no data behind it:
#   1. Was retiring the conviction gate (score>=minimum FIRE/REJECT,
#      banded target_r/size_mult) actually better than today's
#      risk-at-hand-fires-everything policy, in realised R?
#   2. Within the conviction-gated view specifically, does the
#      liquidity-sweep term in the score (Tier 2 sweep_bonus=+20,
#      Tier 3 sweep_bonus=+30/no_sweep_penalty=-10 — by far its
#      heaviest-weighted factor) actually separate winners from
#      losers, or is it dead weight in the gate?
# Three independently-logged policy variants per tier (Tier 1 has no
# sweep term, so it only gets two — see experiment_4_policy_lab):
#   CONVICTION_SWEEP_ON  — old score formula unchanged, gated at
#                          CONVICTION_MIN_BY_TIER, banded target_r.
#   CONVICTION_SWEEP_OFF — same gate, but the sweep bonus/penalty is
#                          stripped from the score BEFORE the minimum
#                          check — isolates the sweep term's effect on
#                          which trades clear the gate at all, holding
#                          every other scoring input constant.
#   RISK_AT_HAND         — today's live policy: fires unconditionally
#                          once activated, flat RR_RATIO target, flat
#                          1.0 size. The control group.
# A variant only gets logged for a given leg if THAT variant's own
# fire condition is met — a CONVICTION_SWEEP_ON REJECT logs nothing
# for that arm this leg, same as the old live gate simply not firing.
# This is deliberate: the point is to reconstruct what each policy's
# OWN realised R distribution would have looked like, rejections and
# all, not to force three trades out of every leg.


def _empty_experiment_stat():
    return {"logged": 0, "resolved": 0, "wins": 0, "losses": 0,
            "timed_out": 0, "sum_r": 0.0, "hit_1r": 0, "hit_2r": 0, "hit_3r": 0}


def load_shadow_stats():
    corrupted = False
    try:
        with open(SHADOW_STATS_FILE, "r") as f:
            stats = json.load(f)
    except FileNotFoundError:
        # Legitimate first-run condition, nothing to warn about.
        stats = {}
    except Exception as e:
        # File exists but failed to parse -- this is data loss, not a normal
        # first-run condition. Previously this silently reset every
        # experiment's logged/resolved/wins/losses to zero with no warning,
        # which is how "resolved" ended up exceeding "logged": shadow_state.json
        # (holding in-flight "pending" trades) is a separate file and wasn't
        # wiped, so old trades kept resolving into freshly-zeroed counters
        # while nothing re-incremented "logged" for them. Surface this loudly
        # instead of eating it silently.
        msg = (f"[SHADOW STATS CORRUPTION] {SHADOW_STATS_FILE} failed to parse "
               f"({type(e).__name__}: {e}). Resetting per-experiment counters "
               f"to zero -- 'logged' will now undercount vs shadow_trade_log.jsonl "
               f"until manually reconciled with the repair script.")
        print(msg)
        try:
            send_telegram(
                "\u26a0\ufe0f shadow_stats.json failed to load ("
                + type(e).__name__ +
                "). Counters have been reset to zero. shadow_trade_log.jsonl "
                "is unaffected (append-only). Run repair_logged_counts.py "
                "against shadow_state.json to fix 'logged' before trusting "
                "/shadow output again."
            )
        except Exception:
            pass
        stats = {}
        corrupted = True

    if stats.get("_methodology_version") != SHADOW_METHODOLOGY_VERSION:
        if stats and not corrupted:
            # A genuine, intentional methodology bump -- not corruption.
            print(f"[SHADOW STATS] methodology_version changed "
                  f"({stats.get('_methodology_version')!r} -> "
                  f"{SHADOW_METHODOLOGY_VERSION!r}); resetting per-experiment "
                  f"counters intentionally.")
        stats = {"_methodology_version": SHADOW_METHODOLOGY_VERSION}

    for key in _SHADOW_STATS_EXPERIMENT_KEYS:
        exp = stats.setdefault(key, _empty_experiment_stat())
        # BUG FIX (found via /shadow exp3 going silent): setdefault() above
        # only backfills a per-experiment dict when the WHOLE key is
        # missing. If EXP3_POI (or any experiment) already exists on disk
        # because it has real trades logged, but was created before a
        # field (e.g. hit_1r/hit_2r/hit_3r) was added to
        # _empty_experiment_stat(), that field is just absent from the
        # loaded dict -- setdefault never looks inside it. Any formatter
        # doing direct s['hit_1r'] indexing then throws KeyError, and
        # since most /shadow commands have no local try/except, the
        # command fails completely silently on Telegram. Backfilling
        # every sub-key here, for every experiment, every load, fixes it
        # at the source instead of patching each formatter individually.
        for subkey, default in _empty_experiment_stat().items():
            exp.setdefault(subkey, default)
    return stats


def save_shadow_stats(shadow_stats):
    try:
        atomic_write_json(SHADOW_STATS_FILE, shadow_stats, indent=2)
    except Exception as e:
        print("[SHADOW STATS SAVE ERROR] " + str(e))


# ---- building + tracking setups --------------------------------------------
def build_shadow_setup(experiment, direction, entry, sl_raw, now_utc,
                        variant=None, tags=None, note="", atr_pips=None, tier_number=None,
                        target_r=3.0):
    """A hypothetical trade for research purposes only. Returns None if the
    risk is degenerate (entry == sl_raw).

    atr_pips: the 5M ATR (in pips) at the moment this setup was logged —
    the SAME metric ATR_MIN_PIPS gates live trading on. Carried through
    to resolution and permanently appended to SHADOW_TRADE_LOG_FILE so
    ATR-vs-outcome can be analyzed later (see compute_atr_suitability()).
    tier_number: 1/2/3 when this setup mirrors one of the live tiers
    (Experiment 7 — Tier ATR Mirror); None for every other experiment.
    """
    risk = (entry - sl_raw) if direction == "BUY" else (sl_raw - entry)
    if risk is None or risk <= 0:
        return None
    sign = 1 if direction == "BUY" else -1
    return {
        "id": uuid.uuid4().hex[:12],
        "methodology_version": SHADOW_METHODOLOGY_VERSION,
        "experiment": experiment,
        "variant": variant,
        "direction": direction,
        "entry": float(entry),
        "sl": float(sl_raw),
        "r1": float(entry + sign * 1 * risk),
        "r2": float(entry + sign * 2 * risk),
        "r3": float(entry + sign * 3 * risk),
        "target_r": float(target_r),
        "target": float(entry + sign * target_r * risk),
        "opened_at": now_utc.isoformat(),
        "bars_open": 0,
        "max_r_reached": 0,
        "tags": tags or {},
        "note": note,
        "atr_pips": round(atr_pips, 2) if atr_pips is not None else None,
        "tier_number": tier_number,
    }


def _dedup_key(experiment, variant):
    return experiment if variant is None else f"{experiment}::{variant}"


def log_shadow_setup(shadow_state, shadow_stats, setup, leg_id):
    """Applies the per-experiment dedup (one log per leg_id) and the
    per-experiment pending cap, then appends + bumps 'logged' count.

    Dedup (audit fix): bounded seen-set per (experiment, variant) key,
    not a single last-value comparison. The old "last_leg[key] == leg_id"
    check only remembered the ONE most recently logged leg per key, so a
    leg that recurred after being displaced by a different leg — leg A
    logged, leg B displaces it, leg A reforms — read as a fresh leg on
    its second appearance and got re-logged, inflating shadow_stats
    ['logged'] and duplicating shadow_state['pending']. Confirmed to
    have affected EXP2_FIB and EXP5_ABLATION; structurally present for
    every experiment routed through this function. SHADOW_SEEN_LEG_CAP
    bounds each key's seen-set so it can't grow unboundedly over the
    life of shadow_state.json."""
    if setup is None:
        return
    key = _dedup_key(setup["experiment"], setup["variant"])
    seen_legs = shadow_state.setdefault("seen_legs", {})
    seen_for_key = seen_legs.setdefault(key, [])
    if leg_id in seen_for_key:
        return  # already logged this exact leg for this experiment/variant

    pending_count = sum(1 for p in shadow_state["pending"] if p["experiment"] == setup["experiment"])
    if pending_count >= SHADOW_MAX_PENDING_PER_EXPERIMENT:
        return

    seen_for_key.append(leg_id)
    if len(seen_for_key) > SHADOW_SEEN_LEG_CAP:
        del seen_for_key[: len(seen_for_key) - SHADOW_SEEN_LEG_CAP]
    shadow_state["pending"].append(setup)
    shadow_stats[setup["experiment"]]["logged"] += 1


def _append_shadow_trade_log(setup, outcome, r_achieved, now_utc):
    """Permanently appends ONE resolved shadow trade to
    SHADOW_TRADE_LOG_FILE (append mode — this file is never truncated or
    rewritten, unlike shadow_state.json/shadow_stats.json). This is the
    raw per-trade record (tier, ATR, result) the ATR-suitability analysis
    is built on.

    Returns True if the line is confirmed written to disk, False if every
    retry failed (per chat, "safety against duplication"): the caller
    (resolve_pending) MUST NOT mark this trade as logged or count it in
    shadow_stats unless this returns True — the old version swallowed the
    write exception internally and returned nothing, so the caller always
    proceeded as if it had succeeded. On a genuine disk failure that
    silently and permanently lost the trade: stats were incremented,
    logged_trade_ids marked it as done (so the crash/restart dedup guard
    would then skip it forever), but the line never actually made it to
    disk. Returning a real success/failure signal is what makes fixing
    that possible."""
    record = {
        # FIX (per chat): build_shadow_setup() has generated a unique
        # "id" per trade all along, but this function was building the
        # permanent record from scratch and never carrying it over — so
        # the id existed only while the trade sat in the transient
        # pending queue, then vanished the moment it resolved. That's the
        # exact gap that made "pinpoint which specific historical trade
        # this is" impossible. Trades resolved BEFORE this fix simply
        # won't have a trade_id; every reader must treat it as optional.
        "trade_id":     setup.get("id"),
        "methodology_version": setup.get("methodology_version"),
        "resolved_at":  now_utc.isoformat(),
        "experiment":   setup["experiment"],
        "variant":      setup["variant"],
        "tier_number":  setup["tier_number"],
        "atr_pips":     setup["atr_pips"],
        "target_r":     setup.get("target_r", 3.0),
        "direction":    setup["direction"],
        "opened_at":    setup["opened_at"],
        "bars_open":    setup["bars_open"],
        "resolved_candle_time": setup.get("resolved_candle_time"),
        "outcome":      outcome,          # "WIN" / "LOSS" / "TIMEOUT_WIN" / "TIMEOUT_LOSS"
        "r_achieved":   round(r_achieved, 2),
        # AUDIT NOTE (drill-down rework): tags used to be dropped at
        # resolution time, which meant the /shadow drill-down commands
        # (session split, rejection-reason breakdown, tier-activation
        # breakdown for EXPE_REJECTED_LIVE) had nothing to read — the
        # only place `tags` ever lived was the transient pending setup.
        # Persisting them here is what makes those commands possible.
        # Trades resolved BEFORE this change will simply have tags=None;
        # every drill-down below handles that gracefully.
        "tags":         setup.get("tags") or {},
    }
    line = json.dumps(record, default=_json_default) + "\n"
    last_error = None
    for attempt in range(1, SHADOW_TRADE_LOG_WRITE_RETRIES + 1):
        try:
            with open(SHADOW_TRADE_LOG_FILE, "a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as e:
            last_error = e
            if attempt < SHADOW_TRADE_LOG_WRITE_RETRIES:
                time.sleep(SHADOW_TRADE_LOG_WRITE_RETRY_DELAY_SEC)
    print(f"[SHADOW TRADE LOG ERROR] gave up after {SHADOW_TRADE_LOG_WRITE_RETRIES} "
          f"attempts: {last_error}")
    return False

# =========================================================================
# EXPERIMENTAL LAB — "what could we have learned from this setup?" Reads
# facts/ctx rebuilt from the cached candles (see main loop at bottom of
# this file), never from a live fetch.
# =========================================================================
def update_pending_shadow_setups(shadow_state, shadow_stats, df_5m, now_utc):
    """Processes every unseen closed 5M candle and records realized outcomes."""
    last_processed = shadow_state.get("last_processed_candle")
    if last_processed:
        try:
            candles = df_5m.loc[df_5m.index > pd.Timestamp(last_processed)]
        except Exception:
            candles = df_5m.tail(1)
    else:
        candles = df_5m.tail(1)

    if candles.empty:
        return

    # DEDUP GUARD (crash/restart safety net): logged_trade_ids is a persisted,
    # bounded record of trade ids that have already been permanently written
    # to SHADOW_TRADE_LOG_FILE and folded into shadow_stats. If the process
    # dies between the _append_shadow_trade_log() call below and
    # save_shadow_state() actually reaching disk, a restart would reload the
    # stale "pending"/"last_processed_candle" and re-resolve + re-append the
    # exact same trades. This guard makes that re-processing a no-op instead
    # of a silent duplicate. It is a backstop, not the primary fix — the
    # primary fix is that save_shadow_state() is now called immediately after
    # this function returns (see scan-loop caller) instead of ~8 experiment
    # functions later, which shrinks the crash window to essentially zero.
    logged_ids = shadow_state.setdefault("logged_trade_ids", [])
    logged_id_set = set(logged_ids)

    pending = shadow_state.get("pending", [])
    for candle_time, candle in candles.iterrows():
        high = float(candle["High"])
        low = float(candle["Low"])
        close = float(candle["Close"])
        still_pending = []

        for setup in pending:
            setup["bars_open"] += 1
            direction = setup["direction"]
            exp_stats = shadow_stats.setdefault(setup["experiment"], _empty_experiment_stat())

            if direction == "BUY":
                hit_sl = low <= setup["sl"]
                hit_target = high >= setup.get("target", setup["r3"])
                for r_level, r_val in ((setup["r3"], 3), (setup["r2"], 2), (setup["r1"], 1)):
                    if high >= r_level:
                        setup["max_r_reached"] = max(setup["max_r_reached"], r_val)
                        break
            else:
                hit_sl = high >= setup["sl"]
                hit_target = low <= setup.get("target", setup["r3"])
                for r_level, r_val in ((setup["r3"], 3), (setup["r2"], 2), (setup["r1"], 1)):
                    if low <= r_level:
                        setup["max_r_reached"] = max(setup["max_r_reached"], r_val)
                        break

            timed_out = setup["bars_open"] >= SHADOW_MAX_PENDING_BARS
            outcome_label = None
            r_final = None

            if hit_sl:
                outcome_label = "LOSS"
                r_final = -1.0
            elif hit_target:
                outcome_label = "WIN"
                r_final = float(setup.get("target_r", 3.0))
            elif timed_out:
                risk = abs(setup["entry"] - setup["sl"])
                sign = 1 if direction == "BUY" else -1
                r_final = ((close - setup["entry"]) * sign / risk) if risk > 0 else -1.0
                r_final = max(-1.0, min(float(setup.get("target_r", 3.0)), float(r_final)))
                outcome_label = "TIMEOUT_WIN" if r_final > 0 else "TIMEOUT_LOSS"

            if outcome_label is None:
                still_pending.append(setup)
                continue

            _tid = setup.get("id")
            if _tid is not None and _tid in logged_id_set:
                # Already permanently logged in a prior cycle that crashed
                # before shadow_state was persisted. Drop it: do NOT
                # increment exp_stats again and do NOT re-append to the log.
                continue

            # Attempt the permanent write FIRST, before touching any stats
            # or the dedup guard (per chat, "safety against duplication" —
            # audit fix). The old order incremented exp_stats and marked
            # _tid as logged unconditionally, then attempted the append —
            # so a failed write (which used to fail silently) still got
            # counted AND got permanently blacklisted from ever being
            # retried, losing the record for good with no trace beyond a
            # console print nobody was watching. Now: only a CONFIRMED
            # write moves this trade out of "pending." A failed write —
            # even after retries — puts it straight back into
            # still_pending so the next scan tries again, and fires a
            # Telegram alert so a persistent failure doesn't go unnoticed.
            setup["resolved_candle_time"] = candle_time.isoformat()
            resolved_at = pd.Timestamp(candle_time).to_pydatetime()
            wrote_ok = _append_shadow_trade_log(setup, outcome_label, r_final, resolved_at)
            if not wrote_ok:
                still_pending.append(setup)
                try:
                    send_telegram(
                        "⚠️ *Shadow trade log write failed* — a resolved "
                        f"{setup.get('experiment')}/{setup.get('variant')} trade "
                        "could not be permanently saved after "
                        f"{SHADOW_TRADE_LOG_WRITE_RETRIES} attempts. It's been kept "
                        "pending and will retry next scan; check disk space/permissions "
                        "if this repeats."
                    )
                except Exception:
                    pass  # alerting is best-effort — must never block the resolution loop
                continue

            exp_stats["resolved"] += 1
            if timed_out and not hit_sl and not hit_target:
                exp_stats["timed_out"] += 1
            if r_final > 0:
                exp_stats["wins"] += 1
            else:
                exp_stats["losses"] += 1
            exp_stats["sum_r"] += r_final
            if setup["max_r_reached"] >= 1:
                exp_stats["hit_1r"] += 1
            if setup["max_r_reached"] >= 2:
                exp_stats["hit_2r"] += 1
            if setup["max_r_reached"] >= 3:
                exp_stats["hit_3r"] += 1

            if _tid is not None:
                logged_id_set.add(_tid)
                logged_ids.append(_tid)
            if r_final <= 0:
                try:
                    _case = open_failure_case(setup, outcome_label, r_final, resolved_at)
                    if _case is not None:
                        send_telegram(format_case_note(_case))
                except Exception as e:
                    print("[FAILURE BUREAU ERROR] " + str(e))

        pending = still_pending

    shadow_state["pending"] = pending
    shadow_state["last_processed_candle"] = candles.index[-1].isoformat()
    # Bound the guard list so shadow_state.json doesn't grow forever. 500 is
    # comfortably larger than the number of trades that could resolve within
    # one crash-loop window, so nothing legitimate falls off the back before
    # it's had a chance to protect against a duplicate re-log.
    shadow_state["logged_trade_ids"] = logged_ids[-500:]


# ---- Experiment 1: Structure Only ------------------------------------------

# ---- Experiment 1: Structure Only ------------------------------------------
def experiment_1_structure(facts, current_atr_pips, shadow_state, shadow_stats, now_utc):
    """Logs every clean CHoCH/BOS continuation aligned with 1H bias —
    answers: does structure alone have an edge, and how often does it
    reach 1R/2R/3R?"""
    if not facts.has_fresh_bos_aligned_with_bias():
        return
    bos = facts.bos_15m()
    side = bias_to_side(facts.macro_bias)
    leg_id = "{}|{:.5f}|{:.5f}".format(side, bos["impulse_start"], bos["impulse_end"])
    setup = build_shadow_setup(
        "EXP1_STRUCTURE", side, float(facts.last_candle_5m()["Close"]), bos["impulse_start"], now_utc,
        tags={
            # Boolean gate result (one-liner wrapper)
            "choch": facts.has_choch_15m(),
            # Raw continuous measurements — pulled here so a Step-1 rejection
            # in a higher tier still has structural magnitudes logged.
            "choch_magnitude_pips": facts.choch_magnitude(),
            "fib_distance_pips": facts.fib_distance_pips(),
            "atr_ratio_5m_vs_15m": facts.atr_ratio_5m_vs_15m(),
        },
        note="clean CHoCH/BOS continuation aligned with 1H bias",
        atr_pips=current_atr_pips,
    )
    log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)


# ---- Experiment 2: Fib Only — helpers (pure, no side effects) --------------

# Individual retrace levels added in Group B / Group C.
# Group A still uses adaptive + fixed_382 + fixed_50 (unchanged).
_EXP2_EXTRA_LEVELS = [          # levels not covered by the existing Group A variants
    ("lvl_618", 0.618),
    ("lvl_705", 0.705),
    ("lvl_786", 0.786),
]
_EXP2_ALL_LEVELS = [            # full sweep used by Group C
    ("lvl_382", 0.382),
    ("lvl_50",  0.500),
    ("lvl_618", 0.618),
    ("lvl_705", 0.705),
    ("lvl_786", 0.786),
]


def _exp2_fib_price(swing_high, swing_low, bias, retrace):
    """Exact price at `retrace` fraction from the impulse extreme.
    BULLISH: retraces DOWN from swing_high.
    BEARISH: retraces UP   from swing_low.
    Returns None if the swing is degenerate (zero or inverted range)."""
    rng = swing_high - swing_low
    if rng <= 0:
        return None
    if bias == "BULLISH":
        return swing_high - retrace * rng
    return swing_low + retrace * rng


def _exp2_price_touches(df_5m, level):
    """Did the last two 5M candles reach `level` within ZONE_TOLERANCE_PIPS?
    Mirrors the same two-candle window used by MarketFacts.price_in_zone()
    so Group B/C trigger on exactly the same condition as Group A."""
    tol = ZONE_TOLERANCE_PIPS * PIP_SIZE
    c  = df_5m.iloc[-1]
    cp = df_5m.iloc[-2]
    lo = min(c["Low"],  cp["Low"])
    hi = max(c["High"], cp["High"])
    return lo <= level + tol and hi >= level - tol


def _exp2_bos_5m(df_5m):
    """Current dominant 5M BOS leg → (direction, swing_high, swing_low) or None.
    Uses the same fractal wing and tail window as facts.bos_15m() uses on 15M
    (FRACTAL_WING=2, 60-bar tail ≈ 5 hours of 5M data).  Pure — no state."""
    bos = detect_bos_impulse(df_5m.tail(60), wing=FRACTAL_WING)
    if bos is None:
        return None
    d = bos["direction"]
    # impulse_start is the ORIGIN (low for BULLISH, high for BEARISH)
    # impulse_end   is the EXTREME (high for BULLISH, low for BEARISH)
    if d == "BULLISH":
        sh, sl = bos["impulse_end"], bos["impulse_start"]
    else:
        sh, sl = bos["impulse_start"], bos["impulse_end"]
    if sh <= sl:
        return None
    return d, sh, sl


def _exp2_bos_5m_latest_swing(df_5m):
    """Same current dominant 5M BOS leg as _exp2_bos_5m, but pairs the
    extreme with latest_swing_origin instead of impulse_start (per chat —
    "in an uptrend, the latest low, not the extreme/original low").
    detect_bos_impulse() doesn't move impulse_start on a same-direction
    continuation break by design (the leg's founding origin shouldn't
    change), so it stays anchored on the leg's very first swing even
    after several continuations. latest_swing_origin is the swing point
    behind the MOST RECENT break instead — equal to impulse_start when
    the leg hasn't continued yet (break_count == 1), a genuinely
    different, more recent price once it has.
    Returns (direction, swing_high, swing_low, break_count) or None."""
    bos = detect_bos_impulse(df_5m.tail(60), wing=FRACTAL_WING)
    if bos is None:
        return None
    d = bos["direction"]
    if d == "BULLISH":
        sh, sl = bos["impulse_end"], bos["latest_swing_origin"]
    else:
        sh, sl = bos["latest_swing_origin"], bos["impulse_end"]
    if sh <= sl:
        return None
    return d, sh, sl, bos["break_count"]


def _exp2_15m_direction(df_15m):
    """Current 15M BOS direction, or None if no valid leg.
    Reuses the same constants as facts.bos_15m() — guaranteed identical
    detection logic so results are comparable across experiments."""
    bos = detect_bos_impulse(
        df_15m.tail(SWING_LOOKBACK_15),
        wing=FRACTAL_WING,
        break_buffer_atr_mult=BOS_15M_BREAK_BUFFER_ATR_MULT,
    )
    return bos["direction"] if bos is not None else None


def _exp2_log_ext_extra_levels(facts, current_atr_pips, shadow_state, shadow_stats, now_utc):
    """GROUP B — External (1H macro) swing, 1H authority, levels 61.8 / 70.5 / 78.6.

    Completes the individual-level sweep that Group A started (38.2 and 50).
    Everything else — swing source, direction authority, dedup key format —
    mirrors Group A so the three groups are directly comparable in the log."""
    side     = bias_to_side(facts.macro_bias)
    leg_base = "{}|{:.5f}|{:.5f}".format(side, facts.swing_high, facts.swing_low)
    close    = float(facts.last_candle_5m()["Close"])
    rc       = facts.rejection_candle()

    for lvl_name, retrace in _EXP2_EXTRA_LEVELS:
        level = _exp2_fib_price(facts.swing_high, facts.swing_low, facts.macro_bias, retrace)
        if level is None:
            continue
        if not _exp2_price_touches(facts.df_5m, level):
            continue
        variant_name = f"ext_1h_{lvl_name}"
        setup = build_shadow_setup(
            "EXP2_FIB", side, close, level, now_utc,
            variant=variant_name,
            tags={
                "rejection_candle_present": rc,
                "htf_authority":  "1h",
                "swing_source":   "external_macro_1h",
                "fib_retrace":    retrace,
            },
            note=f"EXP2 Group B: ext/1H swing, level {retrace * 100:.1f}%",
            atr_pips=current_atr_pips,
        )
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_base + "|" + variant_name)


def _exp2_log_authority_cross(facts, current_atr_pips, shadow_state, shadow_stats, now_utc):
    """GROUP C — Internal (5M BOS) swing × HTF authority (none/15m/1h) × all levels.

    Answers the two core research questions:
      1. Does a higher-timeframe authority filter improve outcomes when the fib
         is drawn from the fresh 5M impulse ('internal structure')?
      2. Does the external (1H macro) structure need to govern the internal
         (5M BOS) for the fib pocket to be statistically reliable?

    Three authority arms are logged independently on every qualifying scan:
      no_htf — direction from 5M BOS only; no HTF filter applied.
      15m    — 5M BOS direction must agree with 15M BOS direction.
      1h     — 5M BOS direction must agree with 1H macro bias.

    A setup is only logged when price is already touching the fib level, so
    the three arms share the same entry conditions — only the authority gate
    differs.  Each arm × level combination gets its own dedup slot so they
    resolve independently in the shadow pipeline."""
    bos_5m = _exp2_bos_5m(facts.df_5m)
    if bos_5m is None:
        return                          # no valid 5M impulse — nothing to fib off

    bos_dir, sh5, sl5 = bos_5m
    side_5m  = bias_to_side(bos_dir)
    close    = float(facts.last_candle_5m()["Close"])
    rc       = facts.rejection_candle()
    dir_15m  = _exp2_15m_direction(facts.df_15m)   # may be None
    dir_1h   = facts.macro_bias                    # "BULLISH" / "BEARISH" / "CONSOLIDATION"

    # Authority arms: (label, whether the direction gate passes)
    authority_arms = [
        ("no_htf", True),                           # no filter — always passes
        ("15m",    dir_15m == bos_dir),              # 15M must agree with 5M BOS
        ("1h",     dir_1h  == bos_dir),              # 1H macro bias must agree
    ]

    # Dedup base includes the 5M swing so a new impulse creates fresh slots
    leg_base = f"int5|{side_5m}|{sh5:.5f}|{sl5:.5f}"

    for auth_label, auth_passes in authority_arms:
        if not auth_passes:
            continue                    # HTF filter rejects this arm — skip cleanly
        for lvl_name, retrace in _EXP2_ALL_LEVELS:
            level = _exp2_fib_price(sh5, sl5, bos_dir, retrace)
            if level is None:
                continue
            if not _exp2_price_touches(facts.df_5m, level):
                continue
            variant_name = f"int_{auth_label}_{lvl_name}"
            setup = build_shadow_setup(
                "EXP2_FIB", side_5m, close, level, now_utc,
                variant=variant_name,
                tags={
                    "rejection_candle_present": rc,
                    "htf_authority":  auth_label,
                    "swing_source":   "internal_5m_bos",
                    "fib_retrace":    retrace,
                    "dir_5m":         bos_dir,
                    "dir_15m":        dir_15m,
                    "dir_1h":         dir_1h,
                },
                note=(
                    f"EXP2 Group C: int/5M swing, auth={auth_label}, "
                    f"level={retrace * 100:.1f}%"
                ),
                atr_pips=current_atr_pips,
            )
            log_shadow_setup(
                shadow_state, shadow_stats, setup,
                f"{leg_base}|{auth_label}|{lvl_name}",
            )


def _exp2_log_latest_swing(facts, current_atr_pips, shadow_state, shadow_stats, now_utc):
    """GROUP D — internal 5M swing, but anchored on the leg's latest
    (most recent) swing point instead of its founding origin (per chat —
    "in an uptrend, the latest low, not the extreme/original low").

    Mirrors Group C exactly (same authority arms, same five levels, same
    dedup shape) so the two are directly comparable in the log — the ONLY
    difference is _exp2_bos_5m_latest_swing() in place of _exp2_bos_5m().
    Only logged when break_count > 1 (the leg has actually continued at
    least once) — at break_count == 1 the latest swing point IS the
    founding origin, so Group D and Group C would log the identical
    price and just duplicate each other's sample for no research value."""
    bos_5m = _exp2_bos_5m_latest_swing(facts.df_5m)
    if bos_5m is None:
        return                          # no valid 5M impulse — nothing to fib off

    bos_dir, sh5, sl5, break_count = bos_5m
    if break_count <= 1:
        return                          # latest swing == founding origin — Group C already covers this
    side_5m  = bias_to_side(bos_dir)
    close    = float(facts.last_candle_5m()["Close"])
    rc       = facts.rejection_candle()
    dir_15m  = _exp2_15m_direction(facts.df_15m)
    dir_1h   = facts.macro_bias

    authority_arms = [
        ("no_htf", True),
        ("15m",    dir_15m == bos_dir),
        ("1h",     dir_1h  == bos_dir),
    ]

    # Dedup base includes the swing itself, same as Group C, so a new
    # latest-swing price (leg continues further) creates a fresh slot
    # rather than colliding with the previous continuation's entry.
    leg_base = f"int5latest|{side_5m}|{sh5:.5f}|{sl5:.5f}"

    for auth_label, auth_passes in authority_arms:
        if not auth_passes:
            continue
        for lvl_name, retrace in _EXP2_ALL_LEVELS:
            level = _exp2_fib_price(sh5, sl5, bos_dir, retrace)
            if level is None:
                continue
            if not _exp2_price_touches(facts.df_5m, level):
                continue
            variant_name = f"intlatest_{auth_label}_{lvl_name}"
            setup = build_shadow_setup(
                "EXP2_FIB", side_5m, close, level, now_utc,
                variant=variant_name,
                tags={
                    "rejection_candle_present": rc,
                    "htf_authority":  auth_label,
                    "swing_source":   "internal_5m_latest_swing",
                    "fib_retrace":    retrace,
                    "dir_5m":         bos_dir,
                    "dir_15m":        dir_15m,
                    "dir_1h":         dir_1h,
                    "leg_break_count": break_count,
                },
                note=(
                    f"EXP2 Group D: int/5M LATEST swing (break #{break_count}), "
                    f"auth={auth_label}, level={retrace * 100:.1f}%"
                ),
                atr_pips=current_atr_pips,
            )
            log_shadow_setup(
                shadow_state, shadow_stats, setup,
                f"{leg_base}|{auth_label}|{lvl_name}",
            )


# ---- Experiment 2: Fib Only -------------------------------------------------
def experiment_2_fib(facts, current_atr_pips, shadow_state, shadow_stats, now_utc):
    """Logs quality HTF pullbacks across three independently-guarded groups.

    GROUP A (original, unchanged)
        Three variants of the 1H macro swing fib: adaptive / fixed 38.2 / fixed 50.
        Direction = 1H macro bias.  Rejection candle intentionally ignored.

    GROUP B (new — external swing, extended levels)
        Same 1H macro swing and authority as Group A, but individual levels
        61.8%, 70.5%, and 78.6% — completes the sweep Group A started.

    GROUP C (new — internal swing × HTF authority × all levels)
        Fib drawn from the FRESH 5M BOS impulse ('internal structure').
        Three authority arms logged in parallel:
          no_htf → no HTF filter (5M direction only)
          15m    → 15M BOS must agree
          1h     → 1H macro bias must agree
        Five levels each: 38.2%, 50%, 61.8%, 70.5%, 78.6%.

    GROUP D (new — internal swing, LATEST swing point, not founding origin)
        Same as Group C, except the near anchor is the leg's most recent
        swing point (detect_bos_impulse()'s latest_swing_origin), not its
        founding one — per chat, "in an uptrend, the latest low, not the
        extreme/original low." Only fires once a leg has actually
        continued (break_count > 1); before that, latest == founding and
        Group C already covers it. Same authority arms, same five levels,
        directly comparable to Group C in the log via swing_source.

    Each group is wrapped in its own try/except so a crash in one cannot
    silence another or affect the live bot (the whole function is already
    wrapped again in run_shadow_pipeline).  No state is shared between groups."""

    # ---- GROUP A: original 3 variants (preserved exactly) ------------------
    try:
        side   = bias_to_side(facts.macro_bias)
        leg_id = "{}|{:.5f}|{:.5f}".format(side, facts.swing_high, facts.swing_low)
        variants = [
            ("adaptive",  facts.fib_zone()),
            ("fixed_382", facts.fib_zone(near=0.382, far=0.382)),
            ("fixed_50",  facts.fib_zone(near=0.5,   far=0.5)),
        ]
        for variant_name, zone in variants:
            if not facts.price_in_zone(zone):
                continue
            setup = build_shadow_setup(
                "EXP2_FIB", side, float(facts.last_candle_5m()["Close"]), zone, now_utc,
                variant=variant_name,
                tags={"rejection_candle_present": facts.rejection_candle()},
                note=f"HTF fib pullback ({variant_name}), rejection candle not required",
                atr_pips=current_atr_pips,
            )
            log_shadow_setup(shadow_state, shadow_stats, setup, leg_id + "|" + variant_name)
    except Exception as e:
        print(f"[EXP2 GROUP A ERROR] {e}")

    # ---- GROUP B: external swing, 1H authority, extra levels ----------------
    try:
        _exp2_log_ext_extra_levels(facts, current_atr_pips, shadow_state, shadow_stats, now_utc)
    except Exception as e:
        print(f"[EXP2 GROUP B ERROR] {e}")

    # ---- GROUP C: internal 5M BOS swing × HTF authority × all levels --------
    try:
        _exp2_log_authority_cross(facts, current_atr_pips, shadow_state, shadow_stats, now_utc)
    except Exception as e:
        print(f"[EXP2 GROUP C ERROR] {e}")

    # ---- GROUP D: internal 5M swing, LATEST swing point (not origin) --------
    try:
        _exp2_log_latest_swing(facts, current_atr_pips, shadow_state, shadow_stats, now_utc)
    except Exception as e:
        print(f"[EXP2 GROUP D ERROR] {e}")


# ---- Experiment 3: POI Only (Order Block AND quality-filtered FVG) --------
def experiment_3_poi(facts, atr_15m_series, current_atr_pips, shadow_state, shadow_stats, now_utc):
    """Logs every Order Block reaction AND every quality FVG reaction,
    tagged separately (variant="order_block" / variant="fvg") — answers:
    are order blocks carrying the strategy, and which POI type performs
    best? Rejection candle is NOT required here (unlike live Tier 1) —
    tagged instead, so it can be sliced afterward."""
    side = bias_to_side(facts.macro_bias)

    ob = facts.order_block()
    if ob is not None and facts.price_in_order_block():
        leg_id = "ob|{}|{:.5f}|{:.5f}".format(side, ob["high"], ob["low"])
        setup = build_shadow_setup(
            "EXP3_POI", side, float(facts.last_candle_5m()["Close"]),
            ob["low"] if side == "BUY" else ob["high"], now_utc,
            variant="order_block",
            tags={
                "rejection_candle_present": facts.rejection_candle(),
                "choch": facts.has_choch_15m(),
                # Raw measurements: allow slicing by OB penetration depth
                # and structural magnitude even when no tier activated.
                "ob_penetration_pct": facts.ob_penetration_pct(),
                "choch_magnitude_pips": facts.choch_magnitude(),
                "fib_distance_pips": facts.fib_distance_pips(),
            },
            note="Order block reaction",
            atr_pips=current_atr_pips,
        )
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)

    quality_gaps = detect_significant_fvg(facts.df_15m, facts.macro_bias, atr_15m_series)
    if quality_gaps:
        gap = quality_gaps[-1]  # nearest/most-recent quality gap
        c_last = facts.last_candle_5m()
        tol = ZONE_TOLERANCE_PIPS * PIP_SIZE
        touching = c_last["Low"] <= gap["high"] + tol and c_last["High"] >= gap["low"] - tol
        if touching:
            leg_id = "fvg|{}|{}".format(side, gap["time"])
            gap_level = gap["low"] if side == "BUY" else gap["high"]
            setup = build_shadow_setup(
                "EXP3_POI", side, float(c_last["Close"]), gap_level, now_utc,
                variant="fvg",
                tags={"size_atr_ratio": round(gap["size_atr_ratio"], 2),
                      "age_candles": gap["age_candles"],
                      "rejection_candle_present": facts.rejection_candle()},
                note="Quality-filtered FVG reaction (size>={}x ATR, age<={} candles)".format(
                    FVG_MIN_SIZE_ATR_MULT, FVG_MAX_AGE_CANDLES),
                atr_pips=current_atr_pips,
            )
            log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)


# ---- Experiment 4: Policy Lab (conviction-gated vs risk-at-hand, with a
# sweep-term ablation) — replaces the old, permanently-dead Liquidity
# Sweep slot. See the comment above _SHADOW_STATS_EXPERIMENT_KEYS for
# the full writeup of both. ------------------------------------------------
def _score_without_sweep(score, breakdown):
    """Strips this tier's sweep-related score term(s) using the exact
    line items that tier's own formula already produced — sweep_bonus
    (Tier 2/3 when swept) and/or no_sweep_penalty (Tier 3 when not) —
    zero new weights invented, zero re-derivation of the formula.
    Returns `score` unchanged for a tier with no sweep term (Tier 1)."""
    adjustment = breakdown.get("sweep_bonus", 0) + breakdown.get("no_sweep_penalty", 0)
    return score - adjustment


def experiment_4_policy_lab(facts, ctx, state, now_utc, shadow_state, shadow_stats):
    """
    Three independently-logged policy variants per tier, peeked
    read-only exactly like Experiment 7 (state_updates discarded, never
    claims a leg, never touches live state):

      CONVICTION_SWEEP_ON  — the old score>=CONVICTION_MIN_BY_TIER gate,
                              score formula unmodified (sweep counted).
      CONVICTION_SWEEP_OFF — same gate, sweep_bonus/no_sweep_penalty
                              stripped from the score BEFORE the minimum
                              check. Skipped for Tier 1 (no sweep term
                              exists in its score at all). Isolates the
                              sweep term's effect on which trades clear
                              the gate, holding everything else fixed.
      RISK_AT_HAND          — today's live policy: fires unconditionally
                              once activated + risk-gate-passes, flat
                              RR_RATIO target. The control group.

    A variant only gets logged for a leg if THAT variant's own fire
    condition is met this scan — a CONVICTION_SWEEP_ON REJECT logs
    nothing for that arm, same as the old live gate simply not firing.
    The point is each policy's own realised-R distribution, rejections
    included, not a forced trade out of every leg regardless of policy.
    """
    leg_key = compute_leg_id(facts.macro_bias, facts.swing_high, facts.swing_low)
    entry = float(facts.last_candle_5m()["Close"])

    for tier_label, tier_fn in TIER_REGISTRY.items():
        peek = tier_fn(facts, ctx, state, now_utc)  # read-only — state_updates discarded
        if not peek.activated or peek.score is None or peek.sl_raw is None:
            continue

        tier_number = TIER_NUMBER.get(tier_label)
        side = peek.direction or bias_to_side(facts.macro_bias)
        sl_buffer = max(sl_multiplier_for_context(ctx) * ctx.current_atr, SL_MIN_PIPS * PIP_SIZE)
        sl_final = (peek.sl_raw - sl_buffer if side == "BUY" else peek.sl_raw + sl_buffer)
        risk = (entry - sl_final) if side == "BUY" else (sl_final - entry)
        # Same dual ATR/flat-pip risk ceiling every live and mirrored
        # trade goes through (apply_risk_gate_and_finalize) — a setup
        # that can't clear this isn't a real candidate for ANY policy.
        risk_gate_pass = (
            math.isfinite(risk) and risk > 0 and
            risk <= MAX_RISK_ATR_MULT * ctx.current_atr and
            risk <= MAX_RISK_PIPS * PIP_SIZE
        )
        if not risk_gate_pass:
            continue

        has_sweep_term = ("sweep_bonus" in peek.breakdown) or ("no_sweep_penalty" in peek.breakdown)
        # Computed once per leg/tier, shared across all three policy
        # variants below — it's the SAME structural setup in each arm,
        # only the fire/target decision differs. Tagging it here is what
        # lets format_policy_lab_report bucket "what would-have-been-
        # signal actually did" by risk_band, independent of policy.
        risk_band = peek.risk["band"] if peek.risk else None

        variants = [
            ("CONVICTION_SWEEP_ON", classify_conviction(tier_label, peek.score), peek.score),
        ]
        if has_sweep_term:
            score_no_sweep = _score_without_sweep(peek.score, peek.breakdown)
            variants.append(
                ("CONVICTION_SWEEP_OFF", classify_conviction(tier_label, score_no_sweep), score_no_sweep))
        variants.append(
            ("RISK_AT_HAND", {"decision": "FIRE", "target_r": RR_RATIO}, peek.score))

        for policy_name, decision, score_used in variants:
            if decision.get("decision") != "FIRE":
                continue  # this arm's own gate rejected — nothing logged for it this leg
            variant_key = f"{tier_label}::{policy_name}"
            target_r = decision.get("target_r") or RR_RATIO
            setup = build_shadow_setup(
                "EXP4_POLICY_LAB", side, entry, sl_final, now_utc,
                variant=variant_key,
                tags={
                    "tier_label": tier_label, "policy": policy_name,
                    "score_used": score_used, "raw_score": peek.score,
                    "had_sweep_term": has_sweep_term,
                    "swept": peek.breakdown.get("liquidity_sweep"),
                    "band_label": decision.get("band_label"),
                    "risk_band": risk_band,
                    **(peek.breakdown or {}),
                },
                note=f"{tier_label} {policy_name} — score {score_used}, target {target_r}R",
                atr_pips=ctx.current_atr_pips,
                tier_number=tier_number,
                target_r=target_r,
            )
            log_shadow_setup(shadow_state, shadow_stats, setup, f"{leg_key}|{variant_key}")

# ---- Experiment 5: Filter Ablation ------------------------------------------
def experiment_5_filter_ablation(facts, ctx, state, df_15m, shadow_state, shadow_stats, now_utc):
    """A Tier-3-shaped setup (CHoCH + aligned BOS) with exactly ONE filter
    stripped per variant, so any R-multiple difference vs Experiment 1 (or
    vs each other) is attributable to that ONE filter."""
    side = bias_to_side(facts.macro_bias)
    entry = float(facts.last_candle_5m()["Close"])
    bos_15m_only = detect_bos_impulse(df_15m.tail(SWING_LOOKBACK_15), wing=FRACTAL_WING,
                                       break_buffer_atr_mult=BOS_15M_BREAK_BUFFER_ATR_MULT)
    if bos_15m_only is not None:
        alt_side = bias_to_side(bos_15m_only["direction"])
        leg_id = "b15|{}|{:.5f}|{:.5f}".format(
            alt_side, bos_15m_only["impulse_start"], bos_15m_only["impulse_end"])
        setup = build_shadow_setup(
            "EXP5_ABLATION", alt_side, entry, bos_15m_only["impulse_start"], now_utc,
            variant="bias_15m",
            tags={"agrees_with_1h_bias": alt_side == side},
            note="15M structure used AS the bias authority (may diverge from 1H)",
            atr_pips=ctx.current_atr_pips,
        )
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)

    if not facts.has_fresh_bos_aligned_with_bias():
        return
    bos = facts.bos_15m()
    choch = facts.has_choch_15m()
    swept = facts.has_liquidity_sweep(bos["impulse_start"])
    bias_stale = state.get("macro_bias_stale", False)

    # variant: no_liquidity_sweep — only log the un-swept subset (the
    # swept subset is already fully covered by Experiment 1 / Tier 3).
    if choch and not swept:
        leg_id = "nls|{}|{:.5f}".format(side, bos["impulse_start"])
        setup = build_shadow_setup("EXP5_ABLATION", side, entry, bos["impulse_start"], now_utc,
                                    variant="no_liquidity_sweep",
                                    note="CHoCH+BOS setup that would be REJECTED by a sweep-required gate",
                                    atr_pips=ctx.current_atr_pips)
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)

    # variant: no_ema_agreement — proxy: log even while the 1H bias is
    # STALE (i.e. ignore the distrust a stale bias normally earns).
    if choch and bias_stale:
        leg_id = "nea|{}|{:.5f}".format(side, bos["impulse_start"])
        setup = build_shadow_setup("EXP5_ABLATION", side, entry, bos["impulse_start"], now_utc,
                                    variant="no_ema_agreement",
                                    note="CHoCH+BOS setup taken despite a STALE 1H bias",
                                    atr_pips=ctx.current_atr_pips)
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)

    # variant: choch_only — isolated CHoCH-only bucket (excludes plain
    # continuation BOS, which Experiment 1 mixes in).
    if choch:
        leg_id = "co|{}|{:.5f}".format(side, bos["impulse_start"])
        setup = build_shadow_setup("EXP5_ABLATION", side, entry, bos["impulse_start"], now_utc,
                                    variant="choch_only",
                                    note="Pure CHoCH-only bucket, continuation BOS excluded",
                                    atr_pips=ctx.current_atr_pips)
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)

# ---- Experiment 6: Alternative Bias Logic ----------------------------------
def experiment_6_alt_bias(facts, state, current_atr_pips, shadow_state, shadow_stats, now_utc):
    """Whenever the OLD bias rule (compute_macro_bias_shadow_old_rule)
    disagrees with the live 1H bias, logs the hypothetical CHoCH/BOS-style
    trade IN THE OLD RULE'S DIRECTION, so the divergence gets a real R
    outcome instead of just a print statement."""
    shadow_bias = state.get("shadow_macro_bias_confirmed")
    if shadow_bias not in ("BULLISH", "BEARISH") or shadow_bias == facts.macro_bias:
        return

    side = bias_to_side(shadow_bias)
    entry = float(facts.last_candle_5m()["Close"])
    origin = state.get("shadow_macro_leg_origin")
    if origin is None:
        return
    leg_id = "{}|{:.5f}".format(side, origin)
    setup = build_shadow_setup("EXP6_ALT_BIAS", side, entry, origin, now_utc,
                                note=f"Old-rule bias ({shadow_bias}) diverges from live bias ({facts.macro_bias})",
                                atr_pips=current_atr_pips)
    log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)


# ---- Experiment E: Rejected Live Trades ------------------------------------
# Cap on shadow_state["drained_leg_ids"] — bounds the file's growth forever.
# Must comfortably exceed any plausible replay window: the failure mode this
# guards against is a stale union-merge replay of an un-truncated queue,
# which only ever reintroduces leg_ids from recent passes, not old history.
DRAINED_LEG_ID_CAP = 5000


def drain_rejected_live_queue(shadow_state, shadow_stats):
    """Replaces experiment_e_rejected_live(). Live scanner no longer builds
    EXPE_REJECTED_LIVE setups itself (see log_rejected_live_to_queue() in
    scanner_live.py) — it just queues the raw facts of each rejection to
    REJECTED_LIVE_QUEUE_FILE. This drains that queue, turns each line into
    a proper shadow setup via the same build_shadow_setup/log_shadow_setup
    every other experiment uses, then truncates the queue file.

    Run this once per MIN pass, same idea as update_pending_shadow_setups:
    process everything queued since last time, then persist.

    IDEMPOTENCY NOTE: truncating the queue file is NOT a reliable
    "already handled" signal. rejected_live_queue.jsonl is committed by
    live-scan.yml AND min-scan.yml and carries a merge=union .gitattributes
    driver (to stop live's concurrent appends being silently discarded on
    conflict). Union merge has no concept of intentional deletion — if
    live pushes new appends to origin in the same window MIN is truncating,
    the rebase unions "empty" (MIN's truncated version) with "A,B,C,D"
    (live's extended version) and produces A,B,C,D right back on main.
    MIN then reads the SAME already-processed lines again next pass.
    log_shadow_setup's own dedup (a bounded per-key seen-set, see
    SHADOW_SEEN_LEG_CAP) is NOT a reliable enough guard for THIS failure
    mode on its own: it only remembers the most recent SHADOW_SEEN_LEG_CAP
    leg_ids per experiment/variant key, so a replayed batch old enough to
    have aged out of that window would still slip through and get
    silently re-logged (inflates shadow_stats['logged'], duplicates
    shadow_state['pending']).
    Fix: shadow_state["drained_leg_ids"] is the actual source of truth for
    "have I handled this leg_id" — uncapped-in-practice (DRAINED_LEG_ID_CAP
    is generous) and lives in shadow_state.json, which MIN exclusively
    writes (no cross-workflow contention, no union driver on that file),
    so it survives regardless of what happens to the queue file's git
    history or how far back a replayed batch reaches."""
    if not os.path.exists(REJECTED_LIVE_QUEUE_FILE):
        return 0

    try:
        with open(REJECTED_LIVE_QUEUE_FILE, "r") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except Exception as e:
        print("[REJECTED-LIVE QUEUE READ ERROR] " + str(e))
        return 0

    if not lines:
        return 0

    drained_leg_ids = shadow_state.setdefault("drained_leg_ids", [])
    drained_set = set(drained_leg_ids)

    processed = 0
    skipped_dupe = 0
    for line in lines:
        try:
            record = json.loads(line)
        except Exception:
            continue  # one bad line shouldn't sink the whole drain

        leg_id = record.get("leg_id")
        if leg_id is None:
            # No leg_id to dedup against — can't safely skip it, but can't
            # mark it drained either. Process it and move on; this matches
            # prior behavior for the (should-be-rare) malformed record.
            pass
        elif leg_id in drained_set:
            skipped_dupe += 1
            continue  # already folded into shadow_state/shadow_stats before

        now_utc = datetime.now(timezone.utc)
        try:
            queued_at = datetime.fromisoformat(record["queued_at"])
        except Exception:
            queued_at = now_utc

        setup = build_shadow_setup(
            "EXPE_REJECTED_LIVE", record["side"], record["entry"], record["sl_raw"],
            queued_at, tags=record.get("tags", {}), note=record.get("note", ""),
            atr_pips=record.get("atr_pips"),
        )
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)
        processed += 1

        if leg_id is not None:
            drained_set.add(leg_id)
            drained_leg_ids.append(leg_id)

    if skipped_dupe:
        print(f"[MIN] skipped {skipped_dupe} already-drained record(s) "
              f"replayed from rejected_live_queue.jsonl (union-merge replay "
              f"or similar — this is the guard working as intended)")

    # Bound growth — only recent leg_ids are ever at risk of replay.
    if len(drained_leg_ids) > DRAINED_LEG_ID_CAP:
        del drained_leg_ids[: len(drained_leg_ids) - DRAINED_LEG_ID_CAP]

    # Truncate — best-effort only. Even if a concurrent union-merge rebase
    # resurrects these lines on main, drained_leg_ids (committed in the
    # same shadow_state.json save as this function's caller) is what
    # actually prevents re-logging next pass, not this truncation.
    try:
        open(REJECTED_LIVE_QUEUE_FILE, "w").close()
    except Exception as e:
        print("[REJECTED-LIVE QUEUE TRUNCATE ERROR] " + str(e))

    return processed



# ---- Experiment 7: Tier ATR Mirror -----------------------------------------
# compute_market_state() / classify_regime() / MARKET_STATE_*_LOOKBACK used to
# live here — moved to scanner_observation.py (Market Thesis Engine 15M-layer
# work, per chat) since build_market_thesis() needed the same "how strong/
# extended/compressed is the CURRENT 15M leg" snapshot _leg_obs_formation_
# state() below already used, and scanner_observation.py is the shared layer
# both scanner_live.py and min_scanner.py import from (min_scanner.py is
# never allowed to be imported FROM the other direction — see that module's
# docstring). Zero behavior change: same functions, same bodies, imported
# from scanner_observation.py now instead of defined locally. Bonus: the two
# functions read facts._atr_15m_series (a "private" MarketFacts attribute)
# directly — that was always a cross-module leak reaching into
# scanner_observation.py's class from here; now that they live in the same
# module as MarketFacts, it's genuinely internal instead.


def _tier_prior_posterior(tier_label, min_n=EVIDENCE_MIN_N):
    """
    VALIDATION ENGINE FIX (audit #3, per chat): a frozen, forecast-BEFORE-
    outcome snapshot of this tier's Beta-Binomial posterior win probability,
    computed from ONLY the resolved trades already sitting in the permanent
    EXP7_TIER_ATR log at call time — never including the trade currently
    being logged, since that trade hasn't resolved yet.

    This is what makes calibration honest: the "stated" number gets frozen
    into the NEW setup's tags right now, at prediction time, then carried
    verbatim through to resolution (tags flow straight through in
    _append_shadow_trade_log). Later, format_calibration_report() compares
    THIS frozen number against what actually happened to THIS specific
    trade — not, as before, deriving both "stated" and "actual" from the
    same completed sample (which is tautological: a posterior mean is
    guaranteed to differ from the raw win rate it was computed from, purely
    from prior shrinkage, regardless of whether the tier has any real edge).

    Returns None below min_n — same dormancy floor as the Evidence Engine
    (compute_evidence()) — rather than freezing a noisy near-0%/100%
    estimate off a handful of trades.
    """
    tier_number = TIER_NUMBER.get(tier_label)
    records = _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
    same_tier = [r for r in records if r.get("tier_number") == tier_number]
    n = len(same_tier)
    if n < min_n:
        return None
    wins = sum(1 for r in same_tier if r.get("r_achieved", 0.0) > 0)
    losses = n - wins
    post_mean, _, _ = _beta_posterior(wins, losses)
    return round(post_mean, 4)


def experiment_7_tier_atr_mirror(facts, ctx, state, now_utc, shadow_state, shadow_stats):
    """
    Answers the friend's question directly: for EACH live tier, log the
    setup + current ATR(5m, pips) + eventual R result. Runs every scan
    regardless of ctx.tradeable, including scans in session/post-spike
    conditions the live bot itself skips, so the resulting dataset spans
    the FULL ATR range, not just whatever the live gate happens to admit
    (which would make the analysis circular).

    UPDATE (2026-08-11, per chat): ATR_MIN_PIPS is no longer a hard gate
    on the live bot either — a live signal can now fire at ANY ATR, just
    tagged very-low/low in the alert (see MarketContext in
    scanner_observation.py). This experiment predates that change and
    used to exist specifically to build the dataset needed to justify
    removing the gate; it's kept running unchanged because it's still
    the source of truth compute_atr_suitability()/format_atr_bands
    reads from, and because it independently peeks every tier's
    structural setup regardless of session/post-spike state too, which
    ctx.tradeable still gates on live.

    Each tier is peeked read-only (state_updates discarded, exactly like
    Experiment E) — this can never claim a leg or touch live state. A
    tier is logged once its structural setup is complete and scored,
    regardless of the live market-context gate accepting it or not —
    that gate is measured via risk_gate_pass in the stored tags.

    SCOPED TO ATR ONLY (per chat, 2026-08-11): this used to also carry
    the conviction/policy comparison (conviction_score,
    would_have_fired_pre_context/live, risk_band, fires_live_now) —
    that comparison now lives in EXP4_POLICY_LAB, which runs three
    independently-gated policy variants per tier instead of piggy-
    backing retrospective tags onto this experiment's fixed population.
    Removing them here means EXP7's population and target_r are no
    longer entangled with a policy question at all — every mirrored
    trade uses the flat RR_RATIO target, same as the live bot's actual
    risk-at-hand execution, so this stays a clean, single-purpose
    ATR-vs-outcome dataset.
    """
    leg_key = compute_leg_id(facts.macro_bias, facts.swing_high, facts.swing_low)
    entry = float(facts.last_candle_5m()["Close"])

    # Fingerprint facts (per chat) — cheap, self-contained, computed once
    # per scan since they describe the current leg/market, not any one
    # tier. Deliberately NOT included here: EMA slope (would need df_1h
    # threaded into this function, which doesn't receive it today —
    # bigger plumbing change, held back rather than rushed), sweep
    # distance (needs detect_liquidity_sweep's return signature changed,
    # done separately below), volume (Twelve Data's forex feed has no
    # volume field at all — not a "later", genuinely unavailable),
    # reaction-time-in-candles (needs cross-scan state tracking of first
    # zone touch — real bug surface, held back for its own session).
    bos = facts.bos_15m()
    ob = facts.order_block()
    fingerprint = {
        "leg_length_pips": round(abs(bos["impulse_end"] - bos["impulse_start"]) / PIP_SIZE, 1) if bos else None,
        "break_count": bos["break_count"] if bos else None,
        "atr_percentile_15m": facts.atr_percentile_15m(),
        "ob_freshness_candles": (len(facts.df_15m) - 1 - ob["origin_idx"]) if ob else None,
        # Same level Tier 3 already checks liquidity_sweep against
        # (bos["impulse_start"]) — kept consistent so this is directly
        # comparable to that tier's own boolean flag, not a different
        # measurement of a different thing wearing a similar name.
        "sweep_distance_pips": facts.sweep_distance_pips(bos["impulse_start"]) if bos else None,
        # Regime tags (per chat, salvaged from Thompson Sampling pitch) —
        # tagging only, no decision logic. See classify_regime()'s
        # docstring for why these are 4 separate fields, not one
        # concatenated string.
        **classify_regime(facts, ctx, state),
        # Formation story (per chat) — was THIS leg a genuine CHoCH flip,
        # or a fresh formation with no prior dominant to flip from? See
        # detect_bos_impulse()'s was_choch tracking.
        "was_choch": bos["was_choch"] if bos else None,
        **compute_market_state(facts, bos),
    }

    for tier_label, tier_fn in TIER_REGISTRY.items():
        peek = tier_fn(facts, ctx, state, now_utc)  # read-only — state_updates discarded
        if not peek.activated or peek.score is None:
            continue

        tier_number = TIER_NUMBER.get(tier_label)
        # Use the tier's own proposed entry/sl when it fired one; fall
        # back to current price + that tier's structural sl_raw is
        # unavailable pre-fire, so a non-fired-but-activated peek (e.g.
        # blocked only by conviction) still needs SOME sl. Re-derive it
        # the same way the tier would if it fires, by using peek.sl_raw
        # when present (tiers always set it alongside activated=True in
        # this codebase), otherwise skip — no structural sl means no
        # honest R multiple.
        if peek.sl_raw is None:
            continue

        mirror_side = peek.direction or bias_to_side(facts.macro_bias)
        sl_buffer = max(
            sl_multiplier_for_context(ctx) * ctx.current_atr,
            SL_MIN_PIPS * PIP_SIZE,
        )
        sl_final = (
            peek.sl_raw - sl_buffer if mirror_side == "BUY"
            else peek.sl_raw + sl_buffer
        )
        mirror_risk = (
            entry - sl_final if mirror_side == "BUY"
            else sl_final - entry
        )
        risk_gate_pass = (
            math.isfinite(mirror_risk) and mirror_risk > 0 and
            mirror_risk <= MAX_RISK_ATR_MULT * ctx.current_atr and
            mirror_risk <= MAX_RISK_PIPS * PIP_SIZE
        )
        # Flat RR_RATIO (per chat, 2026-08-11) — matches the live bot's
        # actual risk-at-hand execution now that target_r isn't policy-
        # dependent here anymore (see docstring: policy comparison moved
        # to EXP4_POLICY_LAB).
        target_r = RR_RATIO

        # VALIDATION ENGINE FIX (audit #3, per chat): freeze this tier's
        # prior-posterior win probability NOW, before this trade resolves —
        # using only what's already in the permanent log. This is the
        # "stated" half of a genuine forecast-before-outcome calibration
        # check; without freezing it here, #3 of format_calibration_report
        # would have to derive "stated" from the same completed sample as
        # "actual," which is tautological (see chat).
        predicted_win_prob = _tier_prior_posterior(tier_label)

        # Market Thesis capture (per chat, friend's reply): "preserve
        # phase, transition_cause, expected event, failure risk... then
        # conviction becomes just another feature we can investigate."
        # Pure tagging — nothing here reads or decides anything, it just
        # freezes what build_market_thesis() (scanner_observation.py)
        # already computed and scan() already persisted to state.json
        # THIS pass, before this trade resolves, same "stated" timing
        # discipline as predicted_win_prob above. This is the data half of
        # the still-deferred "Case File actual-vs-expected resolution"
        # comparison (see MarketThesis's docstring) — comparing
        # thesis_expected_next_event against what actually happened is
        # NOT built here; that needs leg_obs's forward-observation
        # plumbing extended, its own separate pass. This just makes sure
        # the "stated" side of that eventual comparison isn't lost between
        # now and whenever that plumbing gets built.
        thesis_tags = {
            "thesis_phase":                 state.get("market_phase"),
            "thesis_transition_cause":      state.get("market_thesis_transition_cause"),
            "thesis_failure_risk":          state.get("market_thesis_failure_risk"),
            "thesis_expected_next_event":   state.get("market_thesis_expected_next_event"),
            "thesis_weakness_count":        len(state.get("market_thesis_weaknesses") or []),
            "thesis_evidence_count":        len(state.get("market_thesis_evidence") or []),
        }

        setup = build_shadow_setup(
            "EXP7_TIER_ATR", mirror_side,
            entry, sl_final, now_utc,
            variant=tier_label,
            # "Store everything" (per chat) — conviction_score/
            # would_have_fired_live/atr_floor_pips were already here;
            # peek.breakdown is merged in so the EVIDENCE ENGINE has the
            # tier's actual structural facts (order_block, choch, sweep,
            # etc.) to compare against, not just its final score. Raw
            # facts, not another derived number — same principle as the
            # friend's "rows of facts, not scores" design.
            # predicted_win_prob: frozen calibration prediction, None if
            # this tier didn't have EVIDENCE_MIN_N resolved trades yet.
            tags={# ATR/risk-ceiling facts only (per chat, 2026-08-11) —
                  # conviction_score / would_have_fired_pre_context /
                  # would_have_fired_live / risk_band / fires_live_now
                  # moved to EXP4_POLICY_LAB, which gates each policy
                  # variant independently instead of tagging one fixed
                  # population with several retrospective what-ifs.
                  "risk_gate_pass": risk_gate_pass,
                  # atr_min_pips/atr_warn_pips: LABEL thresholds now, not a
                  # gate (removed 2026-08-11) — kept here so historical
                  # rows are comparable even if these constants change later.
                  "atr_min_pips": ATR_MIN_PIPS, "atr_warn_pips": ATR_WARN_PIPS,
                  "atr_very_low": ctx.very_low_atr_warning, "atr_low": ctx.low_atr_warning,
                  "predicted_win_prob": predicted_win_prob,
                  **(peek.breakdown or {}), **fingerprint, **thesis_tags},
            note=f"Tier {tier_number} ({tier_label}) mirror — ATR {ctx.current_atr_pips:.1f}p "
                 f"({'very low' if ctx.very_low_atr_warning else 'low' if ctx.low_atr_warning else 'normal'})",
            atr_pips=ctx.current_atr_pips,
            tier_number=tier_number,
            target_r=target_r,
        )
        log_shadow_setup(shadow_state, shadow_stats, setup, f"{leg_key}|{tier_label}")


# ---- Bayesian posterior math (dependency-free Beta-Binomial) --------------

def _betacf(a, b, x):
    """Continued fraction used by _betainc. Numerical-Recipes algorithm."""
    MAXIT, EPS, FPMIN = 200, 3e-12, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betainc(a, b, x):
    """Regularized incomplete beta I_x(a,b) — the Beta(a,b) CDF at x."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - ln_beta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_ppf(a, b, p, tol=1e-6):
    """Inverse CDF of Beta(a,b) at probability p, via bisection."""
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if _betainc(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


def _beta_posterior(wins, losses, alpha0=BAYES_PRIOR_ALPHA, beta0=BAYES_PRIOR_BETA,
                     ci=BAYES_CI_LEVEL):
    """Beta-Binomial posterior over a true win rate given `wins`/`losses`
    resolved outcomes. Returns (posterior_mean, ci_low, ci_high) as
    fractions in [0, 1]. One rule, used by both compute_evidence() and
    format_atr_suitability_table() — so the evidence note for a tier and
    its /atrbands row can never quietly disagree with each other.

    ⚠  SERIAL-CORRELATION / EFFECTIVE-N CAVEAT  ⚠
    This model treats every resolved trade as an independent Bernoulli draw
    from a stationary process. That assumption is violated when consecutive
    trades share the same macro regime (BULLISH_FRESH → trade 1 → trade 2
    → trade 3 …): outcomes within a regime run are serially correlated —
    a trending market that catches trade 1 onside tends to catch trade 2
    as well. The effective N is therefore materially LOWER than the nominal
    `wins + losses` count, and the Beta credible interval is TIGHTER than
    warranted. In plain terms: the CI looks precise but is overconfident.

    Practical implication: treat the CI endpoints as directional guides
    rather than exact probability bounds. Before relying on them for live
    sizing or conviction-band decisions, verify with compute_tier_sharpe()
    that the realised Sharpe is consistent with the claimed edge, and
    cross-reference the Markov transition matrix (which captures regime
    stickiness) to estimate how much serial correlation is actually present.

    There is no code fix applied here — correcting for arbitrary serial
    correlation requires either an ARMA-extended model or block
    bootstrap, neither of which is in scope for this version.
    """
    a = alpha0 + wins
    b = beta0 + losses
    mean = a / (a + b)
    lo = _beta_ppf(a, b, (1 - ci) / 2)
    hi = _beta_ppf(a, b, 1 - (1 - ci) / 2)
    return mean, lo, hi


def classify_bias_state(macro_bias, bias_stale):
    """Collapses the live (macro_bias, macro_bias_stale) pair into one of
    MARKOV_STATES. Pure. CONSOLIDATION has no FRESH/STALE split — stale-
    ness only describes a hold-over DIRECTIONAL leg, it isn't a concept
    that applies while there's no directional leg at all."""
    if macro_bias == "CONSOLIDATION":
        return "CONSOLIDATION"
    return "{}_{}".format(macro_bias, "STALE" if bias_stale else "FRESH")


def record_markov_transition(state, markov_data, current_state_label):
    """Records one scan-to-scan transition into the persisted count
    matrix (markov_data, mutated in place — caller saves it), then
    advances state['markov_last_state'] (mutated in place — caller
    saves state) for next scan. PURE side effect, no return value.

    First-ever call (no last_state recorded yet, e.g. right after this
    feature is deployed) records no transition — you can't transition
    FROM a state you were never observed to hold — but still seeds
    last_state so the NEXT scan has something to transition from.

    Also collects Validation Engine calibration data: for every possible
    next state, records the stated probability BEFORE updating the count
    matrix (before-outcome probabilities are what calibration needs) and
    whether that state actually happened. This is the weather-forecaster
    format: if we said "P=72%" for state X, how often did X occur over
    all such predictions? format_calibration_report() reads this list.

    ⚠  SERIAL-CORRELATION / EFFECTIVE-N CAVEAT  ⚠
    The transition matrix is built from scan-to-scan state observations.
    In practice, GBPUSD can hold the same regime (e.g. BULLISH_FRESH) for
    dozens of consecutive scans, making adjacent rows in the count matrix
    highly correlated. The effective number of independent transition
    observations is substantially below the raw cell-count totals shown in
    /markov — regime persistence means the same underlying market event
    inflates many consecutive rows. This means:
      • Transition probabilities converge slowly — more counts look more
        precise but may just be the same multi-hour trend counted 30 times.
      • The calibration predictions from this function share the same
        correlation structure: stated-vs-actual accuracy appears high
        during persistent regimes because consecutive predictions are
        almost identical.
    No code fix is applied here. Cross-reference session-level data (e.g.
    unique bias-change events) rather than raw scan counts before trusting
    tight confidence on a specific transition row.
    """
    last_state = state.get("markov_last_state")
    if last_state is not None:
        # ── Calibration: capture stated probs BEFORE updating counts ─────────
        probs, n_obs = compute_transition_probabilities(markov_data, last_state)
        if n_obs > 0:   # skip pure-prior period; no real history to calibrate
            cal_list = markov_data.setdefault("calibration_predictions", [])
            for next_state in MARKOV_STATES:
                cal_list.append({
                    "stated_prob":     round(probs.get(next_state, 0.0), 4),
                    "predicted_state": next_state,
                    "actual_happened": next_state == current_state_label,
                })
            # Rolling cap: 5 entries per scan, cap at ~10 000 total (~2000 scans)
            if len(cal_list) > 10000:
                markov_data["calibration_predictions"] = cal_list[-10000:]

        # ── Now update the transition counts ──────────────────────────────────
        transitions = markov_data.setdefault("transitions", {})
        row = transitions.setdefault(last_state, {})
        row[current_state_label] = row.get(current_state_label, 0) + 1
    state["markov_last_state"] = current_state_label


def compute_transition_probabilities(markov_data, from_state):
    """Dirichlet-smoothed P(next state | from_state) over all of
    MARKOV_STATES. Returns (probs_dict, n) where n is the raw count of
    observed transitions FROM from_state — n=0 means probs is pure
    prior (uniform 20% each across 5 states), not a real estimate yet.
    Never a raw count/total: that would let a from_state seen twice,
    both times moving to the same neighbor, report a false 100%. PURE.
    """
    row = (markov_data.get("transitions", {}) or {}).get(from_state, {})
    n = sum(row.values())
    k = len(MARKOV_STATES)
    denom = n + k * MARKOV_PRIOR_ALPHA
    probs = {s: (row.get(s, 0) + MARKOV_PRIOR_ALPHA) / denom for s in MARKOV_STATES}
    return probs, n


def format_markov_report(markov_data, current_state_label):
    """Full regime report for /markov — every outgoing transition
    probability from the current state, sorted by likelihood."""
    probs, n = compute_transition_probabilities(markov_data, current_state_label)
    lines = [
        "🔀 *Market Evolution — regime transition model*",
        "─────────────────────",
        f"Current state: `{current_state_label}`",
        f"Observed transitions FROM this state: `{n}`",
        "",
    ]
    if n == 0:
        lines.append("_No history FROM this state yet — probabilities below are pure prior (uniform)._\n")
    for s, p in sorted(probs.items(), key=lambda kv: -kv[1]):
        marker = "  ← self" if s == current_state_label else ""
        lines.append(f"`{s:<15}` `{p * 100:5.1f}%`{marker}")
    lines.append("")
    lines.append("_Informational only — never overrides Rule of Law or gates a live signal._")
    return "\n".join(lines)


def format_markov_line(markov_data, current_state_label):
    """Compact one-line regime annotation for the live signal alert —
    current state + single most-likely next state. Returns None (stay
    silent) when n=0, since a pure-prior 20% isn't worth printing next
    to an actual trade signal."""
    probs, n = compute_transition_probabilities(markov_data, current_state_label)
    if n == 0:
        return None
    top_state, top_p = max(probs.items(), key=lambda kv: kv[1])
    return (f"🔀 *Regime:* `{current_state_label}` (n={n}) — most likely next: "
            f"`{top_state}` `{top_p * 100:.0f}%`")


# =========================================================================
# FORWARD OBSERVATION — Per-leg record (one per H1 leg, not per experiment)
# =========================================================================
# Opens the moment an H1 leg forms, regardless of any tier or experiment.
# Tracks forward scan by scan until the leg resolves. Three facets, one
# unified record — NOT three separate systems:
#
#   Facet 1 — Leg fate (forward-looking):
#     CONTINUED   — another BOS in the same direction (leg extended further)
#     REVERSED    — genuine CHoCH (new leg formed in the opposite direction)
#     INVALIDATED — origin violated / 78.6%+ retrace, nothing new formed
#
#   Facet 2 — Formation-time state (backward-looking snapshot):
#     Captured ONCE at the moment the record opens. This is free because
#     the trigger point (leg formation) is exactly when that data is
#     naturally available. Includes: was_choch, regime, session, ATR,
#     volatility state, trend strength, and 15M structure at formation.
#
#   Facet 3 — Zone touches (per tier):
#     Tier 1 = price inside the Order Block zone
#     Tier 2 = price inside the HTF Fib pocket
#     Tier 3 = 15M CHoCH/BOS aligned with 1H bias (structural trigger)
#     Stored as bar count at first touch; None if the leg resolved without
#     price ever reaching that zone.
#
# BUG FIX — invalidation re-open loop:
#   After a leg is INVALIDATED, leg_id does NOT change (swing points stay
#   in state until a new leg replaces them). Without the closed_ids guard
#   below, the system immediately reopens a fresh record for the same dead
#   leg because compute_leg_id() returns the same string. closed_ids blocks
#   that: an INVALIDATED leg_id is added to this rolling set and skipped on
#   reopen. A genuinely new leg carries different swing points → different
#   leg_id → not in the set → opens fresh. The set is capped at
#   LEG_OBS_CLOSED_MAX so it never grows unboundedly.
# =========================================================================
# =========================================================================

def load_leg_obs_state():
    try:
        with open(LEG_OBS_STATE_FILE, "r") as f:
            state = json.load(f)
        if state.get("methodology_version") != LEG_OBS_METHODOLOGY_VERSION:
            return {"open": None, "closed_ids": [],
                    "methodology_version": LEG_OBS_METHODOLOGY_VERSION}
        return state
    except Exception:
        return {"open": None, "closed_ids": [],
                "methodology_version": LEG_OBS_METHODOLOGY_VERSION}


def save_leg_obs_state(obs_state):
    try:
        atomic_write_json(LEG_OBS_STATE_FILE, obs_state, indent=2)
    except Exception as e:
        print("[LEG OBS SAVE ERROR] " + str(e))


def _append_leg_obs_log(record):
    """Permanently appends one resolved leg record to LEG_OBS_LOG_FILE.
    Append-only — never truncated. Same pattern as SHADOW_TRADE_LOG_FILE,
    including the same write-hardening (per chat, "safety against
    duplication"): retries SHADOW_TRADE_LOG_WRITE_RETRIES times before
    giving up, and returns True/False so the caller (_close_leg_obs)
    knows whether it's actually safe to clear obs_state["open"] — the
    old version swallowed the exception and returned nothing, so a
    failed write still got the record dropped from "open" with nothing
    else pointing at it. Reuses the shadow-trade-log retry constants
    rather than adding a parallel set — same file-write failure modes,
    no reason for different tuning."""
    line = json.dumps(record, default=_json_default) + "\n"
    last_error = None
    for attempt in range(1, SHADOW_TRADE_LOG_WRITE_RETRIES + 1):
        try:
            with open(LEG_OBS_LOG_FILE, "a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as e:
            last_error = e
            if attempt < SHADOW_TRADE_LOG_WRITE_RETRIES:
                time.sleep(SHADOW_TRADE_LOG_WRITE_RETRY_DELAY_SEC)
    print(f"[LEG OBS LOG ERROR] gave up after {SHADOW_TRADE_LOG_WRITE_RETRIES} "
          f"attempts: {last_error}")
    return False


def _leg_obs_formation_state(facts, ctx, state):
    """Facet 2: formation-time market snapshot. Captured ONCE, at record
    open. Pure — reads only what's already computed; no new fetches or
    mutations. Returns a flat dict merged into the record."""
    bos = facts.bos_15m()
    regime = classify_regime(facts, ctx, state)
    mkt = compute_market_state(facts, bos)
    origin  = state.get("macro_leg_origin")
    extreme = state.get("macro_leg_extreme")
    leg_len = (
        round(abs(extreme - origin) / PIP_SIZE, 1)
        if origin is not None and extreme is not None else None
    )
    # Market Phase + measured-move extension at formation time. Purely
    # additive fields (same pattern as origin_idx/was_choch on
    # detect_bos_impulse) — older records in leg_obs_log.jsonl simply
    # won't have these keys; format_scenario_summary() below skips any
    # record missing them rather than treating a missing key as a value.
    # Both are READ from state (already computed/persisted by
    # scanner_live.py's compute_market_phase() call each scan) — no new
    # detection happens here, this only tags the snapshot with it.
    extension = compute_measured_move_extension(state)

    return {
        # 1H leg's own story
        "macro_was_choch":       state.get("macro_leg_was_choch"),
        "macro_leg_direction":   state.get("macro_leg_direction"),
        "macro_leg_length_pips": leg_len,
        # 15M structure AT formation time
        "bos_15m_direction":     bos["direction"] if bos else None,
        "bos_15m_break_count":   bos["break_count"] if bos else None,
        "bos_15m_was_choch":     bos.get("was_choch") if bos else None,
        # Market conditions at formation
        "atr_pips":              round(facts.current_atr_5m_pips(), 2),
        "atr_percentile_15m":    facts.atr_percentile_15m(),
        "market_phase":          state.get("market_phase"),
        "market_phase_age_bars": state.get("market_phase_age_bars"),
        "measured_move_ratio":   extension["ratio"] if extension else None,
        "measured_move_bucket":  extension["bucket"] if extension else None,
        # Market Thesis Engine's WHY behind market_phase (per chat) — same
        # additive/skip-if-missing discipline as measured_move_bucket
        # above. Feeds format_scenario_summary()'s bucket key so THAT
        # existing engine gets richer instead of a second one being built.
        "transition_cause":      state.get("market_thesis_transition_cause"),
        # Campaign Extension (per chat, 2026-08-12) — the long-horizon
        # maturity lens, distinct from measured_move_ratio/bucket above
        # (which is leg-to-leg AB=CD and resets every leg). Same additive/
        # skip-if-missing discipline: None until a campaign exists (fresh
        # state.json, or first leg since this shipped). Tagging ONLY —
        # deliberately NOT folded into format_scenario_summary()'s
        # (phase, cause, ext_bucket) key yet. extension_multiple is a
        # continuous, effectively unbounded ratio, not a small fixed set
        # of buckets like measured_move_bucket — adding it as a 4th raw
        # dimension there would explode cardinality and starve every
        # bucket of samples (see that function's own correlation caveat).
        # If it's worth bucketing later, it needs its own CAMPAIGN_
        # EXTENSION_BUCKETS the same way MEASURED_MOVE_BUCKETS exists —
        # a deliberate follow-up, not a side effect of adding these two
        # raw fields now.
        "campaign_extension_multiple":  (state.get("market_thesis_campaign") or {}).get("extension_multiple"),
        "campaign_continuation_count":  (state.get("market_thesis_campaign") or {}).get("continuation_count"),
        **regime,
        **mkt,
    }


def _update_zone_touches(rec, facts, macro_bias):
    """Facet 3: stamp bar count at FIRST touch of each tier's entry zone.
    Only the first touch per zone is recorded — we want bars-until-touch,
    not every touch. Called each scan while the record is still open."""
    bars = rec["bars_open"]   # already incremented for this scan by caller

    # Tier 1 zone: price inside the current Order Block
    if rec.get("tier1_touched_bar") is None and facts.price_in_order_block():
        rec["tier1_touched_bar"] = bars

    # Tier 2 zone: price inside the HTF Fib pocket
    if rec.get("tier2_touched_bar") is None and facts.price_in_fib_pocket():
        rec["tier2_touched_bar"] = bars

    # Tier 3 "zone" is the 15M CHoCH aligned with 1H bias — a structural
    # event, not a price level. Touched = the CHoCH condition fires while
    # this H1 leg is still alive.
    if rec.get("tier3_touched_bar") is None:
        bos = facts.bos_15m()
        if facts.has_choch_15m() and bos is not None and bos["direction"] == macro_bias:
            rec["tier3_touched_bar"] = bars


def _update_leg_timeline(rec, facts, state, macro_bias):
    """Market Story layer (Phase 2b, per chat — extends the SAME open
    record Facets 2/3 already maintain, rather than inventing a second
    timeline mechanism). Appends an event whenever something structurally
    meaningful happens: a new 15M break, a CHoCH edge, a zone touch (reuses
    Facet 3's tier1/2/3_touched_bar — an event fires exactly when one of
    those flips from None to a bar count THIS scan), or a market_phase
    change. Called each scan while the record is still open, AFTER
    _update_zone_touches() so tier*_touched_bar already reflects this scan.

    Fixed set of triggers, same discipline as classify_transition_cause()/
    classify_thesis_delta() — never freeform text, never logs every scan.
    Mutates `rec` in place; caller persists it. Best-effort: a bug here
    must never break Facet 1/2/3, which is why run_leg_observation() wraps
    its whole body in try/except already.
    """
    bars = rec["bars_open"]
    tl = rec.setdefault("timeline", [])

    def _emit(kind, detail):
        tl.append({"bar": bars, "kind": kind, "detail": detail})
        rec["timeline"] = tl[-LEG_OBS_TIMELINE_MAX:]

    # New 15M break — compare against the break_count last seen on this leg.
    bos = facts.bos_15m()
    new_bc = bos["break_count"] if bos else None
    prev_bc = rec.get("_tl_last_break_count")
    if new_bc is not None and prev_bc is not None and new_bc > prev_bc:
        _emit("break", f"New 15M break — count now {new_bc}")
    if new_bc is not None:
        rec["_tl_last_break_count"] = new_bc

    # CHoCH — rising edge only (don't re-log every scan it stays true).
    choch_now = bool(facts.has_choch_15m())
    if choch_now and not rec.get("_tl_choch_active", False):
        _emit("choch", "15M CHoCH confirmed")
    rec["_tl_choch_active"] = choch_now

    # Zone touches — reuse Facet 3's own stamps; an event fires exactly on
    # the scan a touch first registers (touched_bar == bars this scan).
    if rec.get("tier1_touched_bar") == bars:
        _emit("zone_touch", "Tier 1 zone touched (Order Block)")
    if rec.get("tier2_touched_bar") == bars:
        _emit("zone_touch", "Tier 2 zone touched (Fib pocket)")
    if rec.get("tier3_touched_bar") == bars:
        _emit("zone_touch", "Tier 3 zone touched (CHoCH/BOS aligned)")

    # Market Phase change — read from state, same field the Market Thesis
    # Engine already persists each scan (compute_market_phase()).
    new_phase = state.get("market_phase")
    prev_phase = rec.get("_tl_last_phase")
    if new_phase is not None and prev_phase is not None and new_phase != prev_phase:
        _emit("phase_change", f"Phase changed: {prev_phase} → {new_phase}")
    if new_phase is not None:
        rec["_tl_last_phase"] = new_phase


def _close_leg_obs(obs_state, open_rec, fate, now_utc, add_to_closed=False):
    """Close the open record with the given fate, append to permanent log,
    and optionally guard against reopen. add_to_closed=True only for
    INVALIDATED — CONTINUED/REVERSED close because a new leg formed, which
    carries a different leg_id, so no guard is needed there.

    Returns the (possibly mutated) obs_state either way. On a confirmed
    write failure (per chat, "safety against duplication"): obs_state
    ["open"] is deliberately left AS-IS rather than cleared, so the next
    scan's run_leg_observation() sees the same still-open record and
    retries the close instead of the record vanishing with nothing
    written and nothing left pointing at it. A Telegram alert fires so a
    persistent failure doesn't go unnoticed."""
    open_rec["fate"] = fate
    open_rec["resolved_at"] = now_utc.isoformat()
    wrote_ok = _append_leg_obs_log(open_rec)
    if not wrote_ok:
        try:
            send_telegram(
                "⚠️ *Leg observation log write failed* — a resolved leg record "
                f"({open_rec.get('leg_id')}, fate={fate}) could not be permanently "
                f"saved after {SHADOW_TRADE_LOG_WRITE_RETRIES} attempts. Left open "
                "to retry next scan; check disk space/permissions if this repeats."
            )
        except Exception:
            pass  # alerting is best-effort — must never block the observation loop
        return obs_state
    print(
        f"  [LEG OBS] {fate} — {open_rec['leg_id']} "
        f"after {open_rec['bars_open']} bars | "
        f"T1={open_rec.get('tier1_touched_bar')} "
        f"T2={open_rec.get('tier2_touched_bar')} "
        f"T3={open_rec.get('tier3_touched_bar')}"
    )
    if add_to_closed:
        closed = obs_state.get("closed_ids", [])
        if open_rec["leg_id"] not in closed:
            closed = (closed + [open_rec["leg_id"]])[-LEG_OBS_CLOSED_MAX:]
        obs_state["closed_ids"] = closed
    obs_state["open"] = None
    return obs_state


def run_leg_observation(facts, ctx, state, macro_bias, bias_stale, now_utc, obs_state):
    """
    Main driver — called from scan() after MarketFacts is built. Runs on
    EVERY scan with a directional bias (not gated by ATR or any live-trading
    condition). Handles all three facets for the current scan.

    Returns the (possibly mutated) obs_state; caller saves it.

    Never raises — errors are caught and printed. Forward Observation
    failures never affect the live bot.
    """
    # Defensive guard, not active filtering: scan() already returns before
    # ever calling run_leg_observation() when macro_bias == "CONSOLIDATION"
    # (see the early return right after compute_macro_bias in scan()), so
    # this branch is unreachable from the current call site. Kept anyway as
    # a safety net in case run_leg_observation() is ever called from
    # somewhere else that doesn't have that same early return.
    if macro_bias == "CONSOLIDATION":
        return obs_state

    def _advance_bars(rec):
        last_processed = rec.get("last_processed_candle")
        if last_processed:
            try:
                count = int((facts.df_5m.index > pd.Timestamp(last_processed)).sum())
            except Exception:
                count = 1
        else:
            count = 1
        if count > 0:
            rec["bars_open"] += count
            rec["last_processed_candle"] = facts.df_5m.index[-1].isoformat()
        return count

    current_leg_id = compute_leg_id(macro_bias, facts.swing_high, facts.swing_low)
    open_rec   = obs_state.get("open")
    closed_ids = obs_state.get("closed_ids", [])

    if open_rec is not None:
        open_leg_id = open_rec["leg_id"]

        if _same_leg(open_leg_id, current_leg_id):
            new_bars = _advance_bars(open_rec)

            if bias_stale:
                # Anchor didn't survive and nothing new replaced it.
                # INVALIDATED: add to closed_ids so this dead leg_id cannot
                # immediately reopen next scan (the bug fix).
                obs_state = _close_leg_obs(
                    obs_state, open_rec, "INVALIDATED", now_utc, add_to_closed=True)
            else:
                # Still alive — update zone touches (Facet 3), then the
                # Market Story timeline (Phase 2b) which reuses those same
                # touch stamps plus break/CHoCH/phase comparisons.
                if new_bars > 0:
                    _update_zone_touches(open_rec, facts, macro_bias)
                    _update_leg_timeline(open_rec, facts, state, macro_bias)
                obs_state["open"] = open_rec

        else:
            # Leg changed — determine fate from direction, close, then fall
            # through to open a new record below.
            old_dir = open_rec.get("formation_state", {}).get("macro_leg_direction")
            fate    = "CONTINUED" if old_dir == macro_bias else "REVERSED"
            _advance_bars(open_rec)
            obs_state = _close_leg_obs(
                obs_state, open_rec, fate, now_utc, add_to_closed=False)
            # CONTINUED/REVERSED don't need closed_ids guard — the new
            # leg_id is different, so there is no reopen risk.

    # Open a new record when:
    #   • no record is currently open (either just closed or never opened)
    #   • the current leg is confirmed (not stale)
    #   • this specific leg_id hasn't already been closed (the bug fix)
    if obs_state.get("open") is None and not bias_stale:
        if current_leg_id not in closed_ids:
            formation = _leg_obs_formation_state(facts, ctx, state)
            obs_state["open"] = {
                "id":                uuid.uuid4().hex[:12],
                "methodology_version": LEG_OBS_METHODOLOGY_VERSION,
                "leg_id":            current_leg_id,
                "opened_at":         now_utc.isoformat(),
                "bars_open":         0,
                "last_processed_candle": facts.df_5m.index[-1].isoformat(),
                "fate":              None,
                "resolved_at":       None,
                "formation_state":   formation,
                "tier1_touched_bar": None,
                "tier2_touched_bar": None,
                "tier3_touched_bar": None,
                # Market Story timeline (Phase 2b, per chat). Seeded with a
                # "formed" event at bar 0 plus scratch fields
                # (_tl_last_break_count / _tl_choch_active / _tl_last_phase)
                # so _update_leg_timeline()'s first comparison next scan is
                # against formation-time values, not None — a break count
                # that's simply unchanged from formation shouldn't log a
                # spurious "new break" event on bar 1.
                "timeline": [{
                    "bar": 0, "kind": "formed",
                    "detail": f"Leg formed — {formation.get('macro_leg_direction','?')}, "
                              f"was_choch={formation.get('macro_was_choch')}",
                }],
                "_tl_last_break_count": formation.get("bos_15m_break_count"),
                "_tl_choch_active":     bool(formation.get("bos_15m_was_choch")),
                "_tl_last_phase":       formation.get("market_phase"),
            }
            print(
                f"  [LEG OBS] Opened — {current_leg_id} | "
                f"was_choch={formation.get('macro_was_choch')} "
                f"session={formation.get('session')} "
                f"atr={formation.get('atr_pips')}p"
            )

    return obs_state


def format_leg_obs_status(obs_state, now_utc):
    """On-demand snapshot of the currently open leg observation record.
    Used by the /legobs Telegram command. Returns a formatted string."""
    rec = obs_state.get("open")
    if rec is None:
        return "🔭 *Forward Observation*\n_No leg currently being tracked._"
    try:
        age_min = int(
            (now_utc - datetime.fromisoformat(rec["opened_at"])).total_seconds() / 60)
    except Exception:
        age_min = "?"
    fs = rec.get("formation_state", {})

    def _touch(key):
        v = rec.get(key)
        return f"`{v}` bars" if v is not None else "_not yet_"

    lines = [
        "🔭 *Forward Observation — Current Leg*",
        "─────────────────────",
        f"Leg: `{rec['leg_id']}`",
        f"Opened: `{rec['opened_at'][:16]}` (~{age_min}m ago) | "
        f"Bars tracked: `{rec['bars_open']}`",
        "",
        "*Facet 2 — Formation-time state:*",
        f"  Direction: `{fs.get('macro_leg_direction','?')}` | "
        f"1H was\\_choch: `{fs.get('macro_was_choch','?')}`",
        f"  Leg length: `{fs.get('macro_leg_length_pips','?')}p` | "
        f"ATR: `{fs.get('atr_pips','?')}p` (pct `{fs.get('atr_percentile_15m','?')}`)",
        f"  Session: `{fs.get('session','?')}` | "
        f"Bias: `{fs.get('bias_state','?')}` | "
        f"Spike: `{fs.get('spike_state','?')}`",
        f"  Volatility: `{fs.get('volatility_state','?')}` | "
        f"Trend str: `{fs.get('trend_strength_atr_mult','?')}`",
        f"  15M at formation: dir=`{fs.get('bos_15m_direction','?')}` "
        f"bc=`{fs.get('bos_15m_break_count','?')}` "
        f"choch=`{fs.get('bos_15m_was_choch','?')}`",
        "",
        "*Facet 3 — Zone touches:*",
        f"  Tier 1 (Order Block): {_touch('tier1_touched_bar')}",
        f"  Tier 2 (Fib pocket):  {_touch('tier2_touched_bar')}",
        f"  Tier 3 (CHoCH/BOS):   {_touch('tier3_touched_bar')}",
        "",
    ]

    # Market Story timeline (Phase 2b, per chat). Newest-first so the most
    # recent structural event is the first thing read on a long-running leg.
    timeline = rec.get("timeline") or []
    if timeline:
        lines.append("*Market Story — timeline:*")
        for ev in reversed(timeline):
            lines.append(f"  bar {ev.get('bar','?')}: {ev.get('detail','?')}")
        lines.append("")

    lines.append("*Facet 1 — Fate:* `tracking...`")
    return "\n".join(lines)


def format_leg_obs_summary():
    """Summary of all resolved leg records from the permanent log.
    Called by /legobs summary."""
    try:
        with open(LEG_OBS_LOG_FILE, "r") as f:
            records = [
                rec for rec in (json.loads(ln) for ln in f if ln.strip())
                if rec.get("methodology_version") == LEG_OBS_METHODOLOGY_VERSION
            ]
    except FileNotFoundError:
        return "🔭 *Forward Observation*\n_No resolved legs yet._"
    except Exception as e:
        return f"🔭 _Error reading leg log: {e}_"

    if not records:
        return "🔭 *Forward Observation*\n_No resolved legs yet._"

    n = len(records)
    fates = {}
    bars_by_fate = {}
    touch_bars   = {"tier1": [], "tier2": [], "tier3": []}
    touch_counts = {"tier1": 0,  "tier2": 0,  "tier3": 0}

    for r in records:
        fate = r.get("fate", "?")
        bars = r.get("bars_open", 0)
        fates[fate] = fates.get(fate, 0) + 1
        bars_by_fate.setdefault(fate, []).append(bars)
        for tier, key in [("tier1", "tier1_touched_bar"),
                           ("tier2", "tier2_touched_bar"),
                           ("tier3", "tier3_touched_bar")]:
            val = r.get(key)
            if val is not None:
                touch_bars[tier].append(val)
                touch_counts[tier] += 1

    lines = [
        "🔭 *Forward Observation — Resolved Legs*",
        "─────────────────────",
        f"Total resolved: `{n}`",
        "",
        "*Facet 1 — Leg fate distribution:*",
    ]
    for fate in ("CONTINUED", "REVERSED", "INVALIDATED"):
        cnt = fates.get(fate, 0)
        pct = f"{cnt/n*100:.0f}%" if n else "—"
        bl  = bars_by_fate.get(fate, [])
        avg = f"{sum(bl)/len(bl):.1f}b avg" if bl else "—"
        lines.append(f"  {fate}: `{cnt}` ({pct}) — {avg}")

    lines.append("")
    lines.append("*Facet 3 — Zone touches (bars until first touch):*")
    for label, key in [("Tier 1 (Order Block)", "tier1"),
                        ("Tier 2 (Fib pocket)",  "tier2"),
                        ("Tier 3 (CHoCH/BOS)",   "tier3")]:
        times = sorted(touch_bars[key])
        if times:
            median = times[len(times) // 2]
            pct    = f"{touch_counts[key]/n*100:.0f}%"
            lines.append(
                f"  {label}: touched in `{pct}` of legs, median `{median}` bars")
        else:
            lines.append(f"  {label}: _no touches recorded yet_")

    return "\n".join(lines)


def format_scenario_summary():
    """
    Historical scenario forecast — buckets RESOLVED Forward Observation
    legs by their formation-time (market_phase, transition_cause,
    measured_move_bucket) and reports the empirical P(REVERSED) per
    bucket via the same Beta-Binomial posterior used for tier evidence
    (_beta_posterior / EVIDENCE_MIN_N). Called by `/legobs scenario`.

    transition_cause (Market Thesis Engine, per chat) was added onto the
    EXISTING (market_phase, measured_move_bucket) key here rather than
    given its own separate lookup/engine — this IS the Similarity Engine
    the Market Thesis Engine chat asked for; it already existed under a
    different name. Same EVIDENCE_MIN_N gate, same "skip untagged
    records rather than guess" discipline as phase/ext_bucket already
    used — records logged before transition_cause was added simply won't
    match any bucket here until enough NEW resolved legs accumulate.

    This is the thing that actually answers "Scenario A: reversal, X%,
    N=Y" — and deliberately does NOT print a number for any bucket with
    fewer than EVIDENCE_MIN_N resolved legs. An unfilled bucket is
    reported as "N=k — below EVIDENCE_MIN_N, not shown," not a guess.

    Descriptive research output only — no tier reads this, nothing here
    gates a live decision.

    ⚠ Same serial-correlation caveat _beta_posterior() already carries for
    tier evidence applies here, arguably more so: legs opened during the
    same trending run tend to share phase, cause, AND extension bucket,
    so consecutive samples in the same bucket are correlated draws, not
    independent ones. MORE bucket dimensions (phase × cause × extension)
    means more ways to slice a still-limited, still-correlated sample —
    this does not fix that problem, it just gives more buckets to misread
    with false confidence. Treat every percentage below as directional,
    and cross-check against compute_tier_sharpe()/the Markov transition
    matrix before trusting it with anything beyond curiosity.
    """
    try:
        with open(LEG_OBS_LOG_FILE, "r") as f:
            records = [
                rec for rec in (json.loads(ln) for ln in f if ln.strip())
                if rec.get("methodology_version") == LEG_OBS_METHODOLOGY_VERSION
            ]
    except FileNotFoundError:
        return "🔭 *Scenario Forecast*\n_No resolved legs yet._"
    except Exception as e:
        return f"🔭 _Error reading leg log: {e}_"

    buckets = {}
    tagged_n = 0
    for r in records:
        fs = r.get("formation_state", {})
        phase = fs.get("market_phase")
        ext_bucket = fs.get("measured_move_bucket")
        cause = fs.get("transition_cause")
        # Older records predate this tagging — skip rather than guess.
        if phase is None or ext_bucket is None or cause is None:
            continue
        tagged_n += 1
        key = (phase, cause, ext_bucket)
        b = buckets.setdefault(key, {"wins": 0, "losses": 0, "n": 0,
                                      "continued": 0, "reversed": 0, "invalidated": 0})
        b["n"] += 1
        fate = r.get("fate")
        if fate == "REVERSED":
            b["wins"] += 1
            b["reversed"] += 1
        else:
            b["losses"] += 1
            if fate == "CONTINUED":
                b["continued"] += 1
            elif fate == "INVALIDATED":
                b["invalidated"] += 1

    if not buckets:
        return (
            "🔭 *Scenario Forecast*\n"
            "_No resolved legs tagged with phase/cause/extension yet — "
            "transition_cause was only just added; give it time to "
            "accumulate on top of existing phase/extension tagging._"
        )

    lines = [
        "🔭 *Scenario Forecast — P(Reversal) by Phase + Cause + Leg Extension*",
        "─────────────────────",
        f"Tagged resolved legs: `{tagged_n}` (of `{len(records)}` total resolved)",
        "",
        "_Each bucket also shows the Market Thesis Engine's STATED prediction for that_ "
        "_(phase, cause) pair (per chat — the actual-vs-expected comparison), so the "
        "empirical fate split sits right next to what was predicted. This is a "
        "side-by-side, not a scored hit/miss — several of the stated predictions are "
        "deliberately disjunctive ('CHoCH or range') and judging which counts as a "
        "'hit' would just be a new arbitrary rule; read the two together yourself._",
        "",
    ]
    for (phase, cause, ext_bucket), b in sorted(buckets.items(), key=lambda kv: -kv[1]["n"]):
        n = b["n"]
        predicted = EXPECTED_NEXT_EVENT_MAP.get((phase, cause))
        header = f"  `{phase}` / `{cause}` / `{ext_bucket}`:"
        if predicted:
            header += f" _predicted: \"{predicted}\"_"
        lines.append(header)
        if n < EVIDENCE_MIN_N:
            lines.append(f"    N=`{n}` — _below EVIDENCE_MIN_N ({EVIDENCE_MIN_N}), not shown_")
            continue
        post_mean, ci_lo, ci_hi = _beta_posterior(b["wins"], b["losses"])
        lines.append(
            f"    N=`{n}` — P(reversal) `{post_mean*100:.0f}%` "
            f"(95% CI `{ci_lo*100:.0f}\u2013{ci_hi*100:.0f}%`)"
        )
        lines.append(
            f"    fate split: CONTINUED=`{b['continued']}` REVERSED=`{b['reversed']}` "
            f"INVALIDATED=`{b['invalidated']}`"
        )
    lines.append("")
    lines.append("_CI assumes independent draws — see docstring caveat on serial correlation._")
    return "\n".join(lines)


# =========================================================================
# VALIDATION ENGINE — Calibration check for all probability gates
# =========================================================================
# Standard calibration check (weather-forecaster technique): does "X%
# probability" actually happen X% of the time over many instances?
#
# Three probability sources covered, one report:
#   1. Markov transition model  — P(next regime | current regime)
#      calibration_predictions[] in markov_transitions.json, written by
#      record_markov_transition() above.
#   2. Conviction gate          — implied P(WIN) by conviction band
#      derived from EXP7_TIER_ATR resolved records (conviction_score).
#   3. Evidence posterior       — Bayesian P(WIN) per tier vs actual rate
#      derived from EXP7_TIER_ATR resolved records (_beta_posterior).
#
# Read-only annotation — never touches fired/decision/sizing/state.
# Triggered by /calibration Telegram command.
# =========================================================================

def _calibration_bucket_label(prob):
    """Map a float probability to its reliability-diagram bucket label."""
    for lo, hi, label in CALIBRATION_BUCKETS:
        if lo <= prob < hi:
            return label
    return "90%+"


def format_calibration_report():
    """
    Reliability report across all probability gates.

    For each stated-probability bucket, shows the empirical frequency over
    all recorded instances. A well-calibrated system sits near the diagonal:
    stated ≈ actual. Deviations reveal overconfidence (actual < stated) or
    underconfidence (actual > stated).

    Informational only — never alters any live decision. /calibration
    """
    lines = [
        "🎯 *Validation Engine — Calibration Report*",
        "─────────────────────",
        "_Does 'X% probability' actually happen X% of the time?_",
        "_Stated probability bucket → empirical frequency. Well-calibrated_",
        "_systems sit on the diagonal (70% stated → 70% actual)._",
        "",
    ]

    # ── 1. Markov transition calibration ─────────────────────────────────
    try:
        markov_data = load_markov_data()
        cal_data = markov_data.get("calibration_predictions", [])
        n_cal = len(cal_data)
        if n_cal < CALIBRATION_MIN_N:
            lines.append(
                f"*1. Markov (regime transitions):* "
                f"_`{n_cal}` predictions recorded — need `{CALIBRATION_MIN_N}+`_"
            )
        else:
            buckets = {}
            for entry in cal_data:
                lbl = _calibration_bucket_label(entry.get("stated_prob", 0.0))
                b = buckets.setdefault(lbl, {"n": 0, "hits": 0})
                b["n"] += 1
                b["hits"] += int(entry.get("actual_happened", False))

            lines.append(
                f"*1. Markov — regime transition calibration* "
                f"(total predictions: `{n_cal}`)"
            )
            lines.append(
                "_`stated%` → `actual%` (n). "
                "Perfect forecast lies on the diagonal._"
            )
            any_bucket_shown = False
            for _, _, lbl in CALIBRATION_BUCKETS:
                b = buckets.get(lbl)
                if b and b["n"] >= CALIBRATION_MIN_N:
                    actual = b["hits"] / b["n"] * 100
                    lines.append(f"  `{lbl}` → `{actual:.0f}%` (n=`{b['n']}`)")
                    any_bucket_shown = True
            if not any_bucket_shown:
                lines.append("  _No bucket has 10+ predictions yet_")

            # Brier score: mean squared error (0 = perfect, 0.25 = random)
            brier = sum(
                (e["stated_prob"] - int(e.get("actual_happened", False))) ** 2
                for e in cal_data
            ) / n_cal
            lines.append(
                f"  Brier score: `{brier:.3f}` "
                f"(0.00=perfect, 0.160=uniform five-state baseline)"
            )
        lines.append("")
    except Exception as e:
        lines.append(f"*1. Markov:* _error — {e}_\n")

    # ── 2. Conviction gate calibration ───────────────────────────────────
    try:
        exp7_records = _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
        fired = [
            r for r in exp7_records
            if r.get("tags", {}).get("would_have_fired_live") is True
        ]
        n_fired = len(fired)
        if n_fired < CALIBRATION_MIN_N:
            lines.append(
                f"*2. Conviction gate:* "
                f"_`{n_fired}` resolved fired signals — need `{CALIBRATION_MIN_N}+`_"
            )
        else:
            def _score_to_band(score):
                for floor, label, *_ in CONVICTION_MANAGEMENT_BANDS:
                    if score >= floor:
                        return label
                return "CONSERVATIVE"

            band_data = {}
            for r in fired:
                score = r.get("tags", {}).get("conviction_score")
                if score is None:
                    continue
                band = _score_to_band(score)
                b = band_data.setdefault(band, {"n": 0, "wins": 0})
                b["n"] += 1
                if r.get("r_achieved", -1.0) > 0:
                    b["wins"] += 1

            lines.append(
                f"*2. Conviction gate calibration* "
                f"(n=`{n_fired}` EXP7 fired signals)"
            )
            lines.append(
                "_A calibrated gate: FULL bands win more often than NORMAL,_"
            )
            lines.append(
                "_NORMAL more often than CONSERVATIVE._"
            )
            for _, label, *_ in CONVICTION_MANAGEMENT_BANDS:
                b = band_data.get(label)
                if not b:
                    lines.append(f"  `{label}`: _no data_")
                    continue
                if b["n"] < 3:
                    lines.append(f"  `{label}`: n=`{b['n']}` (need 3+ to report)")
                    continue
                actual_wr = b["wins"] / b["n"]
                post_mean, ci_lo, ci_hi = _beta_posterior(
                    b["wins"], b["n"] - b["wins"])
                lines.append(
                    f"  `{label}` (n=`{b['n']}`): "
                    f"actual `{actual_wr*100:.0f}%` | "
                    f"posterior `{post_mean*100:.0f}%` "
                    f"CI [`{ci_lo*100:.0f}–{ci_hi*100:.0f}%`]"
                )
        lines.append("")
    except Exception as e:
        lines.append(f"*2. Conviction gate:* _error — {e}_\n")

    # ── 3. Evidence posterior calibration ────────────────────────────────
    # FIX (audit #3, per chat): this used to compare a tier's posterior
    # mean against the raw win rate computed from the SAME completed
    # sample — tautological, since a posterior mean always differs from
    # the sample mean it was derived from purely by prior-shrinkage math,
    # regardless of whether the tier has a real edge. Now compares each
    # trade's FROZEN pre-outcome prediction (predicted_win_prob, stamped
    # at setup time by _tier_prior_posterior — see experiment_7_tier_atr_
    # mirror) against what actually happened to THAT trade, bucketed the
    # same reliability-diagram way as #1 above.
    try:
        exp7_records = _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
        lines.append("*3. Evidence posterior — frozen prediction vs actual:*")
        lines.append(
            "_Each trade's predicted win-prob was frozen BEFORE it_"
        )
        lines.append(
            "_resolved (no reuse of the same sample for both numbers)._"
        )
        any_tier_shown = False
        for tier_label in TIER_PRIORITY:
            recs = [
                r for r in exp7_records
                if r.get("variant") == tier_label
                and r.get("tags", {}).get("predicted_win_prob") is not None
            ]
            n = len(recs)
            if n < CALIBRATION_MIN_N:
                lines.append(
                    f"  `{tier_label}`: `{n}` frozen predictions — need `{CALIBRATION_MIN_N}+`"
                )
                continue

            buckets = {}
            for r in recs:
                stated = r["tags"]["predicted_win_prob"]
                lbl = _calibration_bucket_label(stated)
                b = buckets.setdefault(lbl, {"n": 0, "hits": 0})
                b["n"] += 1
                b["hits"] += int(r.get("r_achieved", 0.0) > 0)

            brier = sum(
                (r["tags"]["predicted_win_prob"] - int(r.get("r_achieved", 0.0) > 0)) ** 2
                for r in recs
            ) / n

            lines.append(f"  `{tier_label}` (n=`{n}`, Brier `{brier:.3f}`):")
            shown = False
            for _, _, lbl in CALIBRATION_BUCKETS:
                b = buckets.get(lbl)
                if b and b["n"] >= CALIBRATION_MIN_N:
                    actual = b["hits"] / b["n"] * 100
                    lines.append(f"    `{lbl}` → `{actual:.0f}%` (n=`{b['n']}`)")
                    shown = True
            if not shown:
                lines.append("    _no bucket has 10+ predictions yet_")
            any_tier_shown = True
        if not any_tier_shown:
            lines.append(
                f"  _No tier has {CALIBRATION_MIN_N}+ frozen predictions yet — "
                f"these only start accumulating from setups logged AFTER this fix_"
            )
        lines.append("")
    except Exception as e:
        lines.append(f"*3. Evidence posterior:* _error — {e}_\n")

    lines.append(
        "_Calibration data accumulates over time. Small samples produce_\n"
        "_wide confidence intervals, not conclusions. Check back after_\n"
        "_several hundred resolved signals._"
    )
    lines.append("_Informational only — never overrides any live decision._")
    return "\n".join(lines)


# =========================================================================
# FAILURE INVESTIGATION BUREAU
# =========================================================================
# Per chat. Opens a "case" whenever a signal that WOULD have fired live
# (EXP7_TIER_ATR, tags.would_have_fired_live=True) resolves as a loss —
# i.e. the live engine expected a WIN and got one, and reality diverged.
# Investigates WHY by comparing this trade's formation tags against the
# SAME tier's own historical winners/losers, and forwards a research
# QUESTION to the rest of MIN — never a strategy change, never touches
# fired/decision/sizing.
#
# Steps 1-4 and 7, as originally proposed, are implemented close to as
# specified:
#   1. Open a Case       — open_failure_case() below.
#   2. Expectation vs Reality — "expected WIN" (would_have_fired_live=True)
#      vs "observed {LOSS/TIMEOUT_LOSS}" is the discrepancy; no fabrication
#      needed, it's just the tag that's already there.
#   3. Investigate why    — _compare_tag() checks each formation tag
#      against this tier's own historical winners/losers.
#   4. Compare against historical cases — same-tier EXP7 records, split
#      by outcome, read from the existing permanent log.
#
# Steps 5-6 are DELIBERATELY NOT built as originally proposed. The
# original spec wanted a ranked percentage ("+31% for OB Freshness") and
# a fabricated confidence figure ("82%"). Both were flagged in chat as
# fabricated precision: with the sample sizes this bot will realistically
# see, that's a number wearing a costume, not a measurement, and it would
# mislead more than it would help. What's built instead:
#   5. Rank by RAW gap (winners' mean/mode vs losers' mean/mode, real
#      units — pips, count, percentile), gated by CASE_MIN_GROUP_N in
#      BOTH groups. No normalization into a percentage, no invented
#      "explained variance."
#   6. Conclusion is a plain sentence naming the single largest measured
#      difference with its raw numbers and sample sizes — explicitly
#      correlational, no "confidence: X%," no causal "root cause"
#      language. If nothing clears the min-N gate, it says so plainly
#      instead of inventing an answer.
#
# The probability-calibration idea appended to the original proposal
# ("was this the 22% outcome, or does the model systematically
# overestimate this market state?") is answered by DEFERRING to the
# Validation Engine above, not by computing a per-case p-value from n=1
# — a single loss can never distinguish normal variance from systematic
# miscalibration; only the aggregate bucket check in
# format_calibration_report() can. This case report just surfaces
# whatever that tier's aggregate calibration currently shows.
# =========================================================================

def _read_failure_cases(limit=None):
    """Reads the PERMANENT failure_case_log.jsonl. Pure, safe to call
    from a Telegram command. Oldest-first; pass limit for the most
    recent N (applied after reading, same pattern as _read_shadow_trade_log)."""
    cases = []
    try:
        with open(FAILURE_CASE_LOG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    case = json.loads(line)
                    if case.get("methodology_version") == SHADOW_METHODOLOGY_VERSION:
                        cases.append(case)
                except Exception:
                    continue
    except FileNotFoundError:
        return cases
    if limit:
        cases = cases[-limit:]
    return cases


def _append_failure_case(case):
    """Permanently appends one case to FAILURE_CASE_LOG_FILE. Retries the
    same as SHADOW_TRADE_LOG_FILE/LEG_OBS_LOG_FILE (per chat, "safety
    against duplication"). Returns True/False. No re-queue here on
    failure, unlike the other two logs — a failure case is a one-shot
    investigative note about a trade whose own permanent record is
    already safely written by the time this runs (open_failure_case is
    only ever called after _append_shadow_trade_log succeeds), so there's
    no "pending" slot to put this back into. A failed write here means
    that one note is lost, not the trade data it was about — the caller
    prints on failure so it's visible in the console, same as before."""
    line = json.dumps(case, default=_json_default) + "\n"
    last_error = None
    for attempt in range(1, SHADOW_TRADE_LOG_WRITE_RETRIES + 1):
        try:
            with open(FAILURE_CASE_LOG_FILE, "a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as e:
            last_error = e
            if attempt < SHADOW_TRADE_LOG_WRITE_RETRIES:
                time.sleep(SHADOW_TRADE_LOG_WRITE_RETRY_DELAY_SEC)
    print(f"[CASE LOG ERROR] gave up after {SHADOW_TRADE_LOG_WRITE_RETRIES} "
          f"attempts: {last_error}")
    return False


def _next_case_number():
    """Sequential, human-readable case numbers ("Case #441") instead of
    the raw uuid trade_id — read once from the existing log length."""
    return len(_read_failure_cases()) + 1


def _is_numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _cohens_d(vals_a, vals_b):
    """Cohen's d — standardized mean difference, in pooled-SD units.
    Standard published statistic (Cohen, 1988), not invented here. This
    is what lets a numeric tag's gap be judged relative to how much
    that tag naturally varies: a 3.5-pip gap on a tag with huge natural
    spread is a SMALLER effect than a 0.3-pip gap on a tightly
    clustered one, even though 3.5 > 0.3 in raw units. Returns None
    when a pooled SD can't be computed (fewer than 2 values in either
    group, or both groups perfectly constant) rather than divide by
    zero — caller must treat None as "no effect size available", not
    as zero effect.
    """
    n_a, n_b = len(vals_a), len(vals_b)
    if n_a < 2 or n_b < 2:
        return None
    mean_a = sum(vals_a) / n_a
    mean_b = sum(vals_b) / n_b
    var_a = sum((v - mean_a) ** 2 for v in vals_a) / (n_a - 1)
    var_b = sum((v - mean_b) ** 2 for v in vals_b) / (n_b - 1)
    pooled_var = ((n_a - 1) * var_a + (n_b - 1) * var_b) / (n_a + n_b - 2)
    pooled_std = pooled_var ** 0.5
    if pooled_std < 1e-9:
        return None
    return (mean_a - mean_b) / pooled_std


def _cohens_h(p_a, p_b):
    """Cohen's h — the proportion-difference analogue of Cohen's d
    (Cohen, 1988), via the arcsine variance-stabilizing transform. This
    is what actually lets a CATEGORICAL tag's mismatch-rate gap share
    one ranked list with numeric tags' Cohen's d: both conventionally
    read on the same small=0.2 / medium=0.5 / large=0.8 scale, so a
    0.30 proportion gap and a numeric gap can finally be compared on a
    common footing instead of just on raw, incommensurate units.
    """
    phi_a = 2 * math.asin(math.sqrt(min(max(p_a, 0.0), 1.0)))
    phi_b = 2 * math.asin(math.sqrt(min(max(p_b, 0.0), 1.0)))
    return phi_a - phi_b


def _compare_tag(tag_key, this_value, winners, losers, min_n=CASE_MIN_GROUP_N):
    """
    Step 3/4/5 core: compares ONE formation tag between this tier's
    historical winners and losers. Returns None if the tag is missing,
    non-comparable, or either group has fewer than min_n VALUES present
    for this tag (not just min_n records overall — a tag that's usually
    None shouldn't borrow another tag's sample size).

    Numeric tags -> compares means, gap = abs difference in raw units.
    Categorical/bool tags -> compares modal value, gap = mismatch-rate
    difference (still a real proportion of real counts, not a percentage
    invented from a single trade).

    Never returns a "confidence" or "% explained" figure — see section
    docstring above for why.
    """
    w_vals = [r["tags"].get(tag_key) for r in winners if r.get("tags", {}).get(tag_key) is not None]
    l_vals = [r["tags"].get(tag_key) for r in losers if r.get("tags", {}).get(tag_key) is not None]
    if len(w_vals) < min_n or len(l_vals) < min_n or this_value is None:
        return None

    if _is_numeric(this_value) and all(_is_numeric(v) for v in w_vals + l_vals):
        w_mean = sum(w_vals) / len(w_vals)
        l_mean = sum(l_vals) / len(l_vals)
        gap = abs(w_mean - l_mean)
        effect_size = _cohens_d(w_vals, l_vals)
        return {
            "tag": tag_key, "type": "numeric",
            "this_value": round(this_value, 2),
            "winners_mean": round(w_mean, 2), "losers_mean": round(l_mean, 2),
            "gap": round(gap, 2),
            "effect_size": round(effect_size, 3) if effect_size is not None else None,
            "winners_n": len(w_vals), "losers_n": len(l_vals),
            "closer_to": "losers" if abs(this_value - l_mean) < abs(this_value - w_mean) else "winners",
        }
    else:
        def _mode(vals):
            counts = {}
            for v in vals:
                counts[v] = counts.get(v, 0) + 1
            return max(counts.items(), key=lambda kv: kv[1])
        w_mode_val, w_mode_n = _mode(w_vals)
        l_mode_val, l_mode_n = _mode(l_vals)
        w_match_rate = sum(1 for v in w_vals if v == this_value) / len(w_vals)
        l_match_rate = sum(1 for v in l_vals if v == this_value) / len(l_vals)
        gap = abs(w_match_rate - l_match_rate)
        effect_size = _cohens_h(w_match_rate, l_match_rate)
        return {
            "tag": tag_key, "type": "categorical",
            "this_value": this_value,
            "winners_mode": w_mode_val, "winners_mode_share": round(w_mode_n / len(w_vals), 2),
            "losers_mode": l_mode_val, "losers_mode_share": round(l_mode_n / len(l_vals), 2),
            "gap": round(gap, 2),
            "effect_size": round(effect_size, 3) if effect_size is not None else None,
            "winners_n": len(w_vals), "losers_n": len(l_vals),
            "closer_to": "losers" if l_match_rate > w_match_rate else "winners",
        }


def investigate_failure_case(tier_number, formation_tags, exclude_trade_id=None):
    """
    Steps 3-6. Reads this tier's OWN permanent EXP7_TIER_ATR history
    (tier-isolated, same "jury, not judge" isolation as compute_evidence()
    — Tier 1 only ever learns from Tier 1), splits into winners/losers,
    compares every formation tag on this trade against both groups, and
    ranks the comparisons that cleared CASE_MIN_GROUP_N by raw gap size.

    Returns {"comparisons": [...], "conclusion": str, "n_winners": int,
    "n_losers": int} — comparisons is empty and conclusion says so
    plainly if nothing has enough history yet. Never raises.
    """
    records = [
        r for r in _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
        if r.get("tier_number") == tier_number and r.get("trade_id") != exclude_trade_id
    ]
    winners = [r for r in records if r.get("r_achieved", 0.0) > 0]
    losers  = [r for r in records if r.get("r_achieved", 0.0) <= 0]

    comparisons = []
    for tag_key, this_value in (formation_tags or {}).items():
        if tag_key in CASE_EXCLUDED_TAGS:
            continue
        cmp_result = _compare_tag(tag_key, this_value, winners, losers)
        if cmp_result is not None:
            comparisons.append(cmp_result)

    # Step 5: rank by EFFECT SIZE (Cohen's d for numeric tags, Cohen's h
    # for categorical — both real, standard statistics that read on the
    # SAME conventional scale: ~0.2 small, ~0.5 medium, ~0.8 large).
    # BUG FIX: this used to sort by raw `gap`, which mixes a numeric
    # tag's native-unit gap (pips, degrees, whatever) with a categorical
    # tag's proportion gap (0-1) in one list — different units, so
    # whichever tag happened to have larger raw units always "won" the
    # ranking regardless of which one actually separates winners from
    # losers. `gap` is still shown to the reader in its native unit
    # below; only the SORT KEY changed. A comparison with no computable
    # effect size (pooled SD exactly 0 — the tag never varies) sorts
    # last, not first: "cannot measure a difference" isn't "measures a
    # large one."
    comparisons.sort(
        key=lambda c: abs(c["effect_size"]) if c.get("effect_size") is not None else -1,
        reverse=True,
    )

    # Step 6: plain conclusion, correlational only, no confidence figure.
    if not comparisons:
        conclusion = (
            f"Not enough resolved history yet to identify a driving factor "
            f"(need {CASE_MIN_GROUP_N}+ winners AND {CASE_MIN_GROUP_N}+ losers "
            f"per tag; have {len(winners)} winners / {len(losers)} losers total)."
        )
    else:
        top = comparisons[0]
        es_note = f", effect size `{top['effect_size']:+.2f}`" if top.get("effect_size") is not None else ""
        if top["type"] == "numeric":
            conclusion = (
                f"Largest measured effect: `{top['tag']}` — losers averaged "
                f"`{top['losers_mean']}` vs winners' `{top['winners_mean']}` "
                f"(this trade: `{top['this_value']}`, n={top['winners_n']}W/{top['losers_n']}L{es_note}). "
                f"Correlational, not causal — worth researching, not acting on alone."
            )
        else:
            conclusion = (
                f"Largest measured effect: `{top['tag']}` — losers were usually "
                f"`{top['losers_mode']}` ({int(top['losers_mode_share']*100)}% of losers), "
                f"winners usually `{top['winners_mode']}` ({int(top['winners_mode_share']*100)}% "
                f"of winners); this trade was `{top['this_value']}` "
                f"(n={top['winners_n']}W/{top['losers_n']}L{es_note}). "
                f"Correlational, not causal — worth researching, not acting on alone."
            )

    return {
        "comparisons": comparisons,
        "conclusion": conclusion,
        "n_winners": len(winners),
        "n_losers": len(losers),
    }


def open_failure_case(setup, outcome, r_achieved, now_utc):
    """
    Steps 1-2. Called from update_pending_shadow_setups() at the exact
    moment an EXP7_TIER_ATR setup resolves. Only opens a case when the
    live engine actually expected a win — tags.would_have_fired_live is
    True — and reality diverged (outcome is a loss). A resolved loss on a
    setup that would NOT have fired live isn't a case: nothing was
    expected to happen, so there's no discrepancy to explain.

    Never raises — errors are caught by the caller (same "research never
    breaks the live bot" contract as the rest of MIN). Returns the case
    dict, or None if this resolution doesn't qualify for a case.
    """
    if setup.get("experiment") != "EXP7_TIER_ATR":
        return None
    if not setup.get("tags", {}).get("would_have_fired_live"):
        return None
    if r_achieved > 0:
        return None   # expected WIN, got WIN — no discrepancy, no case

    tier_number = setup.get("tier_number")
    formation_tags = dict(setup.get("tags") or {})

    investigation = investigate_failure_case(
        tier_number, formation_tags, exclude_trade_id=setup.get("id"))

    case = {
        "case_number": _next_case_number(),
        "methodology_version": SHADOW_METHODOLOGY_VERSION,
        "id": setup.get("id"),
        "opened_at": now_utc.isoformat(),
        "tier_label": setup.get("variant"),
        "tier_number": tier_number,
        "direction": setup.get("direction"),
        "expected": "WIN",
        "observed": outcome,
        "r_achieved": round(r_achieved, 2),
        "atr_pips": setup.get("atr_pips"),
        "bars_open": setup.get("bars_open"),
        "conviction_score": formation_tags.get("conviction_score"),
        "predicted_win_prob": formation_tags.get("predicted_win_prob"),
        "comparisons": investigation["comparisons"][:5],   # top 5, avoid unbounded growth
        "conclusion": investigation["conclusion"],
        "n_winners_compared": investigation["n_winners"],
        "n_losers_compared": investigation["n_losers"],
    }
    _append_failure_case(case)
    return case


def format_case_note(case):
    """
    Step 7. Telegram rendering of one case — a research QUESTION, not a
    strategy change. Sent right after the case is opened.
    """
    lines = [
        f"🕵️ *Failure Investigation — Case #{case['case_number']}*",
        "─────────────────────",
        f"Tier: `{case['tier_label']}` | Direction: `{case['direction']}`",
        f"Expected: `{case['expected']}` → Observed: `{case['observed']}` "
        f"(`{case['r_achieved']}R`)",
    ]
    if case.get("predicted_win_prob") is not None:
        lines.append(
            f"Frozen predicted win-prob at signal time: "
            f"`{case['predicted_win_prob']*100:.0f}%` — see /calibration for "
            f"whether this tier is systematically over/under-confident, "
            f"rather than reading this one loss as evidence either way."
        )
    lines.append("")
    lines.append(f"*Conclusion:* {case['conclusion']}")
    if len(case.get("comparisons", [])) > 1:
        lines.append("")
        lines.append("_Other measured differences (ranked by effect size, largest first):_")
        # AUDIT FIX: `gap` is in each tag's OWN native unit (pips/degrees
        # for numeric, a 0-1 mismatch-rate for categorical) — a numeric
        # tag's gap and a categorical tag's gap are NOT the same scale,
        # so `gap` alone can't tell the reader why one line ranks above
        # another. The list IS already sorted correctly by effect_size
        # (Cohen's d / Cohen's h, both on the same small=0.2/medium=0.5/
        # large=0.8 convention — see investigate_failure_case()); this
        # just makes that visible instead of showing only the native-unit
        # gap and leaving the ranking unexplained. `type` is shown too so
        # it's never ambiguous which formula produced the number.
        for c in case["comparisons"][1:4]:
            es = f"{c['effect_size']:+.2f}" if c.get("effect_size") is not None else "n/a"
            if c["type"] == "numeric":
                lines.append(
                    f"  `{c['tag']}` (numeric, Cohen's d `{es}`): this=`{c['this_value']}` "
                    f"W-avg=`{c['winners_mean']}` L-avg=`{c['losers_mean']}` "
                    f"(gap `{c['gap']}` native units)"
                )
            else:
                lines.append(
                    f"  `{c['tag']}` (categorical, Cohen's h `{es}`): this=`{c['this_value']}` "
                    f"W-mode=`{c['winners_mode']}` L-mode=`{c['losers_mode']}` "
                    f"(gap `{c['gap']}` mismatch-rate)"
                )
    lines.append("")
    lines.append(
        "_Recommendation: research the top factor above — this is a "
        "question for the rest of MIN, not a filter change._"
    )
    return "\n".join(lines)


def format_recent_cases(n=3):
    """/cases Telegram command — last n case summaries, most recent first."""
    cases = _read_failure_cases()
    if not cases:
        return "🕵️ *Failure Investigation Bureau*\n_No cases opened yet._"
    recent = list(reversed(cases[-n:]))
    lines = [
        "🕵️ *Failure Investigation Bureau — Recent Cases*",
        "─────────────────────",
        f"Total cases: `{len(cases)}`",
        "",
    ]
    for case in recent:
        lines.append(
            f"*Case #{case['case_number']}* — `{case['tier_label']}` "
            f"`{case['observed']}` (`{case['r_achieved']}R`)"
        )
        lines.append(f"  {case['conclusion']}")
        lines.append("")
    lines.append("_Send /case <number> for full detail on one case._")
    return "\n".join(lines)


def format_case_detail(case_number):
    """/case <n> Telegram command — full detail on one specific case."""
    cases = _read_failure_cases()
    match = next((c for c in cases if c.get("case_number") == case_number), None)
    if match is None:
        return f"🕵️ _Case #{case_number} not found._"
    return format_case_note(match)

def compute_atr_suitability(band_width=ATR_SUITABILITY_BAND_WIDTH_PIPS):
    """
    Reads the PERMANENT shadow_trade_log.jsonl and buckets EXP7_TIER_ATR
    records by (tier_number, ATR band) -> {n, win_rate, avg_r}. This is
    the exact table the friend described: rows = ATR band, columns =
    Tier 1/2/3 win rate. Pure function — safe to call from a Telegram
    command or an offline analysis script.
    """
    table = {}  # {tier_number: {band_label: {"n":, "wins":, "sum_r":}}}
    try:
        with open(SHADOW_TRADE_LOG_FILE, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return table

    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("methodology_version") != SHADOW_METHODOLOGY_VERSION:
            continue
        if rec.get("experiment") != "EXP7_TIER_ATR":
            continue
        tier_number = rec.get("tier_number")
        atr_pips = rec.get("atr_pips")
        if tier_number is None or atr_pips is None:
            continue

        band_floor = int(atr_pips // band_width) * band_width
        band_label = f"{band_floor:.0f}-{band_floor + band_width:.0f}"

        tier_table = table.setdefault(tier_number, {})
        bucket = tier_table.setdefault(band_label, {"n": 0, "wins": 0, "sum_r": 0.0})
        bucket["n"] += 1
        r = rec.get("r_achieved", 0.0)
        if r > 0:
            bucket["wins"] += 1
        bucket["sum_r"] += r

    return table


def format_atr_suitability_table(band_width=ATR_SUITABILITY_BAND_WIDTH_PIPS, min_n=1):
    """Human-readable ATR-band x Tier win-rate table — see /atrbands.
    Cells show the Beta-Binomial posterior win rate (shrunk toward the
    BAYES_PRIOR_ALPHA/BETA prior), not raw wins/n — a thin band with n=4
    no longer gets to print a headline 100% or 0%."""
    table = compute_atr_suitability(band_width)
    if not table:
        return None

    all_bands = sorted(
        {band for tier_table in table.values() for band in tier_table},
        key=lambda b: float(b.split("-")[0]),
    )
    lines = ["📊 *ATR Suitability — Tier x ATR band (posterior win rate)*\n"]
    header = "Band(p)  | " + " | ".join(f"Tier {t}" for t in sorted(table.keys()))
    lines.append(f"`{header}`")
    for band in all_bands:
        row = [f"{band:>7}"]
        for t in sorted(table.keys()):
            bucket = table[t].get(band)
            if bucket is None or bucket["n"] < min_n:
                row.append("   —  ")
            else:
                wins = bucket["wins"]
                losses = bucket["n"] - wins
                post_mean, _, _ = _beta_posterior(wins, losses)
                row.append(f"{post_mean * 100:5.0f}%")
        lines.append("`" + " | ".join(row) + "`")

    lines.append("")
    for t in sorted(table.keys()):
        total_n = sum(b["n"] for b in table[t].values())
        total_r = sum(b["sum_r"] for b in table[t].values())
        lines.append(f"Tier {t}: {total_n} logged, avg R {total_r/total_n:+.2f}" if total_n else f"Tier {t}: 0 logged")
    return "\n".join(lines)


# ---- orchestrator -----------------------------------------------------------

def _evidence_similarity(current_tags, historical_tags, keys):
    """Fraction of `keys` on which current_tags and historical_tags agree
    (both present AND equal). A key missing from either side counts as a
    mismatch, not a skip — an older log line without a key can't be
    assumed to match it. PURE."""
    if not keys:
        return 0.0
    matches = sum(
        1 for k in keys
        if k in current_tags and k in historical_tags and current_tags[k] == historical_tags[k]
    )
    return matches / len(keys)


def compute_evidence(tier_label, breakdown, min_n=EVIDENCE_MIN_N):
    """
    Phase 4 — Evidence Engine. PURE, read-only, informational ONLY.

    "Jury, not judge" (per chat): looks up this tier's OWN permanent
    EXP7_TIER_ATR log — tier-isolated on purpose, Tier 1 only ever learns
    from Tier 1 — keeps records whose structural facts agree with
    `breakdown` on at least EVIDENCE_MATCH_THRESHOLD of that tier's
    TIER_EVIDENCE_KEYS, and reports what actually happened to them.

    Returns None (stays SILENT — the "sleeping pathway") if fewer than
    `min_n` similar RESOLVED trades exist yet. There is no code path from
    this function back into `fired`, `decision`, `classify_conviction`,
    or sizing — callers only ever use its output to annotate a message,
    never to decide anything. If that ever changes, it stops being a
    jury and starts being a second, un-validated rule engine — see chat
    history for why that's the one thing this must never become.
    """
    keys = TIER_EVIDENCE_KEYS.get(tier_label)
    if not keys or not breakdown:
        return None

    tier_number = TIER_NUMBER.get(tier_label)
    records = _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
    similar = [
        r for r in records
        if r.get("tier_number") == tier_number
        and _evidence_similarity(breakdown, r.get("tags") or {}, keys) >= EVIDENCE_MATCH_THRESHOLD
    ]

    n = len(similar)
    if n < min_n:
        return None  # dormant — not enough resolved history yet, say nothing at all

    r_values = [float(r.get("r_achieved", 0.0)) for r in similar]
    wins = sum(1 for r in r_values if r > 0)
    losses = n - wins
    avg_r = sum(r_values) / n

    # Bayesian layer: Beta-Binomial posterior over this tier's TRUE win
    # rate, not the raw wins/n point estimate above — same math as
    # format_atr_suitability_table() via _beta_posterior(). This is what
    # gets reported; win_rate is kept in the dict for continuity/logging
    # but the note below leads with the posterior figure.
    post_mean, ci_lo, ci_hi = _beta_posterior(wins, losses)

    # "Let it say what it hit — 1R, 2R, 3R, or SL" (per chat): reuse the
    # same exact-bucket distribution Research Centre's block analysis
    # uses, rather than collapsing to one "most common" number. Two
    # different summaries of the same underlying data is exactly the
    # kind of clutter this whole reporting rework was meant to remove.
    buckets = outcome_distribution(similar)

    strength = "WEAK"   # unreachable in practice (n >= EVIDENCE_MIN_N == the
                         # lowest band floor, so the loop always matches) —
                         # kept as the honest fallback, not "MODERATE"
    for floor, label in EVIDENCE_STRENGTH_BANDS:
        if n >= floor:
            strength = label
            break

    return {
        "n": n,
        "win_rate": round(100 * wins / n, 1),
        "posterior_win_rate": round(100 * post_mean, 1),
        "ci_low": round(100 * ci_lo, 1),
        "ci_high": round(100 * ci_hi, 1),
        "avg_r": round(avg_r, 2),
        "outcome_buckets": buckets,
        "strength": strength,
    }


def format_evidence_note(tier_label, evidence):
    """Renders compute_evidence()'s output as a short, human-readable
    annotation — never a directive, never a decision. `evidence` may be
    None (dormant), in which case this returns None too."""
    if evidence is None:
        return None
    tier_number = TIER_NUMBER.get(tier_label, "?")
    dist_line = format_outcome_distribution(evidence["outcome_buckets"])
    return (
        f"📚 *Research Evidence (Tier {tier_number}, informational only):*\n"
        f"`{evidence['n']}` similar resolved setups this tier has seen — "
        f"posterior win rate `{evidence['posterior_win_rate']}%` "
        f"(95% CI `{evidence['ci_low']}–{evidence['ci_high']}%`), avg `{evidence['avg_r']}R`\n"
        + (dist_line + "\n" if dist_line else "")
        + f"Strength: `{evidence['strength']}` — this never overrides Rule of Law."
    )

# ---- Market Intelligence Network reporting: dashboard + drill-downs ---
# REWORK NOTE: /shadow used to print one full paragraph per experiment,
# which stopped being readable once EXPE_REJECTED_LIVE alone hit 90+
# logged setups. Now /shadow is a compact aligned table (a health check,
# not a report), and everything else lives behind drill-down commands:
#   /shadow            -> this table (renamed "Market Intelligence
#                          Network" on screen — command itself left as
#                          /shadow on purpose, so nothing you already
#                          type by habit breaks)
#   /shadow <name>     -> per-experiment detail (Experimental Lab, aliases below)
#   /shadow rejected   -> EXPE_REJECTED_LIVE-specific breakdown (Evidence & Research)
#   /shadow blocked    -> per-tier block-reason breakdown (Evidence & Research)
#   /shadow recent     -> last 10 resolved shadow trades, any experiment
#   /leaderboard       -> every experiment ranked by avg R

_SHADOW_DISPLAY_NAME = {
    "EXP1_STRUCTURE":     "EXP1 Structure",
    "EXP2_FIB":           "EXP2 Fib",
    "EXP3_POI":           "EXP3 POI",
    "EXP4_POLICY_LAB":    "EXP4 Policy Lab",
    "EXP5_ABLATION":      "EXP5 Ablation",
    "EXP6_ALT_BIAS":      "EXP6 Alt Bias",
    "EXP7_TIER_ATR":      "EXP7 Tier ATR",
    "EXP8_CISD":          "EXP8 CISD",
    "EXPE_REJECTED_LIVE": "Rejected Live",
}

# What a person is likely to type for /shadow <name> -> canonical stats key.
_SHADOW_ALIASES = {
    "exp1": "EXP1_STRUCTURE", "structure": "EXP1_STRUCTURE", "exp1_structure": "EXP1_STRUCTURE",
    "exp2": "EXP2_FIB", "fib": "EXP2_FIB", "exp2_fib": "EXP2_FIB",
    "exp3": "EXP3_POI", "poi": "EXP3_POI", "exp3_poi": "EXP3_POI",
    "exp4": "EXP4_POLICY_LAB", "policy": "EXP4_POLICY_LAB", "policylab": "EXP4_POLICY_LAB",
    "exp5": "EXP5_ABLATION", "ablation": "EXP5_ABLATION", "exp5_ablation": "EXP5_ABLATION",
    "exp6": "EXP6_ALT_BIAS", "altbias": "EXP6_ALT_BIAS", "exp6_alt_bias": "EXP6_ALT_BIAS",
    "exp7": "EXP7_TIER_ATR", "atr": "EXP7_TIER_ATR", "exp7_tier_atr": "EXP7_TIER_ATR",
    "exp8": "EXP8_CISD", "cisd": "EXP8_CISD", "exp8_cisd": "EXP8_CISD",
    "rejected": "EXPE_REJECTED_LIVE", "reject": "EXPE_REJECTED_LIVE", "live": "EXPE_REJECTED_LIVE",
}

# Rejection-reason bucketing. These substrings are copied verbatim from
# the actual TierResult.reason strings each tier returns (see the
# `reason=` grep in the module docstring area / tier evaluate() functions)
# — NOT a guessed category list. If a tier's wording changes, update the
# matching substring here too, or the bucket will silently fall through
# to "Other / Unclassified."
_REJECTION_REASON_BUCKETS = [
    ("ATR too low",                         "Market Context (ATR/session/regime)"),
    ("no order block",                      "POI — no order block"),
    ("price not currently inside the order block", "POI — price not in zone"),
    ("price not in the HTF fib pocket",     "Fib — price not in pocket"),
    ("not a CHoCH",                         "Structure — continuation, not CHoCH"),
    ("no 15M structure aligned",            "Structure — no aligned structure"),
    ("no fresh 15M BOS",                    "Structure — no fresh BOS"),
    ("conviction",                          "Conviction score below minimum"),
    ("no tier activated",                   "No tier activated at all"),
]


def _classify_context_block(tags):
    """BUG FIX (per chat, 2026-08-11): log_rejected_live_to_queue() peeks
    all three tiers on every rejected scan REGARDLESS of whether market
    context (ATR/session/post-spike) already blocked the scan before
    Rule of Law ever ran — so a context-blocked record's `tags` dict
    still carries real (but operatively moot) tier reasons alongside
    the `_blocked_by_*` flags. format_rejected_live_detail() used to
    read straight past those flags into the tier `checks` dict, so
    every ATR/session/post-spike-blocked rejection was silently
    mislabeled under whatever structural reason a tier happened to
    report at that same moment (e.g. "Structure — no fresh BOS") even
    though the tier was never actually the operative gate.

    Returns the context-block bucket label if any `_blocked_by_*` flag
    is set, else None (caller falls through to tier-reason classification).
    Precedence mirrors _scan_once()'s own atr_ok > post_spike_active >
    session_active order (see the stats["atr_invalid"] / ["regime_shift_
    skip"] / ["session_skip"] increments in scanner_live.py) so this
    reporting-only classification can never disagree with what the live
    scanner itself decided actually caused the block.

    NOTE (2026-08-11): ATR pip level is no longer a hard gate — a scan is
    only "_blocked_by_atr" now if the ATR reading itself was unusable
    (NaN/0), which should be rare. Use the _atr_very_low/_atr_low tags
    (set on every record, blocked or not) to see thin-volatility scans."""
    if tags.get("_blocked_by_atr"):
        return "Market Context — ATR data invalid"
    if tags.get("_blocked_by_post_spike"):
        return "Market Context — post-spike cooldown"
    if tags.get("_blocked_by_session"):
        return "Market Context — outside session"
    return None


def _classify_rejection_reason(reason):
    if not reason:
        return "Other / Unclassified"
    for needle, bucket in _REJECTION_REASON_BUCKETS:
        if needle.lower() in reason.lower():
            return bucket
    return "Other / Unclassified"


def _md_safe_variant(name):
    """BUG FIX (found via /shadow exp3 going silent with zero error output):
    variant names like 'order_block' or 'int_no_htf_618' get interpolated
    directly into Telegram messages sent with parse_mode='Markdown'. A
    single/odd-count literal underscore is parsed by Telegram as an
    unterminated italic tag, so the ENTIRE message is rejected by the
    Telegram API with a 'can't parse entities' error -- and
    send_telegram() catches that internally and just prints to console,
    so the command produces literally nothing on Telegram and no error
    report either. (An even underscore count, like EXP2's variants,
    happens to parse "successfully" but renders mangled/italicized --
    that's why EXP2's variant names show up with the underscores
    silently eaten in the app.) Replacing '_' with '·' for display
    removes the risk entirely without changing what the code stores."""
    return str(name).replace("_", "·")


def _classify_session(iso_ts):
    """Buckets an ISO timestamp into London / New York / Overlap /
    Off-session using the SAME SESSION_WINDOWS_UTC the live bot's
    is_active_session() already gates on — not a separately invented
    schedule."""
    try:
        dt = datetime.fromisoformat(iso_ts)
    except Exception:
        return "Unknown"
    hour = dt.hour
    london = SESSION_WINDOWS_UTC[0][0] <= hour < SESSION_WINDOWS_UTC[0][1]
    ny     = SESSION_WINDOWS_UTC[1][0] <= hour < SESSION_WINDOWS_UTC[1][1]
    if london and ny:
        return "London/NY overlap"
    if london:
        return "London"
    if ny:
        return "New York"
    return "Off-session"


def _read_shadow_trade_log(experiment=None, limit=None):
    """Reads the PERMANENT shadow_trade_log.jsonl, optionally filtered to
    one experiment. Returns a list of dicts, oldest-first. Pure, safe to
    call from a Telegram command."""
    records = []
    try:
        with open(SHADOW_TRADE_LOG_FILE, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return records
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("methodology_version") != SHADOW_METHODOLOGY_VERSION:
            continue
        if experiment is not None and rec.get("experiment") != experiment:
            continue
        records.append(rec)
    if limit:
        records = records[-limit:]
    return records


def format_shadow_summary(shadow_stats):
    """Compact aligned dashboard — a health check, not a report. Call
    periodically (e.g. every STATS_SUMMARY_EVERY scans) and on /shadow.
    Drill down with /shadow <name>, /shadow rejected, /shadow recent, or
    /leaderboard for anything this table doesn't show."""
    rows = []
    for key in _SHADOW_STATS_EXPERIMENT_KEYS:
        s = shadow_stats.get(key, _empty_experiment_stat())
        if s["logged"] == 0:
            continue
        win_rate = (s["wins"] / s["resolved"] * 100) if s["resolved"] else None
        avg_r = (s["sum_r"] / s["resolved"]) if s["resolved"] else None
        rows.append((key, s, win_rate, avg_r))

    if not rows:
        return None

    name_w = max(len(_SHADOW_DISPLAY_NAME.get(k, k)) for k, _, _, _ in rows)
    lines = ["🔬 *Market Intelligence Network*", "`" + "─" * (name_w + 28) + "`"]
    for key, s, win_rate, avg_r in rows:
        name = _SHADOW_DISPLAY_NAME.get(key, key).ljust(name_w)
        wr_str = f"WR {win_rate:3.0f}%" if win_rate is not None else "WR  — "
        avgr_str = f"AvgR {avg_r:+.2f}" if avg_r is not None else "AvgR  —  "
        lines.append(f"`{name}  {s['logged']:>3} | {s['resolved']:>3} | {wr_str} | {avgr_str}`")
    lines.append("")
    lines.append("_logged | resolved | win rate | avg R — drill down with /shadow <name>, "
                  "/shadow rejected, /shadow recent, or /leaderboard_")
    return "\n".join(lines)


def format_shadow_detail(shadow_stats, key):
    """Per-experiment drill-down. Aggregate counts come from shadow_stats
    (fast, always available); duration/session/direction/variant splits
    are derived from the permanent trade log, so they only reflect
    RESOLVED trades and only what's accumulated so far."""
    if key not in _SHADOW_STATS_EXPERIMENT_KEYS:
        return None
    s = shadow_stats.get(key, _empty_experiment_stat())
    if s["logged"] == 0:
        return f"🔬 *{_SHADOW_DISPLAY_NAME.get(key, key)}*\n_Nothing logged yet._"

    win_rate = (s["wins"] / s["resolved"] * 100) if s["resolved"] else None
    avg_r = (s["sum_r"] / s["resolved"]) if s["resolved"] else None

    lines = [f"🔬 *{_SHADOW_DISPLAY_NAME.get(key, key)}*", "─────────────────────"]
    lines.append(f"Logged: `{s['logged']}`   Resolved: `{s['resolved']}`")
    lines.append(f"Wins: `{s['wins']}`   Losses: `{s['losses']}`   Timed out: `{s['timed_out']}`")
    lines.append(f"Win Rate: `{win_rate:.0f}%`" if win_rate is not None else "Win Rate: `—`")
    lines.append(f"Average R: `{avg_r:+.2f}`" if avg_r is not None else "Average R: `—`")
    lines.append(f"Reached — 1R: `{s['hit_1r']}`  2R: `{s['hit_2r']}`  3R: `{s['hit_3r']}`")

    records = _read_shadow_trade_log(experiment=key)
    if records:
        durations = [r["bars_open"] for r in records if r.get("bars_open") is not None]
        if durations:
            lines.append(f"\nAverage duration: `{sum(durations)/len(durations):.1f} candles` (5m)")

        buys  = sum(1 for r in records if r.get("direction") == "BUY")
        sells = sum(1 for r in records if r.get("direction") == "SELL")
        lines.append(f"Buy/Sell: `{buys}` / `{sells}`")

        sessions = {}
        for r in records:
            sess = _classify_session(r.get("opened_at", ""))
            sessions[sess] = sessions.get(sess, 0) + 1
        if sessions:
            lines.append("\nBy Session:")
            for sess, n in sorted(sessions.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {sess}: `{n}`")

        variants = {}
        for r in records:
            v = r.get("variant")
            if v:
                variants.setdefault(v, {"n": 0, "wins": 0, "sum_r": 0.0})
                variants[v]["n"] += 1
                variants[v]["sum_r"] += r.get("r_achieved", 0.0)
                if r.get("r_achieved", 0.0) > 0:
                    variants[v]["wins"] += 1
        if variants:
            lines.append("\nBy Variant:")
            for v, d in sorted(variants.items(), key=lambda kv: -kv[1]["n"]):
                wr = d["wins"] / d["n"] * 100
                lines.append(f"  {_md_safe_variant(v)}: `{d['n']}` logged, WR `{wr:.0f}%`, avg R `{d['sum_r']/d['n']:+.2f}`")
    else:
        lines.append("\n_No resolved trades in the permanent log yet — "
                      "duration/session/variant splits need at least one resolution._")

    return "\n".join(lines)


def format_rejected_live_detail(shadow_stats):
    """Drill-down for EXPE_REJECTED_LIVE specifically — the one the
    friend said he'd use most. Two breakdowns:
      1. Rejection reason buckets, from EVERY logged rejection (tags are
         captured at build time, so this covers all resolved+pending).
      2. Of the ones that resolved as WINNERS, which tier(s) were
         actually "activated" (watching/close) at the moment of
         rejection — i.e. which filter cost you a real winning trade.
    Both need `tags` to have been persisted at resolution time (see the
    audit note in _append_shadow_trade_log) — records resolved before
    that fix won't have tags and are silently excluded from these two
    breakdowns (they still count in the top-line win/loss numbers).
    """
    key = "EXPE_REJECTED_LIVE"
    s = shadow_stats.get(key, _empty_experiment_stat())
    if s["logged"] == 0:
        return "🔬 *Rejected Live Analysis*\n_Nothing logged yet._"

    win_rate = (s["wins"] / s["resolved"] * 100) if s["resolved"] else None
    avg_r = (s["sum_r"] / s["resolved"]) if s["resolved"] else None

    lines = ["🔬 *Rejected Live Analysis*", "─────────────────────"]
    lines.append(f"Rejected: `{s['logged']}`   Resolved: `{s['resolved']}`")
    lines.append(f"Won: `{s['wins']}`   Lost: `{s['losses']}`")
    lines.append(f"Win Rate: `{win_rate:.0f}%`" if win_rate is not None else "Win Rate: `—`")
    lines.append(f"Average R: `{avg_r:+.2f}`" if avg_r is not None else "Average R: `—`")

    records = _read_shadow_trade_log(experiment=key)
    # What actually hit, not just win/loss (per chat) — every resolved
    # record has an exact r_achieved regardless of whether tags exist,
    # so this line works even for records logged before the tag-
    # persistence fix (unlike the reason/tier breakdowns below).
    resolved_records = [r for r in records if r.get("outcome")]
    dist_line = format_outcome_distribution(outcome_distribution(resolved_records))
    if dist_line:
        lines.append(dist_line.replace("    ↳ ", "Hit: "))

    tagged = [r for r in records if r.get("tags")]
    if not tagged:
        lines.append("\n_No tag data yet for reason/tier breakdowns — this needs at least one "
                      "resolution AFTER the tag-persistence fix. Check back after the next few "
                      "resolved rejections._")
        return "\n".join(lines)

    reason_buckets = {}
    for r in tagged:
        # tags here is {"_blocked_by_atr": bool, tier_label: {"activated","reason"}, ...}
        # — _blocked_by_atr is a SIBLING key, not a per-tier entry, so it
        # must be filtered out before treating every value as a tier dict.
        # (Found via testing just now: this crashed on the first record
        # logged after _blocked_by_atr was added — the two isinstance()
        # guards two blocks below were already written correctly; these
        # two were the gap.)
        #
        # BUG FIX (per chat, 2026-08-11): context block MUST be checked
        # BEFORE any tier reason is consulted. log_rejected_live_to_queue()
        # peeks every tier regardless of ctx.tradeable, so a context-
        # blocked record still has real tier `checks` sitting right next
        # to the `_blocked_by_*` flags — reading straight into `checks`
        # (as this used to) silently mislabels every ATR/session/post-
        # spike rejection under whichever tier reason happened to also be
        # true that scan, hiding the actual (and, per /stats, dominant —
        # 52.8% of all scans) cause entirely from this report.
        context_bucket = _classify_context_block(r["tags"])
        if context_bucket:
            bucket = context_bucket
        else:
            checks = {k: v for k, v in r["tags"].items() if isinstance(v, dict)}
            any_activated = any(v.get("activated") for v in checks.values())
            if any_activated:
                bucket = "Activated but didn't fire (conviction/risk gate)"
            else:
                # Pick the most specific (non-generic) reason among the tiers.
                reasons = [v.get("reason", "") for v in checks.values()]
                bucket = _classify_rejection_reason(next((x for x in reasons if x), ""))
        reason_buckets[bucket] = reason_buckets.get(bucket, 0) + 1

    lines.append("\nReasons rejected:")
    for bucket, n in sorted(reason_buckets.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {bucket}: `{n}`")

    # Tier breakdown of the "activated but didn't fire" bucket specifically
    # — i.e. of the setups blocked by conviction/risk gate rather than a
    # tier gate, which tier(s) they belonged to, and how those actually
    # resolved. This is the denominator needed to judge whether a given
    # tier's conviction threshold is cutting real winners, not just the
    # numerator ("rejected winners") shown below.
    #
    # BUG FIX (per chat, 2026-08-11): must exclude context-blocked records
    # for the same reason as the reason_buckets fix above — a tier can
    # peek as "activated" on a scan that market context already killed
    # before Rule of Law ran, and that's not a conviction/risk-gate story
    # at all. Without this exclusion this breakdown silently counted
    # ATR/session/post-spike-blocked scans as if a tier's own gate had
    # blocked them.
    activated_not_fired = [r for r in tagged
                           if not _classify_context_block(r["tags"])
                           and any(isinstance(v, dict) and v.get("activated")
                                  for v in r["tags"].values())]
    if activated_not_fired:
        tier_breakdown = {}
        for r in activated_not_fired:
            won = r.get("r_achieved", 0.0) > 0
            for tier_label, v in r["tags"].items():
                if isinstance(v, dict) and v.get("activated"):
                    d = tier_breakdown.setdefault(tier_label, {"n": 0, "wins": 0})
                    d["n"] += 1
                    if won:
                        d["wins"] += 1
        lines.append("\nOf 'activated but didn't fire', by tier:")
        for tier_label, d in sorted(tier_breakdown.items(), key=lambda kv: -kv[1]["n"]):
            wr = (d["wins"] / d["n"] * 100) if d["n"] else 0.0
            lines.append(f"  {tier_label}: `{d['n']}` (won `{d['wins']}`, `{wr:.0f}%`)")

    winners_tagged = [r for r in tagged if r.get("r_achieved", 0.0) > 0]
    if winners_tagged:
        tier_wins = {}
        for r in winners_tagged:
            for tier_label, v in r["tags"].items():
                if isinstance(v, dict) and v.get("activated"):
                    tier_wins[tier_label] = tier_wins.get(tier_label, 0) + 1
        lines.append(f"\nRejected winners: `{len(winners_tagged)}`")
        if tier_wins:
            lines.append("Of those, tier was activated but blocked elsewhere:")
            for tier_label, n in sorted(tier_wins.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {tier_label}: `{n}`")
        else:
            lines.append("_None of the tagged winners had any tier activated — "
                          "these were rejected before any tier saw a setup at all._")

    return "\n".join(lines)


def outcome_distribution(records):
    """Exact R-outcome buckets for a set of resolved shadow records — SL
    (-1R), 1R, 2R, 3R — not just win/loss. r_achieved in this codebase
    only ever lands on one of these four values (see
    update_pending_shadow_setups: it tracks max_r_reached as a discrete
    1/2/3 checkpoint crossing, or -1.0 on a stop-out), so round()-based
    bucketing here is exact, not an approximation. 'Other' exists only
    to catch anything that doesn't fit that pattern (e.g. old records
    from before this resolution logic), so it's never silently hidden."""
    buckets = {"SL (-1R)": 0, "1R": 0, "2R": 0, "3R": 0, "Other": 0}
    for r in records:
        r_val = round(float(r.get("r_achieved", 0.0)))
        if r_val == -1:
            buckets["SL (-1R)"] += 1
        elif r_val == 1:
            buckets["1R"] += 1
        elif r_val == 2:
            buckets["2R"] += 1
        elif r_val == 3:
            buckets["3R"] += 1
        else:
            buckets["Other"] += 1
    return buckets


def format_outcome_distribution(buckets):
    """Renders outcome_distribution()'s buckets as one compact line.
    Zero-count buckets are omitted rather than padded with zeros — with
    small samples, a row of mostly-zero buckets reads as more precision
    than the data actually supports."""
    parts = [f"{label} `{count}`" for label, count in buckets.items() if count > 0]
    return "    ↳ " + "  ".join(parts) if parts else ""


def format_tier_block_analysis(tier_label):
    """
    "Tier Block Analysis" (per chat, one level deeper than the existing
    'activated but didn't fire, by tier' line): of this tier's own
    resolved 'activated but didn't fire' cases, what specifically
    blocked it, and how did those setups actually resolve — down to
    which R level was hit, not just win/loss (per chat: "let it say
    what it hit be it 1R or 2R or 3R or SL").

    HONEST SCOPE NOTE (updated 2026-08-12 — CONVICTION_GATE retired):
    conviction no longer gates live `fired` on any tier (Risk-at-Hand
    change, 2026-08-11) — every tier fires unconditionally once its own
    mandatory conditions + trigger pass, so a conviction-based rejection
    can no longer happen and the old CONVICTION_GATE bucket, which
    matched on the literal word "conviction" in a tier's reason text,
    could never populate again. Replaced with the real, distinguishable
    reasons an activated tier still ends up unfired in the CURRENT code:
      - ATR_DATA_INVALID:   the ATR reading itself was unusable (NaN/0)
                             and blocked the whole scan before Rule of
                             Law ever ran. ATR pip LEVEL is not a gate at
                             all anymore, so expect this near-empty —
                             use tags["_atr_very_low"]/["_atr_low"] to
                             see thin-volatility rejections instead.
      - STALE_BIAS_BLOCKED: this tier's OWN raw peek says its mandatory
                             conditions + trigger were satisfied (fired
                             would be True in isolation), but the live
                             1H bias was marked stale that scan, and
                             _gate_stale_bias forces fired=False on any
                             would-be signal riding a held-over,
                             no-longer-confirmed direction.
      - WATCHING:            activated, but the rejection-candle trigger
                             hadn't confirmed yet this scan — not
                             blocked by anything, just not there yet.
      - OTHER / Unclassified: activated, fired=True in this tier's own
                             raw peek, bias NOT stale — meaning some
                             OTHER (higher-priority) tier actually held
                             or won ownership of the leg that scan. This
                             peek doesn't run Rule of Law's arbitration,
                             so which tier won isn't recoverable from
                             this tier's own record alone.
    There is no separate risk:reward gate or spread gate anywhere in the
    live code — the friend's original 5-category list doesn't map onto
    what's actually implemented. If those become real gates later, add
    their own bucket here then; this function only reports what's
    computed.
    """
    records = _read_shadow_trade_log(experiment="EXPE_REJECTED_LIVE")
    tier_number = TIER_NUMBER.get(tier_label, "?")

    buckets = {"ATR_DATA_INVALID": {"records": []},
               "STALE_BIAS_BLOCKED": {"records": []},
               "WATCHING": {"records": []},
               "OTHER / Unclassified": {"records": []}}

    for r in records:
        tags = r.get("tags") or {}
        v = tags.get(tier_label)
        if not isinstance(v, dict) or not v.get("activated"):
            continue  # this tier wasn't even structurally activated that scan

        # "fired" wasn't recorded on records logged before 2026-08-12 —
        # treat missing/unknown the same as WATCHING's old inference
        # (activated-but-not-fired, cause unknown) rather than crash or
        # silently mis-bucket older rows as OTHER.
        tier_fired = v.get("fired")

        if tags.get("_blocked_by_atr"):
            bucket = "ATR_DATA_INVALID"
        elif tier_fired and tags.get("_blocked_by_stale_bias"):
            bucket = "STALE_BIAS_BLOCKED"
        elif tier_fired is False:
            bucket = "WATCHING"
        else:
            bucket = "OTHER / Unclassified"

        buckets[bucket]["records"].append(r)

    total = sum(len(b["records"]) for b in buckets.values())
    if total == 0:
        return (f"🔬 *Tier {tier_number} Block Analysis* (`{tier_label}`)\n"
                "_No resolved 'activated but didn't fire' records for this tier yet._")

    lines = [f"🔬 *Tier {tier_number} Block Analysis* (`{tier_label}`)",
             "─────────────────────",
             f"Activated-but-blocked (resolved): `{total}`", ""]
    for bucket, b in sorted(buckets.items(), key=lambda kv: -len(kv[1]["records"])):
        recs = b["records"]
        n = len(recs)
        if n == 0:
            continue
        wins = sum(1 for r in recs if r.get("r_achieved", 0.0) > 0)
        wr = (wins / n * 100) if n else 0.0
        lines.append(f"*{bucket}*: `{n}` (won `{wins}`, `{wr:.0f}%`)")
        dist_line = format_outcome_distribution(outcome_distribution(recs))
        if dist_line:
            lines.append(dist_line)
    lines.append("")
    lines.append("_ATR-invalid, stale-bias, and WATCHING are the real, distinguishable "
                 "blocks in the current code — conviction no longer gates anything, and "
                 "no separate risk/spread gate exists yet to report on. OTHER means a "
                 "different tier held ownership that scan. Small buckets (<~30) are "
                 "directional, not conclusive._")
    return "\n".join(lines)


# ---- MARKET INTELLIGENCE NETWORK: Information Coefficient ------------------
# Salvaged from the IC/Grinold material (per chat). The original theory
# is CROSS-SECTIONAL — correlating a signal across many different stocks
# on the same day, then multiplying by breadth (sqrt of independent
# bets). That doesn't transfer here: this bot trades ONE pair,
# sequentially, so there's no cross-section and no real "breadth" the
# way Grinold's law assumes (consecutive trades share the same regime
# and aren't independent bets). What DOES transfer: correlating a
# continuous feature against r_achieved OVER TIME, on this tier's own
# resolved trades. Different math (time-series correlation, not
# cross-sectional), same underlying tool (Pearson correlation).
IC_MIN_N = 30


def _rank(values):
    """Fractional (average) ranks for Spearman, pure stdlib. Tied values
    get the mean of the positions they'd otherwise occupy — the standard
    tie-handling for Spearman's rho, and necessary here since features
    like break_count (small integers, 1-3) produce lots of ties."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1  # 1-indexed rank, averaged over the tie block
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _pearson(xs, ys):
    """Plain Pearson correlation on two equal-length numeric lists. Pure
    stdlib. Shared by compute_ic (on ranks, giving Spearman) and anything
    else that wants a straight linear correlation."""
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return 0.0
    return cov / ((var_x ** 0.5) * (var_y ** 0.5))


def compute_ic(tier_label, feature_key, min_n=IC_MIN_N):
    """
    Spearman rank correlation between one fingerprint feature and
    r_achieved, over this tier's own resolved EXP7 trades only.

    Changed from Pearson (per chat): several fingerprint fields —
    break_count in particular, a small integer mostly 1-3 — have no
    reason to move *linearly* with r_achieved (itself discrete: -1, 1,
    2, 3), so Pearson could understate or misread a real monotonic
    relationship. Spearman correlates RANKS instead of raw values, which
    only assumes "as one goes up, does the other tend to go up too" —
    a weaker, more honest assumption for ordinal/discrete data like this.

    Pure stdlib (no numpy dependency). Returns None below min_n — same
    discipline as Evidence Engine: a correlation coefficient computed on
    a handful of trades is noise dressed as a finding, not a result.
    """
    records = [r for r in _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
               if r.get("variant") == tier_label]
    pairs = [(r.get("tags", {}).get(feature_key), r.get("r_achieved"))
             for r in records
             if r.get("tags", {}).get(feature_key) is not None and r.get("r_achieved") is not None]
    n = len(pairs)
    if n < min_n:
        return None

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    ic = _pearson(_rank(xs), _rank(ys))
    return {"n": n, "ic": round(ic, 3), "method": "spearman"}


_IC_FEATURES = ["leg_length_pips", "break_count", "atr_percentile_15m",
                "ob_freshness_candles", "sweep_distance_pips",
                # Added per chat alongside was_choch/market state. Numeric
                # only, by design: was_choch (bool) and volatility_state
                # (categorical) aren't valid inputs for Pearson
                # correlation without encoding, which is out of scope
                # for this pass -- they're still stored as tags, just not
                # correlated here.
                "trend_strength_atr_mult", "compression_ratio", "pullback_depth_pct"]
# ⚠  MULTIPLE-COMPARISONS WARNING  ⚠
# compute_ic() and format_ic_report() run Spearman IC across the
# len(_IC_FEATURES) features above × 3 tiers × up to 7 experiments
# continuously, with NO Bonferroni, Benjamini-Hochberg, or any other
# false-discovery-rate correction applied.  At this test volume, a
# spurious IC of ±0.3 or even ±0.4 is expected from noise alone with
# conventional significance thresholds (p < 0.05).
#
# Practical implication: a single IC reading that looks impressive in
# isolation may simply be the most extreme draw from a large number of
# simultaneous tests. To claim a feature genuinely predicts outcome:
#   1. Observe the same sign and magnitude across ≥ 2 tiers or experiments.
#   2. Apply a Bonferroni correction: p-threshold = 0.05 / N_tests, where
#      N_tests ≈ len(_IC_FEATURES) × 3 tiers = ~24 at minimum.
#   3. Treat it as a hypothesis to test on held-out data, not a confirmed edge.
# No code correction is applied here — IC numbers are raw and uncorrected.


def format_ic_report(tier_label):
    """On-demand report: IC for every known continuous fingerprint
    field, this tier's own resolved trades only. Deliberately NOT a
    ranked "most predictive variable" list — several of these features
    move together (ATR percentile and leg length, for instance), so
    ranking them as if independent would overstate what's actually
    known. Raw numbers, side by side, reader draws their own conclusion
    — same "concatenation, not verdict" discipline as Advisory Council."""
    tier_number = TIER_NUMBER.get(tier_label, "?")
    n_tests = len(_IC_FEATURES) * 3  # features × tiers; approximate test count
    bonferroni_p = round(0.05 / max(n_tests, 1), 4)
    lines = [f"📈 *Information Coefficient (Spearman) — Tier {tier_number}* (`{tier_label}`)",
             "─────────────────────",
             "_Correlation only — NOT a ranking or \"most predictive variable\" claim. "
             "Several of these features move together (e.g. ATR percentile and leg "
             "length), so treating this as a ranked list would overstate independence "
             "that isn't there. Each number below is rank correlation (Spearman) vs "
             "r_achieved, not Pearson — safer for small-integer/discrete fields like "
             "break_count._",
             "",
             f"⚠ _Multiple-comparisons caution: this report runs ~{n_tests} simultaneous "
             f"tests ({len(_IC_FEATURES)} features × 3 tiers). At this volume a spurious "
             f"IC of ±0.3–0.4 is expected from noise at p<0.05. Bonferroni-adjusted "
             f"threshold ≈ p<{bonferroni_p}. Do NOT treat a single strong IC reading as "
             f"a confirmed edge; require the same sign across ≥ 2 tiers or confirm on "
             f"held-out data._",
             ""]

    # ---- NULL-MODEL BASELINE GAP -------------------------------------------
    # There is currently NO comparison of these IC figures against a dumb
    # structureless baseline (e.g., random entries at identical risk params).
    # Until that benchmark is run, a positive IC demonstrates correlation
    # between feature X and outcome Y within SMC setups only — it does NOT
    # confirm that SMC structure itself adds edge over a random entry timed
    # to the same macro regime. See compute_ic() for the raw Spearman
    # computation. Adding a baseline experiment (null model via EXP_NULL or
    # similar) is the correct fix and is tracked as a known falsifiability gap.
    # -------------------------------------------------------------------------

    any_shown = False
    for field in _IC_FEATURES:
        result = compute_ic(tier_label, field)
        if result is None:
            lines.append(f"  `{field}`: _insufficient data (<{IC_MIN_N} resolved)_")
        else:
            lines.append(f"  `{field}`: IC=`{result['ic']:+.3f}` (n=`{result['n']}`)")
            any_shown = True
    if not any_shown:
        lines.append("\n_No field has enough data yet — check back later._")
    return "\n".join(lines)


# ---- MARKET INTELLIGENCE NETWORK: Failure Investigation Bureau ------------
# "Instead of recording LOSS, investigate it" (per chat). Purely
# read-only, on-demand, one trade at a time — never triggered
# automatically from scan(), never touches a live decision. Compares one
# resolved trade's own fingerprint against the AVERAGE fingerprint of
# that SAME TIER's winners, and reports which fields deviated most.
# Requires trade_id, which is why /shadow recent now displays it.
FAILURE_INVESTIGATION_MIN_WINNERS = 10
TIER_SHARPE_MIN_TRADES = 5  # minimum resolved EXP7 trades to compute Sharpe


def compute_tier_sharpe(tier_label, min_trades=TIER_SHARPE_MIN_TRADES):
    """Compute the realised (unannualised) Sharpe ratio for one tier from
    resolved EXP7_TIER_ATR records.

    Sharpe here = mean(R) / std(R) across resolved trades, where R is
    r_achieved (positive = win). This is trade-Sharpe, not calendar-Sharpe —
    it is comparable across tiers only, NOT to a published Sharpe benchmark
    (which assumes daily/monthly returns, not trade-level P&L).

    Returns a dict with keys: n, mean_r, std_r, sharpe
    Returns None when fewer than min_trades are resolved (a std from 2
    trades is noise).

    SHARPE AWARENESS GAP: sizing bands are win-rate-based, not tied to
    this figure. Per the note near CONVICTION_MANAGEMENT_BANDS, per-tier
    realised Sharpe should be evaluated before trusting band sizes with real
    risk — a lower win-rate tier that wins bigger and with lower variance
    can have a BETTER trade-Sharpe than a higher win-rate tier. This function
    supplies that data; acting on it is left to the operator.
    """
    records = _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
    tier_records = [r for r in records if r.get("variant") == tier_label
                    and r.get("r_achieved") is not None]
    n = len(tier_records)
    if n < min_trades:
        return None
    rs = [r["r_achieved"] for r in tier_records]
    mean_r = sum(rs) / n
    variance = sum((x - mean_r) ** 2 for x in rs) / (n - 1)  # sample variance
    std_r = variance ** 0.5
    sharpe = mean_r / std_r if std_r > 0 else 0.0
    return {
        "n":      n,
        "mean_r": round(mean_r, 4),
        "std_r":  round(std_r, 4),
        "sharpe": round(sharpe, 4),
    }


# ---- MARKET INTELLIGENCE NETWORK: Conviction Audit -------------------------
# Per friend's proposal (per chat): the conviction score has LIVE decision
# authority (classify_conviction / CONVICTION_MIN_BY_TIER gates what an
# activated setup is allowed to do) but its point weights were hand-chosen,
# not derived — "if 75 means good only because we decided 75 means good,
# that's circular." This section does NOT touch that gate. It's a
# read-only report, built entirely from data experiment_7_tier_atr_mirror
# already tags on every resolved EXP7_TIER_ATR trade (conviction_score,
# would_have_fired_pre_context, and peek.breakdown's own boolean
# components), answering three of the friend's questions directly:
#   1. Does conviction score actually correlate with expectancy?
#      (compute_conviction_buckets — bucketed on the SAME floors
#      CONVICTION_MANAGEMENT_BANDS already gates live sizing on, so this
#      checks whether those floors earn their keep, not arbitrary new ones)
#   2. Does the score gate itself add value, or would every activated
#      setup have done as well or better without it? (below- vs at/above-
#      CONVICTION_MIN_BY_TIER comparison, inside format_conviction_audit)
#   3. Which individual scoring components actually separate winners from
#      losers, and which are dead weight? (compute_factor_attribution —
#      same Cohen's h/d standardized-effect-size machinery the Failure
#      Investigation Bureau already uses, applied across the tier's own
#      boolean breakdown keys instead of one trade vs its tier's history)
# Same dormancy discipline as EVIDENCE_MIN_N/CALIBRATION_MIN_N throughout
# this file: a bucket or group below its floor stays silent rather than
# printing a number with no real support behind it. Nothing here can ever
# feed back into classify_conviction() or a live gate — read-only
# annotation layer, same as the Evidence Engine and Markov model.
CONVICTION_AUDIT_MIN_BUCKET_N = 10
CONVICTION_AUDIT_MIN_GROUP_N  = 10


def _r_stats(records):
    """n, win rate, avg R, profit factor for a list of resolved-trade
    dicts (each must have r_achieved). PF = gross wins / abs(gross
    losses); None (not infinite) when there are no losses to divide by —
    same "don't invent a number" discipline as _compare_tag."""
    n = len(records)
    if n == 0:
        return None
    rs = [r["r_achieved"] for r in records]
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else None
    return {
        "n": n,
        "win_rate": round(len(wins) / n, 3),
        "avg_r": round(sum(rs) / n, 3),
        "pf": round(pf, 2) if pf is not None else None,
    }


def compute_conviction_buckets(tier_label, min_n=CONVICTION_AUDIT_MIN_BUCKET_N):
    """Buckets this tier's resolved EXP7_TIER_ATR trades by their FROZEN
    conviction_score tag (frozen at signal time — see experiment_7_tier_
    atr_mirror, not recomputed after the fact), using this tier's own
    CONVICTION_MIN_BY_TIER floor plus the CONVICTION_MANAGEMENT_BANDS
    floors as edges — the exact floors already gating live eligibility
    and sizing, so this checks whether THOSE floors earn their keep
    rather than inventing new ones to bucket on.

    Returns an ordered list of (label, stats-or-None, raw_n) tuples —
    stats is None when raw_n is below min_n (bucket too thin to read
    anything into yet, not "no data")."""
    records = [r for r in _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
               if r.get("variant") == tier_label and r.get("r_achieved") is not None]

    tier_min = CONVICTION_MIN_BY_TIER.get(tier_label)
    band_floors = {b[0] for b in CONVICTION_MANAGEMENT_BANDS}
    edges = sorted(band_floors | ({tier_min} if tier_min is not None else set()))

    buckets = []
    for i, floor in enumerate(edges):
        ceiling = edges[i + 1] if i + 1 < len(edges) else None
        label = f"{floor}-{ceiling - 1}" if ceiling is not None else f"{floor}+"
        group = [r for r in records
                 if r.get("tags", {}).get("conviction_score") is not None
                 and r["tags"]["conviction_score"] >= floor
                 and (ceiling is None or r["tags"]["conviction_score"] < ceiling)]
        stats = _r_stats(group) if len(group) >= min_n else None
        buckets.append((label, stats, len(group)))
    return buckets


def compute_factor_attribution(tier_label, min_n=CONVICTION_AUDIT_MIN_GROUP_N):
    """For each of this tier's own boolean/categorical evidence keys
    (TIER_EVIDENCE_KEYS — same keys the Evidence Engine already treats as
    the tier's real structural facts, not its score-derived ones), splits
    resolved EXP7_TIER_ATR trades into a True group and a False group and
    reports win-rate/avg-R/PF for each, plus Cohen's h (win-rate gap) and
    Cohen's d (r_achieved gap) so a raw percentage-point difference can be
    read against how much that tag naturally varies, not taken at face
    value. This is what actually tells you whether a scoring component
    (e.g. "order_block = +20 points") is earning its weight, separate
    from whether the OVERALL score correlates (see
    compute_conviction_buckets for that).

    Returns a list of dicts, one per key with >= min_n trades in BOTH the
    True and False groups; keys that don't clear that bar are omitted
    (dormant, not "no effect") rather than printed with a misleading
    sample size."""
    keys = TIER_EVIDENCE_KEYS.get(tier_label, [])
    records = [r for r in _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
               if r.get("variant") == tier_label and r.get("r_achieved") is not None]

    results = []
    for key in keys:
        true_group  = [r for r in records if r.get("tags", {}).get(key) is True]
        false_group = [r for r in records if r.get("tags", {}).get(key) is False]
        if len(true_group) < min_n or len(false_group) < min_n:
            continue
        true_stats  = _r_stats(true_group)
        false_stats = _r_stats(false_group)
        h = _cohens_h(true_stats["win_rate"], false_stats["win_rate"])
        d = _cohens_d([r["r_achieved"] for r in true_group],
                       [r["r_achieved"] for r in false_group])
        results.append({
            "key": key,
            "true": true_stats, "false": false_stats,
            "win_rate_cohens_h": round(h, 3),
            "r_cohens_d": round(d, 3) if d is not None else None,
        })
    return results


def format_conviction_audit(tier_label):
    """On-demand report combining the three checks above. Console/Telegram
    text, same style as format_ic_report — raw numbers side by side, no
    ranked "most important factor" claim (see compute_factor_attribution's
    docstring: these are correlational, not causal, and several breakdown
    keys move together within a tier's own scoring logic)."""
    tier_number = TIER_NUMBER.get(tier_label, "?")
    tier_min = CONVICTION_MIN_BY_TIER.get(tier_label, "?")
    lines = [f"🔎 *Conviction Audit — Tier {tier_number}* (`{tier_label}`)",
             "─────────────────────",
             "_Read-only. Nothing here changes the live conviction gate — "
             "it only checks, from this tier's own resolved history, "
             "whether that gate's score and floors are earning their "
             "authority._", ""]

    # ---- 1. Score buckets ----
    lines.append(f"*1. Score buckets* (live floor for this tier: `{tier_min}`)")
    buckets = compute_conviction_buckets(tier_label)
    any_bucket = False
    for label, stats, raw_n in buckets:
        if stats is None:
            lines.append(f"  `{label}`: _insufficient data (n={raw_n} < {CONVICTION_AUDIT_MIN_BUCKET_N})_")
        else:
            any_bucket = True
            lines.append(
                f"  `{label}`: n=`{stats['n']}` win=`{stats['win_rate']:.0%}` "
                f"avgR=`{stats['avg_r']:+.2f}` PF=`{stats['pf'] if stats['pf'] is not None else '—'}`"
            )
    if not any_bucket:
        lines.append("  _No bucket has enough resolved trades yet._")

    # ---- 2. Does the score floor itself add value? ----
    lines.append("")
    lines.append("*2. Below floor vs at/above floor*")
    records = [r for r in _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
               if r.get("variant") == tier_label and r.get("r_achieved") is not None
               and r.get("tags", {}).get("conviction_score") is not None]
    if isinstance(tier_min, (int, float)):
        below = [r for r in records if r["tags"]["conviction_score"] < tier_min]
        at_above = [r for r in records if r["tags"]["conviction_score"] >= tier_min]
        below_stats = _r_stats(below) if len(below) >= CONVICTION_AUDIT_MIN_GROUP_N else None
        at_above_stats = _r_stats(at_above) if len(at_above) >= CONVICTION_AUDIT_MIN_GROUP_N else None
        if below_stats is None or at_above_stats is None:
            lines.append(f"  _insufficient data (need >= {CONVICTION_AUDIT_MIN_GROUP_N} resolved on each side)_")
        else:
            lines.append(
                f"  below `{tier_min}`: n=`{below_stats['n']}` win=`{below_stats['win_rate']:.0%}` "
                f"avgR=`{below_stats['avg_r']:+.2f}` PF=`{below_stats['pf'] if below_stats['pf'] is not None else '—'}`"
            )
            lines.append(
                f"  at/above `{tier_min}`: n=`{at_above_stats['n']}` win=`{at_above_stats['win_rate']:.0%}` "
                f"avgR=`{at_above_stats['avg_r']:+.2f}` PF=`{at_above_stats['pf'] if at_above_stats['pf'] is not None else '—'}`"
            )
            h = _cohens_h(at_above_stats["win_rate"], below_stats["win_rate"])
            lines.append(f"  win-rate Cohen's h = `{h:+.3f}` (|h|: 0.2 small / 0.5 medium / 0.8 large)")
    else:
        lines.append("  _no floor configured for this tier_")

    # ---- 3. Per-factor attribution ----
    lines.append("")
    lines.append("*3. Individual scoring factors* (True vs False group)")
    lines.append(f"_Correlational only, min n=`{CONVICTION_AUDIT_MIN_GROUP_N}` per side — "
                 "several of a tier's own factors move together, so treat this as "
                 "leads to check, not a ranked importance list._")
    attribution = compute_factor_attribution(tier_label)
    if not attribution:
        lines.append("  _No factor has enough data on both sides yet._")
    else:
        for row in attribution:
            t, f = row["true"], row["false"]
            lines.append(
                f"  `{row['key']}`: True n=`{t['n']}` win=`{t['win_rate']:.0%}` avgR=`{t['avg_r']:+.2f}`  "
                f"| False n=`{f['n']}` win=`{f['win_rate']:.0%}` avgR=`{f['avg_r']:+.2f}`  "
                f"| h=`{row['win_rate_cohens_h']:+.3f}` d=`{row['r_cohens_d']:+.3f}`" if row['r_cohens_d'] is not None
                else f"  `{row['key']}`: True n=`{t['n']}` win=`{t['win_rate']:.0%}` avgR=`{t['avg_r']:+.2f}`  "
                     f"| False n=`{f['n']}` win=`{f['win_rate']:.0%}` avgR=`{f['avg_r']:+.2f}`  "
                     f"| h=`{row['win_rate_cohens_h']:+.3f}` d=_n/a_"
            )
    return "\n".join(lines)


# ---- MARKET INTELLIGENCE NETWORK: Policy Lab report ------------------------
# EXP4_POLICY_LAB report — per chat: "bucketing what would-have-been-signal,
# so it can later read 'trades taken at LOW risk: 150, won: 100'..." — a
# /shadow-style breakdown, NOT a rolling live feed like EXPE_REJECTED_LIVE.
# Deliberately built as an on-demand report over the PERMANENT resolved
# log (same pattern as format_conviction_audit above), not a running
# console/Telegram print every scan — nothing about this pushes a message
# on every rejection, which is the specific shape EXPE_REJECTED_LIVE had
# to be fixed away from twice (entry-price dedup drift, then single-
# last-leg dedup — see the gbpusd scanner history).
#
# EXAGGERATION CHECK (per chat, "won't take a trade every 5 minutes while
# a leg sits watching"): a variant only calls log_shadow_setup() at all
# once its OWN policy gate says FIRE (see experiment_4_policy_lab) — a
# WATCHING tier has peek.score is None and never reaches the variant
# loop in the first place, so nothing is even a candidate to log yet.
# Once a variant does fire, log_shadow_setup's dedup key is `leg_key|
# tier::policy` — leg_key is compute_leg_id(macro_bias, swing_high,
# swing_low), which is constant for as long as the SAME leg persists,
# not the entry price (that was the old EXPE_REJECTED_LIVE bug) and not
# a single-last-value comparison (that was the old EXP2_FIB/EXP5
# bug — see SHADOW_SEEN_LEG_CAP). So a leg that keeps re-triggering a
# rejection candle across several consecutive 5-minute scans still logs
# AT MOST ONE trade per (leg, tier, policy) — re-scans of the same leg
# hit the seen_legs check and are skipped, exactly like EXP7 already
# relies on for its own dedup. A genuinely NEW leg is a genuinely new
# hypothetical trade, which is what should log again.
POLICY_LAB_MIN_BUCKET_N = 10
_POLICY_LAB_ORDER = ["CONVICTION_SWEEP_ON", "CONVICTION_SWEEP_OFF", "RISK_AT_HAND"]
_RISK_BAND_ORDER = ["LOW", "MEDIUM", "HIGH"]


def compute_policy_lab_buckets(tier_label, policy, min_n=POLICY_LAB_MIN_BUCKET_N):
    """Buckets this tier's resolved EXP4_POLICY_LAB trades for ONE policy
    variant by their frozen risk_band tag (LOW/MEDIUM/HIGH — the same
    classify_tierN_risk label the live Telegram message already shows,
    here checked against what actually happened instead of just printed
    and forgotten). Returns an ordered list of (band, stats-or-None,
    raw_n) tuples, stats None below min_n."""
    variant = f"{tier_label}::{policy}"
    records = [r for r in _read_shadow_trade_log(experiment="EXP4_POLICY_LAB")
               if r.get("variant") == variant and r.get("r_achieved") is not None]
    buckets = []
    for band in _RISK_BAND_ORDER:
        group = [r for r in records if r.get("tags", {}).get("risk_band") == band]
        stats = _r_stats(group) if len(group) >= min_n else None
        buckets.append((band, stats, len(group)))
    return buckets


def format_policy_lab_report(tier_label):
    """On-demand report, one section per policy variant this tier ran
    (Tier 1 has no CONVICTION_SWEEP_OFF arm — see experiment_4_policy_lab).
    Each section is the risk_band breakdown from compute_policy_lab_buckets,
    plus a policy-level total so the three policies are directly
    comparable at a glance (the actual point of the experiment)."""
    tier_number = TIER_NUMBER.get(tier_label, "?")
    lines = [f"🧪 *Policy Lab — Tier {tier_number}* (`{tier_label}`)",
             "─────────────────────",
             "_On-demand report over the permanent resolved log — not a "
             "running feed. Each policy only logged a trade when ITS OWN "
             "rule said fire; a rejected arm contributes nothing for that "
             "leg, so these n's reflect each policy's real trade "
             "frequency, not one shared population three ways._", ""]

    policies = _POLICY_LAB_ORDER if tier_label != "TIER_1_POI" else ["CONVICTION_SWEEP_ON", "RISK_AT_HAND"]
    for policy in policies:
        variant = f"{tier_label}::{policy}"
        all_records = [r for r in _read_shadow_trade_log(experiment="EXP4_POLICY_LAB")
                        if r.get("variant") == variant and r.get("r_achieved") is not None]
        total = _r_stats(all_records)
        lines.append(f"*{policy}*" + (f" — total n=`{total['n']}` win=`{total['win_rate']:.0%}` "
                                       f"avgR=`{total['avg_r']:+.2f}` PF=`{total['pf'] if total['pf'] is not None else '—'}`"
                                       if total else " — _no resolved trades yet_"))
        buckets = compute_policy_lab_buckets(tier_label, policy)
        any_bucket = False
        for band, stats, raw_n in buckets:
            if stats is None:
                lines.append(f"  {band}: _insufficient data (n={raw_n} < {POLICY_LAB_MIN_BUCKET_N})_")
            else:
                any_bucket = True
                lines.append(
                    f"  {band}: n=`{stats['n']}` won=`{round(stats['win_rate'] * stats['n'])}` "
                    f"win=`{stats['win_rate']:.0%}` avgR=`{stats['avg_r']:+.2f}` "
                    f"PF=`{stats['pf'] if stats['pf'] is not None else '—'}`"
                )
        if not any_bucket:
            lines.append("  _no risk-band bucket has enough resolved trades yet._")
        lines.append("")
    return "\n".join(lines).rstrip()



# Per chat — "tag each signal with the Market Thesis Engine's phase read,
# so tier performance can be measured conditional on market phase" (framed
# as a new EXP9_REGIME experiment). No new experiment or logging needed:
# experiment_7_tier_atr_mirror already tags thesis_phase on every record
# (added earlier for the leg_obs/thesis calibration work), so this is a
# report over data that already exists, not a new data-collection
# pathway. Deliberately scoped to EXP7_TIER_ATR only — EXP1-4/6/8 don't
# currently receive `state` in their function signature (only EXP5/6/7
# do), so tagging phase onto them would mean threading state through
# several more call sites for experiments that aren't the tier-mirror
# control population anyway. If regime-conditioning on those becomes
# genuinely wanted later, that's a separate, assessable change — not
# bundled in here on spec.
REGIME_MIN_BUCKET_N = 10


def compute_regime_performance(tier_label, min_n=REGIME_MIN_BUCKET_N):
    """Buckets this tier's resolved EXP7_TIER_ATR trades by the frozen
    thesis_phase tag (EXPANSION/EXHAUSTION/TRANSITION/MANIPULATION).
    Returns an ordered list of (phase, stats-or-None, raw_n) — same
    dormancy convention as compute_conviction_buckets: None means raw_n
    is below min_n, not "no data"."""
    records = [r for r in _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
               if r.get("variant") == tier_label and r.get("r_achieved") is not None]
    by_phase = {}
    for r in records:
        phase = r.get("tags", {}).get("thesis_phase")
        if phase is None:
            continue  # predates thesis tagging — skip rather than guess
        by_phase.setdefault(phase, []).append(r)

    buckets = []
    for phase in sorted(by_phase):
        group = by_phase[phase]
        stats = _r_stats(group) if len(group) >= min_n else None
        buckets.append((phase, stats, len(group)))
    return buckets


def format_regime_performance(tier_label):
    """/shadow regime tierN — win%/avgR/PF by market phase, so a tier
    that looks fine in aggregate but is actually only working in one
    regime (or actively losing in another) doesn't stay hidden inside a
    blended number. Read-only, same as the Conviction Audit — nothing
    here feeds back into activation, conviction, or the live gate."""
    tier_number = TIER_NUMBER.get(tier_label, "?")
    buckets = compute_regime_performance(tier_label)
    lines = [f"🌦️ *Regime-Conditional Performance — Tier {tier_number}* (`{tier_label}`)",
             "─────────────────────",
             "_Read-only — win rate/avg R/PF split by the Market Thesis Engine's "
             "phase read at signal time. Diagnostic only, same as the Conviction "
             "Audit; nothing here changes activation, conviction, or the live gate._",
             ""]
    if not buckets:
        lines.append("_No resolved EXP7_TIER_ATR trades with a phase tag yet._")
        return "\n".join(lines)
    any_shown = False
    for phase, stats, raw_n in buckets:
        if stats is None:
            lines.append(f"  `{phase}`: _insufficient data (n={raw_n} < {REGIME_MIN_BUCKET_N})_")
        else:
            any_shown = True
            lines.append(
                f"  `{phase}`: n=`{stats['n']}` win=`{stats['win_rate']:.0%}` "
                f"avgR=`{stats['avg_r']:+.2f}` PF=`{stats['pf'] if stats['pf'] is not None else '—'}`"
            )
    if not any_shown:
        lines.append("")
        lines.append("_No phase bucket has enough resolved trades yet._")
    return "\n".join(lines)


def format_failure_investigation(trade_id):
    """
    Given one resolved trade's id, reports how its fingerprint compares
    to its own tier's typical winner — not a verdict on why it lost
    (that would be an inference this function isn't entitled to make),
    just the facts side by side: this trade's leg length/ATR percentile/
    OB freshness/sweep distance vs. the tier's own winners' averages.
    Gated at FAILURE_INVESTIGATION_MIN_WINNERS the same way everything
    else in this file is gated — a comparison against 3 winners isn't a
    baseline, it's noise.
    """
    records = _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
    target = next((r for r in records if r.get("trade_id") == trade_id), None)
    if target is None:
        return f"🕵️ _No resolved EXP7 trade found with id `{trade_id}`. " \
               f"Check `/shadow recent` for valid ids._"

    tier_label = target.get("variant")
    tier_number = TIER_NUMBER.get(tier_label, "?")
    r_achieved = target.get("r_achieved", 0.0)
    outcome_word = "WIN" if r_achieved > 0 else target.get("outcome", "LOSS")

    winners = [r for r in records
               if r.get("variant") == tier_label
               and r.get("r_achieved", 0.0) > 0
               and r.get("trade_id") != trade_id]

    lines = [f"🕵️ *Failure Investigation — `{trade_id}`* (Tier {tier_number})",
             "─────────────────────",
             f"Outcome: `{outcome_word}` (`{r_achieved:+.2f}R`)", ""]

    if len(winners) < FAILURE_INVESTIGATION_MIN_WINNERS:
        lines.append(f"_Only {len(winners)} resolved winners for this tier so far "
                      f"(need {FAILURE_INVESTIGATION_MIN_WINNERS}+) — not enough to "
                      f"compare against yet._")
        return "\n".join(lines)

    lines.append(f"Compared against `{len(winners)}` resolved winners, same tier:")
    lines.append("")
    target_tags = target.get("tags", {}) or {}
    any_shown = False
    for field in _IC_FEATURES:
        this_val = target_tags.get(field)
        winner_vals = [r.get("tags", {}).get(field) for r in winners
                       if r.get("tags", {}).get(field) is not None]
        if this_val is None or not winner_vals:
            continue
        avg_winner = sum(winner_vals) / len(winner_vals)
        lines.append(f"  `{field}`: this trade=`{this_val}`  winners avg=`{avg_winner:.1f}`")
        any_shown = True
    if not any_shown:
        lines.append("_No comparable fingerprint fields on this trade — "
                      "likely logged before fingerprint tracking was added._")

    return "\n".join(lines)


def format_market_thesis(state):
    """
    Market Thesis Engine, Phase 1 (per chat — replaces Advisory Council).
    Pure formatter: reads the thesis scan() already assembled and
    persisted VERBATIM to state.json (see build_market_thesis() in
    scanner_observation.py) — computes nothing itself, only renders.
    Runs ZERO experiments, touches ZERO API — same as every other
    formatter imported into scanner_live.py for instant replies. Can't
    call build_market_thesis() directly here: that needs live OHLC
    dataframes (via MarketFacts/MarketContext) which don't exist in a
    Telegram-command context, only inside scan() itself.

    Folds in the active tier's realised Sharpe (compute_tier_sharpe)
    ONLY when a tier currently owns the leg — Advisory Council's one
    piece of information with no other command surface after Advisory
    was retired. Everything else Advisory concatenated (block analysis,
    ATR suitability) already has its own standalone command
    (`/shadow blocked`, `/atrbands`) and isn't duplicated here.
    """
    current_state = state.get("market_thesis_current_state")
    if current_state is None:
        return ("📖 _No thesis recorded yet — the scanner needs to complete "
                "at least one scan after this update before `/thesis` has "
                "anything to show._")

    lines = [
        "📖 *Market Thesis*",
        "─────────────────────",
        f"*Current State:* {current_state}",
        "",
        f"*Transition:* {state.get('market_thesis_transition_narrative', '?')}",
        "",
        f"*Trend Health:* {state.get('market_thesis_trend_health', '?')}",
        "",
    ]

    # "What Changed" (Phase 2a, per chat — Layer 3 of the memo). Rendered
    # right after Trend Health so it reads as "here's where we are, here's
    # what moved" before Evidence/Weaknesses restate the current snapshot.
    # market_thesis_delta is a plain list of bullets from
    # classify_thesis_delta() — nothing here recomputes anything.
    delta = state.get("market_thesis_delta")
    if delta:
        lines.append("*Since Last Scan:*")
        lines.extend(f"  • {d}" for d in delta)
        lines.append("")

    # 5M texture (Phase 2c, per chat). Same optional/skip-if-missing
    # discipline as the 15M block above.
    m5 = state.get("market_thesis_mtf_5m")
    if m5 and m5.get("m5_direction"):
        rel_parts = []
        if m5.get("m5_relationship_to_htf"):
            rel_parts.append(f"vs HTF: {m5['m5_relationship_to_htf']}")
        if m5.get("m5_relationship_to_15m"):
            rel_parts.append(f"vs 15M: {m5['m5_relationship_to_15m']}")
        rel_txt = f" ({', '.join(rel_parts)})" if rel_parts else ""
        choch_txt = " · fresh CHoCH" if m5.get("m5_was_choch") else ""
        lines.append(f"*5M Read:* {m5['m5_direction']}{rel_txt}{choch_txt}")
        lines.append("")

    # Structural Interpretation / narrative stitch (Phase 2d, per chat —
    # Layer 4 of the memo). Deliberately short — see stitch_narrative()
    # (scanner_observation.py) for why this is templated, not freeform.
    narrative = state.get("market_thesis_narrative")
    if narrative:
        lines.append("*Story:*")
        lines.append(f"_{narrative}_")
        lines.append("")

    # 15M texture (Phase 1b, per chat — "cheap wins" pass). The current-state/
    # trend-health lines above are 1H-grain (Phase); this is the same read
    # one timeframe down, using numbers _leg_obs_formation_state() already
    # computed for tagging. Every sub-field is optional — a leg with no BOS
    # this scan (e.g. TRANSITION) just omits trend/pullback rather than
    # showing a misleading 0 or N/A for something that has no meaning yet.
    mtf = state.get("market_thesis_mtf_15m")
    if mtf:
        parts = []
        if mtf.get("trend_strength_atr_mult") is not None:
            parts.append(f"leg {mtf['trend_strength_atr_mult']:.1f}x ATR")
        if mtf.get("pullback_depth_pct") is not None:
            parts.append(f"pullback {mtf['pullback_depth_pct']:.0f}%")
        if mtf.get("compression_ratio") is not None:
            parts.append(f"compression {mtf['compression_ratio']:.1f}x")
        if mtf.get("volatility_state"):
            parts.append(f"15M vol {mtf['volatility_state']}")
        if parts:
            lines.append(f"*15M Read:* {' | '.join(parts)}")
            regime_bits = [
                mtf.get("atr_bucket"), mtf.get("session"),
                mtf.get("bias_state"), mtf.get("spike_state"),
            ]
            regime_bits = [b for b in regime_bits if b]
            if regime_bits:
                lines.append(f"  _regime: {' · '.join(regime_bits)}_")
            lines.append("")

    # Campaign Extension (per chat, 2026-08-12 — the friend's "long-
    # horizon maturity lens"). Same optional/skip-if-missing discipline
    # as every other additive block above — a fresh state.json or the
    # first leg since this shipped just omits the block entirely rather
    # than showing a misleading blank. Deliberately renders as its own
    # separate, neutrally-worded observation (no "X breaks = exhausted"
    # framing baked in here) — see compute_campaign_extension()'s
    # docstring for why this stays observation-only.
    campaign = state.get("market_thesis_campaign")
    if campaign:
        age_txt = f"{campaign['age_hours']:.0f}h" if campaign.get("age_hours") is not None else "?"
        lines.append("*Campaign Extension:*")
        lines.append(
            f"  {campaign['direction'].title()} campaign, origin `{campaign['origin']:.5f}` "
            f"({age_txt} ago, `{campaign['continuation_count']}` continuation leg"
            f"{'s' if campaign['continuation_count'] != 1 else ''})"
        )
        lines.append(
            f"  Initial impulse `{campaign['initial_impulse_pips']:.1f}p` → "
            f"total travel `{campaign['total_travel_pips']:.1f}p` "
            f"(`{campaign['extension_multiple']:.2f}x`)"
        )
        lines.append("")

    evidence = state.get("market_thesis_evidence") or []
    if evidence:
        lines.append("*Evidence:*")
        lines.extend(f"  ✓ {e}" for e in evidence)
        lines.append("")

    weaknesses = state.get("market_thesis_weaknesses") or []
    if weaknesses:
        lines.append("*Weaknesses:*")
        lines.extend(f"  • {w}" for w in weaknesses)
        lines.append("")

    lines.append(f"*Failure Risk:* {state.get('market_thesis_failure_risk', 'N/A')}")
    lines.extend(f"  — {r}" for r in (state.get("market_thesis_failure_risk_reasons") or []))
    lines.append("")

    expected = state.get("market_thesis_expected_next_event")
    lines.append(
        "*Expected Next Event:* "
        + (expected or "_no textbook expectation defined for this combination yet_")
    )
    lines.append("")

    lines.append(f"*Invalidation:* {state.get('market_thesis_invalidation', '?')}")
    lines.append("")

    confidence_line = state.get("market_thesis_confidence", "Research only")
    owner = get_leg_owner(state)
    if owner and owner.get("tier"):
        tier_label = owner["tier"]
        sharpe_result = compute_tier_sharpe(tier_label)
        if sharpe_result is not None:
            confidence_line += (
                f" — Tier {TIER_NUMBER.get(tier_label, '?')} realised Sharpe "
                f"≈{sharpe_result['sharpe']:+.2f} (n={sharpe_result['n']})"
            )
    lines.append(f"*Confidence:* {confidence_line}")
    lines.append("")
    lines.append(f"_Updated {state.get('market_thesis_updated_at', '?')}_")

    return "\n".join(lines)


def format_shadow_leaderboard(shadow_stats, min_resolved=5):
    """Ranks every experiment by average R. Experiments below
    min_resolved resolved trades are marked Insufficient Data rather
    than ranked — an avg R from 2 trades is noise, not a result."""
    ranked, insufficient = [], []
    for key in _SHADOW_STATS_EXPERIMENT_KEYS:
        s = shadow_stats.get(key, _empty_experiment_stat())
        if s["logged"] == 0:
            continue
        if s["resolved"] < min_resolved:
            insufficient.append(key)
            continue
        avg_r = s["sum_r"] / s["resolved"]
        ranked.append((key, avg_r, s["resolved"]))

    if not ranked and not insufficient:
        return None

    ranked.sort(key=lambda t: -t[1])
    lines = ["🏆 *Experiment Leaderboard*", "─────────────────────"]
    pos = 1
    for key, avg_r, n in ranked:
        lines.append(f"{pos}. *{_SHADOW_DISPLAY_NAME.get(key, key)}*\n   AvgR `{avg_r:+.2f}` ({n} resolved)")
        pos += 1
    for key in insufficient:
        s = shadow_stats.get(key, _empty_experiment_stat())
        lines.append(f"{pos}. *{_SHADOW_DISPLAY_NAME.get(key, key)}*\n   Insufficient Data ({s['resolved']}/{min_resolved} resolved)")
        pos += 1
    return "\n".join(lines)


def format_shadow_recent(limit=10):
    """Last N resolved shadow trades across every experiment, most
    recent first — a fast way to spot patterns without scrolling
    through Telegram history."""
    records = _read_shadow_trade_log(limit=200)  # read a bit extra, then trim
    if not records:
        return None
    records = records[-limit:][::-1]
    lines = [f"🕒 *Shadow — Last {len(records)} Resolved*", "─────────────────────"]
    for r in records:
        try:
            t = datetime.fromisoformat(r["resolved_at"]).strftime("%H:%M")
        except Exception:
            t = "??:??"
        name = _SHADOW_DISPLAY_NAME.get(r.get("experiment"), r.get("experiment", "?"))
        outcome = r.get("outcome", "?")
        icon = "✅" if r.get("r_achieved", 0.0) > 0 else "❌"
        tid = r.get("trade_id")
        tid_str = f"  `id:{tid}`" if tid else ""
        lines.append(f"`{t}`  {name}  {r.get('direction','?')}  {icon} {outcome}  "
                      f"`{r.get('r_achieved', 0):+.1f}R`{tid_str}")
    return "\n".join(lines)


# =========================================================================

# ---- /biasab formatter — pure read of bias_ab_log, lives here so live
# can import it for instant replies without needing MIN's other imports
def format_bias_ab_summary(bias_ab_log):
    """
    /biasab — live (gated CHoCH+BOS+EMA) vs shadow (old ungated rule) 1H
    bias agreement, scan by scan. Ported from V6's format_shadow_summary,
    renamed to avoid colliding with V3's own format_shadow_summary()
    (which summarizes the 6-experiment research pipeline — a different
    thing entirely, kept under /shadow).
    """
    if not bias_ab_log:
        return "🕶️ _No bias A/B log entries yet — give it a few scans._"

    n = len(bias_ab_log)
    agree = sum(1 for e in bias_ab_log if e.get("agree"))
    diverge = n - agree
    agree_pct = f"{agree/n*100:.0f}%"

    first_t = bias_ab_log[0].get("time", "?")
    last_t  = bias_ab_log[-1].get("time", "?")

    lines = [
        "",
        "🕶️ *Bias A/B — Live (gated) vs Old Rule (ungated)*",
        f"_Period: {first_t} → {last_t}_",
        "─────────────────────",
        f"🔍 Scans logged: `{n}`",
        f"🤝 Agreement:    `{agree}/{n}` ({agree_pct})",
        f"↔️ Divergence:   `{diverge}`",
        "─────────────────────",
    ]

    if diverge == 0:
        lines.append("_No divergences yet._")
    else:
        windows = []
        current = None
        for e in bias_ab_log:
            if not e.get("agree"):
                if current is None:
                    current = {"start": e["time"], "end": e["time"],
                               "live": e["live_bias"], "shadow": e["shadow_bias"]}
                else:
                    current["end"] = e["time"]
            else:
                if current is not None:
                    windows.append(current)
                    current = None
        if current is not None:
            windows.append(current)

        lines.append(f"*Recent divergence windows* (last {min(len(windows), 8)} of {len(windows)}):")
        for w in windows[-8:]:
            lines.append(f"  `{w['start']}` → `{w['end']}` — live=`{w['live']}` vs old-rule=`{w['shadow']}`")

    return "\n".join(lines)


# =========================================================================
# CANDLE CACHE — reader. scanner_live.py's write_candle_cache() writes
# {"cached_at": iso, "5min": [...], "15min": [...], "1h": [...]}, where
# each list is df.reset_index().to_dict(orient="records") with the index
# column explicitly renamed to "datetime" first (plain to_dict(orient=
# "records") silently DROPS a DataFrame's index — caught this while
# writing this reader, fixed on the writer side too).
# =========================================================================
def _deserialize_cached_df(records):
    df = pd.DataFrame(records)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    return df.set_index("datetime").sort_index()


def load_candle_cache(max_age_minutes=20):
    """Returns {"cached_at", "df_5m", "df_15m", "df_1h"} or None if the
    cache is missing, unparseable, or older than max_age_minutes (e.g.
    scanner_live.py hasn't run recently — weekend, outage, etc.). Callers
    treat None as "skip candle-dependent work this pass," not an error."""
    try:
        with open(CANDLE_CACHE_FILE, "r") as f:
            cache = json.load(f)
        cached_at = datetime.fromisoformat(cache["cached_at"])
    except Exception:
        return None

    if (datetime.now(timezone.utc) - cached_at) > timedelta(minutes=max_age_minutes):
        return None

    try:
        return {
            "cached_at": cached_at,
            "df_5m":  _deserialize_cached_df(cache["5min"]),
            "df_15m": _deserialize_cached_df(cache["15min"]),
            "df_1h":  _deserialize_cached_df(cache["1h"]),
        }
    except Exception as e:
        print("[CANDLE CACHE PARSE ERROR] " + str(e))
        return None


# =========================================================================
# ORCHESTRATOR — one pass, called on MIN's own cron/Cronjob.com schedule.
# Mirrors what run_shadow_pipeline() + the Forward Observation/Markov call
# sites in the original monolith's scan() used to do, minus everything
# that depended on `live_result` (see drain_rejected_live_queue above) and
# minus every save_state() call — this process reads state.json as a
# read-only snapshot and NEVER writes it back. apply_state_updates() below
# only mutates the in-memory snapshot for this pass's own use.
# =========================================================================
# ---- Experiment 8: CISD (Change In State of Delivery) — reversal ----------
# UNLIKE every other experiment above, this one deliberately fires AGAINST
# facts.macro_bias, not aligned with it. A bullish CISD requires a
# downtrend first (macro_bias == BEARISH), a bearish CISD requires an
# uptrend (macro_bias == BULLISH) — it's testing "did the trend just
# reverse at a key level," not "is this trend continuing." Keep that in
# mind reading its win rate against EXP1-7's — different hypothesis
# entirely, not a fair head-to-head.

_EXP8_VARIANTS = [
    # (variant_name, htf_for_key_level, ltf_for_cisd_pattern)
    # htf==ltf entries are the single-timeframe variants (the level AND
    # the CISD candle pattern both come from the same timeframe's own
    # leg); htf!=ltf entries are the HTF-confirmation variants your
    # original CISD description asked for (identify the key level on a
    # higher timeframe, wait for the CISD to confirm on a lower one).
    ("1h_to_5m",   "1h",  "5m"),
    ("1h_to_15m",  "1h",  "15m"),
    ("15m_to_5m",  "15m", "5m"),
    ("5m_only",    "5m",  "5m"),
    ("15m_only",   "15m", "15m"),
    ("1h_only",    "1h",  "1h"),
]


def _exp8_leg_extremes_for_tf(facts, timeframe):
    """(swing_high, swing_low) for the given KEY-LEVEL timeframe, or
    (None, None) if no valid leg exists there.

    "1h" reuses the already-tracked macro leg (facts.swing_high/low —
    the same leg state.json's macro_swing_high/low is built from)
    rather than re-deriving an independent one that could disagree with
    the rest of the system's idea of "the 1H leg."

    "15m"/"5m" derive a fresh BOS leg from that timeframe's own
    candles — the exact same detect_bos_impulse() call Experiment 2's
    _exp2_bos_5m() already uses for its 5m leg, generalized here to
    take either dataframe so 15m gets identical treatment."""
    if timeframe == "1h":
        return facts.swing_high, facts.swing_low
    df = facts.df_15m if timeframe == "15m" else facts.df_5m
    bos = detect_bos_impulse(df.tail(60), wing=FRACTAL_WING)
    if bos is None:
        return None, None
    d = bos["direction"]
    if d == "BULLISH":
        return bos["impulse_end"], bos["impulse_start"]   # sh, sl
    return bos["impulse_start"], bos["impulse_end"]         # sh, sl


def _exp8_ltf_df(facts, timeframe):
    return {"5m": facts.df_5m, "15m": facts.df_15m, "1h": facts.df_1h}[timeframe]


def _exp8_key_level_touched(level, ltf_df, tolerance_pips=ZONE_TOLERANCE_PIPS):
    """Same last-two-candle proximity check MarketFacts.price_in_zone()
    uses internally, but generalized to whichever LTF dataframe this
    variant is checking against — price_in_zone() itself is hardcoded
    to facts.df_5m, so it can't be reused directly for 15m/1h variants."""
    tol = tolerance_pips * PIP_SIZE
    c, cp = ltf_df.iloc[-1], ltf_df.iloc[-2]
    lo = min(c["Low"],  cp["Low"])
    hi = max(c["High"], cp["High"])
    return lo <= level + tol and hi >= level - tol


def _exp8_detect_cisd(df):
    """Looks for a CISD confirmation on the most recent candle close of
    whichever timeframe `df` is (5m/15m/1h — this function is timeframe-
    agnostic, callers pass in whichever LTF dataframe applies).
    Returns (direction, failure_level, entry) or None.

    Walks backward from the second-to-last candle collecting a contiguous
    run of same-colored candles (the "preceding candle(s)" the rules
    describe), then checks whether the LAST candle is the opposite color
    and its CLOSE breaks past that run's extreme — not just a wick, a
    close, per the "Close Matters" rule. failure_level is that run's own
    extreme plus the CISD candle's own extreme, per the "Failure Level"
    rule (CISD candle's low for bullish, high for bearish) — using the
    tighter of the two (whichever is closer to entry) as the stop, since
    the rules describe the CISD candle's own extreme specifically.
    """
    if len(df) < 3:
        return None

    trigger = df.iloc[-1]
    trigger_bullish = trigger["Close"] > trigger["Open"]
    trigger_bearish = trigger["Close"] < trigger["Open"]
    if not (trigger_bullish or trigger_bearish):
        return None  # doji — no decisive close either way

    # Collect the contiguous opposite-colored run immediately before the
    # trigger candle (walking backward from the second-to-last candle).
    run_high, run_low = None, None
    i = len(df) - 2
    while i >= 0 and i >= len(df) - 1 - 10:  # cap the lookback at 10 candles
        c = df.iloc[i]
        c_bullish = c["Close"] > c["Open"]
        c_bearish = c["Close"] < c["Open"]
        if trigger_bullish and not c_bearish:
            break   # run of bearish candles ended
        if trigger_bearish and not c_bullish:
            break   # run of bullish candles ended
        run_high = c["High"] if run_high is None else max(run_high, c["High"])
        run_low  = c["Low"]  if run_low  is None else min(run_low,  c["Low"])
        i -= 1

    if run_high is None or run_low is None:
        return None  # no opposite-colored candle immediately precedes the trigger

    if trigger_bullish and trigger["Close"] > run_high:
        # Bullish CISD confirmed — failure level is the LOW of the CISD
        # candle (per the rules), floored further by the preceding run's
        # own low so the stop never sits inside the reversal candle body
        # if the run's low happened to be lower.
        failure_level = min(trigger["Low"], run_low)
        return "BULLISH", failure_level, float(trigger["Close"])

    if trigger_bearish and trigger["Close"] < run_low:
        failure_level = max(trigger["High"], run_high)
        return "BEARISH", failure_level, float(trigger["Close"])

    return None


def experiment_8_cisd(facts, current_atr_pips, shadow_state, shadow_stats, now_utc):
    """Logs a hypothetical reversal trade for EVERY CISD variant in
    _EXP8_VARIANTS that confirms this scan — testing which timeframe
    combination (if any) actually has an edge, per Nelly's "let's test
    different variables" ask. All six variants share identical pattern-
    detection and trend-context logic; they only differ in WHICH
    timeframe supplies the key level (HTF) and WHICH timeframe the CISD
    candle pattern is detected on (LTF). Each variant dedups and caps
    independently (see _dedup_key — already variant-aware, no extra
    plumbing needed), and /shadow cisd breaks out win rate/avg-R per
    variant the same way it already does for EXP2's fib-level variants.

    NOTE, same as before: every variant fires AGAINST facts.macro_bias,
    not aligned with it — this is a reversal hypothesis, not a
    continuation one, unlike EXP1-7."""
    for variant, htf, ltf in _EXP8_VARIANTS:
        ltf_df = _exp8_ltf_df(facts, ltf)
        cisd = _exp8_detect_cisd(ltf_df)
        if cisd is None:
            continue
        direction, failure_level, entry = cisd

        required_bias = "BEARISH" if direction == "BULLISH" else "BULLISH"
        if facts.macro_bias != required_bias:
            continue

        swing_high, swing_low = _exp8_leg_extremes_for_tf(facts, htf)
        if swing_high is None or swing_low is None:
            continue
        key_level = swing_low if direction == "BULLISH" else swing_high
        if not _exp8_key_level_touched(key_level, ltf_df):
            continue

        trigger_candle_time = ltf_df.index[-1]
        side = bias_to_side(direction)
        leg_id = "{}|{}|{:.5f}|{}".format(variant, side, failure_level, trigger_candle_time.isoformat())

        setup = build_shadow_setup(
            "EXP8_CISD", side, entry, failure_level, now_utc,
            variant=variant,
            tags={
                "reversal_against_bias": True,
                "htf": htf, "ltf": ltf,
                "key_level": "swing_low" if direction == "BULLISH" else "swing_high",
            },
            note="CISD ({}) confirmed at {} key level, reversing against 1H {} bias".format(
                variant, htf, required_bias),
            atr_pips=current_atr_pips,
        )
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)


def run_min_pass():
    now_utc = datetime.now(timezone.utc)
    state = load_state()  # read-only snapshot — never saved back from here
    shadow_state = load_shadow_state()
    shadow_stats = load_shadow_stats()

    cache = load_candle_cache()
    if cache is None:
        print("[MIN] candle cache missing/stale — skipping candle-dependent "
              "work this pass (weekend, or scanner_live.py hasn't run recently).")
        df_5m = df_15m = df_1h = None
    else:
        df_5m, df_15m, df_1h = cache["df_5m"], cache["df_15m"], cache["df_1h"]

    # Resolve anything already pending against the freshest candles we have
    # — this can run even on a slightly-stale cache, since it's just
    # checking historical high/low against already-open shadow setups.
    if df_5m is not None:
        try:
            update_pending_shadow_setups(shadow_state, shadow_stats, df_5m, now_utc)
        except Exception as e:
            print("[SHADOW ERROR] update_pending: " + str(e))
        save_shadow_state(shadow_state)
        save_shadow_stats(shadow_stats)

    # Rejected-live queue drains regardless of cache freshness — it's just
    # replaying facts scanner_live.py already queued, no fresh candles needed.
    try:
        n_drained = drain_rejected_live_queue(shadow_state, shadow_stats)
        if n_drained:
            print(f"[MIN] drained {n_drained} rejected-live record(s) from queue")
    except Exception as e:
        print("[SHADOW ERROR] drain_rejected_live_queue: " + str(e))
    save_shadow_state(shadow_state)
    save_shadow_stats(shadow_stats)

    if df_5m is None or len(df_1h) < HTF_BIAS_MIN_BARS:
        return  # nothing candle-dependent left we can safely do this pass

    # ── Rebuild bias exactly like live does, against the read-only snapshot ──
    macro_bias, bias_updates = compute_macro_bias(df_1h, df_15m, state)
    apply_state_updates(state, bias_updates)  # mutates the LOCAL snapshot only
    bias_stale = state.get("macro_bias_stale", False)

    # ── Market Evolution (Markov) — NOTE: deliberately NOT run here.
    # record_markov_transition() is explicitly a "one scan-to-scan
    # transition per call" model (see its own docstring) tied to a single
    # cadence. Running it here too, on MIN's independent schedule, would
    # both double-count transitions against live's copy AND race both
    # processes on the same full-file rewrite of markov_state.json — same
    # class of bug the rejected-live queue exists to avoid for shadow_
    # state.json. Markov recording is scanner_live.py's job only; MIN only
    # ever READS markov_state.json (via load_markov_data, read-only) for
    # the /markov command's report formatter. ──

    if macro_bias == "CONSOLIDATION":
        return

    swing_high = state.get("macro_swing_high")
    swing_low = state.get("macro_swing_low")
    if swing_high is None or swing_low is None:
        return

    ctx, ctx_reason, _ctx_updates = evaluate_market_context(df_5m, state, now_utc)
    facts = MarketFacts(df_5m, df_15m, df_1h, macro_bias, swing_high, swing_low, now_utc)

    # ── Forward Observation — runs every pass with a directional bias ──
    try:
        obs_state = load_leg_obs_state()
        obs_state = run_leg_observation(
            facts, ctx, state, macro_bias, bias_stale, now_utc, obs_state)
        save_leg_obs_state(obs_state)
    except Exception as e:
        print("[LEG OBS ERROR] " + str(e))

    # ── Experimental Lab — same 7 experiments run_shadow_pipeline used to
    # fire every live scan, run here instead against the cached candles. ──
    current_atr_pips = ctx.current_atr_pips
    atr_15m_series = atr(df_15m, period=14)
    experiments = [
        ("Experiment 1 (Structure)", lambda: experiment_1_structure(
            facts, current_atr_pips, shadow_state, shadow_stats, now_utc)),
        ("Experiment 2 (Fib)", lambda: experiment_2_fib(
            facts, current_atr_pips, shadow_state, shadow_stats, now_utc)),
        ("Experiment 3 (POI)", lambda: experiment_3_poi(
            facts, atr_15m_series, current_atr_pips, shadow_state, shadow_stats, now_utc)),
        ("Experiment 4 (Policy Lab)", lambda: experiment_4_policy_lab(
            facts, ctx, state, now_utc, shadow_state, shadow_stats)),
        ("Experiment 5 (Filter Ablation)", lambda: experiment_5_filter_ablation(
            facts, ctx, state, df_15m, shadow_state, shadow_stats, now_utc)),
        ("Experiment 6 (Alt Bias)", lambda: experiment_6_alt_bias(
            facts, state, current_atr_pips, shadow_state, shadow_stats, now_utc)),
        ("Experiment 7 (Tier ATR Mirror)", lambda: experiment_7_tier_atr_mirror(
            facts, ctx, state, now_utc, shadow_state, shadow_stats)),
        ("Experiment 8 (CISD)", lambda: experiment_8_cisd(
            facts, current_atr_pips, shadow_state, shadow_stats, now_utc)),
    ]
    for name, fn in experiments:
        try:
            fn()
        except NameError as e:
            # NameError here almost always means a def/helper this experiment
            # needs got placed below the `if __name__ == "__main__":` guard
            # (see the banner comment above that guard) or was renamed/deleted
            # without updating this dispatch list — a STRUCTURAL bug, not a
            # data/runtime one. The generic except below would otherwise bury
            # this as a routine one-line log print, exactly what let
            # Experiment 8 (CISD) silently no-op on every pass for a full day
            # before anyone noticed. Escalate distinctly so it's impossible
            # to miss.
            msg = f"[SHADOW ERROR — STRUCTURAL] {name}: {e}"
            print(msg)
            try:
                send_telegram(
                    f"\u26a0\ufe0f {name} threw NameError and did not run "
                    f"this pass: {e}\nThis usually means a function it "
                    f"depends on is defined below the `if __name__` guard "
                    f"in min_scanner.py, or was renamed without updating "
                    f"the experiments dispatch list."
                )
            except Exception:
                pass
        except Exception as e:
            print(f"[SHADOW ERROR] {name}: {e}")

    save_shadow_state(shadow_state)
    save_shadow_stats(shadow_stats)

    print(f"[MIN] pass complete — bias={macro_bias}"
          f"{' (STALE)' if bias_stale else ''}, ATR={current_atr_pips:.1f}p")


# =============================================================================
# ⚠️  STOP — READ BEFORE ADDING ANYTHING BELOW THIS LINE  ⚠️
# =============================================================================
# This `if __name__ == "__main__":` guard MUST be the last thing in the file.
# Python executes top-to-bottom: any def/class placed AFTER this guard does
# not exist yet when this line runs, because the guard fires immediately
# during module execution — Python hasn't reached the later code yet.
#
# THIS EXACT BUG ALREADY HAPPENED ONCE: Experiment 8 (CISD) — its function,
# variants list, and helper were all written below this guard. Every MIN
# pass silently threw NameError: "name 'experiment_8_cisd' is not defined",
# caught by the generic `except Exception` in the experiments loop below,
# and logged as a one-line [SHADOW ERROR] that's easy to miss in Action
# logs. Experiment 8 never ran for as long as it sat below this line.
#
# If you're adding a new experiment or helper function: put it ABOVE
# `def run_min_pass():`, not here. If you're not sure whether something
# needs to go above, it does — nothing should be added below this comment
# except this guard itself.
# =============================================================================
if __name__ == "__main__":
    run_min_pass()
