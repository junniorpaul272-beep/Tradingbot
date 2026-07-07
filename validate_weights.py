"""
validate_weights.py
====================
Run this once you have 50+ logged trades with their per-component scores
and outcomes. It fits a logistic regression of WIN/LOSS against the
individual component scores and prints the empirical (fitted) weights
next to the current guessed weights in smc_logic_engine_eurusd.py, so you
can see how far off the hand-picked numbers were.

INPUT
-----
A CSV with one row per closed trade, one column per score component
(matching the keys in SCORE_WEIGHTS, i.e. everything except htf_bias,
since bias is now a gate, not a scored component), and an `outcome`
column of "WIN"/"LOSS".

Example trade_log.csv:

    liquidity,structure,fib,atr,session,confirmation,outcome
    31,25,19,13,6,6,WIN
    15,12,0,13,0,6,LOSS
    31,0,19,7,6,0,WIN
    ...

Log this from the execution bot: whenever compute_confidence_score()
runs, dump `score_breakdown` plus the eventual trade outcome to this CSV
(one row per trade, appended as trades close).

USAGE
-----
    pip install scikit-learn pandas --break-system-packages
    python3 validate_weights.py trade_log.csv

OUTPUT
------
Fitted coefficients (rescaled to sum to 100, so they're directly
comparable to SCORE_WEIGHTS), plus model accuracy so you know how much
to trust them at your current sample size. Coefficients from under ~50
trades are noisy — treat them as a directional signal, not gospel, until
you have several hundred.
"""

import sys
import pandas as pd
import numpy as np

COMPONENTS = ["liquidity", "structure", "fib", "atr", "session", "confirmation"]


def main(csv_path: str):
    df = pd.read_csv(csv_path)

    missing = [c for c in COMPONENTS if c not in df.columns]
    if missing:
        raise SystemExit(f"CSV is missing expected columns: {missing}")
    if "outcome" not in df.columns:
        raise SystemExit("CSV must have an 'outcome' column of WIN/LOSS")

    if len(df) < 50:
        print(f"WARNING: only {len(df)} logged trades. The step-2 plan calls "
              f"for 50+ before trusting fitted weights — proceeding anyway, "
              f"but treat this output as very provisional.\n")

    X = df[COMPONENTS].values
    y = (df["outcome"].str.upper() == "WIN").astype(int).values

    if y.sum() == 0 or y.sum() == len(y):
        raise SystemExit("Need both WIN and LOSS examples in the log to fit anything.")

    from sklearn.linear_model import LogisticRegression

    model = LogisticRegression()
    model.fit(X, y)

    coefs = model.coef_[0]
    # Only positive coefficients make sense as "points toward a win" weights.
    # Negative or ~zero coefficients mean that component isn't predictive
    # (or is inversely predictive) in your actual data — that's a real
    # finding, not a bug, and worth flagging rather than silently clipping.
    raw = dict(zip(COMPONENTS, coefs))

    print("Raw fitted coefficients (log-odds per point of component score):")
    for c, w in raw.items():
        flag = "  <-- non-positive: not predictive in your data" if w <= 0 else ""
        print(f"  {c:14s} {w:+.4f}{flag}")

    positive = {c: max(w, 0.0) for c, w in raw.items()}
    total = sum(positive.values())
    if total > 0:
        rescaled = {c: round(w / total * 100) for c, w in positive.items()}
        # fix rounding drift so it still sums to exactly 100
        drift = 100 - sum(rescaled.values())
        if drift != 0:
            biggest = max(rescaled, key=rescaled.get)
            rescaled[biggest] += drift
        print("\nEmpirical weights (rescaled to sum to 100, comparable to SCORE_WEIGHTS):")
        for c, w in rescaled.items():
            print(f"  \"{c}\":{' ' * (14 - len(c))}{w},")
    else:
        print("\nAll coefficients were non-positive — can't produce a rescaled "
              "weight table from this data yet. Log more trades.")

    acc = model.score(X, y)
    print(f"\nModel accuracy on training data: {acc:.1%} (n={len(df)}, "
          f"{int(y.sum())} wins / {int(len(y) - y.sum())} losses)")
    print("Training accuracy on a small n is optimistic — it is not a "
          "backtest, just a sanity check that the fit isn't degenerate.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 validate_weights.py trade_log.csv")
    main(sys.argv[1])
