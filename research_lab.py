"""
research_lab.py
================
Research & Backtest Lab — the ONE entrypoint. Added 2026-09-18, per chat.
Extended same day after a friend's design review (two follow-up notes) —
see the TOUCH-HISTORY TEST and HOLDOUT sections below for what changed
and why.

Deliberately NOT a folder of specialized files (per chat — "I wouldn't
immediately create a giant folder full of specialized files... The core
idea should be one clean interface"). This is that interface. When a
test earns its own file, split it out of TEST_REGISTRY below — don't
grow this file's dispatch table into a maze first.

ARCHITECTURE — same one-way rule as the rest of this codebase
------------------------------------------------------------------------
research_lab.py sits ABOVE research_dataset.py (imports it) and is a
LEAF with respect to everything else: it imports research_dataset and
scanner_common only. It NEVER imports scanner_live or min_scanner, and
scanner_live/min_scanner must NEVER import this file or research_
dataset.py — check_layer_imports.py enforces both directions. A
research finding does not get to influence a live decision by being
importable; it has to earn its way into MIN the way the roadmap
describes (replication -> controlled comparison -> robustness checks
-> Investigation Bureau -> candidate finding), not via `import
research_lab`.

"Blind to the outcome" is enforced by construction, not by convention:
every test below is written in two hard-separated halves —

    1. CONDITION   — decide whether/where the thing under test exists,
                     using ONLY an AsOfCandles view cut off at the
                     event's own formation time (research_dataset.py).
    2. OUTCOME     — measure what happened next, using ONLY an
                     outcome_window() built from that same cutoff
                     forward. This function never receives the
                     AsOfCandles object, and the condition step never
                     receives the outcome window. There is no shared
                     variable that could leak a future candle into a
                     condition decision.

Dataset / Test / Backtest stay separate concepts (per chat):
  - a DATASET is "what observations are we studying" (research_dataset.py)
  - a TEST asks "what happens?" (this file, run_test())
  - a BACKTEST asks "what happens if I trade it?" — NOT built yet. Per
    chat, don't let every research question turn into a backtest; add
    backtest_engine.py as its own thing once a test result is
    interesting enough to be worth turning into a trading procedure.

MULTIPLE-TESTING / OVERFITTING PROTECTION (per chat, friend's review)
------------------------------------------------------------------------
Two protections added after the review, both structural rather than
"remember to be careful":

  1. HOLDOUT — every test takes `dataset_split` ("development", the
     default, or "holdout"). Development is for exploring/tuning
     parameters; holdout exists to verify ONE already-chosen config
     exactly once. Requesting holdout prints a loud, impossible-to-miss
     banner every single time — there is deliberately no quiet way to
     touch it, because the danger here is habit, not a single mistake.
  2. run_sweep() reports a whole parameter grid plus sample size and a
     95% Wilson confidence interval per cell (see wilson_interval()),
     not a single headline percentage — a 63% outcome rate over 4000
     observations and 63% over 12 are not the same evidence, and a grid
     makes that visible instead of inviting a "best cell wins" read.

Neither of these makes it impossible to fool yourself — no code can —
but both make the honest path the path of least resistance.

USAGE
------------------------------------------------------------------------
    python3 research_lab.py --test zone_reaction --object FVG \
        --timeframe 15min --reaction-pips 8 --lookahead 12 \
        --start 2026-01-01 --end 2026-09-01

    python3 research_lab.py --test touch_history --object FVG

or from code:

    from research_lab import run_test, run_sweep
    result = run_test(
        test="zone_reaction", zone_type="FVG", timeframe="15min",
        reaction_pips=8, lookahead_candles=12,
        period=("2026-01-01", "2026-09-01"),
    )
    sweep = run_sweep(
        test="zone_reaction", zone_type="FVG",
        reaction_pips_grid=[4, 8, 12, 16], lookahead_grid=[6, 12, 24],
    )
"""
import argparse
from itertools import product

import pandas as pd

from scanner_common import PIP_SIZE
from research_dataset import (
    load_candles, load_zones, outcome_window, touch_events,
    matched_session_timestamps, development_holdout_boundary, session_bucket,
)


# =========================================================================
# SHARED OUTCOME CLASSIFIER
# =========================================================================
# Outcome is defined MECHANICALLY, before looking at any results (per
# chat — this is exactly the discipline that keeps a 50% YouTube-style
# reaction-rate claim from being misleading):
#
#     REACTED_UP             price moved >= reaction_pips away from the
#                             touch price, upward, within lookahead_candles,
#                             and that move was the larger of the two
#     REACTED_DOWN            same, downward
#     INSUFFICIENT_MOVEMENT   neither threshold reached in time
#     NOT_TOUCHED             (zone_reaction_test only) price never
#                             traded back into the zone within
#                             max_wait_candles of formation
#
# v1 definition, not a final one — reaction magnitude via max-excursion-
# in-window is simple and auditable, but doesn't capture TIMING (which
# threshold was crossed first) or partial-fill nuance. Refine once real
# output has been checked against real charts, same "prove the pattern
# works before widening it" discipline build_structure_digest() used
# for its own v1. Shared by every test below — one definition, reused,
# not reinvented per test.
def _classify_reaction(entry_price, after, reaction_pips):
    """
    OUTCOME step. `after` MUST be an outcome_window() DataFrame (never
    an AsOfCandles) — see module docstring on why the two are kept as
    different types on purpose. Pure: no I/O, no randomness, no
    knowledge of why `entry_price`/`after` were chosen.
    """
    if after.empty:
        return "INSUFFICIENT_MOVEMENT", 0.0, 0.0
    max_up_pips   = (after["High"].max() - entry_price) / PIP_SIZE
    max_down_pips = (entry_price - after["Low"].min()) / PIP_SIZE
    if max_up_pips >= reaction_pips and max_up_pips >= max_down_pips:
        return "REACTED_UP", max_up_pips, max_down_pips
    if max_down_pips >= reaction_pips and max_down_pips > max_up_pips:
        return "REACTED_DOWN", max_up_pips, max_down_pips
    return "INSUFFICIENT_MOVEMENT", max_up_pips, max_down_pips


def _distribution(outcomes_list):
    n = len(outcomes_list)
    if n == 0:
        return {}
    counts = {}
    for o in outcomes_list:
        counts[o] = counts.get(o, 0) + 1
    return {k: round(100 * v / n, 1) for k, v in counts.items()}


def wilson_interval(successes, n, z=1.96):
    """
    95% Wilson score confidence interval for a binomial proportion —
    added per chat ("63% with 31 observations and 56% with 4,000 are
    very different evidence... the sensitivity surface is much more
    informative than a single headline number"). Wilson rather than
    the naive normal approximation because it stays sane at small n
    and near 0%/100%, which matters here since some zone_type/session/
    timeframe cells will be thin. Returns (low_pct, high_pct), or
    (0.0, 0.0) if n == 0.
    """
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z**2 / n
    center = p + z**2 / (2 * n)
    margin = z * ((p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5)
    low = (center - margin) / denom
    high = (center + margin) / denom
    return round(100 * max(0.0, low), 1), round(100 * min(1.0, high), 1)


def _resolve_split(zones, candles, dataset_split):
    """
    Shared holdout gate — every test calls this before doing anything
    else with its zones/candles. Filters BOTH by the same chronological
    boundary (development_holdout_boundary(), research_dataset.py): a
    zone that formed at/after the boundary has no legitimate
    development-side outcome window at all (its "future" IS holdout),
    so it must be excluded from `zones`, not just have its candle
    search silently truncated — filtering candles alone without also
    filtering zones was a real bug caught during testing (a boundary-
    adjacent zone got measured against a candle set that had already
    been cut out from under it, producing a spurious NOT_TOUCHED).

    `dataset_split` must be "development" (default) or "holdout";
    anything else raises rather than silently falling back, since a
    typo here should never quietly hand back the full dataset.
    """
    boundary = development_holdout_boundary(candles)
    if boundary is None:
        return zones, candles
    if dataset_split == "development":
        return (zones[zones["formation_time"] < boundary],
                candles[candles.index < boundary])
    if dataset_split == "holdout":
        print("\n" + "!" * 72)
        print("! HOLDOUT DATA REQUESTED — this should happen ONCE, for a config")
        print("! already chosen on development data. Re-running holdout after")
        print("! seeing this result and adjusting parameters defeats the point")
        print("! of having a holdout set at all (per chat — 'don't repeatedly")
        print("! optimize against the holdout').")
        print("!" * 72 + "\n")
        return (zones[zones["formation_time"] >= boundary],
                candles[candles.index >= boundary])
    raise ValueError(f"dataset_split must be 'development' or 'holdout', got {dataset_split!r}")


# =========================================================================
# TEST 1 — ZONE REACTION PROBABILITY (per chat — "the perfect first test")
# =========================================================================
# Question asked, deliberately NOT "does FVG make profitable trades?"
# (that already injects a trading framework — see module docstring):
#
#     "When price first touches a zone, what happens afterward?"
def _first_touch(candles_df, zone_low, zone_high, max_wait_candles):
    """
    CONDITION step. Finds the first candle whose [Low, High] range
    overlaps [zone_low, zone_high], searching at most `max_wait_candles`
    candles forward through `candles_df` (already an outcome_window()
    slice strictly after formation — see that function's own docstring).
    Returns (touch_timestamp, entry_price) or (None, None).

    The zone's EXISTENCE (its high/low/formation_time) was already
    decided upstream by zone_log.jsonl at scan time, off a live AsOf-
    equivalent (MarketFacts only ever sees closed candles up to that
    scan) — this function never re-derives that, it only searches
    forward from it.
    """
    window = candles_df.head(max_wait_candles)
    touched = window[(window["High"] >= zone_low) & (window["Low"] <= zone_high)]
    if touched.empty:
        return None, None
    ts = touched.index[0]
    return ts, float(touched.iloc[0]["Close"])


def zone_reaction_test(zone_type="FVG", direction=None, timeframe="15min",
                        reaction_pips=8, lookahead_candles=12,
                        max_wait_candles=12, period=None, seed=42,
                        dataset_split="development"):
    """
    First-touch reaction test for one zone_type ("FVG" or
    "ORDER_BLOCK"). Each ZONE contributes exactly ONE observation (its
    first touch only) — later touches are a separate question, see
    touch_history_test() below; conflating them would silently let a
    single choppy zone outvote everything else in the population (per
    chat — "pseudo-replication").

    `direction` filters which zone direction to include (None = both,
    pooled). Either way, zone direction is retained per observation and
    broken out separately in `by_zone_direction` below — per chat,
    friend's review: "there's a difference between 'bullish FVG
    produces upward reaction' and 'bullish FVG produces any reaction' —
    you want to know whether the zone has directional behaviour or
    merely creates volatility around itself." Filtering `direction`
    alone would lose that distinction when direction=None; this keeps
    it either way.

    `timeframe` ("5min"/"15min"/"1h") — NOTE (per chat, friend's
    review): running this at multiple timeframes and finding the same
    zone_type "works on all of them" is NOT independent confirmation.
    5M/15M/1H candles overlap the same underlying price movement; they
    are different views of one sample, not three samples. Useful for
    seeing whether an effect is timeframe-specific, not for stacking
    p-values across timeframes as if they were separate evidence.

    Returns:
      - real/baseline distribution + delta + Wilson CI, pooled (as before)
      - by_zone_direction: same, split by BULLISH/BEARISH zone direction
      - by_session: same, split by session bucket (per chat — matching
        the baseline to session isn't enough on its own; the breakdown
        itself needs to be visible, e.g. "London: real 62%, baseline
        51%, delta +11pp" rather than only a pooled number)
    """
    zones = load_zones(zone_type=zone_type, direction=direction)
    candles = load_candles(timeframe)
    zones, candles = _resolve_split(zones, candles, dataset_split)

    if period is not None:
        start, end = pd.Timestamp(period[0], tz="UTC"), pd.Timestamp(period[1], tz="UTC")
        zones = zones[(zones["formation_time"] >= start) & (zones["formation_time"] <= end)]

    # One record per zone: zone_direction, touch_time (None if never
    # touched), outcome. Every breakdown below (pooled, by direction,
    # by session) is derived from this SAME list — no separate re-walk
    # of zones per breakdown, so they can't silently drift apart.
    records = []
    for _, zone in zones.iterrows():
        after_formation = outcome_window(candles, zone["formation_time"])
        touch_ts, entry_price = _first_touch(after_formation, zone["low"], zone["high"], max_wait_candles)
        if touch_ts is None:
            records.append({"zone_direction": zone["direction"], "touch_time": None, "outcome": "NOT_TOUCHED"})
            continue
        after_touch = outcome_window(candles, touch_ts, max_candles=lookahead_candles)
        outcome, _, _ = _classify_reaction(entry_price, after_touch, reaction_pips)
        records.append({"zone_direction": zone["direction"], "touch_time": touch_ts, "outcome": outcome})

    touched_records = [r for r in records if r["touch_time"] is not None]
    touch_timestamps = [r["touch_time"] for r in touched_records]
    baseline_timestamps = matched_session_timestamps(candles, touch_timestamps, seed=seed)
    baseline_outcomes = []
    for ts in baseline_timestamps:
        entry_price = float(candles.loc[ts, "Close"])
        after = outcome_window(candles, ts, max_candles=lookahead_candles)
        outcome, _, _ = _classify_reaction(entry_price, after, reaction_pips)
        baseline_outcomes.append(outcome)

    def _report(real_outcomes, baseline_outcomes_subset):
        real_dist = _distribution(real_outcomes)
        baseline_dist = _distribution(baseline_outcomes_subset)
        all_keys = set(real_dist) | set(baseline_dist)
        delta = {k: round(real_dist.get(k, 0.0) - baseline_dist.get(k, 0.0), 1) for k in all_keys}
        ci = {k: wilson_interval(sum(1 for o in real_outcomes if o == k), len(real_outcomes)) for k in all_keys}
        return {"n": len(real_outcomes), "real_distribution_pct": real_dist,
                "baseline_distribution_pct": baseline_dist, "delta_pct_points": delta,
                "confidence_95pct": ci}

    pooled = _report([r["outcome"] for r in records], baseline_outcomes)

    by_zone_direction = {}
    for zone_dir in sorted(set(r["zone_direction"] for r in records)):
        subset_idx = [i for i, r in enumerate(records) if r["zone_direction"] == zone_dir]
        subset_records = [records[i] for i in subset_idx]
        # baseline_outcomes only has one entry per TOUCHED record, in the
        # same order as touched_records — map subset back to that subset
        touched_positions = {id(r): j for j, r in enumerate(touched_records)}
        baseline_subset = [baseline_outcomes[touched_positions[id(r)]]
                            for r in subset_records if id(r) in touched_positions]
        by_zone_direction[zone_dir] = _report([r["outcome"] for r in subset_records], baseline_subset)

    by_session = {}
    touched_positions = {id(r): j for j, r in enumerate(touched_records)}
    for r in touched_records:
        bucket = session_bucket(r["touch_time"])
        by_session.setdefault(bucket, {"real": [], "baseline": []})
        by_session[bucket]["real"].append(r["outcome"])
        by_session[bucket]["baseline"].append(baseline_outcomes[touched_positions[id(r)]])
    by_session = {b: _report(v["real"], v["baseline"]) for b, v in by_session.items()}

    return {
        "test": "zone_reaction",
        "zone_type": zone_type,
        "direction": direction,
        "timeframe": timeframe,
        "dataset_split": dataset_split,
        "reaction_pips": reaction_pips,
        "lookahead_candles": lookahead_candles,
        "n_zones": len(zones),
        "n_touched": len(touch_timestamps),
        "real_distribution_pct": pooled["real_distribution_pct"],
        "baseline_distribution_pct": pooled["baseline_distribution_pct"],
        "delta_pct_points": pooled["delta_pct_points"],
        "confidence_95pct": pooled["confidence_95pct"],
        "by_zone_direction": by_zone_direction,
        "by_session": by_session,
    }


# =========================================================================
# TEST 2 — TOUCH HISTORY (per chat, friend's second review note)
# =========================================================================
# Explicitly does NOT test "does more prior interaction weaken a zone" —
# that's a conclusion, not a question, and baking it in would be
# "analysis masquerading as data" (per chat). Instead this reports the
# reaction-outcome distribution BROKEN DOWN BY touch_number (1st, 2nd,
# 3rd, 4th+), using the EXACT SAME _classify_reaction() every other test
# uses, over the SAME touch_events() every zone/touch question should
# go through. Whatever pattern is in the breakdown — degradation,
# persistence, a peak at touch #2, nothing — is for whoever reads the
# report to see; this function draws no conclusion and returns no
# single collapsed verdict on purpose.
def touch_history_test(zone_type="FVG", direction=None, timeframe="15min",
                        reaction_pips=8, lookahead_candles=12,
                        max_touches=5, period=None, dataset_split="development"):
    """
    Returns, per touch_number (1..max_touches, plus an "ALL" pooled
    row), the reaction-outcome distribution and mean penetration_pips —
    the raw material for asking "does prior interaction history affect
    subsequent behaviour," without answering it for you.
    """
    zones = load_zones(zone_type=zone_type, direction=direction)
    candles = load_candles(timeframe)
    zones, candles = _resolve_split(zones, candles, dataset_split)

    if period is not None:
        start, end = pd.Timestamp(period[0], tz="UTC"), pd.Timestamp(period[1], tz="UTC")
        zones = zones[(zones["formation_time"] >= start) & (zones["formation_time"] <= end)]

    by_touch_number = {}  # touch_number -> list of (outcome, penetration_pips)
    for _, zone in zones.iterrows():
        after_formation = outcome_window(candles, zone["formation_time"])
        touches = touch_events(zone["low"], zone["high"], after_formation, max_touches=max_touches)
        for touch in touches:
            after_touch = outcome_window(candles, touch["run_end_time"], max_candles=lookahead_candles)
            outcome, _, _ = _classify_reaction(touch["entry_price"], after_touch, reaction_pips)
            by_touch_number.setdefault(touch["touch_number"], []).append(
                (outcome, touch["penetration_pips"])
            )

    report = {}
    all_rows = []
    for touch_number in sorted(by_touch_number):
        rows = by_touch_number[touch_number]
        all_rows.extend(rows)
        outcomes = [o for o, _ in rows]
        pens = [p for _, p in rows]
        report[touch_number] = {
            "n": len(rows),
            "distribution_pct": _distribution(outcomes),
            "mean_penetration_pips": round(sum(pens) / len(pens), 2) if pens else None,
        }
    report["ALL"] = {
        "n": len(all_rows),
        "distribution_pct": _distribution([o for o, _ in all_rows]),
        "mean_penetration_pips": round(sum(p for _, p in all_rows) / len(all_rows), 2) if all_rows else None,
    }

    return {
        "test": "touch_history",
        "zone_type": zone_type,
        "direction": direction,
        "timeframe": timeframe,
        "dataset_split": dataset_split,
        "reaction_pips": reaction_pips,
        "lookahead_candles": lookahead_candles,
        "n_zones": len(zones),
        "by_touch_number": report,
    }


# =========================================================================
# DISPATCH — the "one clean interface" (per chat)
# =========================================================================
TEST_REGISTRY = {
    "zone_reaction": zone_reaction_test,
    "touch_history": touch_history_test,
}


def run_test(test, **kwargs):
    if test not in TEST_REGISTRY:
        raise ValueError(f"Unknown test '{test}'. Available: {list(TEST_REGISTRY)}")
    return TEST_REGISTRY[test](**kwargs)


# =========================================================================
# SENSITIVITY SWEEP (per chat, friend's first review note — "absolutely
# first," before trusting any single-config headline number)
# =========================================================================
def run_sweep(test, reaction_pips_grid, lookahead_grid, **fixed_kwargs):
    """
    Runs `test` once per (reaction_pips, lookahead_candles) combination
    in the grid, holding every other kwarg fixed. Returns a list of
    rows: {reaction_pips, lookahead_candles, n, real_pct, baseline_pct,
    delta, ci_95} for whichever outcome key has the largest n (so the
    grid stays readable) — full per-cell distributions are still in
    each cell's raw result if you need them.

    This exists specifically so a "best-looking" cell in a 4x3 grid
    (or bigger) doesn't get reported as THE finding — per chat, running
    every combination and picking the best one is "a gigantic multiple-
    testing machine... eventually something will look impressive purely
    by chance." Report the WHOLE grid, always, not the winning cell.
    """
    rows = []
    for reaction_pips, lookahead_candles in product(reaction_pips_grid, lookahead_grid):
        result = run_test(test, reaction_pips=reaction_pips,
                           lookahead_candles=lookahead_candles, **fixed_kwargs)
        real_dist = result["real_distribution_pct"]
        if not real_dist:
            rows.append({"reaction_pips": reaction_pips, "lookahead_candles": lookahead_candles,
                         "n": 0, "outcome": None, "real_pct": None, "baseline_pct": None,
                         "delta": None, "ci_95": None})
            continue
        top_outcome = max(real_dist, key=real_dist.get)
        rows.append({
            "reaction_pips": reaction_pips,
            "lookahead_candles": lookahead_candles,
            "n": result.get("n_touched", result.get("n_zones")),
            "outcome": top_outcome,
            "real_pct": real_dist.get(top_outcome),
            "baseline_pct": result.get("baseline_distribution_pct", {}).get(top_outcome),
            "delta": result.get("delta_pct_points", {}).get(top_outcome),
            "ci_95": result.get("confidence_95pct", {}).get(top_outcome),
        })
    print(f"\n{len(rows)} configurations tested — exploratory, not confirmatory. "
          "Pick ONE based on this grid, then verify it once on holdout data.\n")
    return rows


def _print_breakdown(label, sub_result):
    print(f"\n  -- {label} --")
    print(f"  {'Outcome':<22}{'Real %':>10}{'Baseline %':>13}{'Delta':>10}{'95% CI':>16}")
    for k in sorted(set(sub_result["real_distribution_pct"]) | set(sub_result["baseline_distribution_pct"])):
        ci = sub_result["confidence_95pct"].get(k, (0.0, 0.0))
        print(f"  {k:<22}{sub_result['real_distribution_pct'].get(k, 0.0):>10}"
              f"{sub_result['baseline_distribution_pct'].get(k, 0.0):>13}"
              f"{sub_result['delta_pct_points'].get(k, 0.0):>+10}"
              f"{f'[{ci[0]}, {ci[1]}]':>16}")


def _print_report(result):
    if result["test"] == "zone_reaction":
        print(f"\n=== zone_reaction — {result['zone_type']}"
              f"{' ' + result['direction'] if result.get('direction') else ''} "
              f"({result['timeframe']}, {result['dataset_split']}) ===")
        print(f"Zones in period: {result['n_zones']}  |  Touched: {result['n_touched']}")
        print(f"Reaction threshold: {result['reaction_pips']} pips within "
              f"{result['lookahead_candles']} candles")
        _print_breakdown("POOLED", result)
        for zone_dir, sub in result["by_zone_direction"].items():
            _print_breakdown(f"ZONE DIRECTION = {zone_dir} (n={sub['n']})", sub)
        for bucket, sub in result["by_session"].items():
            label = f"SESSION #{bucket}" if bucket is not None else "SESSION = unmatched"
            _print_breakdown(f"{label} (n={sub['n']})", sub)
    elif result["test"] == "touch_history":
        print(f"\n=== touch_history — {result['zone_type']}"
              f"{' ' + result['direction'] if result.get('direction') else ''} "
              f"({result['timeframe']}, {result['dataset_split']}) ===")
        print(f"Zones in period: {result['n_zones']}\n")
        print(f"{'Touch #':<10}{'n':>6}   distribution %                          mean penetration (pips)")
        for touch_number, row in result["by_touch_number"].items():
            print(f"{str(touch_number):<10}{row['n']:>6}   {row['distribution_pct']}   {row['mean_penetration_pips']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Research Lab — run a test.")
    parser.add_argument("--test", default="zone_reaction")
    parser.add_argument("--object", dest="zone_type", default="FVG")
    parser.add_argument("--direction", default=None, choices=[None, "BULLISH", "BEARISH"])
    parser.add_argument("--timeframe", default="15min", choices=["5min", "15min", "1h"])
    parser.add_argument("--reaction-pips", type=float, default=8)
    parser.add_argument("--lookahead", type=int, default=12, dest="lookahead_candles")
    parser.add_argument("--max-wait", type=int, default=12, dest="max_wait_candles")
    parser.add_argument("--max-touches", type=int, default=5, dest="max_touches")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--split", default="development", dest="dataset_split",
                         choices=["development", "holdout"])
    args = parser.parse_args()

    period = (args.start, args.end) if args.start and args.end else None
    kwargs = dict(zone_type=args.zone_type, direction=args.direction,
                  timeframe=args.timeframe, reaction_pips=args.reaction_pips,
                  lookahead_candles=args.lookahead_candles, period=period,
                  dataset_split=args.dataset_split)
    if args.test == "zone_reaction":
        kwargs["max_wait_candles"] = args.max_wait_candles
        kwargs["seed"] = args.seed
    elif args.test == "touch_history":
        kwargs["max_touches"] = args.max_touches

    out = run_test(args.test, **kwargs)
    _print_report(out)
