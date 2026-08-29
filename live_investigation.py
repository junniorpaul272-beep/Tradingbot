#!/usr/bin/env python3
"""
Live trade investigation — condition-level reconstruction for actual
executed trades (tier1/2/3 signals), separate from the shadow
experiments (see shadow_investigation.py for those).

Reads live_trade_log.jsonl (see _append_live_trade_log in
scanner_live.py — pair, direction, entry/exit, sl/tp, opened_at,
closed_at, outcome, r_achieved, target_r, tier_label, score,
tier_rating, close_method) and leg_obs_log.jsonl, joins by timestamp
containment exactly like the shadow script.

SCHEMA DIFFERENCES FROM SHADOW, HANDLED HERE (do not assume the two
logs are interchangeable):
  - live_trade_log uses "closed_at", shadow uses "resolved_at" —
    passed explicitly to enrich_trades() below.
  - live trades have no "experiment" field and no "tags" dict — this
    script groups by tier_label instead (TIER1POI / TIER2FIB / etc.),
    since that's the only cohort dimension live trades carry natively.
  - live trades carry entry/exit/sl/tp directly (shadow trades don't) —
    included in the per-trade CSV since they're free and useful for
    manually re-checking a specific trade against a chart.

Same caveats as shadow_investigation.py (leg-level granularity,
no_matching_leg trades kept not dropped, older records may lack newer
fields) — not repeated in full here, see that script's docstring.

USAGE:
    python3 live_investigation.py
    python3 live_investigation.py --live-log /path/to/live_trade_log.jsonl --leg-obs-log /path/to/leg_obs_log.jsonl
"""
import argparse
import csv
from datetime import datetime, timezone
from collections import defaultdict

from investigation_lib import (
    load_jsonl, build_leg_intervals, enrich_trades, summarize_conditions,
    format_counter_block, build_weekly_curve, find_peak_then_trough,
    context_flat, iso_week_key,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live-log", default="live_trade_log.jsonl")
    ap.add_argument("--leg-obs-log", default="leg_obs_log.jsonl")
    ap.add_argument("--out-report", default="live_investigation_report.md")
    ap.add_argument("--out-csv", default="live_investigation_per_trade.csv")
    args = ap.parse_args()

    print(f"Loading {args.live_log} ...")
    all_trades = load_jsonl(args.live_log)
    print(f"  {len(all_trades)} total resolved live trades")

    print(f"Loading {args.leg_obs_log} ...")
    leg_records = load_jsonl(args.leg_obs_log)
    leg_intervals = build_leg_intervals(leg_records)
    print(f"  {len(leg_records)} resolved leg records, {len(leg_intervals)} usable")

    if not all_trades:
        print("No live trades found — nothing to investigate.")
        return

    enriched, unmatched = enrich_trades(all_trades, leg_intervals, resolved_key="closed_at")
    print(f"  {len(enriched)} enriched, {unmatched} with no enclosing leg "
          f"({unmatched/len(enriched)*100:.0f}%)" if enriched else "")

    # ---- Per-trade CSV — includes entry/exit/sl/tp since live trades
    # carry them natively and it's useful for manually re-checking a
    # specific trade against a chart. ----
    fieldnames = [
        "opened_at", "closed_at", "direction", "entry_price", "exit_price",
        "sl", "tp", "outcome", "r_achieved", "target_r", "tier_label",
        "score", "tier_rating", "close_method", "enclosing_leg_fate",
    ] + [f"cond_{k}" for k in ["market_phase", "atr_bucket", "session", "bias_state", "spike_state", "measured_move_bucket"]]

    with open(args.out_csv, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for r in enriched:
            ctx = context_flat(r.get("_context"))
            writer.writerow({
                "opened_at": r.get("opened_at"), "closed_at": r.get("closed_at"),
                "direction": r.get("direction"), "entry_price": r.get("entry_price"),
                "exit_price": r.get("exit_price"), "sl": r.get("sl"), "tp": r.get("tp"),
                "outcome": r.get("outcome"), "r_achieved": r.get("r_achieved"),
                "target_r": r.get("target_r"), "tier_label": r.get("tier_label"),
                "score": r.get("score"), "tier_rating": r.get("tier_rating"),
                "close_method": r.get("close_method"),
                "enclosing_leg_fate": r.get("_enclosing_leg_fate"),
                **{f"cond_{k}": v for k, v in ctx.items()},
            })

    # ---- Cohort breakdown by tier_label (live's native grouping — no
    # experiment/tags field exists here, unlike shadow trades). ----
    by_tier = defaultdict(list)
    for r in enriched:
        by_tier[r.get("tier_label", "unknown")].append(r)

    lines = [f"# Live Trade Investigation — {datetime.now(timezone.utc).isoformat()}\n"]
    lines.append(f"Total: {len(enriched)} trades, {unmatched} with no enclosing leg\n")

    for tier, rows in sorted(by_tier.items(), key=lambda kv: -len(kv[1])):
        wins = sum(1 for r in rows if r.get("outcome") == "WIN")
        total_r = sum(r.get("r_achieved") or 0.0 for r in rows)
        lines.append(f"\n## {tier} — {len(rows)} trades, {wins}/{len(rows)} wins "
                     f"({wins/len(rows)*100:.0f}%), {total_r:+.2f}R total")
        lines.append(format_counter_block("Condition mix", summarize_conditions(rows), len(rows)))

    # ---- Overall weekly curve, same peak/trough auto-detection as
    # shadow, in case a losing streak in live trading also traces to a
    # condition-mix shift. ----
    curve, _ = build_weekly_curve(enriched)
    peak_idx, trough_idx = find_peak_then_trough(curve)
    lines.append("\n## Weekly cumulative R (all tiers combined)\n")
    lines.append("| Week | Trades | Weekly R | Cumulative R |")
    lines.append("|---|---|---|---|")
    for i, (wk, wr, cum, n) in enumerate(curve):
        marker = " ← peak" if i == peak_idx else (
            " ← trough after peak" if trough_idx is not None and i == trough_idx and trough_idx != peak_idx else "")
        lines.append(f"| {wk} | {n} | {wr:+.2f}R | {cum:+.2f}R{marker} |")

    report = "\n".join(lines)
    with open(args.out_report, "w") as f:
        f.write(report)
    print(f"\nPer-trade CSV: {args.out_csv}")
    print(f"Summary report: {args.out_report}")
    print("\n" + report)


if __name__ == "__main__":
    main()
