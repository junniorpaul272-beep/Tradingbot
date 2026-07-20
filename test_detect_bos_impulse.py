"""
Unit tests for detect_bos_impulse() — the fractal-based dominant-leg
tracker underneath everything else (Macro Bias, all three tiers, the
Shadow Pipeline). Per chat: this is the most complex function in the
file and the one place a single off-by-one would silently corrupt
every downstream decision, so it gets its own known-answer tests
rather than relying on live data to eventually surface a bug.

Run: python3 test_detect_bos_impulse.py
"""
import os
os.environ.setdefault("TELEGRAM_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "x")
os.environ.setdefault("TWELVE_DATA_KEY", "x")

import pandas as pd
import scanner as s


def make_df(rows):
    idx = pd.date_range("2026-07-01", periods=len(rows), freq="15min", tz="UTC")
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])


# A clean, hand-traced bullish leg: fractal low at idx=2 (0.95), fractal
# high at idx=6 (1.10), close breaks above 1.10 at idx=10 -> BULLISH BOS
# with origin=0.95. Every value here was traced by hand against the
# actual algorithm (candidate tracking, external_high/low, break
# detection) before running it — see chat history for the full trace.
BASE_ROWS = [
    [1.005, 1.02, 1.00, 1.01],   # 0
    [1.010, 1.01, 0.99, 1.00],   # 1
    [1.000, 1.00, 0.95, 0.98],   # 2  <- fractal LOW (0.95)
    [0.980, 1.03, 0.98, 1.02],   # 3
    [1.020, 1.05, 1.00, 1.04],   # 4
    [1.040, 1.06, 1.02, 1.05],   # 5
    [1.050, 1.10, 1.03, 1.06],   # 6  <- fractal HIGH (1.10)
    [1.060, 1.08, 1.04, 1.05],   # 7
    [1.050, 1.07, 1.03, 1.04],   # 8
    [1.040, 1.09, 1.02, 1.06],   # 9
    [1.060, 1.12, 1.05, 1.11],   # 10 <- close breaks above 1.10 -> BULLISH BOS
    [1.110, 1.13, 1.10, 1.12],   # 11
    [1.120, 1.15, 1.11, 1.14],   # 12
]


def test_clean_bullish_bos():
    df = make_df(BASE_ROWS)
    result = s.detect_bos_impulse(df)
    assert result is not None, "expected a confirmed BULLISH leg, got None"
    assert result["direction"] == "BULLISH"
    assert result["impulse_start"] == 0.95, result["impulse_start"]
    assert result["impulse_end"] == 1.15, result["impulse_end"]
    assert result["break_count"] == 1, result["break_count"]
    assert result["origin_idx"] == 2, result["origin_idx"]
    assert result["was_choch"] is False, (
        "first-ever leg has no prior dominant to flip from -- must not be a CHoCH"
    )
    print("PASS: test_clean_bullish_bos")


def test_insufficient_fractals_returns_none():
    flat_rows = [[1.0, 1.001, 0.999, 1.0]] * 5
    df = make_df(flat_rows)
    assert s.detect_bos_impulse(df) is None
    print("PASS: test_insufficient_fractals_returns_none")


def test_origin_invalidation_resets_dominant():
    # Extends the confirmed bullish leg above, then trades back through
    # its own origin (0.95) with no new opposing break afterward — the
    # leg must be invalidated (return None), not silently held stale.
    rows = BASE_ROWS + [
        [1.14, 1.145, 0.90, 1.05],   # low violates origin (0.90 <= 0.95)
        [1.05, 1.06, 1.00, 1.02],
    ]
    df = make_df(rows)
    result = s.detect_bos_impulse(df)
    assert result is None, f"expected origin violation to invalidate the leg, got {result}"
    print("PASS: test_origin_invalidation_resets_dominant")


def test_choch_flips_dominant_direction():
    # Continues the confirmed bullish leg above with a pullback that forms
    # a new low fractal, then a new high fractal, then a close breaking
    # below that new low -- a genuine CHoCH. Dominant must flip to
    # BEARISH, anchored on the NEW high fractal (1.13), not the old
    # bullish origin, and break_count must reset to 1, not keep counting
    # from the prior bullish leg.
    rows = BASE_ROWS[:11] + [
        [1.11, 1.12, 1.09, 1.10],
        [1.10, 1.105, 1.06, 1.07],
        [1.07, 1.08, 1.03, 1.05],    # <- fractal LOW (1.03)
        [1.05, 1.09, 1.04, 1.08],
        [1.08, 1.11, 1.05, 1.10],
        [1.10, 1.13, 1.07, 1.09],    # <- fractal HIGH (1.13)
        [1.09, 1.10, 1.06, 1.07],
        [1.07, 1.085, 1.02, 1.01],   # <- close breaks below 1.03 -> CHoCH to BEARISH
        [1.01, 1.03, 0.98, 0.99],
        [0.99, 1.00, 0.95, 0.97],
    ]
    df = make_df(rows)
    result = s.detect_bos_impulse(df)
    assert result is not None, "expected a confirmed BEARISH leg after CHoCH, got None"
    assert result["direction"] == "BEARISH", result["direction"]
    assert result["impulse_start"] == 1.13, result["impulse_start"]
    assert result["break_count"] == 1, (
        f"break_count should reset to 1 on a fresh CHoCH, got {result['break_count']}"
    )
    assert result["was_choch"] is True, (
        "this leg flipped from a prior BULLISH dominant -- must be flagged as a CHoCH"
    )
    print("PASS: test_choch_flips_dominant_direction")


def test_reformation_after_invalidation_is_not_choch():
    # Extends the origin-invalidation scenario: after the bullish leg is
    # invalidated (dominant -> None), a FRESH leg later forms via a
    # genuine break. Even though direction differs from the invalidated
    # leg, this must NOT be flagged was_choch=True -- there was no ACTIVE
    # dominant at the moment it formed to flip from (dominant was None,
    # not BULLISH). Confirms was_choch tracks "flipped from an active
    # dominant," not "differs from whatever came before."
    rows = BASE_ROWS + [
        [1.14, 1.145, 0.90, 1.05],   # origin violated (0.90 <= 0.95) -> dominant None
        [1.05, 1.06, 0.85, 0.95],
        [1.00, 1.02, 0.80, 0.90],    # fractal LOW candidate
        [0.90, 0.95, 0.85, 0.92],
        [0.92, 1.00, 0.88, 0.98],
        [0.98, 1.10, 0.90, 1.02],    # fractal HIGH candidate
        [1.02, 1.05, 0.95, 1.00],
        [1.00, 1.03, 0.75, 0.78],    # close breaks below the low fractal -> fresh BEARISH
    ]
    df = make_df(rows)
    result = s.detect_bos_impulse(df)
    assert result is not None, "expected a confirmed BEARISH leg to reform, got None"
    assert result["direction"] == "BEARISH", result["direction"]
    assert result["was_choch"] is False, (
        "reformation after invalidation (dominant was None) must NOT count as a CHoCH"
    )
    print("PASS: test_reformation_after_invalidation_is_not_choch")


if __name__ == "__main__":
    test_clean_bullish_bos()
    test_insufficient_fractals_returns_none()
    test_origin_invalidation_resets_dominant()
    test_choch_flips_dominant_direction()
    test_reformation_after_invalidation_is_not_choch()
    print("\nAll detect_bos_impulse tests passed.")
