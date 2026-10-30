import unittest, datetime as dt, random
from . import awareness as aw

def hours(day, price, rng=0.0010, n=24, drift=0.0):
    """n hourly candles for a date, price walking from `price` by `drift` per hour."""
    out = []; p = price
    for h in range(n):
        o = p; c = p + drift; hi = max(o, c) + rng / 2; lo = min(o, c) - rng / 2
        out.append({"datetime": f"{day}T{h:02d}:00:00+00:00", "open": o, "high": hi, "low": lo, "close": c}); p = c
    return out

def weekdays(start, n):
    d = dt.date.fromisoformat(start); out = []
    while len(out) < n:
        if d.weekday() < 5: out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out

def series(n, daily_move, start="2026-01-05", price=1.3000):
    rows = []; p = price
    for day in weekdays(start, n):
        rows += hours(day, p, drift=daily_move / 24); p += daily_move
    return rows

class Measures(unittest.TestCase):
    def test_a_straight_line_has_full_efficiency_and_every_day_up(self):
        led, _ = aw.build_ledger(series(45, 0.0010))
        m = led[-1]["measurements"]["20"]
        self.assertEqual(m["path_efficiency"], 1.0); self.assertEqual(m["up_day_share"], 1.0)
        self.assertEqual(m["net_pips"], round(19 * 0.0010 / aw.PIP, 1))

    def test_a_zigzag_makes_no_progress(self):
        rows = []; p = 1.3
        for i, day in enumerate(weekdays("2026-01-05", 45)):
            mv = 0.0050 if i % 2 == 0 else -0.0050
            rows += hours(day, p, drift=mv / 24); p += mv
        led, _ = aw.build_ledger(rows)
        self.assertLess(led[-1]["measurements"]["20"]["path_efficiency"], 0.1)

    def test_range_position_is_between_zero_and_one(self):
        random.seed(3); rows = []; p = 1.3
        for day in weekdays("2026-01-05", 60):
            mv = random.uniform(-0.004, 0.004); rows += hours(day, p, drift=mv / 24); p += mv
        led, _ = aw.build_ledger(rows)
        for o in led:
            for m in o["measurements"].values():
                if m and m["range_position"] is not None: self.assertTrue(0 <= m["range_position"] <= 1)

class PointInTime(unittest.TestCase):
    def test_later_candles_never_change_an_earlier_observation(self):
        rows = series(60, 0.0007)
        full, _ = aw.build_ledger(rows)
        for cut in (25, 42, 55):
            cut_date = weekdays("2026-01-05", 60)[cut - 1]
            part, _ = aw.build_ledger([r for r in rows if r["datetime"][:10] <= cut_date])
            self.assertEqual(part, full[:len(part)], f"observation changed when later data arrived (cut at day {cut})")

    def test_rebuilding_gives_identical_output(self):
        rows = series(50, -0.0004)
        self.assertEqual(aw.build_ledger(rows), aw.build_ledger(list(rows)))

    def test_not_enough_history_is_none_not_a_made_up_number(self):
        led, _ = aw.build_ledger(series(15, 0.001))
        self.assertIsNone(led[-1]["measurements"]["40"]); self.assertIsNone(led[-1]["measurements"]["20"]); self.assertIsNotNone(led[-1]["measurements"]["10"])

    def test_no_label_is_invented(self):
        led, _ = aw.build_ledger(series(45, 0.001))
        self.assertTrue(all(o["classification"] == "NOT_DEFINED_YET" for o in led))

class DataQuality(unittest.TestCase):
    def test_duplicates_are_collapsed_and_counted(self):
        rows = series(12, 0.001); rows = rows + rows[:30]
        _, rep = aw.build_ledger(rows); self.assertEqual(rep["duplicates"], 30)

    def test_duplicates_with_different_prices_are_flagged(self):
        rows = series(12, 0.001); changed = dict(rows[40]); changed["close"] = changed["close"] + 0.0001; changed["high"] += 0.0002
        _, rep = aw.build_ledger(rows + [changed]); self.assertEqual((rep["duplicates"], rep["conflicting_duplicates"]), (1, 1))

    def test_malformed_rows_are_dropped_and_counted(self):
        rows = series(12, 0.001)
        rows[5]["high"] = rows[5]["low"] - 0.001          # high below low
        rows[6]["close"] = "abc"; rows[7]["open"] = float("nan"); rows[8]["close"] = -1; del rows[9]["low"]; rows[10]["datetime"] = "garbage"
        _, rep = aw.build_ledger(rows); self.assertEqual(rep["malformed"], 6)

    def test_weekend_filler_is_dropped_and_counted(self):
        rows = series(10, 0.001) + hours("2026-01-17", 1.31, n=24) + hours("2026-01-18", 1.31, n=24)    # a Saturday and a Sunday
        _, rep = aw.build_ledger(rows); self.assertEqual(rep["weekend_dropped"], 48)

    def test_a_missing_weekday_is_disclosed_not_filled(self):
        rows = [r for r in series(14, 0.001) if r["datetime"][:10] != weekdays("2026-01-05", 14)[6]]
        led, rep = aw.build_ledger(rows)
        self.assertEqual(rep["gaps"], [weekdays("2026-01-05", 14)[6]]); self.assertEqual(rep["trading_days"], 13)

    def test_a_thin_day_is_not_counted_as_a_trading_day(self):
        rows = series(12, 0.001); day = weekdays("2026-01-05", 12)[4]
        rows = [r for r in rows if not (r["datetime"][:10] == day and int(r["datetime"][11:13]) >= 5)]   # only 5 candles that day
        _, rep = aw.build_ledger(rows); self.assertIn(day, rep["gaps"])

class Integrity(unittest.TestCase):
    """Historical-integrity cases from the friend's review."""
    def base(self):
        return series(50, 0.0007)

    def test_conflicting_duplicate_for_an_old_candle_is_not_silently_resolved(self):
        rows = self.base(); led0, _ = aw.build_ledger(rows)
        old = dict(rows[100]); old["close"] += 0.0005; old["high"] += 0.0006
        led1, rep = aw.build_ledger(rows + [old])
        day = old["datetime"][:10]
        self.assertEqual(rep["conflicted_days"], [day]); self.assertEqual(len(rep["conflicts"]), 1)
        self.assertEqual(len(rep["conflicts"][0]["versions"]), 2)           # both versions kept as evidence
        self.assertIn(day, rep["blocked_days"])
        before = {o["as_of"]: o for o in led0}
        for o in led1:
            if o["as_of"] < day:
                self.assertEqual(o, before[o["as_of"]])                    # nothing before the conflict changes
            for w, m in o["measurements"].items():                          # nothing after it silently uses either version
                if m and "unavailable" not in m and o["as_of"] >= day:
                    self.assertFalse(m["from"] < day < m["to"])
        later = [o for o in led1 if o["as_of"] > day and o["measurements"]["10"] and "unavailable" in o["measurements"]["10"]]
        self.assertTrue(later)                                              # affected windows are marked unavailable, with a reason
        self.assertIn("conflicting", later[0]["measurements"]["10"]["unavailable"])

    def test_conflict_result_does_not_depend_on_which_copy_arrives_first(self):
        rows = self.base(); old = dict(rows[100]); old["close"] += 0.0005; old["high"] += 0.0006
        self.assertEqual(aw.build_ledger(rows + [old]), aw.build_ledger([old] + rows))

    def test_input_order_does_not_matter(self):
        rows = self.base(); sh = list(rows); random.Random(3).shuffle(sh)
        self.assertEqual(aw.build_ledger(rows), aw.build_ledger(sh))

    def test_identical_duplicate_changes_nothing_but_the_count(self):
        rows = self.base(); led0, r0 = aw.build_ledger(rows); led1, r1 = aw.build_ledger(rows + [rows[300]])
        self.assertEqual(led0, led1); self.assertEqual(r1["duplicates"], 1); self.assertEqual(r1["conflicting_duplicates"], 0)

    def test_same_instant_in_different_notations_is_one_candle(self):
        rows = self.base(); z = dict(rows[200]); z["datetime"] = z["datetime"].replace("+00:00", "Z")
        n = dict(rows[201]); n["datetime"] = n["datetime"].replace("+00:00", "")             # no offset: taken as UTC
        led1, rep = aw.build_ledger(rows + [z, n])
        self.assertEqual((rep["duplicates"], rep["conflicting_duplicates"]), (2, 0)); self.assertEqual(led1, aw.build_ledger(rows)[0])

    def test_offset_timestamps_are_converted_to_utc_before_grouping(self):
        # 00:30+01:00 on Tuesday 6 Jan is 23:30Z Monday 5 Jan -> must land on the 5th, not the 6th
        self.assertEqual(aw.canon_ts("2026-01-06T00:30:00+01:00"), "2026-01-05T23:30:00+00:00")
        rows = []
        for r in series(3, 0.001):
            t = aw.canon_ts(r["datetime"]); import datetime as d
            local = d.datetime.fromisoformat(t).astimezone(d.timezone(d.timedelta(hours=1))).isoformat()
            rows.append(dict(r, datetime=local))
        self.assertEqual(aw.build_ledger(rows)[0], aw.build_ledger(series(3, 0.001))[0])

    def test_offset_conversion_moves_weekday_filtering_too(self):
        # Sat 00:30 +01:00 is Friday 23:30Z -> a trading candle; Mon 00:30+01:00 is Sunday 23:30Z -> weekend
        _, rep = aw.clean_hourly([{"datetime": "2026-01-10T00:30:00+01:00", "open": 1.3, "high": 1.31, "low": 1.29, "close": 1.3},
                                  {"datetime": "2026-01-12T00:30:00+01:00", "open": 1.3, "high": 1.31, "low": 1.29, "close": 1.3}])
        self.assertEqual((rep["kept"], rep["weekend_dropped"]), (1, 1))

    def test_an_incomplete_day_is_not_used_and_windows_spanning_it_are_unavailable(self):
        rows = series(30, 0.001); day = weekdays("2026-01-05", 30)[14]
        rows = [r for r in rows if not (r["datetime"][:10] == day and int(r["datetime"][11:13]) >= 12)]   # 12 of 24 bars
        led, rep = aw.build_ledger(rows)
        self.assertEqual(rep["blocked_days"][day], "incomplete (12 bars)")
        spanning = [o for o in led if o["measurements"]["10"] and "unavailable" in o["measurements"]["10"]]
        self.assertTrue(spanning)
        for o in led:
            m = o["measurements"]["10"]
            if m and "unavailable" not in m: self.assertFalse(m["from"] < day < m["to"])

    def test_friday_may_close_early_but_not_before_the_weekly_close_window(self):
        fri = weekdays("2026-01-05", 30)[4]
        def cut(h):
            return [r for r in series(30, 0.001) if not (r["datetime"][:10] == fri and int(r["datetime"][11:13]) > h)]
        for h in (19, 20, 21, 22):                       # ordinary weekly closes seen in the audit
            self.assertNotIn(fri, aw.build_ledger(cut(h))[1]["blocked_days"], h)
        self.assertIn(fri, aw.build_ledger(cut(13))[1]["blocked_days"])      # 2021-style truncated Friday
        self.assertIn(fri, aw.build_ledger([r for r in cut(21) if int(r["datetime"][11:13]) != 9 or r["datetime"][:10] != fri])[1]["blocked_days"])   # a hole

    def test_a_short_monday_to_thursday_is_not_complete_even_if_it_has_20_hours(self):
        thu = weekdays("2026-01-05", 30)[3]
        rows = [r for r in series(30, 0.001) if not (r["datetime"][:10] == thu and int(r["datetime"][11:13]) > 19)]
        self.assertIn(thu, aw.build_ledger(rows)[1]["blocked_days"])

    def test_partial_first_and_last_days_are_excluded_and_not_called_gaps(self):
        rows = series(14, 0.001); first, last = weekdays("2026-01-05", 14)[0], weekdays("2026-01-05", 14)[-1]
        rows = [r for r in rows if not (r["datetime"][:10] == first and int(r["datetime"][11:13]) < 7)
                and not (r["datetime"][:10] == last and int(r["datetime"][11:13]) > 14)]
        led, rep = aw.build_ledger(rows)
        self.assertEqual((rep["first"], rep["last"], rep["trading_days"]), (weekdays("2026-01-05", 14)[1], weekdays("2026-01-05", 14)[-2], 12))
        self.assertEqual(rep["blocked_days"], {})

    def test_each_record_keeps_the_definition_version_it_was_built_under(self):
        days, blocked = aw.to_days(aw.clean_hourly(series(15, 0.001))[0])
        a = aw.observation(days, 12, blocked=blocked, version="awareness-v0.1-provisional")
        b = aw.observation(days, 12, blocked=blocked, version="awareness-v0.2-test")
        self.assertEqual((a["definition_version"], b["definition_version"]), ("awareness-v0.1-provisional", "awareness-v0.2-test"))
        self.assertEqual(a["measurements"], b["measurements"])
        self.assertEqual(aw.build_ledger(series(15, 0.001))[0][12]["definition_version"], aw.DEFINITION_VERSION)

class ChartLegend(unittest.TestCase):
    def test_change_body_and_first_candle(self):
        c, _ = aw.clean_hourly([{"datetime": "2026-01-05T00:00:00+00:00", "open": 1.3000, "high": 1.3020, "low": 1.2990, "close": 1.3010},
                                {"datetime": "2026-01-05T01:00:00+00:00", "open": 1.3012, "high": 1.3040, "low": 1.3005, "close": 1.3030}])
        e = aw.enrich(c)
        self.assertIsNone(e[0]["change_pips"]); self.assertEqual(e[0]["body_pips"], 10.0)
        self.assertEqual(e[1]["change_pips"], 20.0); self.assertEqual(e[1]["body_pips"], 18.0); self.assertEqual(e[1]["gap_hours"], 1.0)
        self.assertAlmostEqual(e[1]["change_pct"], (1.3030 / 1.3010 - 1) * 100, places=4)

    def test_ema_matches_pandas_and_is_marked_warm_only_after_3_spans(self):
        import pandas as pd
        c, _ = aw.clean_hourly(series(40, 0.0003, price=1.30)[:600]); e = aw.enrich(c, (20,))
        ref = pd.Series([x[4] for x in c]).ewm(span=20, adjust=False).mean()
        self.assertAlmostEqual(e[-1]["ema_20"], ref.iloc[-1], places=5)
        self.assertFalse(e[59]["ema_20_warm"]); self.assertTrue(e[60]["ema_20_warm"])

    def test_a_later_candle_never_changes_an_earlier_row(self):
        c, _ = aw.clean_hourly(series(30, 0.0004)); a = aw.enrich(c[:300]); b = aw.enrich(c)
        self.assertEqual(a, b[:300])

    def test_gap_is_reported_not_bridged(self):
        c, _ = aw.clean_hourly(series(8, 0.0004)); c = c[:10] + c[15:]
        self.assertEqual(aw.enrich(c)[10]["gap_hours"], 6.0)

if __name__ == "__main__":
    unittest.main()
