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
    # ADDED (2026-08-30, per chat — targeted, human-initiated drilldown,
    # not gated behind waiting for a dimension to reach CANDIDATE_EDGE
    # automatically. Per Vally: "My immediate next move would be...
    # a dedicated investigation on [a specific dimension] first.")
    LEG_DIMENSIONS, ENV_CATEGORICAL, ENV_CONTINUOUS, DERIVED_DIMENSIONS,
    confound_scan, subpopulation_drilldown, _bucket_continuous, _value_for_dimension,
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

    # CONFOUND SCAN + DRILLDOWN (2026-08-30, per chat) — only present on
    # CANDIDATE_EDGE/ROBUST_CANDIDATE results (see run_edge_finder_cycle()
    # in edge_finder_lib.py for why this isn't run on every SIGNAL).
    if "confounds" in r:
        lines.append("\n      --- Confound investigation (per chat: before this becomes a "
                     "hypothesis, is it actually comparable populations?) ---")
        if not r["confounds"]:
            lines.append("      No confound concerns surfaced by the automatic checks (time "
                         "concentration, experiment concentration, cross-dimension distribution). "
                         "That's a real point in this candidate's favor, not proof it's genuine.")
        else:
            for c in r["confounds"]:
                lines.append(f"      ⚠️ {c}")

    if "drilldown" in r:
        lines.append("\n      --- Sub-population drilldown: within each side of this candidate, "
                     "what separates ITS OWN winners from ITS OWN losers? (per chat — the "
                     "generalized hypothesis-generation step, run on BOTH sides, not just the "
                     "underperforming one) ---")
        for side_label, side_key in ((r["best_value"], "best_value_subset"), (r["worst_value"], "worst_value_subset")):
            side = r["drilldown"][side_key]
            lines.append(f"      Within '{side_label}' (n={side['n']}):")
            interesting = [res for res in side["results"] if res["status"] not in ("INSUFFICIENT_DATA", "NO_SEPARATION")]
            if not interesting:
                lines.append("        No other dimension separates outcomes within this subgroup — "
                             f"it may be more homogeneous than it looks, or the available dimensions "
                             f"just don't capture what actually differs here yet.")
            for res in interesting:
                lines.append(f"        {res['dimension']}: {res['best_value']} vs {res['worst_value']} "
                             f"— gap {res['gap']:+.2f}R (n={res['pooled_n']}) — "
                             f"a genuine hypothesis candidate for what separates "
                             f"'{side_label}' winners from '{side_label}' losers, not yet stability-tested.")

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
    ap.add_argument("--drilldown", metavar="DIM=VALUE",
                     help="Targeted, manual investigation — e.g. --drilldown market_phase=exhaustion "
                          "asks 'within exhaustion trades, what separates winners from losers?' right "
                          "now, without waiting for market_phase to reach CANDIDATE_EDGE on its own. "
                          "Runs instead of a full cycle; does not touch edge_finder_log.jsonl or the "
                          "sample-based trigger.")
    ap.add_argument("--out-report", default="edge_finder_report.md")
    args = ap.parse_args()

    print(f"Loading {args.shadow_log} ...")
    trades = load_trades(args.shadow_log, experiment=args.experiment,
                          leg_obs_log_path=None if args.no_backfill else args.leg_obs_log)
    print(f"  {len(trades)} resolved trades" + (f" for {args.experiment}" if args.experiment else " (all experiments)"))

    if not trades:
        print("No trades to analyze.")
        return

    if args.drilldown:
        if "=" not in args.drilldown:
            print("--drilldown must be DIM=VALUE, e.g. --drilldown market_phase=exhaustion")
            return
        dim, value = args.drilldown.split("=", 1)
        all_dims = LEG_DIMENSIONS + ENV_CATEGORICAL + ENV_CONTINUOUS + DERIVED_DIMENSIONS
        if dim not in all_dims:
            print(f"Unknown dimension '{dim}'. Available: {', '.join(all_dims)}")
            return
        continuous_buckets = {f: _bucket_continuous(trades, f) for f in ENV_CONTINUOUS}
        n, results = subpopulation_drilldown(trades, dim, value, all_dims, continuous_buckets)
        print(f"\n=== Manual drilldown: within {dim}={value} (n={n}), what separates winners from losers? ===\n")
        if n < MIN_GROUP_N:
            print(f"Only {n} trades match {dim}={value} — below the {MIN_GROUP_N}-trade floor. "
                  f"Not enough to investigate yet.")
        else:
            interesting = [r for r in results if r["status"] not in ("INSUFFICIENT_DATA", "NO_SEPARATION")]
            if not interesting:
                print(f"No other dimension separates outcomes within {dim}={value}. Either this "
                      f"subgroup is more homogeneous than expected, or the available dimensions "
                      f"don't capture what actually differs here yet — see the Research Coverage "
                      f"note from a full cycle for what's NOT YET CAPTURED.")
            for r in interesting:
                print(f"  {r['dimension']}: {r['best_value']} outperforms {r['worst_value']} by "
                      f"{r['gap']:+.2f}R (n={r['pooled_n']}) — a genuine hypothesis candidate, "
                      f"not yet stability-tested (this is a single, targeted drilldown, not a full "
                      f"cycle — re-run through the normal cycle once there's enough data for a "
                      f"proper time-split check on this specific subgroup).")
            for r in results:
                if r["status"] == "NO_SEPARATION":
                    print(f"  {r['dimension']}: no meaningful separation within this subgroup either.")
        print(f"\n--- Confound check on {dim}={value} vs the rest of the dataset ---")
        # Reuses confound_scan by treating "value" as best and pooling
        # everything else as worst is not quite right (confound_scan
        # expects two specific values) — for a single-value drilldown,
        # the meaningful confound question is simpler: is THIS group
        # concentrated in time/experiment relative to the WHOLE dataset?
        # Approximated here by comparing against the dimension's most
        # common OTHER value, which is what a human would naturally
        # compare against anyway.
        other_counts = {}
        for i in range(len(trades)):
            v = _value_for_dimension(trades, i, dim, continuous_buckets)
            if v is not None and v != value:
                other_counts[v] = other_counts.get(v, 0) + 1
        if other_counts:
            comparison_value = max(other_counts, key=other_counts.get)
            concerns = confound_scan(trades, dim, value, comparison_value, all_dims, continuous_buckets)
            if not concerns:
                print(f"No confound concerns vs '{comparison_value}' (the most common other value).")
            else:
                for c in concerns:
                    print(f"  ⚠️ {c}")
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

    lines.append("\n## Research coverage\n")
    lines.append("Per chat (Vally): 'I wouldn't treat INSUFFICIENT_DATA as the Edge Finder "
                 "failing — treat it as a data coverage report. The Bureau improves observation "
                 "coverage; the Edge Finder tells you which observations are researchable.'\n")
    ready = [r["dimension"] for r in level1 if r["status"] != "INSUFFICIENT_DATA"]
    insufficient = [r["dimension"] for r in level1 if r["status"] == "INSUFFICIENT_DATA"]
    lines.append(f"  READY (enough data to say something): {', '.join(ready) if ready else 'none'}")
    lines.append(f"  INSUFFICIENT COVERAGE (captured, but not enough usable variation yet): "
                 f"{', '.join(insufficient) if insufficient else 'none'}")
    lines.append("  NOT YET CAPTURED (per chat — Vally's Setup Anatomy/Entry Characteristics "
                 "dimensions: sweep quality, retest quality, breakout acceptance, wick/body "
                 "ratios, rejection behaviour): none of these exist as data yet, so they can't "
                 "even reach INSUFFICIENT_DATA — they're simply absent from this cycle entirely.")

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
