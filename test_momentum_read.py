"""Run: python test_momentum_read.py — the momentum.state calculation on SYNTHETIC data only (no history has been
looked at). Pins the exact definitions, mirror symmetry, scale/shift invariance, determinism, no look-ahead and every
UNKNOWN reason. Passing means the measure is correct and stable; it says nothing about whether it is useful."""
import math, os, sys, unittest
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import momentum_read as mr

U = timezone.utc
MON = datetime(2026, 10, 5, 0, 0, tzinfo=U)          # Monday 00:00 UTC; the FX week is open (closure ends Sun 21:00 UTC in October)
STEP = timedelta(minutes=15)


def mk(closes, start=MON, rng=0.0004, wick=None):
    """Candles whose closes are given; open = previous close; each bar extends `rng` beyond open/close on both sides."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi, lo = max(o, c) + (rng if wick is None else wick[i]), min(o, c) - (rng if wick is None else wick[i])
        out.append({"t": start + i * STEP, "o": o, "h": hi, "l": lo, "c": c}); prev = c
    return out


def end_of(cs):
    return cs[-1]["t"] + STEP


def line(n, step, base=1.3000):
    return [base + i * step for i in range(n)]


def get(cs, now=None):
    return mr.compute(cs, now or end_of(cs))


class Definitions(unittest.TestCase):
    def test_a_straight_rise_is_up_with_efficiency_one(self):
        r = get(mk(line(60, 0.0003)))
        self.assertEqual(r["direction"]["provisional_label"], "UP")
        self.assertAlmostEqual(r["persistence"]["value"], 1.0)
        self.assertGreater(r["direction"]["value"], 0)

    def test_the_numbers_are_exactly_the_documented_formula(self):
        cs = mk(line(60, 0.0003), rng=0.0004)
        r = get(cs)
        # every TR in this series is the same, so ATR = TR of the last bar
        tr = max((cs[-1]["h"] - cs[-1]["l"]), abs(cs[-1]["h"] - cs[-2]["c"]), abs(cs[-1]["l"] - cs[-2]["c"]))
        d = (cs[-1]["c"] - cs[-1 - 16]["c"]) / tr
        self.assertAlmostEqual(r["direction"]["value"], d, places=9)
        self.assertAlmostEqual(r["strength"]["value"], abs(d), places=9)

    def test_a_straight_fall_mirrors_a_straight_rise(self):
        up, dn = get(mk(line(60, 0.0003))), get(mk(line(60, -0.0003, base=1.31)))
        self.assertEqual(dn["direction"]["provisional_label"], "DOWN")
        self.assertAlmostEqual(dn["direction"]["value"], -up["direction"]["value"], places=9)
        self.assertAlmostEqual(dn["persistence"]["value"], up["persistence"]["value"], places=9)

    def test_reflecting_a_noisy_series_flips_only_the_sign_of_direction(self):
        import random
        rnd = random.Random(7); c = [1.30]
        for _ in range(79): c.append(c[-1] + rnd.gauss(0.0001, 0.0006))
        a = get(mk(c))
        mirror = [2 * 1.30 - x for x in c]
        b = get(mk(mirror))
        self.assertAlmostEqual(b["direction"]["value"], -a["direction"]["value"], places=9)
        self.assertAlmostEqual(b["strength"]["value"], a["strength"]["value"], places=9)
        self.assertAlmostEqual(b["persistence"]["value"], a["persistence"]["value"], places=9)
        self.assertAlmostEqual(b["expansion"]["value"], a["expansion"]["value"], places=9)

    def test_scale_and_shift_invariance(self):
        import random
        rnd = random.Random(3); c = [1.30]
        for _ in range(79): c.append(c[-1] + rnd.gauss(0, 0.0006))
        a = get(mk(c))
        s = get([{**x, **{k: x[k] * 3 + 0.7 for k in "ohlc"}} for x in mk(c)])
        for k in ("direction", "strength", "persistence", "expansion"):
            self.assertAlmostEqual(a[k]["value"], s[k]["value"], places=9, msg=k)

    def test_a_round_trip_has_near_zero_efficiency_and_is_neutral(self):
        c = [1.30 + (0.0010 if i % 2 else 0.0) for i in range(60)]       # saw-tooth: 16 steps, ends where it started
        r = get(mk(c))
        self.assertAlmostEqual(r["persistence"]["value"], 0.0)
        self.assertEqual(r["direction"]["provisional_label"], "NEUTRAL")

    def test_expansion_windows_are_exact_and_do_not_overlap(self):
        wick = [0.0002] * 60
        for i in (56, 57, 58, 59): wick[i] = 0.0010                       # the last 4 bars are wide, the 48 before are quiet
        r = get(mk(line(60, 0.0, 1.30), wick=wick))
        cs = mk(line(60, 0.0, 1.30), wick=wick)
        tr = mr._tr(cs[-53:])
        self.assertEqual(len(tr), 52)
        self.assertAlmostEqual(r["expansion"]["value"], (sum(tr[48:]) / 4) / (sum(tr[:48]) / 48), places=9)
        self.assertEqual(r["expansion"]["provisional_label"], "EXPANDED")
        wick2 = [0.0010] * 56 + [0.0002] * 4
        self.assertEqual(get(mk(line(60, 0.0, 1.30), wick=wick2))["expansion"]["provisional_label"], "CONTRACTED")

    def test_the_bar_before_the_baseline_is_not_used(self):
        wick = [0.0002] * 60; wick[6] = 0.0100                            # bar t-53: outside every window
        cs = mk(line(60, 0.0, 1.30), wick=wick)
        spiked, plain = get(cs), get(mk(line(60, 0.0, 1.30), wick=[0.0002] * 60))
        self.assertAlmostEqual(spiked["expansion"]["value"], plain["expansion"]["value"], places=9)   # the spike is outside bars[-53:]

    def test_direction_and_strength_come_from_one_number(self):
        r = get(mk(line(60, 0.0006)))
        self.assertEqual(abs(r["direction"]["value"]), r["strength"]["value"])

    def test_persistence_is_raw_only_and_has_no_label_even_with_frozen_thresholds(self):
        r = get(mk(line(60, 0.0003)))
        self.assertIsNone(r["persistence"]["label"]); self.assertIsNone(r["persistence"]["provisional_label"])


class Labels(unittest.TestCase):
    def test_labels_are_withheld_until_thresholds_are_frozen(self):
        r = get(mk(line(60, 0.0006)))
        self.assertFalse(r["labels_frozen"])
        for k in ("direction", "strength", "expansion"):
            self.assertIsNone(r[k]["label"], k)
        self.assertEqual(r["direction"]["provisional_label"], "UP")

    def test_frozen_thresholds_release_the_label(self):
        old = dict(mr.THRESHOLDS)
        try:
            mr.THRESHOLDS["frozen"] = True
            r = get(mk(line(60, 0.0006)))
            self.assertEqual(r["direction"]["label"], "UP"); self.assertTrue(r["labels_frozen"])
        finally:
            mr.THRESHOLDS.clear(); mr.THRESHOLDS.update(old)


class DataQuality(unittest.TestCase):
    def reasons(self, r):
        return {k: r[k]["reason"] for k in ("direction", "strength", "persistence", "expansion")}

    def test_too_few_bars_is_unknown_per_reading(self):
        r = get(mk(line(30, 0.0003)))                                      # enough for direction/persistence (17), not expansion (53)
        self.assertEqual(r["direction"]["status"], "OK"); self.assertEqual(r["expansion"]["reason"], "insufficient_bars")
        r = get(mk(line(10, 0.0003)))
        self.assertEqual(set(self.reasons(r).values()), {"insufficient_bars"})
        self.assertEqual(get(mk(line(17, 0.0003)))["direction"]["status"], "OK")
        self.assertEqual(get(mk(line(16, 0.0003)))["direction"]["reason"], "insufficient_bars")
        self.assertEqual(get(mk(line(53, 0.0003)))["expansion"]["status"], "OK")
        self.assertEqual(get(mk(line(52, 0.0003)))["expansion"]["reason"], "insufficient_bars")

    def test_no_candles_and_bad_clock(self):
        self.assertEqual(set(self.reasons(mr.compute([], MON)).values()), {"no_candles"})
        self.assertEqual(set(self.reasons(mr.compute(mk(line(60, 0.0003)), datetime(2026, 10, 5, 20, 0))).values()), {"bad_input"})

    def test_the_forming_candle_is_never_used(self):
        cs = mk(line(60, 0.0003))
        now = cs[-1]["t"] + timedelta(minutes=7)                           # the last candle's interval has not ended
        r = mr.compute(cs, now)
        self.assertTrue(r["forming_bar_excluded"]); self.assertEqual(r["bars_used"], 59)
        self.assertEqual(r["asof_candle_open"], cs[-2]["t"].isoformat())
        same = mr.compute(cs[:-1], cs[-2]["t"] + STEP)
        self.assertEqual(r["direction"], same["direction"])

    def test_stale_feed_when_open_and_closed_market_when_closed(self):
        cs = mk(line(60, 0.0003))
        self.assertEqual(set(self.reasons(mr.compute(cs, end_of(cs) + timedelta(minutes=30))).values()), {None})        # exactly 2 intervals late: ok
        self.assertEqual(set(self.reasons(mr.compute(cs, end_of(cs) + timedelta(minutes=31))).values()), {"stale_feed"})
        fri = datetime(2026, 10, 9, 20, 0, tzinfo=U)                        # Friday 20:00 UTC = 16:00 New York: open
        csf = mk(line(60, 0.0003), start=fri - 60 * STEP)
        self.assertEqual(mr.compute(csf, fri)["direction"]["status"], "OK")
        sat = datetime(2026, 10, 10, 12, 0, tzinfo=U)
        self.assertEqual(set(self.reasons(mr.compute(csf, sat)).values()), {"market_closed"})

    def test_an_interior_gap_in_open_hours_is_missing_bar_for_the_readings_it_touches(self):
        cs = mk(line(60, 0.0003)); del cs[40]
        r = get(cs)
        self.assertEqual(r["expansion"]["reason"], "missing_bar")
        self.assertEqual(r["direction"]["status"], "OK")                    # the gap is older than the direction window
        cs = mk(line(60, 0.0003)); del cs[50]
        r = get(cs)
        self.assertEqual({r[k]["reason"] for k in ("direction", "strength", "persistence", "expansion")}, {"missing_bar"})

    def test_a_weekend_inside_a_window_is_spans_market_closure_not_missing_bar(self):
        # 8 bars before Friday close + 60 bars after Sunday open: a 53-bar expansion window crosses the weekend
        fri_close = datetime(2026, 10, 9, 21, 0, tzinfo=U)                  # 17:00 New York (EDT)
        before = mk(line(30, 0.0003), start=fri_close - 30 * STEP)
        sun_open = datetime(2026, 10, 11, 21, 0, tzinfo=U)
        after = mk(line(30, 0.0003, base=1.31), start=sun_open)
        r = get(before + after)
        self.assertEqual(r["expansion"]["reason"], "spans_market_closure")
        self.assertEqual(r["direction"]["status"], "OK")                    # 30 bars after the open: its own window is clean
        r = get(before + after[:10])
        self.assertEqual(r["direction"]["reason"], "spans_market_closure")

    def test_a_gap_that_is_not_the_weekend_is_never_excused_as_a_closure(self):
        midweek = mk(line(60, 0.0003), start=datetime(2026, 10, 6, 0, 0, tzinfo=U)); del midweek[45]     # a Tuesday
        self.assertEqual(get(midweek)["expansion"]["reason"], "missing_bar")

    def test_zero_denominators_are_unknown_not_zero(self):
        flat = mk([1.30] * 60, rng=0.0)
        r = get(flat)
        self.assertEqual(r["direction"]["reason"], "zero_atr"); self.assertEqual(r["persistence"]["reason"], "zero_path")
        self.assertEqual(r["expansion"]["reason"], "zero_baseline")
        for k in ("direction", "strength", "persistence", "expansion"):
            self.assertIsNone(r[k]["value"])

    def test_a_flat_series_with_wicks_is_neutral_and_persistence_unknown(self):
        r = get(mk([1.30] * 60, rng=0.0003))
        self.assertEqual(r["direction"]["provisional_label"], "NEUTRAL"); self.assertEqual(r["persistence"]["reason"], "zero_path")

    def test_bad_candles_are_reported_not_guessed(self):
        base = mk(line(60, 0.0003))
        cases = {
            "naive": [dict(base[0], t=datetime(2026, 10, 5, 0, 0))] + base[1:],
            "misaligned": [dict(b, t=b["t"] + timedelta(minutes=3)) for b in base],
            "duplicate": base[:5] + [base[4]] + base[5:],
            "reversed": list(reversed(base)),
            "nan": base[:30] + [dict(base[30], c=float("nan"))] + base[31:],
            "high_below_low": base[:30] + [dict(base[30], h=base[30]["l"] - 0.001)] + base[31:],
            "missing_key": base[:30] + [{"t": base[30]["t"], "o": 1.3}] + base[31:],
        }
        for name, cs in cases.items():
            r = mr.compute(cs, end_of(base))
            self.assertEqual({r[k]["reason"] for k in ("direction", "strength", "persistence", "expansion")}, {"bad_input"}, name)


class Reproducibility(unittest.TestCase):
    def test_deterministic(self):
        cs = mk(line(80, 0.0002))
        self.assertEqual(get(cs), get(cs))

    def test_no_look_ahead_future_bars_never_change_a_reading_already_made(self):
        import random
        rnd = random.Random(11); c = [1.30]
        for _ in range(119): c.append(c[-1] + rnd.gauss(0, 0.0005))
        cs = mk(c)
        for k in (55, 70, 90, 110):
            now = cs[k]["t"] + STEP
            a, b = mr.compute(cs[:k + 1], now), mr.compute(cs, now)
            self.assertTrue(b["forming_bar_excluded"] and not a["forming_bar_excluded"])      # the only difference: later bars were dropped
            for key in ("direction", "strength", "persistence", "expansion", "bars_used", "asof_candle_open"):
                self.assertEqual(a[key], b[key], (k, key))

    def test_the_record_says_what_produced_it(self):
        r = get(mk(line(60, 0.0003)))
        for k, v in (("version", "momentum_v0"), ("timeframe", "15m"), ("lookback_bars", 16), ("atr_period", 14),
                     ("expansion_recent_bars", 4), ("expansion_baseline_bars", 48), ("threshold_version", "v0-unfrozen")):
            self.assertEqual(r[k], v, k)
        self.assertEqual(r["asof_candle_close"], end_of(mk(line(60, 0.0003))).isoformat())


class Boundary(unittest.TestCase):
    def test_pure_stdlib_and_no_io(self):
        import ast
        with open(os.path.join(HERE, "momentum_read.py")) as f:
            src = f.read()
        mods = set()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Import): mods |= {a.name.split(".")[0] for a in n.names}
            if isinstance(n, ast.ImportFrom) and n.module: mods.add(n.module.split(".")[0])
        self.assertTrue(mods <= {"math", "datetime", "zoneinfo"}, mods)
        for banned in ("open(", "requests", "os.environ", "datetime.now", "datetime.utcnow", "print("):
            self.assertNotIn(banned, src.replace("# ", ""), banned)

    def test_the_live_loop_only_writes_it_and_no_engine_module_reads_it(self):
        live = os.path.join(HERE, "scanner_live.py")
        if not os.path.exists(live):
            self.skipTest("scanner_live.py not alongside")
        import re
        with open(live) as f:
            src = f.read()
        uses = [m.start() for m in re.finditer(r"momentum_state", src)]
        self.assertEqual(len(uses), 1, "scanner_live may only WRITE momentum_state (one apply_state_updates)")
        i = src.index("_mom = momentum_read.compute(")
        self.assertIn("apply_state_updates(state, {\"momentum_state\"", src[i:i + 400])
        self.assertLess(i, src.index("compute_macro_bias(df_1h_long, df_15m, state)"), "must run before the early returns")
        self.assertIn("except Exception as e:\n        print(\"[MOMENTUM ERROR]", src)                     # a failure cannot stop a scan
        for name in ("scanner_common", "scanner_observation", "min_scanner", "mil", "worldstate", "market_read", "brain", "leg_age"):
            p = os.path.join(HERE, name + ".py")
            if os.path.exists(p):
                with open(p) as f:
                    self.assertNotIn("momentum_state", f.read(), name)


if __name__ == "__main__":
    unittest.main()
