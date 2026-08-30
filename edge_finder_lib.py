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

CONFOUND INVESTIGATION + SUB-POPULATION DRILLDOWN (2026-08-30, per chat
— Vally's proposed stage between Candidate Edge and Hypothesis: "before
testing a policy, you need to ask: is this actually a market
relationship, or missing state history / initialization effects / time-
period concentration / experiment-specific behaviour / regime
distribution?"). Both run automatically once a dimension reaches
CANDIDATE_EDGE or ROBUST_CANDIDATE — see confound_scan() and
subpopulation_drilldown() below. Both are deliberately GENERIC: neither
function has any concept of what a specific dimension or value means
(no hardcoded "exhaustion" or "reacceleration" or any other domain
theory) — per chat, "hypothesis should touch everything that deserves
questioning for every experiment... the discipline of the bot is to
never favor but to generalize." The exact same two functions run
whatever dimension happens to qualify, on whatever experiment produced
it, using only whatever fields the Trade Investigation Bureau has
actually captured so far. If/when the Bureau captures a genuinely new
field (a reacceleration-style signal, a retest-quality score, anything
Vally's Setup Anatomy dimension eventually adds), subpopulation_
drilldown() picks it up automatically — it iterates whatever dimension
list it's given, it doesn't need to be told a new field exists.

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

from investigation_lib import (
    load_jsonl, parse_ts, _r_stats, CONDITION_DIMENSIONS, _display_value,
    build_leg_intervals, find_enclosing_leg,
)

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


def load_trades(path, experiment=None, leg_obs_log_path=None):
    """
    Reads shadow_trade_log.jsonl directly — every record resolved AFTER
    the Trade Investigation Bureau embedding fix already carries
    conditions_at_resolution/environment/tags per-trade, no join needed.

    FIX (2026-08-30, per chat — real report showed EVERY dimension as
    INSUFFICIENT_DATA across 2,250 real trades, which was confusing
    until traced to the actual mechanism: those fields only get written
    AT RESOLUTION TIME, starting from the deploy that added them — every
    trade resolved before that deploy has neither field at all, no
    matter how many trades exist in total). `leg_obs_log_path`, when
    given, backfills conditions_at_resolution for exactly those older
    trades using the SAME retroactive timestamp-containment join
    shadow_investigation.py already does against leg_obs_log.jsonl (see
    that script's own docstring for the join's own caveats — leg-
    formation-time granularity, not moment-exact — which apply equally
    here).

    WHAT THIS DOES NOT FIX, STATED PLAINLY: `environment` (volatility_
    state/compression_ratio/trend_strength_atr_mult/pullback_depth_pct)
    CANNOT be backfilled this way — those are THESIS-level, same-scan
    snapshots (see peek_environment_at_resolution() in min_scanner.py),
    and there is no permanent historical log of past scans' Thesis state
    to retroactively join against, unlike leg_obs_log.jsonl which IS a
    permanent per-leg history. Older trades will still show
    INSUFFICIENT_DATA on those four dimensions specifically until enough
    NEW trades accumulate post-deploy — that part of the original
    explanation still holds, just narrowed to the fields that genuinely
    have no historical backfill path.
    """
    trades = load_jsonl(path)
    if experiment:
        trades = [t for t in trades if t.get("experiment") == experiment]
    trades = [t for t in trades if parse_ts(t.get("resolved_at")) is not None]

    if leg_obs_log_path:
        leg_records = load_jsonl(leg_obs_log_path)
        leg_intervals = build_leg_intervals(leg_records)
        backfilled = 0
        for t in trades:
            if not t.get("conditions_at_resolution"):
                match = find_enclosing_leg(parse_ts(t.get("opened_at")), leg_intervals)
                if match is not None:
                    formation_state, _fate = match
                    t["conditions_at_resolution"] = formation_state
                    backfilled += 1
        print(f"  Backfilled conditions_at_resolution via leg_obs_log.jsonl join for "
              f"{backfilled}/{len(trades)} older trades that predate the embedding fix. "
              f"environment fields (volatility_state/compression_ratio/trend_strength_atr_mult/"
              f"pullback_depth_pct) canNOT be backfilled this way — see this function's own "
              f"docstring for why — those dimensions will still show INSUFFICIENT_DATA for "
              f"older trades regardless of this join.")

    return trades


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


def confound_scan(trades, dim, best_value, worst_value, all_dimensions, continuous_buckets):
    """
    ADDED (2026-08-30, per chat — Vally's "confound investigation"
    stage: before a candidate edge earns the right to become a
    hypothesis, ask whether the two groups being compared are actually
    comparable populations, or whether they differ sharply on some
    OTHER axis that might be the real driver). Fully generic — makes NO
    assumption about what `dim`/`best_value`/`worst_value` mean. This
    is the same mechanism regardless of which dimension or experiment
    triggered it, per the project's stated discipline: never favor a
    specific theory, always generalize.

    Three checks, matching Vally's own list for no_prior_leg almost
    verbatim, just made dimension-agnostic:
      1. TIME CONCENTRATION — are the two groups' median timestamps far
         apart relative to the dataset's full span? (catches "this
         value only started/stopped appearing at some point," exactly
         what a state-history artifact like no_prior_leg would produce.)
      2. EXPERIMENT CONCENTRATION — when the dataset spans multiple
         experiments, is one group dominated by a different experiment
         than the other? (catches "this isn't a market relationship,
         it's just what one specific experiment's setups look like.")
      3. CROSS-DIMENSION DISTRIBUTION — for every OTHER dimension, do
         the two groups have meaningfully different value distributions?
         (catches exactly the no_prior_leg-correlates-with-market_phase
         kind of finding — a candidate on dim A might really be dim B
         wearing dim A's name.)

    Returns a list of plain-English concern strings — empty list means
    no concerns surfaced, which is itself a meaningful, reportable
    result (a candidate with a clean confound scan is more trustworthy
    than one that's never been checked).
    """
    concerns = []
    best_idx = [i for i in range(len(trades))
                if _value_for_dimension(trades, i, dim, continuous_buckets) == best_value]
    worst_idx = [i for i in range(len(trades))
                 if _value_for_dimension(trades, i, dim, continuous_buckets) == worst_value]
    best_rows = [trades[i] for i in best_idx]
    worst_rows = [trades[i] for i in worst_idx]

    # 1. TIME CONCENTRATION
    best_times = sorted(t for t in (parse_ts(r.get("resolved_at")) for r in best_rows) if t)
    worst_times = sorted(t for t in (parse_ts(r.get("resolved_at")) for r in worst_rows) if t)
    if best_times and worst_times:
        all_times = best_times + worst_times
        span = (max(all_times) - min(all_times)).total_seconds()
        if span > 0:
            best_median = best_times[len(best_times) // 2]
            worst_median = worst_times[len(worst_times) // 2]
            gap_pct = abs((best_median - worst_median).total_seconds()) / span * 100
            if gap_pct > 15:
                concerns.append(
                    f"Time concentration: '{best_value}' and '{worst_value}' have median "
                    f"timestamps {gap_pct:.0f}% of the dataset's full time span apart — one "
                    f"group may be concentrated in a different period, not just a different "
                    f"condition. Check whether '{worst_value}' is disproportionately early or "
                    f"late history rather than spread evenly."
                )

    # 2. EXPERIMENT CONCENTRATION (only meaningful across >1 experiment)
    best_exps = defaultdict(int)
    worst_exps = defaultdict(int)
    for r in best_rows:
        best_exps[r.get("experiment")] += 1
    for r in worst_rows:
        worst_exps[r.get("experiment")] += 1
    if len(set(best_exps) | set(worst_exps)) > 1 and best_exps and worst_exps:
        best_dom = max(best_exps.items(), key=lambda kv: kv[1])
        worst_dom = max(worst_exps.items(), key=lambda kv: kv[1])
        if best_dom[0] != worst_dom[0]:
            concerns.append(
                f"Experiment concentration: '{best_value}' is dominated by {best_dom[0]} "
                f"({best_dom[1]}/{len(best_rows)}), '{worst_value}' by {worst_dom[0]} "
                f"({worst_dom[1]}/{len(worst_rows)}) — the two groups may not describe the "
                f"same underlying population at all."
            )

    # 3. CROSS-DIMENSION DISTRIBUTION DIFFERENCE
    for other_dim in all_dimensions:
        if other_dim == dim:
            continue
        best_other = defaultdict(int)
        worst_other = defaultdict(int)
        for i in best_idx:
            v = _value_for_dimension(trades, i, other_dim, continuous_buckets)
            if v is not None:
                best_other[v] += 1
        for i in worst_idx:
            v = _value_for_dimension(trades, i, other_dim, continuous_buckets)
            if v is not None:
                worst_other[v] += 1
        n_best, n_worst = sum(best_other.values()), sum(worst_other.values())
        if n_best < MIN_GROUP_N or n_worst < MIN_GROUP_N:
            continue
        best_dom_val, best_dom_n = max(best_other.items(), key=lambda kv: kv[1])
        worst_dom_val, worst_dom_n = max(worst_other.items(), key=lambda kv: kv[1])
        best_share = best_dom_n / n_best * 100
        worst_share = worst_dom_n / n_worst * 100
        if best_dom_val != worst_dom_val or abs(best_share - worst_share) > 25:
            concerns.append(
                f"Cross-dimension difference on {other_dim}: '{best_value}' is mostly "
                f"{best_dom_val} ({best_share:.0f}%), '{worst_value}' is mostly {worst_dom_val} "
                f"({worst_share:.0f}%) — this candidate might really be describing {other_dim}, "
                f"not {dim}."
            )

    return concerns


def subpopulation_drilldown(trades, dim, value, all_dimensions, continuous_buckets):
    """
    ADDED (2026-08-30, per chat — the generalized hypothesis-generation
    step connecting Edge Finder to the Trade Investigation Bureau).
    Given all trades matching one specific value of a candidate
    dimension (e.g. every 'no_prior_leg' trade, or every 'UNDER_100'
    trade), re-runs the SAME Level 1 contrast machinery WITHIN that
    subgroup across every OTHER available dimension. Answers: "within
    this specific condition, what separates its OWN winners from its
    OWN losers?"

    DELIBERATELY GENERIC (per chat: "hypothesis should touch everything
    that deserves questioning for every experiment... the discipline of
    the bot is to never favor but to generalize"). This function has no
    concept of "exhaustion" or "reacceleration" or any other specific
    market theory — it is the mechanical drill-down that would surface
    a reacceleration-style finding IF that field existed as a captured
    dimension, and works identically for whatever dimensions ARE
    captured today. When the Trade Investigation Bureau eventually
    captures richer fields, this function needs no changes at all to
    start using them.

    Run on BOTH sides of a candidate (best_value AND worst_value), not
    just the underperforming one — asking "what separates the rare
    losers within a good condition" is just as legitimate a question as
    "what separates the rare winners within a bad condition," and
    favoring only one side would itself be a form of bias this project
    has explicitly rejected.

    Returns (subset_size, [level-1-style results across other dims]).
    """
    subset = [t for i, t in enumerate(trades)
              if _value_for_dimension(trades, i, dim, continuous_buckets) == value]
    results = []
    for other_dim in all_dimensions:
        if other_dim == dim:
            continue
        result = _dimension_contrast(subset, other_dim, continuous_buckets)
        results.append(result)
    return len(subset), results


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
                # CONFOUND SCAN + SUB-POPULATION DRILLDOWN (2026-08-30,
                # per chat) — only run once a dimension actually reaches
                # CANDIDATE_EDGE/ROBUST_CANDIDATE, not on every SIGNAL,
                # matching the same "bounded, not brute force" discipline
                # as Level 2 above: this is real computational cost, only
                # worth paying once something has already earned a closer
                # look. Both run on the FULL dimension list, not a
                # hardcoded subset — see each function's own docstring
                # for why genericity matters here specifically.
                result["confounds"] = confound_scan(
                    trades, dim, result["best_value"], result["worst_value"],
                    dimensions, continuous_buckets)
                best_n, best_drilldown = subpopulation_drilldown(
                    trades, dim, result["best_value"], dimensions, continuous_buckets)
                worst_n, worst_drilldown = subpopulation_drilldown(
                    trades, dim, result["worst_value"], dimensions, continuous_buckets)
                result["drilldown"] = {
                    "best_value_subset": {"n": best_n, "results": best_drilldown},
                    "worst_value_subset": {"n": worst_n, "results": worst_drilldown},
                }
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
