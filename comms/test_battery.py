"""Runs the break-it battery (battery.py) and fails on any case that does not pass, naming it."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import battery


class Run(unittest.TestCase):
    def test_every_case_passes(self):
        s = unittest.TestLoader().loadTestsFromTestCase(battery.Battery)
        unittest.TextTestRunner(stream=open(os.devnull, "w")).run(s)
        bad = [(c, m, last[:160]) for c, m, ok, last in battery.Battery.results if not ok]
        self.assertTrue(battery.Battery.results)
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()


class Frozen(unittest.TestCase):
    """The frozen set against REAL saved scans (tests_data_live/): see battery_frozen.py for how it was written and what was corrected."""
    def test_every_frozen_case_passes(self):
        import battery_frozen
        unittest.TextTestRunner(stream=open(os.devnull, "w")).run(unittest.TestLoader().loadTestsFromTestCase(battery_frozen.Frozen))
        bad = [(c, m, last[:160]) for c, m, ok, last in battery_frozen.Frozen.results if not ok]
        self.assertTrue(battery_frozen.Frozen.results)
        self.assertEqual(bad, [])


class LiveAudit(unittest.TestCase):
    """The independent audit of real sessions (19 and 20 Oct): events, timing, scan gaps, zone first-seen, historical FVG state."""
    def test_audit_passes_on_the_real_fixture(self):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = subprocess.run([sys.executable, os.path.join(root, "audit_live_session.py"), os.path.join(root, "tests_data_live"), "2026-10-19", "2026-10-20"],
                           capture_output=True, text=True, timeout=240)
        self.assertEqual(p.returncode, 0, p.stdout[-1500:] + p.stderr[-500:])


class Regression(unittest.TestCase):
    """The friend's 22 Oct review cases (back-to-back events, reruns, FVG edge cases, the four sample conversations) on real saved scans."""
    def test_every_regression_case_passes(self):
        import battery_regression
        unittest.TextTestRunner(stream=open(os.devnull, "w")).run(unittest.TestLoader().loadTestsFromName("Regression.test_run", battery_regression))
        res = battery_regression.Regression.results
        self.assertTrue(res)
        self.assertEqual([(c, m, last[:160]) for c, m, ok, last in res if not ok], [])


class ConsistencyAudit(unittest.TestCase):
    """Every cross-file rule in the real logs: record findings are reported, reader failures fail the run."""
    def test_readers_pass_on_the_real_fixture(self):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = subprocess.run([sys.executable, os.path.join(root, "audit_consistency.py"), os.path.join(root, "tests_data_live")],
                           capture_output=True, text=True, timeout=240)
        self.assertEqual(p.returncode, 0, p.stdout[-1500:] + p.stderr[-500:])


class HardRound(unittest.TestCase):
    """The hard round (stress_hard.py): exhaustive per-scan checks, fuzzing, damaged records, chains, injections. Zero WRONG / LEAK / CRASH allowed."""
    def test_no_wrong_leak_or_crash(self):
        import stress_hard as sh
        os.environ["ONLY"], os.environ["NC"], os.environ["NE"] = "ABCDEFGHI", "150", "40"
        unittest.TextTestRunner(stream=open(os.devnull, "w")).run(unittest.TestLoader().loadTestsFromName("Hard.test_run", sh))
        bad = [(s, l[:100], v, d[:140]) for s, l, v, d in sh.Hard.R if v in ("WRONG", "LEAK", "CRASH")]
        self.assertTrue(len(sh.Hard.R) > 500)
        self.assertEqual(bad, [])
