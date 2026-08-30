"""
Edge Finder — periodic, sample-triggered research over the accumulated
Trade Investigation Bureau dataset. Reads shadow_trade_log.jsonl
directly (NOT failure_case_log.jsonl — every trade's own record already
carries conditions_at_resolution/environment/tags since the Trade
Investigation Bureau expansion, so no join is needed here the way
shadow_investigation.py needs one against leg_obs_log.jsonl).

DESIGN CREDIT: this whole module implements Vally's Edge Finder
proposal (per chat, 2026-08-29) as directly as possible. Key principles
carried over, not just paraphrased:

  "The Edge Finder should discover hypotheses, not declare truth."
  -> the highest label this module ever assigns is ROBUST_CANDIDATE.
  Nothing here is ever called "validated" or "an edge" as settled fact
  — validation is EXP-style controlled testing, a human/engineering
  decision outside this script's scope entirely.

  "Not brute force... protection against combinatorial insanity."
  -> DIMENSIONS below is a small, fixed, explicit list — no searching
  over arbitrary thresholds or feature combinations. Level 2
  (interactions) only tests PAIRS where BOTH dimensions individually
  reached SIGNAL in Level 1 — bounded by what Level 1 already found,
  not all C(n,2) pairs.

  "The Edge Finder should also be able to find non-edges."
  -> every dimension tested gets a result, including NO_SEPARATION and
  INSUFFICIENT_DATA — these are first-class outputs, not silently
  dropped. A cycle that finds nothing new says so explicitly.

  "It should be capable of finding stable conditions, not just highest
  historical return."
  -> promotion from SIGNAL to CANDIDATE_EDGE requires a chronological
  time-split stability check (same direction of effect in both halves),
  not just a large gap on the pooled sample.

HIERARCHY (matches Vally's exactly):
  OBSERVATION      — a gap exists, but sample too small to trust yet.
  SIGNAL           — gap clears the sample bar on the pooled dataset.
  CANDIDATE_EDGE   — SIGNAL survives a chronological time-split.
  ROBUST_CANDIDATE — CANDIDATE_EDGE also holds within each experiment
                     it appears in (not just pooled across all of them).
  (VALIDATED_EDGE does not exist as a label this module can assign —
  see above.)

NOT ATTEMPTED THIS PASS (stated plainly): Level 2 interaction testing
here reports the crosstab for qualifying pairs — it does NOT run a
rigorous interaction-effect test (e.g. checking whether a cell's
expectancy deviates from what the two marginals alone would predict).
That is a real, harder statistical question; this module surfaces the
data for a human to look at, it doesn't claim to have already answered
it. Flagged in the report itself, not just here.
"""
import json
from collections import defaultdict
from datetime import datetime, timezone

from investigation_lib import load_jsonl, parse_ts, _r_stats, CONDITION_DIMENSIONS, _display_value

# Bounded, explicit dimension list (per chat — "not brute force").
# LEG_DIMENSIONS come from conditions_at_resolution (leg-formation-time
# snapshot — see peek_conditions_at_resolution() in min_scanner.py).
# ENV_CATEGORICAL/ENV_CONTINUOUS come from `environment` (thesis-level,
# same-scan — see peek_environment_at_resolution()). Continuous env
# fields get data-driven tertile buckets (computed fresh each cycle,
# not fixed thresholds) since they have no pre-established bucket
# convention the way atr_bucket/session etc already do.
LEG_DIMENSIONS = CONDITION_DIMENSIONS  # market_phase, atr_bucket, session, bias_state, spike_state, measured_move_bucket
ENV_CATEGORICAL = ["volatility_state"]
ENV_CONTINUOUS = ["compression_ratio", "trend_strength_atr_mult", "pullback_depth_pct"]

# DELIBERATELY EXCLUDED, not forgotten: max_r_reached, min_r_reached,
# path_classification, bars_open. These are OUTCOME-SIDE descriptors —
# path_classification is LITERALLY DERIVED from r_achieved (see
# classify_trade_path() in min_scanner.py) — using them as "predictor"
# dimensions here would be circular (of course clean_winner correlates
# with winning; it's defined that way). A contrast dimension has to be
# something knowable at/before entry, not a description of how the
# trade played out afterward.

MIN_GROUP_N = 15          # per-value-group sample floor before a comparison counts at all
MIN_POOLED_N = 40         # combined sample floor to promote OBSERVATION -> SIGNAL
MIN_EFFECT_R = 0.15       # minimum expectancy gap (R) to not be "no meaningful separation"


def load_trades(path, experiment=None):
    """Reads shadow_trade_log.jsonl directly — every record already
    carries conditions_at_resolution/environment/tags per-trade, no
    join needed (unlike shadow_investigation.py's leg_obs_log.jsonl
    join, which exists for a DIFFERENT reason — see that script's own
    docstring)."""
    trades = load_jsonl(path)
    if experiment:
        trades = [t for t in trades if t.get("experiment") == experiment]
    return [t for t in trades if parse_ts(t.get("resolved_at")) is not None]


def _bucket_continuous(trades, field):
    """
    Data-driven tertile bucketing for a continuous environment field.
    Computed FRESH each cycle from whatever data is available this
    run — not a fixed threshold, since these fields (compression_ratio
    etc.) have no established bucket convention the way atr_bucket
    already does. Returns {trade_index_id: "low"|"mid"|"high"} — trades
    missing the field entirely are simply absent from the returned
    dict (not bucketed as anything), same "don't fabricate" discipline
    as everywhere else in this project's investigation tooling.

    Needs at least 3x MIN_GROUP_N values to bother — tertiles on a
    tiny sample are noise dressed as buckets.
    """
    values = []
    for i, t in enumerate(trades):
        v = (t.get("environment") or {}).get(field)
        if v is not None:
            values.append((i, v))
    if len(values) < MIN_GROUP_N * 3:
        return {}
    sorted_vals = sorted(v for _, v in values)
    n = len(sorted_vals)
    low_cut = sorted_vals[n // 3]
    high_cut = sorted_vals[(2 * n) // 3]
    buckets = {}
    for i, v in values:
        if v <= low_cut:
            buckets[i] = "low"
        elif v >= high_cut:
            buckets[i] = "high"
        else:
            buckets[i] = "mid"
    return buckets


def _value_for_dimension(trades, idx, dim, continuous_buckets):
    t = trades[idx]
    if dim in LEG_DIMENSIONS:
        ctx = t.get("conditions_at_resolution") or {}
        return _display_value(dim, ctx.get(dim)) if ctx else None
    if dim in ENV_CATEGORICAL:
        env = t.get("environment") or {}
        return env.get(dim) or "unknown"
    if dim in ENV_CONTINUOUS:
        return continuous_buckets.get(dim, {}).get(idx)
    return None


def _dimension_contrast(trades, dim, continuous_buckets):
    """
    LEVEL 1 — single-dimension contrast. Groups trades by value, runs
    _r_stats() per group (reused from investigation_lib.py — same win
    rate/expectancy/PF math the offline investigation scripts use, R-
    sign based per that module's own docstring on why), and reports the
    largest expectancy gap between any two groups that BOTH clear
    MIN_GROUP_N.

    Returns a dict describing the result — ALWAYS returns something,
    including the "nothing here" cases (per chat: "The Edge Finder
    should also be able to find non-edges" / "a proper researcher must
    be allowed to conclude nothing here").
    """
    by_value = defaultdict(list)
    for i, t in enumerate(trades):
        val = _value_for_dimension(trades, i, dim, continuous_buckets)
        if val is not None:
            by_value[val].append(t)

    groups = {val: _r_stats(rows) for val, rows in by_value.items()}
    qualifying = {val: s for val, s in groups.items() if s["n"] >= MIN_GROUP_N}

    if len(qualifying) < 2:
        return {"dimension": dim, "status": "INSUFFICIENT_DATA", "groups": groups}

    best_val = max(qualifying, key=lambda v: qualifying[v]["expectancy"])
    worst_val = min(qualifying, key=lambda v: qualifying[v]["expectancy"])
    gap = qualifying[best_val]["expectancy"] - qualifying[worst_val]["expectancy"]
    pooled_n = sum(s["n"] for s in qualifying.values())

    if gap < MIN_EFFECT_R:
        return {"dimension": dim, "status": "NO_SEPARATION", "groups": qualifying, "gap": gap}

    status = "SIGNAL" if pooled_n >= MIN_POOLED_N else "OBSERVATION"
    return {
        "dimension": dim, "status": status, "groups": qualifying,
        "best_value": best_val, "worst_value": worst_val, "gap": gap, "pooled_n": pooled_n,
    }


def _time_split_stability(trades, dim, continuous_buckets, best_value, worst_value):
    """
    LEVEL 1 -> CANDIDATE_EDGE promotion check. Splits trades
    chronologically (by resolved_at) into two halves and recomputes
    best_value's expectancy minus worst_value's expectancy on EACH
    half independently. Stable (same sign, i.e. best_value is still
    better than worst_value) in BOTH halves -> promote. Per chat:
    "It should be capable of finding stable conditions, not just
    highest historical return" — this is that check, not a p-value.

    Returns (is_stable: bool, half1_gap, half2_gap) — gaps are None for
    a half if either group didn't clear MIN_GROUP_N on that half alone
    (a real, common outcome for a half-sized sample — reported as
    "not enough data to check stability" rather than silently assumed
    stable).
    """
    sorted_trades = sorted(range(len(trades)), key=lambda i: parse_ts(trades[i].get("resolved_at")))
    mid = len(sorted_trades) // 2
    halves = [sorted_trades[:mid], sorted_trades[mid:]]

    half_gaps = []
    for half_idx in halves:
        best_rows = [trades[i] for i in half_idx
                     if _value_for_dimension(trades, i, dim, continuous_buckets) == best_value]
        worst_rows = [trades[i] for i in half_idx
                      if _value_for_dimension(trades, i, dim, continuous_buckets) == worst_value]
        best_stats, worst_stats = _r_stats(best_rows), _r_stats(worst_rows)
        if best_stats["n"] >= MIN_GROUP_N and worst_stats["n"] >= MIN_GROUP_N:
            half_gaps.append(best_stats["expectancy"] - worst_stats["expectancy"])
        else:
            half_gaps.append(None)

    if any(g is None for g in half_gaps):
        return False, half_gaps[0], half_gaps[1]
    is_stable = half_gaps[0] > 0 and half_gaps[1] > 0
    return is_stable, half_gaps[0], half_gaps[1]


def _experiment_split_stability(trades, dim, continuous_buckets, best_value, worst_value):
    """
    CANDIDATE_EDGE -> ROBUST_CANDIDATE promotion check. Same idea as
    the time split, but splits by `experiment` instead of chronology —
    per chat, a robust candidate should hold up as something more than
    an artifact of one specific experiment's own setup logic. Only
    meaningful when the pooled sample actually spans 2+ experiments;
    returns False with a clear reason otherwise rather than trivially
    promoting a single-experiment finding.
    """
    by_experiment = defaultdict(list)
    for t in trades:
        by_experiment[t.get("experiment")].append(t)

    if len(by_experiment) < 2:
        return False, "only one experiment in this dataset — can't check cross-experiment stability"

    confirmed_in = 0
    for exp, exp_trades in by_experiment.items():
        best_rows = [t for t in exp_trades
                     if _value_for_dimension(exp_trades, exp_trades.index(t), dim, continuous_buckets) == best_value]
        worst_rows = [t for t in exp_trades
                      if _value_for_dimension(exp_trades, exp_trades.index(t), dim, continuous_buckets) == worst_value]
        best_stats, worst_stats = _r_stats(best_rows), _r_stats(worst_rows)
        if best_stats["n"] >= MIN_GROUP_N and worst_stats["n"] >= MIN_GROUP_N:
            if best_stats["expectancy"] > worst_stats["expectancy"]:
                confirmed_in += 1

    if confirmed_in >= 2:
        return True, f"held in the same direction across {confirmed_in} experiments"
    return False, f"only confirmed in {confirmed_in} experiment(s) with enough data — not robust yet"


def run_edge_finder_cycle(trades, dimensions=None):
    """
    Runs the full Level 1 -> stability -> Level 2 pipeline. Returns a
    list of per-dimension result dicts (Level 1 findings, promoted
    where the data supports it) plus a separate list of Level 2
    interaction results for any pair that both reached at least SIGNAL.
    """
    dimensions = dimensions or (LEG_DIMENSIONS + ENV_CATEGORICAL + ENV_CONTINUOUS)
    continuous_buckets = {f: _bucket_continuous(trades, f) for f in ENV_CONTINUOUS}

    level1_results = []
    signal_dims = []
    for dim in dimensions:
        result = _dimension_contrast(trades, dim, continuous_buckets)
        if result["status"] in ("SIGNAL", "OBSERVATION"):
            is_stable, h1, h2 = _time_split_stability(
                trades, dim, continuous_buckets, result["best_value"], result["worst_value"])
            result["time_split_gaps"] = (h1, h2)
            if result["status"] == "SIGNAL" and is_stable:
                result["status"] = "CANDIDATE_EDGE"
                robust, reason = _experiment_split_stability(
                    trades, dim, continuous_buckets, result["best_value"], result["worst_value"])
                result["experiment_split_reason"] = reason
                if robust:
                    result["status"] = "ROBUST_CANDIDATE"
            elif result["status"] == "SIGNAL":
                result["status"] = "SIGNAL"  # explicitly stays — not stable enough to promote
                result["stability_note"] = "did not survive chronological time-split — treat as unstable, not yet a candidate"
        if result["status"] in ("SIGNAL", "CANDIDATE_EDGE", "ROBUST_CANDIDATE"):
            signal_dims.append(dim)
        level1_results.append(result)

    # LEVEL 2 — only pairs where BOTH dimensions reached at least SIGNAL
    # in Level 1 (per chat: bounded by what Level 1 found, not all pairs).
    level2_results = []
    for i in range(len(signal_dims)):
        for j in range(i + 1, len(signal_dims)):
            dim1, dim2 = signal_dims[i], signal_dims[j]
            crosstab = defaultdict(list)
            for idx, t in enumerate(trades):
                v1 = _value_for_dimension(trades, idx, dim1, continuous_buckets)
                v2 = _value_for_dimension(trades, idx, dim2, continuous_buckets)
                if v1 is not None and v2 is not None:
                    crosstab[(v1, v2)].append(t)
            cells = {pair: _r_stats(rows) for pair, rows in crosstab.items() if len(rows) >= MIN_GROUP_N}
            if cells:
                level2_results.append({"dim1": dim1, "dim2": dim2, "cells": cells})

    return level1_results, level2_results


def _load_jsonl_soft(path):
    """
    Like investigation_lib.load_jsonl(), but returns [] on a missing
    file instead of exiting the process. ONLY for edge_finder_log.jsonl
    — that file not existing yet is the NORMAL first-run state (Edge
    Finder creates it), unlike shadow_trade_log.jsonl/leg_obs_log.jsonl
    being missing, which genuinely is a setup problem worth halting on
    (see investigation_lib.load_jsonl()'s own behavior — deliberately
    NOT reused here for that reason).
    """
    try:
        records = []
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records
    except FileNotFoundError:
        return []


def load_last_cycle_count(log_path):
    """Reads the trade count recorded by the most recent cycle, for the
    sample-based trigger gate (see should_run_cycle() below). 0 if the
    log doesn't exist yet — the very first cycle always runs."""
    cycles = _load_jsonl_soft(log_path)
    if not cycles:
        return 0
    return cycles[-1].get("trade_count", 0)


def should_run_cycle(current_trade_count, last_cycle_count, min_new_trades=25):
    """
    Sample-based trigger (per chat — Vally: "7 days passing does not
    create new evidence. New resolved observations do... I actually
    like sample-based triggers more"). Returns (should_run, reason).
    """
    new_trades = current_trade_count - last_cycle_count
    if new_trades < min_new_trades:
        return False, (f"Only {new_trades} new resolved trades since the last cycle "
                        f"(need {min_new_trades}+) — skipping. Run with --force to override.")
    return True, f"{new_trades} new resolved trades since the last cycle — running."


def append_cycle_record(log_path, trade_count, level1_results, level2_results):
    # Level 2's cells use tuple keys ((v1, v2) -> stats) — JSON can't
    # serialize non-string dict keys even with default=str (that hook
    # only covers VALUES, not keys). FOUND (2026-08-29, same pass) via
    # an actual write failure during testing, not assumed safe — flatten
    # to "v1|v2" string keys for persistence only; the in-memory/report
    # version keeps tuples since format_level2_result() in edge_finder.py
    # unpacks them directly.
    serializable_level2 = [
        {"dim1": r["dim1"], "dim2": r["dim2"],
         "cells": {f"{v1}|{v2}": stats for (v1, v2), stats in r["cells"].items()}}
        for r in level2_results
    ]
    record = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "trade_count": trade_count,
        "level1": level1_results,
        "level2": serializable_level2,
    }
    try:
        with open(log_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        print(f"[EDGE FINDER LOG WRITE ERROR] {e}")
    return record
