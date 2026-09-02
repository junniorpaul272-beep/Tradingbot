#!/usr/bin/env python3
"""
Edge Finder — periodic research cycle over the Trade Investigation
Bureau's accumulated dataset. See edge_finder_lib.py's module docstring
for the full design (hierarchy, stability checks, why brute-force
search is deliberately avoided).

THIS IS NOT A CONTINUOUS PROCESS. Per chat (Vally): "I definitely would
not run deep edge discovery after every trade... I would separate
Continuous (Trade Investigation Bureau, every trade) from Periodic
(Edge Finder, every N new trades)." Nor does it belong inside live-
scan.yml/min-scan.yml's own loops — see edge-research.yml's own header
for why research and live scanning "shouldn't be tied to the same
heartbeat," per chat.

TWO WAYS TO RUN THIS, MATCHING TWO DIFFERENT NEEDS:

  1. SINGLE-SCOPE MODE (--experiment X, or neither flag for pooled
     "ALL") — the original mode. One scope, one eligibility check, one
     full report. Good for a manual, deliberate "let me look at EXP2
     specifically" run.

  2. --auto MODE (2026-08-30, per chat — Vally: "I'd consider making
     [eligibility] per-experiment... different experiments may
     represent different policies, and pooling them could create fake
     edges... Don't pool everything automatically"). Cheaply checks
     EVERY experiment's own eligibility independently (see edge_
     finder_lib.check_eligibility_per_experiment()), runs a FULL cycle
     only for the ones that actually cleared their own threshold, and
     sends ONE concise Telegram summary covering whatever ran — not a
     summary on every wake-up, only when something actually happened.
     This is the mode a scheduled workflow (edge-research.yml) uses.

USAGE:
    python3 edge_finder.py                          # pooled ALL, single scope
    python3 edge_finder.py --experiment EXP2_FIB     # single scope
    python3 edge_finder.py --force
    python3 edge_finder.py --drilldown market_phase=exhaustion
    python3 edge_finder.py --auto                    # per-experiment eligibility, all experiments
    python3 edge_finder.py --auto --telegram         # + send a concise summary if anything ran
"""
import argparse
import os
from datetime import datetime, timezone

from edge_finder_lib import (
    load_trades, load_jsonl, run_edge_finder_cycle, load_last_cycle_count,
    should_run_cycle, append_cycle_record, MIN_GROUP_N,
    # ADDED (2026-08-30, per chat — targeted, human-initiated drilldown,
    # not gated behind waiting for a dimension to reach CANDIDATE_EDGE
    # automatically. Per Vally: "My immediate next move would be...
    # a dedicated investigation on [a specific dimension] first.")
    LEG_DIMENSIONS, ENV_CATEGORICAL, ENV_CONTINUOUS, DERIVED_DIMENSIONS,
    confound_scan, subpopulation_drilldown, _bucket_continuous, _value_for_dimension,
    # ADDED (2026-08-30, per chat — automation redesign): per-experiment
    # eligibility and the candidate registry/dedup mechanism.
    ALL_EXPERIMENTS, check_eligibility_per_experiment, last_count_per_scope,
    load_candidate_registry, save_candidate_registry, update_candidate_registry,
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
        lines.append(f"      Fewer than 2 GENUINE values cleared the {MIN_GROUP_N}-trade floor this cycle.")
        if r.get("gap_population") and r["gap_population"]["n"] > 0:
            lines.append(f"      (Data-coverage gap population excluded from comparison: "
                         f"n={r['gap_population']['n']}, expectancy={r['gap_population']['expectancy']:+.2f}R "
                         f"— not a market value, reported for reference only, see chat on no_prior_leg.)")
        return "\n".join(lines)

    if r["status"] == "NO_SEPARATION":
        lines.append(f"      Largest gap between qualifying GENUINE values: {r['gap']:+.2f}R — "
                     f"below the {0.15:.2f}R bar. No meaningful separation found.")
        for val, stats in sorted(r["groups"].items(), key=lambda kv: -kv[1]["expectancy"]):
            lines.append(format_group_line(val, stats))
        if r.get("gap_population") and r["gap_population"]["n"] > 0:
            lines.append(f"      (Data-coverage gap population excluded from comparison above: "
                         f"n={r['gap_population']['n']}, expectancy={r['gap_population']['expectancy']:+.2f}R)")
        return "\n".join(lines)

    # OBSERVATION / SIGNAL / CANDIDATE_EDGE / ROBUST_CANDIDATE all share
    # the best/worst framing.
    lines.append(f"      Best: {r['best_value']} vs worst: {r['worst_value']} — gap {r['gap']:+.2f}R "
                 f"(pooled n={r['pooled_n']})")
    for val, stats in sorted(r["groups"].items(), key=lambda kv: -kv[1]["expectancy"]):
        lines.append(format_group_line(val, stats))
    if r.get("gap_population") and r["gap_population"]["n"] > 0:
        gp = r["gap_population"]
        pf_str = f"{gp['profit_factor']:.2f}" if gp["profit_factor"] is not None else "inf"
        lines.append(f"      (Data-coverage gap population, EXCLUDED from the comparison above — "
                     f"not a market value: n={gp['n']}, win_rate={gp['win_rate']:.0f}%, "
                     f"expectancy={gp['expectancy']:+.2f}R, PF={pf_str})")

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


def send_telegram_message(message):
    """
    ADDED (2026-08-30, per chat — automation redesign). Self-contained,
    NOT imported from scanner_common.py — that module pulls in a large
    web of state-file constants and other scanner-specific machinery
    this standalone script has no business depending on. Same retry-
    once-plain-text behavior as scanner_common.send_telegram() (a
    message failing to parse as Markdown due to a stray _ * ` in a
    dynamically-inserted value — e.g. a bucket label like UNDER_100 —
    shouldn't mean the message vanishes with zero trace; see that
    function's own docstring for the exact incident this guards
    against). Requires the `requests` package and TELEGRAM_TOKEN/
    TELEGRAM_CHAT_ID env vars — the ONE dependency this script has that
    isn't pure stdlib, and only needed when --telegram is actually
    passed (see main() below — import is deferred so --auto without
    --telegram still needs nothing beyond stdlib).
    """
    import requests
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[TELEGRAM] TELEGRAM_TOKEN/TELEGRAM_CHAT_ID not set — skipping send.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=10)
        r.raise_for_status()
        if r.json().get("ok"):
            return True
        raise Exception(r.json())
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
    try:
        r2 = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
        r2.raise_for_status()
        return bool(r2.json().get("ok"))
    except Exception as e:
        print(f"[TELEGRAM FALLBACK ERROR] {e}")
        return False


EVENT_ICON = {"NEW": "🆕", "STRENGTHENED": "🟢", "WEAKENED": "🟡", "DISAPPEARED": "⚫"}


def format_auto_summary(run_results):
    """
    ADDED (2026-08-30, per chat — Vally was explicit: "I wouldn't send
    you the entire report every time. That's going to get annoying
    fast... the Telegram notification should tell you: Something
    happened. Here's what matters. Not dump 3,000 words into your
    chat."). `run_results` is a list of (scope, trade_count, new_count,
    level1, registry_events) tuples for every scope that actually ran
    this cycle (scopes that were skipped for lack of new data are NOT
    in this list at all — see main()'s --auto branch). Only NEW/
    STRENGTHENED/WEAKENED/DISAPPEARED registry events are shown
    prominently — UNCHANGED is deliberately omitted here (see
    update_candidate_registry()'s own docstring for why treating
    UNCHANGED as notification-worthy would be exactly the noise Vally
    warned against).
    """
    if not run_results:
        return None
    lines = ["🔬 *Edge Research Cycle Complete*", ""]
    for scope, trade_count, new_count, level1, events in run_results:
        lines.append(f"*Scope: {scope}* — {trade_count} trades analyzed ({new_count} new)")
        notable_events = [(fid, etype, r) for fid, etype, r in events if etype != "UNCHANGED"]
        if not notable_events:
            candidates = [r for r in level1 if r["status"] in ("CANDIDATE_EDGE", "ROBUST_CANDIDATE")]
            if candidates:
                lines.append("  No change in existing candidates.")
            else:
                lines.append("  No candidates this cycle.")
        for fid, etype, r in notable_events:
            icon = EVENT_ICON.get(etype, "•")
            if etype == "DISAPPEARED":
                lines.append(f"  {icon} A previously-tracked candidate on this scope no longer "
                             f"reached CANDIDATE_EDGE this cycle: `{fid}`")
                continue
            lines.append(f"  {icon} {etype.title()}: *{r['dimension']}* — "
                         f"{r['best_value']} vs {r['worst_value']}, "
                         f"{r['gap']:+.2f}R (n={r['pooled_n']})")
        lines.append("")
    lines.append("📁 Full report saved as a workflow artifact / edge_finder_log.jsonl.")
    lines.append("Not a validated edge — next step for anything NEW is a confound check, "
                 "then a controlled EXP-style test, not a policy change.")
    return "\n".join(lines)


def run_auto_mode(args):
    """
    ADDED (2026-08-30, per chat). Checks EVERY experiment's OWN
    eligibility independently (cheap — counts only), runs a FULL cycle
    only for the ones that cleared their own threshold, updates the
    candidate registry per scope, and — if --telegram — sends ONE
    concise summary covering whatever actually ran. Never pools
    experiments together automatically (per chat: "different
    experiments may represent different policies, and pooling them
    could create fake edges") — "ALL" pooled mode is still available,
    but only via explicit single-scope mode (no --experiment flag),
    never silently substituted here.
    """
    experiments = args.experiments.split(",") if args.experiments else ALL_EXPERIMENTS
    eligibility = check_eligibility_per_experiment(
        args.shadow_log, args.edge_log, experiments, min_new_trades=args.min_new_trades)

    print("Eligibility check (per experiment):")
    for exp, info in eligibility.items():
        status = "ELIGIBLE" if info["eligible"] else "skip"
        print(f"  {exp}: {status} — {info['reason']}")

    registry = load_candidate_registry(args.registry)
    run_results = []
    for exp, info in eligibility.items():
        if not info["eligible"] and not args.force:
            continue
        print(f"\n=== Running full cycle: {exp} ===")
        trades = load_trades(args.shadow_log, experiment=exp,
                              leg_obs_log_path=None if args.no_backfill else args.leg_obs_log)
        if not trades:
            continue
        level1, level2 = run_edge_finder_cycle(trades)
        append_cycle_record(args.edge_log, len(trades), level1, level2, scope=exp)
        run_at = datetime.now(timezone.utc).isoformat()
        events = update_candidate_registry(registry, exp, level1, run_at)
        run_results.append((exp, len(trades), len(trades) - info["last"], level1, events))

    save_candidate_registry(args.registry, registry)

    if not run_results:
        print("\nNo experiment was eligible this cycle — nothing ran.")
        # FIX (2026-08-30, same pass — found before shipping, not after):
        # write an empty-but-present report so a workflow's artifact-
        # upload step has something to find rather than nothing at all,
        # same "don't let a normal no-op look like a failure" discipline
        # as investigation.yml's if-no-files-found: warn.
        with open(args.out_report, "w") as f:
            f.write(f"# Edge Research cycle {datetime.now(timezone.utc).isoformat()}\n\n"
                     f"No experiment was eligible this wake-up — nothing ran.\n")
        return

    # FULL REPORT (2026-08-30, same pass — was missing entirely: --auto
    # mode printed to stdout and sent a CONCISE Telegram summary, but
    # never wrote edge_finder_report.md, so a workflow's artifact-upload
    # step would find nothing. The Telegram summary is intentionally
    # terse (per chat); the report file is where the full per-dimension
    # detail belongs for whoever wants to actually read it later.
    report_lines = [f"# Edge Research cycle {datetime.now(timezone.utc).isoformat()}\n"]
    for scope, trade_count, new_count, level1, events in run_results:
        report_lines.append(f"\n## Scope: {scope} — {trade_count} trades ({new_count} new)\n")
        for r in level1:
            report_lines.append(format_level1_result(r))
            report_lines.append("")
        notable = [(fid, etype) for fid, etype, _ in events if etype != "UNCHANGED"]
        if notable:
            report_lines.append("### Registry events this cycle")
            for fid, etype in notable:
                report_lines.append(f"  {etype}: {fid}")
    with open(args.out_report, "w") as f:
        f.write("\n".join(report_lines))

    summary = format_auto_summary(run_results)
    print("\n" + summary)

    # GATED (per chat, 2026-09-02): previously sent this summary to
    # Telegram every time run_results was non-empty — i.e. every time
    # ANY experiment happened to be eligible this wake-up, even if every
    # single scope's registry events were all UNCHANGED (the "No
    # candidates this cycle" / "No change in existing candidates" case).
    # That's exactly the noise Vally's original design note (see
    # format_auto_summary's own docstring) was trying to avoid, one
    # level up from where the original fix caught it — "ran, but found
    # nothing" was still pinging Telegram every cycle. Report file and
    # stdout print above are UNCHANGED and still happen every run,
    # eligible or not — this only gates the phone notification. Only
    # sends when at least one scope has an actual state-change event:
    # NEW, STRENGTHENED, WEAKENED, or DISAPPEARED (i.e. anything
    # format_auto_summary itself would call "notable" — see its own
    # `notable_events` filter, same definition, not duplicated logic).
    any_notable = any(
        etype != "UNCHANGED"
        for _scope, _tc, _nc, _level1, events in run_results
        for _fid, etype, _r in events
    )
    if args.telegram:
        if any_notable:
            sent = send_telegram_message(summary)
            print(f"\nTelegram: {'sent' if sent else 'failed — see error above'}")
        else:
            print("\nTelegram: skipped — no NEW/STRENGTHENED/WEAKENED/DISAPPEARED "
                  "events this cycle (report file + log still written above).")


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
    ap.add_argument("--auto", action="store_true",
                     help="Per-experiment eligibility mode (see edge_finder_lib.check_eligibility_"
                          "per_experiment) — runs a full cycle only for experiments that cleared "
                          "their OWN threshold independently. Intended for a scheduled workflow, "
                          "not manual single-scope investigation.")
    ap.add_argument("--experiments", help="Comma-separated list for --auto mode (default: all known experiments)")
    ap.add_argument("--telegram", action="store_true",
                     help="Send a concise summary via Telegram after --auto (requires TELEGRAM_TOKEN/"
                          "TELEGRAM_CHAT_ID env vars and the requests package). No effect without --auto.")
    ap.add_argument("--registry", default="candidate_registry.json",
                     help="Candidate registry file (see edge_finder_lib.update_candidate_registry) — "
                          "only used in --auto mode.")
    args = ap.parse_args()

    if args.auto:
        run_auto_mode(args)
        return

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

    scope = args.experiment or "ALL"
    last_count = last_count_per_scope(args.edge_log).get(scope, 0)
    proceed, reason = should_run_cycle(len(trades), last_count, min_new_trades=args.min_new_trades)
    print(reason)
    if not proceed and not args.force:
        with open(args.out_report, "w") as f:
            f.write(f"# Edge Finder — skipped\n\n{reason}\n")
        return
    if not proceed and args.force:
        print("(--force set — running anyway)")

    level1, level2 = run_edge_finder_cycle(trades)
    record = append_cycle_record(args.edge_log, len(trades), level1, level2, scope=args.experiment or "ALL")

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
