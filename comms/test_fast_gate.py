import io, json, os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fast_gate as g

NOW = datetime(2026, 10, 19, 12, 5, 40, tzinfo=timezone.utc)


def stats(minutes_ago):
    return {"last_scan": (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M"), "last_update_id": 41}


class Resp:
    def __init__(self, obj): self.b = json.dumps(obj).encode()
    def read(self): return self.b
    def __enter__(self): return self
    def __exit__(self, *a): return False


class Decide(unittest.TestCase):
    def test_quiet_minutes_do_not_run_the_scan(self):
        for ago in (0, 1, 2, 3, 4):
            self.assertFalse(g.decide(stats(ago), NOW, False)[0], ago)

    def test_the_regular_scan_still_happens_every_five_minutes(self):
        self.assertTrue(g.decide(stats(5), NOW, False)[0]); self.assertTrue(g.decide(stats(9), NOW, False)[0])

    def test_a_waiting_message_runs_it_but_not_back_to_back(self):
        self.assertTrue(g.decide(stats(2), NOW, True)[0]); self.assertTrue(g.decide(stats(4), NOW, True)[0])
        self.assertFalse(g.decide(stats(0), NOW, True)[0]); self.assertFalse(g.decide(stats(1), NOW, True)[0])

    def test_when_unsure_it_runs_so_the_scanner_is_never_blocked(self):
        self.assertTrue(g.decide({}, NOW, False)[0]); self.assertTrue(g.decide({"last_scan": "garbage"}, NOW, False)[0])
        self.assertTrue(g.decide(stats(2), NOW, None)[0])
        self.assertTrue(g.decide({"last_scan": (NOW + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")}, NOW, False)[0])

    def test_every_decision_says_why(self):
        for p in (True, False, None):
            self.assertTrue(g.decide(stats(3), NOW, p)[1])


class Peek(unittest.TestCase):
    def test_reads_waiting_or_not_and_uses_the_saved_offset_without_confirming(self):
        seen = []
        def op(url, timeout=0): seen.append(url); return Resp({"ok": True, "result": [{"update_id": 42}]})
        self.assertTrue(g.peek("T", 41, op)); self.assertIn("offset=42", seen[0]); self.assertIn("limit=1", seen[0]); self.assertIn("timeout=0", seen[0])
        self.assertFalse(g.peek("T", 41, lambda u, timeout=0: Resp({"ok": True, "result": []})))

    def test_failures_are_unknown_not_false(self):
        def boom(u, timeout=0): raise OSError("net")
        self.assertIsNone(g.peek("T", 1, boom)); self.assertIsNone(g.peek("", 1)); self.assertIsNone(g.peek("T", 1, lambda u, timeout=0: Resp({"ok": False})))


class Main(unittest.TestCase):
    def test_writes_the_decision_for_the_workflow(self):
        with tempfile.TemporaryDirectory() as d:
            json.dump(stats(1), open(os.path.join(d, "stats.json"), "w"))
            out = os.path.join(d, "out.txt"); os.environ["GITHUB_OUTPUT"] = out; os.environ["TELEGRAM_TOKEN"] = "T"
            try:
                run = g.main(["x", d], now=NOW, opener=lambda u, timeout=0: Resp({"ok": True, "result": []}))
            finally:
                os.environ.pop("GITHUB_OUTPUT")
            self.assertFalse(run); self.assertIn("run=false", open(out).read())

    def test_a_missing_stats_file_runs(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["TELEGRAM_TOKEN"] = "T"
            self.assertTrue(g.main(["x", d], now=NOW, opener=lambda u, timeout=0: Resp({"ok": True, "result": []})))

    def test_it_only_reads_never_writes_the_repo(self):
        src = open(os.path.join(HERE, "fast_gate.py")).read()
        self.assertEqual(src.count('open(os.path.join(root, STATS_FILE)'), 1)
        self.assertNotIn('"w"', src.replace('GITHUB_OUTPUT', ''))


if __name__ == "__main__":
    unittest.main()
