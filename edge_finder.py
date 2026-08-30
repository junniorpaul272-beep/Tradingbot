#!/usr/bin/env python3
"""
Edge Finder — periodic research cycle over the Trade Investigation
Bureau's accumulated dataset. See edge_finder_lib.py's module docstring
for the full design (hierarchy, stability checks, why brute-force
search is deliberately avoided).

THIS IS NOT A CONTINUOUS PROCESS. Per chat (Vally): "I definitely would
not run deep edge discovery after every trade... I would separate
Continuous (Trade Investigation Bureau, every trade) from Periodic
(Edge Finder, every N new trades)." Run this manually via edge-finder.yml,
or by hand — it will refuse to do a full cycle if too little new data
has accumulated since the last run (see --force to override).

USAGE:
    python3 edge_finder.py
    python3 edge_finder.py --experiment EXP2_FIB
    python3 edge_finder.py --force
    python3 edge_finder.py --min-new-trades 50
"""
import argparse
from datetime import datetime, timezone

from edge_finder_lib import (
    load_trades, run_edge_finder_cycle, load_last_cycle_count,
    should_run_cycle, append_cycle_record, MIN_GROUP_N,
)


STATUS_ICON = {
    "ROBUST_CANDIDATE": "🟢",
    "CANDIDATE_EDGE": "🟡",
    "SIGNAL": "🔵",
    "OBSERVATION": "⚪",
    "NO_SEPARATION": "⚫",
    "INSUFFICIENT_DATA": "❔",
}


def format_group_line(val, stats):
    if stats["n"] < MIN_GROUP_N:
        return f"      {val}: n={stats['n']} (below the {MIN_GROUP_N}-trade floor, not compared)"
    pf_str = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] is not None else "inf (no losses)"
    return (f"      {val}: n={stats['n']}, win_rate={stats['win_rate']:.0f}%, "
            f"expectancy={stats['expectancy']:+.2f}R, PF={pf_str}")


def format_level1_result(r):
    icon = STATUS_ICON.get(r["status"], "•")
    lines = [f"  {icon} {r['dimension']}: {r['status']}"]

    if r["status"] == "INSUFFICIENT_DATA":
        lines.append(f"      Fewer than 2 values cleared the {MIN_GROUP_N}-trade floor this cycle.")
        return "\n".join(lines)

    if r["status"] == "NO_SEPARATION":
        lines.append(f"      Largest gap between qualifying values: {r['gap']:+.2f}R — "
                     f"below the {0.15:.2f}R bar. No meaningful separation found.")
        for val, stats in sorted(r["groups"].items(), key=lambda kv: -kv[1]["expectancy"]):
            lines.append(format_group_line(val, stats))
        return "\n".join(lines)

    # OBSERVATION / SIGNAL / CANDIDATE_EDGE / ROBUST_CANDIDATE all share
    # the best/worst framing.
    lines.append(f"      Best: {r['best_value']} vs worst: {r['worst_value']} — gap {r['gap']:+.2f}R "
                 f"(pooled n={r['pooled_n']})")
    for val, stats in sorted(r["groups"].items(), key=lambda kv: -kv[1]["expectancy"]):
        lines.append(format_group_line(val, stats))

    if r["status"] == "OBSERVATION":
        lines.append(f"      Sample too small to call this a signal yet (pooled n={r['pooled_n']}, "
                     f"need {40}+). Worth watching, not yet worth trusting.")
    if "time_split_gaps" in r:
        h1, h2 = r["time_split_gaps"]
        h1_str = f"{h1:+.2f}R" if h1 is not None else "not enough data"
        h2_str = f"{h2:+.2f}R" if h2 is not None else "not enough data"
        lines.append(f"      Time-split check — first half: {h1_str}, second half: {h2_str}")
    if r.get("stability_note"):
        lines.append(f"      ⚠️ {r['stability_note']}")
    if r.get("experiment_split_reason"):
        lines.append(f"      Cross-experiment check: {r['experiment_split_reason']}")

    return "\n".join(lines)


def format_level2_result(r):
    lines = [f"  {r['dim1']} x {r['dim2']} (NOT a rigorous interaction-effect test — "
             f"see edge_finder_lib.py's module note. This is the crosstab for a human to read.):"]
    for (v1, v2), stats in sorted(r["cells"].items(), key=lambda kv: -kv[1]["n"]):
        pf_str = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] is not None else "inf"
        lines.append(f"      {v1} x {v2}: n={stats['n']}, win_rate={stats['win_rate']:.0f}%, "
                     f"expectancy={stats['expectancy']:+.2f}R, PF={pf_str}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--experiment", help="Restrict to one experiment (default: pooled across all)")
    ap.add_argument("--shadow-log", default="shadow_trade_log.jsonl")
    ap.add_argument("--leg-obs-log", default="leg_obs_log.jsonl",
                     help="Used to backfill conditions_at_resolution for trades that predate the "
                          "Trade Investigation Bureau embedding fix. Pass --no-backfill to disable.")
    ap.add_argument("--no-backfill", action="store_true",
                     help="Skip the leg_obs_log.jsonl backfill join entirely")
    ap.add_argument("--edge-log", default="edge_finder_log.jsonl")
    ap.add_argument("--min-new-trades", type=int, default=25)
    ap.add_argument("--force", action="store_true", help="Run even if the sample-based trigger says not to")
    ap.add_argument("--out-report", default="edge_finder_report.md")
    args = ap.parse_args()

    print(f"Loading {args.shadow_log} ...")
    trades = load_trades(args.shadow_log, experiment=args.experiment,
                          leg_obs_log_path=None if args.no_backfill else args.leg_obs_log)
    print(f"  {len(trades)} resolved trades" + (f" for {args.experiment}" if args.experiment else " (all experiments)"))

    if not trades:
        print("No trades to analyze.")
        return

    last_count = load_last_cycle_count(args.edge_log)
    proceed, reason = should_run_cycle(len(trades), last_count, min_new_trades=args.min_new_trades)
    print(reason)
    if not proceed and not args.force:
        with open(args.out_report, "w") as f:
            f.write(f"# Edge Finder — skipped\n\n{reason}\n")
        return
    if not proceed and args.force:
        print("(--force set — running anyway)")

    level1, level2 = run_edge_finder_cycle(trades)
    record = append_cycle_record(args.edge_log, len(trades), level1, level2)

    lines = [f"# Edge Finder — research cycle {datetime.now(timezone.utc).isoformat()}"]
    lines.append(f"\nTrades analyzed: {len(trades)}"
                 f" ({'restricted to ' + args.experiment if args.experiment else 'pooled across all experiments'})")
    lines.append(f"New trades since last cycle: {len(trades) - last_count}")

    lines.append("\n## Level 1 — single-dimension contrasts\n")
    lines.append("Every dimension gets a result, including the ones with nothing in them — "
                 "a proper research tool has to be allowed to conclude 'nothing here.'\n")
    for r in level1:
        lines.append(format_level1_result(r))
        lines.append("")

    lines.append("\n## Level 2 — interactions (only pairs where BOTH dimensions reached SIGNAL+)\n")
    if not level2:
        lines.append("No dimension pair both reached SIGNAL this cycle — nothing to test at Level 2.")
    else:
        for r in level2:
            lines.append(format_level2_result(r))
            lines.append("")

    candidates = [r for r in level1 if r["status"] in ("CANDIDATE_EDGE", "ROBUST_CANDIDATE")]
    lines.append("\n## Summary\n")
    if candidates:
        for r in candidates:
            lines.append(f"  {STATUS_ICON[r['status']]} {r['dimension']}: {r['status']} — "
                         f"{r['best_value']} outperforms {r['worst_value']} by {r['gap']:+.2f}R, "
                         f"stable across a chronological split. NOT a validated edge — "
                         f"the next step is a controlled EXP-style test, not a policy change.")
    else:
        lines.append("  No candidate edges this cycle. That's a legitimate, honest result — "
                     "not every cycle should find something.")

    report = "\n".join(lines)
    with open(args.out_report, "w") as f:
        f.write(report)
    print(f"\nReport written to {args.out_report}")
    print(f"Cycle appended to {args.edge_log}")
    print("\n" + report)


if __name__ == "__main__":
    main()
