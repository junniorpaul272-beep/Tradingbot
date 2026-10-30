import unittest, datetime as dt
from . import overlap_check as oc, awareness as aw
from .test_awareness import series, hours, weekdays

class T(unittest.TestCase):
    def test_clean_series_has_all_windows_valid_after_warmup(self):
        r = oc.check(series(60, 0.0005)); c = r["coverage"]
        self.assertEqual((c["10"]["insufficient_history"], c["10"]["valid"]), (9, 51)); self.assertEqual(c["40"]["valid"], 21)
        self.assertEqual(r["comparability"]["windows_with_hidden_missing_weekday"], 0)

    def test_a_blocked_day_makes_only_spanning_windows_unavailable_and_creates_incomparable_pairs(self):
        rows = [x for x in series(60, 0.0005) if x["datetime"][:10] != weekdays("2026-01-05", 60)[30]]
        r = oc.check(rows); c = r["coverage"]
        self.assertEqual(c["10"]["unavailable_blocked_day"], 9);
        self.assertGreater(c["40"]["unavailable_blocked_day"], c["10"]["unavailable_blocked_day"])
        self.assertGreater(r["comparability"]["end_dates_where_windows_disagree_on_availability"], 0)

    def test_overlap_numbers(self):
        o = oc.check(series(60, 0.0005))["overlap_between_windows"]["10"]
        self.assertEqual(o["share_of_days_reused_by_next_end_date"], 0.9); self.assertEqual(o["max_non_overlapping_windows"], 6)

    def test_point_in_time_and_reproducibility(self):
        r = oc.check(series(50, 0.0004)); self.assertEqual(r["point_in_time"]["end_dates_that_changed_when_later_data_was_removed"], 0); self.assertTrue(r["reproducibility"]["identical"])

    def test_spearman(self):
        self.assertEqual(oc.spearman([1, 2, 3, 4], [2, 4, 6, 8]), 1.0); self.assertEqual(oc.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)

    def test_period_filter_drops_later_rows(self):
        rows = series(30, 0.0005);
        import tempfile, json, os
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False); [f.write(json.dumps(r) + "\n") for r in rows]; f.close()
        kept = oc.load_period(f.name, "2026-01-05", "2026-01-16"); os.unlink(f.name)
        self.assertTrue(all(r["datetime"][:10] <= "2026-01-16" for r in kept)); self.assertEqual(len(kept), 10 * 24)
if __name__ == "__main__": unittest.main()
