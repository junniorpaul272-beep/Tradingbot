"""
calibrate_threshold.py
=======================
Run this once you have 50+ logged trades with their total score and
outcome. It buckets trades by score (60-65, 65-70, 70-75, ...) and prints
the win rate per bucket, plus the score at which win rate crosses two
reference lines:

  - break-even at 1:3 RR (~25% win rate) — the mathematical floor where
    the strategy stops losing money on average
  - your practical target of 35%+ — where you said you'd want to be
    before sizing up

INPUT
-----
A CSV with one row per closed trade: a `score` column (the total from
compute_confidence_score, 0-100) and an `outcome` column of WIN/LOSS.

Example trade_log.csv:

    score,outcome
    72,WIN
    68,LOSS
    81,WIN
    ...

USAGE
-----
    pip install pandas --break-system-packages
    python3 calibrate_threshold.py trade_log.csv
"""

import sys
import pandas as pd

BUCKET_WIDTH = 5
BREAKEVEN_WR = 1 / (1 + 3.0)   # 1:3 RR -> 25%
TARGET_WR = 0.35


def main(csv_path: str):
    df = pd.read_csv(csv_path)
    if "score" not in df.columns or "outcome" not in df.columns:
        raise SystemExit("CSV must have 'score' and 'outcome' columns")

    if len(df) < 50:
        print(f"WARNING: only {len(df)} logged trades. Bucketed win rates "
              f"on this few trades are noisy — treat this as provisional.\n")

    df = df.copy()
    df["win"] = (df["outcome"].str.upper() == "WIN").astype(int)
    df["bucket_low"] = (df["score"] // BUCKET_WIDTH * BUCKET_WIDTH).astype(int)

    grouped = df.groupby("bucket_low").agg(
        n=("win", "size"),
        win_rate=("win", "mean"),
    ).sort_index()

    print(f"{'score bucket':>14s}  {'n':>4s}  {'win rate':>9s}")
    for bucket_low, row in grouped.iterrows():
        label = f"{bucket_low}-{bucket_low + BUCKET_WIDTH}"
        print(f"{label:>14s}  {int(row['n']):>4d}  {row['win_rate']:>8.1%}")

    print(f"\nBreak-even win rate at 1:3 RR: {BREAKEVEN_WR:.1%}")
    print(f"Practical target win rate: {TARGET_WR:.0%}+")

    # Find the lowest bucket at/above each line, scanning from the top down
    # so a single noisy low-n bucket below threshold doesn't get picked as
    # "the" crossover.
    def lowest_bucket_at_or_above(wr_line):
        candidates = [b for b, row in grouped.iterrows() if row["win_rate"] >= wr_line]
        return min(candidates) if candidates else None

    breakeven_bucket = lowest_bucket_at_or_above(BREAKEVEN_WR)
    target_bucket = lowest_bucket_at_or_above(TARGET_WR)

    if breakeven_bucket is not None:
        print(f"\nBreak-even crossover: score bucket {breakeven_bucket}-{breakeven_bucket + BUCKET_WIDTH}")
    else:
        print("\nNo bucket cleared the break-even win rate yet — need more data "
              "or the current scoring isn't discriminating at all.")

    if target_bucket is not None:
        print(f"35%+ target crossover: score bucket {target_bucket}-{target_bucket + BUCKET_WIDTH}")
        print(f"\n-> Suggested SCORE_TIER_ACCEPTABLE: {target_bucket}")
    else:
        print("No bucket cleared the 35% target yet — SCORE_TIER_ACCEPTABLE "
              "should stay conservative (or higher than currently set) until "
              "one does.")

    print("\nNote: bucket win rates on small n per bucket are unstable. Prefer "
          "the crossover to look stable across 2-3 consecutive buckets, not "
          "just one lucky/unlucky bucket, before moving the real threshold.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 calibrate_threshold.py trade_log.csv")
    main(sys.argv[1])
