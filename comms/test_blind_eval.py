"""The blind-test runner keeps evidence honestly. These tests use a small made-up world, never the author's prompts."""
import json, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("TELEGRAM_CHAT_ID", "1"); os.environ.setdefault("TELEGRAM_TOKEN", "dummy"); os.environ.setdefault("TWELVE_DATA_KEY", "dummy")
import blind_eval as be
import test_chat as tc

CASES = [{"id": "A1", "prompt": "what is the price", "expected": "x", "tags": ["price"]},
         {"id": "A2", "prompt": "Remember this: I only trade London", "expected": "asks first", "tags": ["memory"]},
         {"id": "A3", "turns": ["how many scans today", "thanks"], "expected": "x", "tags": ["followup"]}]


class Runner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(); cls.root = os.path.join(cls.tmp, "root"); os.makedirs(cls.root); tc.make_world(cls.root)
        cls.prompts = os.path.join(cls.tmp, "p.json")
        with open(cls.prompts, "w") as f:
            json.dump(CASES, f)
        cls.out = os.path.join(cls.tmp, "runs")
        cls.cp = subprocess.run([sys.executable, os.path.join(HERE, "blind_eval.py"), "run", cls.prompts, "--root", cls.root, "--out", cls.out, "--now", tc.NOW.isoformat()],
                                capture_output=True, text=True, timeout=300)
        cls.run_dir = os.path.join(cls.out, sorted(os.listdir(cls.out))[0]) if os.path.isdir(cls.out) else None

    @classmethod
    def tearDownClass(cls):
        for r, d, f in os.walk(cls.tmp):
            for x in f:
                os.chmod(os.path.join(r, x), 0o644)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def rows(self):
        return [json.loads(l) for l in open(os.path.join(self.run_dir, "results.jsonl"))]

    def test_the_run_saves_every_first_response_and_a_manifest(self):
        self.assertEqual(self.cp.returncode, 0, self.cp.stderr)
        rows = self.rows(); self.assertEqual([r["test_id"] for r in rows], ["A1", "A2", "A3"])
        self.assertTrue(all(t["first_response"] and t["status"] == "completed" for r in rows for t in r["turns"]))
        self.assertEqual(len(rows[2]["turns"]), 2)
        m = json.load(open(os.path.join(self.run_dir, "MANIFEST.json")))
        for k in ("build", "config", "prompts_sha256", "results_sha256", "rerun_of"):
            self.assertIn(k, m)

    def test_evidence_is_read_only_and_tampering_is_detected(self):
        p = os.path.join(self.run_dir, "results.jsonl")
        self.assertFalse(os.access(p, os.W_OK) and os.stat(p).st_mode & 0o222)
        ok = subprocess.run([sys.executable, os.path.join(HERE, "blind_eval.py"), "verify", self.run_dir], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0)
        copy = os.path.join(self.tmp, "copy_of_run"); shutil.copytree(self.run_dir, copy)
        cp = os.path.join(copy, "results.jsonl"); os.chmod(cp, 0o644)
        with open(cp, "a") as f:
            f.write("tampered\n")
        bad = subprocess.run([sys.executable, os.path.join(HERE, "blind_eval.py"), "verify", copy], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1); self.assertIn("NOT OK", bad.stdout)

    def test_a_run_is_never_overwritten_and_side_effects_are_recorded_not_judged(self):
        rows = self.rows()
        self.assertEqual(rows[1]["turns"][0]["side_effects"]["notes_saved"], 0)            # "remember this" only OFFERS
        self.assertTrue(rows[1]["turns"][0]["side_effects"]["pending_offer_created"])
        self.assertEqual(rows[0]["turns"][0]["unexpected_side_effects"], [])

    def test_a_proactive_announcement_is_traced_to_the_bot_run_not_the_message_and_the_network_is_blocked(self):
        m = json.load(open(os.path.join(self.run_dir, "MANIFEST.json")))
        self.assertEqual(m["network_attempts_blocked"], 0)
        for r in self.rows():
            self.assertIn("control_run", r)
            for t in r["turns"]:
                self.assertEqual(t["side_effects"]["messages_sent"], 1, "a message may cause exactly one reply and nothing else")

    def test_the_runner_never_touches_the_real_records(self):
        self.assertFalse(os.path.exists(os.path.join(self.root, "user_ledger.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "inbox.jsonl")))


if __name__ == "__main__":
    unittest.main()
