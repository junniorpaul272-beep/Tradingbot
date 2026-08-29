#!/usr/bin/env python3
"""
Shadow experiment investigation — condition-level reconstruction for any
(or all) shadow experiments, not just EXP2.

Reads shadow_trade_log.jsonl (thin: outcome, R, timing, a few
experiment-specific tags — see _append_shadow_trade_log in min_scanner.py)
and leg_obs_log.jsonl (rich: market_phase, regime, volatility, campaign
extension — see _leg_obs_formation_state), joins them by timestamp
containment (each trade's opened_at matched against the leg_obs record
whose [opened_at, resolved_at) window contains it), and produces:

  1. A per-experiment weekly cumulative-R curve with the peak/trough
     auto-located, and a condition-mix comparison between the rise and
     decay windows (the original EXP2 investigation, generalized).
  2. A full per-trade CSV — one row per resolved trade, with the
     matched leg's conditions flattened into columns — so individual
     trades can be inspected directly instead of only reading cohort
     percentages. This is the "fine detail" layer: the summary tells
     you THAT something changed, the CSV lets you check WHICH trades.
  3. OUTCOME-CONDITIONED ANALYSIS (2026-08-29, per chat — Vally's
     review: cohort composition alone can't say WHY performance
     changed). Three levels, SINGLE-EXPERIMENT MODE ONLY (--experiment,
     not --all — see note in main() for why):
       A. Win rate / expectancy / profit factor per value of each
          condition dimension.
       B. THE CRUCIAL ONE: for each value, rise-window expectancy next
          to decay-window expectancy. Distinguishes composition shift
          (a value's SHARE of trades changed, its own performance
          didn't) from real decay (the SAME value got worse).
       C. 2D interaction crosstab (default: market_phase x measured_
          move_bucket) — where the real story can hide behind two
          single-dimension views that each look unremarkable alone.

CAVEATS (same as the original EXP2 script — repeated because they
matter for every experiment, not just EXP2):
  - Leg-level granularity: a trade attached to a long-lived leg gets
    that leg's FORMATION-time snapshot, not a fresh one at trade time.
  - A trade with no enclosing leg_obs record shows as "no_matching_leg"
    in cohort breakdowns and blank condition columns in the CSV — never
    silently dropped.
  - Older records may predate newer fields (methodology versioning) —
    shows as blank/unknown, not a crash.
  - Every stats block enforces a minimum sample size (default 5) before
    printing a percentage/expectancy — smaller buckets show "too small
    to trust" instead of a misleadingly precise number built on 1-2
    trades (a real problem in the first report's small experiments).

USAGE:
    python3 shadow_investigation.py --experiment EXP2_FIB
    python3 shadow_investigation.py --all
    python3 shadow_investigation.py --experiment EXP2_FIB --shadow-log /path/to/shadow_trade_log.jsonl --leg-obs-log /path/to/leg_obs_log.jsonl
"""
import argparse
import csv
import sys
from datetime import datetime, timezone

from investigation_lib import (
    load_jsonl, build_leg_intervals, enrich_trades, summarize_conditions,
    format_counter_block, build_weekly_curve, find_peak_then_trough,
    context_flat, iso_week_key,
    outcome_by_dimension, format_outcome_by_dimension,
    compare_windows_by_dimension, format_window_comparison,
    interaction_performance, format_interaction,
)

ALL_EXPERIMENTS = [
    "EXP1_STRUCTURE", "EXP2_FIB", "EXP3_SR", "EXP4_POLICY_LAB",
    "EXP5_ABLATION", "EXP6_ALT_BIAS", "EXP7_TIER_ATR", "EXP8_CISD",
    "EXPE_REJECTED_LIVE",
]

# Level C default pair — per chat, this is specifically where Vally
# suspects EXP2's real story is hiding. Not hardcoded elsewhere; pass
# different dims if investigating a different experiment's interaction.
INTERACTION_DIM1 = "market_phase"
INTERACTION_DIM2 = "measured_move_bucket"


def investigate_experiment(experiment_key, all_trades, leg_intervals, csv_writer, outcome_detail=False, min_n=5):
    trades = [t for t in all_trades if t.get("experiment") == experiment_key]
    if not trades:
        print(f"\n=== {experiment_key}: no resolved trades, skipping ===")
        return None

    enriched, unmatched = enrich_trades(trades, leg_intervals)
    if not enriched:
        print(f"\n=== {experiment_key}: no trades with a resolvable timestamp, skipping ===")
        return None

    print(f"\n=== {experiment_key}: {len(enriched)} resolved trades "
          f"({unmatched} with no enclosing leg, {unmatched/len(enriched)*100:.0f}%) ===")

    if csv_writer is not None:
        for r in enriched:
            ctx = context_flat(r.get("_context"))
            csv_writer.writerow({
                "experiment": experiment_key,
                "trade_id": r.get("trade_id"),
                "opened_at": r.get("opened_at"),
                "resolved_at": r.get("resolved_at"),
                "outcome": r.get("outcome"),
                "r_achieved": r.get("r_achieved"),
                "atr_pips": r.get("atr_pips"),
                "tier_number": r.get("tier_number"),
                "tags": r.get("tags"),
                "enclosing_leg_fate": r.get("_enclosing_leg_fate"),
                **{f"cond_{k}": v for k, v in ctx.items()},
            })

    curve, _ = build_weekly_curve(enriched)
    peak_idx, trough_idx = find_peak_then_trough(curve)

    lines = [f"\n## {experiment_key} — weekly cumulative R\n"]
    lines.append("| Week | Trades | Weekly R | Cumulative R |")
    lines.append("|---|---|---|---|")
    for i, (wk, wr, cum, n) in enumerate(curve):
        marker = ""
        if i == peak_idx:
            marker = " ← peak"
        if trough_idx is not None and i == trough_idx and trough_idx != peak_idx:
            marker = " ← trough after peak"
        lines.append(f"| {wk} | {n} | {wr:+.2f}R | {cum:+.2f}R{marker} |")

    if peak_idx is not None and trough_idx is not None and trough_idx > peak_idx:
        rise_weeks = set(w for w, *_ in curve[:peak_idx + 1])
        decay_weeks = set(w for w, *_ in curve[peak_idx:trough_idx + 1])
        rise_rows = [r for r in enriched if iso_week_key(r["_resolved_dt"]) in rise_weeks]
        decay_rows = [r for r in enriched if iso_week_key(r["_resolved_dt"]) in decay_weeks]

        lines.append(f"\nRise: {curve[0][0]} to {curve[peak_idx][0]} "
                     f"({len(rise_rows)} trades, peaked {curve[peak_idx][2]:+.2f}R)")
        lines.append(format_counter_block("Condition mix during rise", summarize_conditions(rise_rows)))
        lines.append(f"\nDecay: {curve[peak_idx][0]} to {curve[trough_idx][0]} "
                     f"({len(decay_rows)} trades, fell to {curve[trough_idx][2]:+.2f}R)")
        lines.append(format_counter_block("Condition mix during decay", summarize_conditions(decay_rows)))

        if outcome_detail:
            lines.append("\n### A. Performance by dimension (all matched trades, both windows combined)\n")
            matched_all = [r for r in enriched if r.get("_context")]
            lines.append(format_outcome_by_dimension(
                f"{experiment_key} — win rate / expectancy / PF per condition value",
                outcome_by_dimension(matched_all), min_n=min_n))

            lines.append("\n### B. Rise vs decay — same value, both windows (the crucial one)\n")
            lines.append(
                "Composition shift: expectancy similar in both windows, share of trades changed.\n"
                "Real decay: the SAME value's own expectancy got worse between windows.\n"
            )
            lines.append(format_window_comparison(
                f"{experiment_key} — rise vs decay expectancy per condition value",
                compare_windows_by_dimension(rise_rows, decay_rows, min_n=min_n), min_n=min_n))

            lines.append(f"\n### C. Interaction — {INTERACTION_DIM1} x {INTERACTION_DIM2}\n")
            lines.append(format_interaction(
                f"{experiment_key} rise", INTERACTION_DIM1, INTERACTION_DIM2,
                interaction_performance(rise_rows, INTERACTION_DIM1, INTERACTION_DIM2, min_n=min_n), min_n=min_n))
            lines.append(format_interaction(
                f"{experiment_key} decay", INTERACTION_DIM1, INTERACTION_DIM2,
                interaction_performance(decay_rows, INTERACTION_DIM1, INTERACTION_DIM2, min_n=min_n), min_n=min_n))
    else:
        lines.append("\nNo clear peak-then-trough pattern — check the weekly table directly.")
        if outcome_detail:
            lines.append("\n(Outcome-conditioned analysis needs a peak-then-trough pattern to define "
                         "rise/decay windows — Level A still runs on all matched trades below.)\n")
            matched_all = [r for r in enriched if r.get("_context")]
            lines.append(format_outcome_by_dimension(
                f"{experiment_key} — win rate / expectancy / PF per condition value",
                outcome_by_dimension(matched_all), min_n=min_n))

    report = "\n".join(lines)
    print(report)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--experiment", help="Single experiment key, e.g. EXP2_FIB")
    ap.add_argument("--all", action="store_true", help="Investigate every experiment (Levels A/B/C skipped — see note below)")
    ap.add_argument("--min-n", type=int, default=5, help="Minimum sample size before a stat is trusted (default 5)")
    ap.add_argument("--shadow-log", default="shadow_trade_log.jsonl")
    ap.add_argument("--leg-obs-log", default="leg_obs_log.jsonl")
    ap.add_argument("--out-report", default="shadow_investigation_report.md")
    ap.add_argument("--out-csv", default="shadow_investigation_per_trade.csv")
    args = ap.parse_args()

    if not args.experiment and not args.all:
        print("Specify --experiment KEY or --all", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {args.shadow_log} ...")
    all_trades = load_jsonl(args.shadow_log)
    print(f"  {len(all_trades)} total resolved shadow trades")

    print(f"Loading {args.leg_obs_log} ...")
    leg_records = load_jsonl(args.leg_obs_log)
    leg_intervals = build_leg_intervals(leg_records)
    print(f"  {len(leg_records)} resolved leg records, {len(leg_intervals)} usable")

    targets = ALL_EXPERIMENTS if args.all else [args.experiment]
    # Levels A/B/C only run in single-experiment mode. Each level's
    # report block is several lines per condition value per dimension —
    # multiplied across 9 experiments in --all mode that's an
    # unreadable wall of text, not a report. Use --experiment for the
    # one you actually want to dig into after --all points you at it.
    outcome_detail = bool(args.experiment) and not args.all

    fieldnames = [
        "experiment", "trade_id", "opened_at", "resolved_at", "outcome",
        "r_achieved", "atr_pips", "tier_number", "tags", "enclosing_leg_fate",
    ] + [f"cond_{k}" for k in ["market_phase", "atr_bucket", "session", "bias_state", "spike_state", "measured_move_bucket"]]

    reports = []
    with open(args.out_csv, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for exp_key in targets:
            r = investigate_experiment(exp_key, all_trades, leg_intervals, writer,
                                        outcome_detail=outcome_detail, min_n=args.min_n)
            if r:
                reports.append(r)

    with open(args.out_report, "w") as f:
        f.write(f"# Shadow Investigation — {datetime.now(timezone.utc).isoformat()}\n")
        f.write("\n".join(reports))

    print(f"\nPer-trade CSV: {args.out_csv}")
    print(f"Summary report: {args.out_report}")


if __name__ == "__main__":
    main()
