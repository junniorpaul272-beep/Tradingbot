import unittest
from . import compare_repair as cr
def row(day, h, p=1.3): return (f"{day}T{h:02d}:00:00+00:00", (p, p + .0005, p - .0005, p + .0001))
def build(days, last, p=1.3): return dict(row(d, h, p) for d in days for h in range(last + 1))
FR = ["2021-07-09", "2021-07-16"]
class T(unittest.TestCase):
    def test_nothing_recovered(self):
        o = build(FR, 13); r = dict(o); res = cr.compare(o, r, "2021-07-05", "2021-07-17")
        self.assertEqual((res["recovered_new_hours"], res["conflicting_overlap"], res["fridays_repaired"]), (0, 0, 0)); self.assertIn("NOTHING", res["verdict"])
    def test_recovered_and_identical_overlap(self):
        o = build(FR, 13); r = build(FR, 21); res = cr.compare(o, r, "2021-07-05", "2021-07-17")
        self.assertEqual((res["recovered_new_hours"], res["conflicting_overlap"], res["fridays_repaired"], res["identical_overlap"]), (16, 0, 2, 28))
    def test_conflict_is_reported_and_inputs_untouched(self):
        o = build(FR, 13); r = build(FR, 21); k = "2021-07-09T05:00:00+00:00"; r[k] = (1.31, 1.311, 1.309, 1.3105); before = dict(o)
        res = cr.compare(o, r, "2021-07-05", "2021-07-17"); self.assertEqual(res["conflicting_overlap"], 1); self.assertEqual(o, before)
    def test_range_limits_what_is_compared(self):
        o = build(FR, 13); r = build(FR + ["2021-12-03"], 21); self.assertEqual(cr.compare(o, r, "2021-07-05", "2021-07-17")["repair_candles"], 44)
if __name__ == "__main__": unittest.main()
