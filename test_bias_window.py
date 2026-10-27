"""Pins the 26 Oct 2026 finding: the bias leg origin (= invalidation level)
must not depend on how many old 1H candles happen to be in the window.
Uses the repo's own logged candles (ohlc_1h_log.jsonl); skipped if absent."""
import os, re, json, unittest
os.environ.setdefault("TELEGRAM_TOKEN", "x"); os.environ.setdefault("TELEGRAM_CHAT_ID", "0")
os.environ.setdefault("TWELVE_DATA_API_KEY", "x")
import pandas as pd
import scanner_common as sc

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "ohlc_1h_log.jsonl")

def _candles():
    r = [json.loads(l) for l in open(LOG) if l.strip()]
    d = pd.DataFrame(r); d["datetime"] = pd.to_datetime(d["datetime"])
    d = d.drop_duplicates("datetime").set_index("datetime").sort_index()
    d.columns = ["Open", "High", "Low", "Close"]
    return d

def _leg(df, end, n):
    b = sc.detect_bos_impulse(df[df.index <= (end if isinstance(end, pd.Timestamp) else pd.Timestamp(end, tz="UTC"))].tail(n), wing=sc.HTF_STRUCTURE_WING)
    return None if b is None else (b["direction"], round(float(b["impulse_start"]), 5))

class TestConfig(unittest.TestCase):
    def test_long_window_is_longer_than_old_window(self):
        self.assertGreaterEqual(sc.HTF_STRUCTURE_FETCH_BARS, 500)
        self.assertGreater(sc.HTF_STRUCTURE_FETCH_BARS, sc.HTF_BIAS_MIN_BARS + 20)

    def test_live_scanner_feeds_long_history_only_to_bias(self):
        s = open(os.path.join(HERE, "scanner_live.py")).read()
        for fn in ("compute_macro_bias", "compute_macro_bias_shadow_old_rule", "compute_macro_bias_min_leg_gate"):
            self.assertRegex(s, fn + r"\(df_1h_long,")
        self.assertIn("HTF_STRUCTURE_FETCH_BARS", s)
        self.assertRegex(s, r"df_1h\s*=\s*df_1h_long\.tail\(HTF_BIAS_MIN_BARS \+ 20\)")

@unittest.skipUnless(os.path.exists(LOG), "no logged candles")
class TestRealDates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = _candles()
        if cls.df.index[0] > pd.Timestamp("2026-09-21", tz="UTC"):
            raise unittest.SkipTest("log does not reach September")

    def test_bullish_origin_stable_19_to_23_oct_with_long_window(self):
        seen = {_leg(self.df, e, sc.HTF_STRUCTURE_FETCH_BARS)
                for e in self.df.index[(self.df.index >= pd.Timestamp("2026-10-19", tz="UTC")) &
                                       (self.df.index <= pd.Timestamp("2026-10-23 10:00", tz="UTC"))]}
        self.assertEqual(seen, {("BULLISH", 1.31382)})

    def test_old_window_was_unstable_same_dates(self):
        seen = {_leg(self.df, e, 120)
                for e in self.df.index[(self.df.index >= pd.Timestamp("2026-10-19", tz="UTC")) &
                                       (self.df.index <= pd.Timestamp("2026-10-23 10:00", tz="UTC"))]}
        self.assertGreater(len(seen), 1)  # documents the defect being fixed

    def test_sept_22_24_no_flipflop_with_long_window(self):
        seen = {_leg(self.df, e, sc.HTF_STRUCTURE_FETCH_BARS)
                for e in self.df.index[(self.df.index >= pd.Timestamp("2026-09-21 15:00", tz="UTC")) &
                                       (self.df.index <= pd.Timestamp("2026-09-25 16:00", tz="UTC"))]}
        self.assertEqual(len(seen), 1)

    def test_real_structure_break_still_detected_23_oct(self):
        self.assertEqual(_leg(self.df, "2026-10-23 11:00", sc.HTF_STRUCTURE_FETCH_BARS)[0], "BEARISH")

if __name__ == "__main__":
    unittest.main()
