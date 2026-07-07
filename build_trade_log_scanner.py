"""
build_trade_log_scanner.py
===========================
Converts this scanner's stats.json (specifically stats["journal"], the
list built by /win and /loss Telegram commands) into the two CSV formats
the calibration scripts expect. Safe to re-run any time — it always
regenerates from the full journal.

Note the outcome source here is different from the MT5 execution bot:
this scanner never sees broker fills, so WIN/LOSS comes entirely from
whatever you typed in Telegram (/win or /loss) after each signal. Garbage
in, garbage out — if you were inconsistent about logging results, the
calibration will reflect that, not the strategy's real edge.

USAGE
-----
    python3 build_trade_log_scanner.py stats.json

OUTPUT
------
    trade_log_components.csv  -> for validate_weights.py
        columns: liquidity,structure,fib,atr,session,confirmation,outcome
        (htf_bias is dropped — it's a gate, not a scored component, same
        as the execution-bot engine; rows missing it are unaffected)

    trade_log_score.csv       -> for calibrate_threshold.py
        columns: score,outcome
"""

import sys
import json
import csv

COMPONENTS = ["liquidity", "structure", "fib", "atr", "session", "confirmation"]


def main(stats_path: str):
    with open(stats_path, "r") as f:
        stats = json.load(f)

    journal = stats.get("journal", [])
    print(f"Found {len(journal)} journal entries in {stats_path}.")

    # Only entries with a definite WIN/LOSS result count.
    resolved = [e for e in journal if e.get("result") in ("WIN", "LOSS")]
    print(f"{len(resolved)} entries have a logged WIN/LOSS result "
          f"({len(journal) - len(resolved)} skipped — no result logged).")

    # ── score_breakdown CSV (for validate_weights.py) ───────────────────
    comp_rows = []
    skipped_no_breakdown = 0
    for e in resolved:
        bd = e.get("score_breakdown")
        if not isinstance(bd, dict) or any(c not in bd for c in COMPONENTS):
            skipped_no_breakdown += 1
            continue
        row = {c: bd[c] for c in COMPONENTS}
        row["outcome"] = e["result"]
        comp_rows.append(row)

    with open("trade_log_components.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COMPONENTS + ["outcome"])
        writer.writeheader()
        writer.writerows(comp_rows)

    print(f"trade_log_components.csv: {len(comp_rows)} rows "
          f"({skipped_no_breakdown} resolved entries skipped — no score_breakdown "
          f"logged, likely results logged before the logging fix was deployed).")

    # ── total score CSV (for calibrate_threshold.py) ────────────────────
    score_rows = []
    skipped_no_score = 0
    for e in resolved:
        score = e.get("score")
        if score is None or score == "?":
            skipped_no_score += 1
            continue
        score_rows.append({"score": score, "outcome": e["result"]})

    with open("trade_log_score.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["score", "outcome"])
        writer.writeheader()
        writer.writerows(score_rows)

    print(f"trade_log_score.csv: {len(score_rows)} rows "
          f"({skipped_no_score} resolved entries skipped — no score logged).")

    print("\nNext steps:")
    print("  python3 validate_weights.py trade_log_components.csv")
    print("  python3 calibrate_threshold.py trade_log_score.csv")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 build_trade_log_scanner.py stats.json")
    main(sys.argv[1])
