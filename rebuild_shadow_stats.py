#!/usr/bin/env python3
"""
rebuild_shadow_stats.py

Recomputes the per-experiment RESOLUTION counters in shadow_stats.json
(resolved, wins, losses, timed_out, sum_r) from a de-duplicated
shadow_trade_log.jsonl, and merges them back into your existing
shadow_stats.json -- WITHOUT touching fields this script can't safely
recompute.

WHAT THIS FIXES:
    resolved, wins, losses, timed_out, sum_r
    -- these are derived 1:1 from resolved trade log rows, so they can
    be exactly reconstructed from a clean log.

WHAT THIS DOES NOT TOUCH (and why):
    logged
    -- counts setups OPENED, not resolved. It's incremented in
    log_shadow_setup() behind an idempotent leg_id guard, a different
    code path than the one that had the crash-window bug. Left as-is.

    hit_1r, hit_2r, hit_3r
    -- these depend on max_r_reached, a value tracked only on the
    transient in-memory "pending" setup and NEVER written to
    shadow_trade_log.jsonl. Once a trade resolves and the pending
    record is discarded, max_r_reached is gone for good -- it cannot
    be reconstructed from the log alone. This script leaves these
    fields at whatever value is currently in your shadow_stats.json.
    Treat them as approximate/legacy until you add max_r_reached to
    _append_shadow_trade_log() going forward (recommended -- see below).

Usage:
    python3 rebuild_shadow_stats.py <cleaned_log.jsonl> <current_shadow_stats.json> <output_shadow_stats.json>
"""
import json
import sys
from collections import defaultdict

EXPERIMENT_KEYS = [
    "EXP1_STRUCTURE", "EXP2_FIB", "EXP3_POI", "EXP4_LIQUIDITY",
    "EXP5_ABLATION", "EXP6_ALT_BIAS", "EXP7_TIER_ATR", "EXPE_REJECTED_LIVE",
]


def rebuild(log_path, stats_path, output_path):
    with open(stats_path, "r") as f:
        stats = json.load(f)

    # Recompute exactly the way update_pending_shadow_setups() does it,
    # so the rebuilt numbers are apples-to-apples with how they were
    # originally accumulated.
    agg = defaultdict(lambda: {"resolved": 0, "wins": 0, "losses": 0,
                                "timed_out": 0, "sum_r": 0.0})

    with open(log_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            exp = row.get("experiment")
            if exp not in EXPERIMENT_KEYS:
                continue  # unrecognized/legacy experiment key, skip safely
            r = float(row.get("r_achieved", 0.0))
            outcome = row.get("outcome")

            a = agg[exp]
            a["resolved"] += 1
            if outcome in ("TIMEOUT_WIN", "TIMEOUT_LOSS"):
                a["timed_out"] += 1
            if r > 0:
                a["wins"] += 1
            else:
                a["losses"] += 1
            a["sum_r"] += r

    for exp in EXPERIMENT_KEYS:
        stats.setdefault(exp, {"logged": 0, "resolved": 0, "wins": 0,
                                "losses": 0, "timed_out": 0, "sum_r": 0.0,
                                "hit_1r": 0, "hit_2r": 0, "hit_3r": 0})
        before = dict(stats[exp])
        recomputed = agg.get(exp, {"resolved": 0, "wins": 0, "losses": 0,
                                    "timed_out": 0, "sum_r": 0.0})
        stats[exp]["resolved"] = recomputed["resolved"]
        stats[exp]["wins"] = recomputed["wins"]
        stats[exp]["losses"] = recomputed["losses"]
        stats[exp]["timed_out"] = recomputed["timed_out"]
        stats[exp]["sum_r"] = round(recomputed["sum_r"], 4)
        # logged, hit_1r, hit_2r, hit_3r intentionally left untouched

        print(f"{exp}:")
        print(f"  resolved   {before.get('resolved')} -> {stats[exp]['resolved']}")
        print(f"  wins       {before.get('wins')} -> {stats[exp]['wins']}")
        print(f"  losses     {before.get('losses')} -> {stats[exp]['losses']}")
        print(f"  timed_out  {before.get('timed_out')} -> {stats[exp]['timed_out']}")
        print(f"  sum_r      {before.get('sum_r')} -> {stats[exp]['sum_r']}")
        print(f"  (logged, hit_1r/2r/3r left untouched: not recomputable from log)")

    with open(output_path, "w") as f:
        json.dump(stats, f, indent=2)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python3 rebuild_shadow_stats.py <cleaned_log.jsonl> "
              "<current_shadow_stats.json> <output_shadow_stats.json>")
        sys.exit(1)
    rebuild(sys.argv[1], sys.argv[2], sys.argv[3])
