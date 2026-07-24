#!/usr/bin/env python3
"""
repair_logged_counts.py

Fixes the 'logged' counter in shadow_stats.json after a silent
methodology_version-triggered reset (or any shadow_stats.json corruption)
caused it to undercount relative to 'resolved'.

WHY THIS IS SAFE:
    Every trade that has ever resolved MUST have been logged at some
    point -- there's no other way for it to exist. And every trade
    currently sitting in shadow_state.json's "pending" list was also
    logged (just not resolved yet). So for each experiment:

        true_logged >= resolved + count(pending trades for that experiment)

    This is a LOWER BOUND, not an exact reconstruction -- it can't
    recover setups that were logged, then themselves discarded/replaced
    before ever resolving (e.g. superseded by a fresher leg). Those are
    gone for good, same as the hit_1r/2r/3r gap from before. This script
    raises 'logged' to at least this floor wherever it's currently lower
    than the floor, and leaves it alone otherwise.

Usage:
    python3 repair_logged_counts.py <shadow_stats.json> <shadow_state.json> <output_shadow_stats.json>
"""
import json
import sys
from collections import Counter

EXPERIMENT_KEYS = [
    "EXP1_STRUCTURE", "EXP2_FIB", "EXP3_POI", "EXP4_LIQUIDITY",
    "EXP5_ABLATION", "EXP6_ALT_BIAS", "EXP7_TIER_ATR", "EXPE_REJECTED_LIVE",
]


def repair(stats_path, state_path, output_path):
    with open(stats_path, "r") as f:
        stats = json.load(f)
    with open(state_path, "r") as f:
        state = json.load(f)

    pending = state.get("pending", [])
    pending_counts = Counter(p.get("experiment") for p in pending)

    for exp in EXPERIMENT_KEYS:
        stats.setdefault(exp, {"logged": 0, "resolved": 0, "wins": 0,
                                "losses": 0, "timed_out": 0, "sum_r": 0.0,
                                "hit_1r": 0, "hit_2r": 0, "hit_3r": 0})
        current_logged = stats[exp].get("logged", 0)
        resolved = stats[exp].get("resolved", 0)
        floor = resolved + pending_counts.get(exp, 0)

        if current_logged < floor:
            print(f"{exp}: logged {current_logged} -> {floor} "
                  f"(resolved={resolved}, currently_pending={pending_counts.get(exp, 0)})")
            stats[exp]["logged"] = floor
        else:
            print(f"{exp}: logged {current_logged} already >= floor {floor}, left as-is")

    with open(output_path, "w") as f:
        json.dump(stats, f, indent=2)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python3 repair_logged_counts.py <shadow_stats.json> "
              "<shadow_state.json> <output_shadow_stats.json>")
        sys.exit(1)
    repair(sys.argv[1], sys.argv[2], sys.argv[3])
