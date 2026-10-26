"""Pins the bugs the hard round found (23 Oct), each with the smallest case that failed."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import informal, timeref
from datetime import datetime, timezone


class Fixes(unittest.TestCase):
    def test_a_slipped_finger_is_repaired_and_reported(self):
        for typo, want in (("laast bos", "last bos"), ("when was the last chcoh", "when was the last choch"), ("how muny chochs today", "how many chochs today"),
                           ("explaan order block", "explain order block"), ("what is a fiar value gap", "what is a fair value gap")):
            r = informal.normalise(typo)
            self.assertEqual(r["text"], want); self.assertTrue(r["changes"] and r["changes"][0]["basis"] == "near-miss spelling")

    def test_real_words_are_never_turned_into_trading_words(self):
        for w in ("may", "black", "far", "gas", "gain", "sow", "fist", "weep", "lost", "list", "lock"):
            self.assertEqual(informal.normalise(f"the {w} is here")["text"], f"the {w} is here", w)

    def test_a_question_without_a_question_mark_or_with_filler_is_still_a_question(self):
        for q in ("wasnt price inside d fvg at 13:46", "was price inside the fvg at 13:46", "so how many chochs today", "wuen was the choch"):
            self.assertEqual(informal.kind_of(q), "question", q)
        self.assertEqual(informal.kind_of("price broke the high"), "statement")

    def test_a_dotted_time_is_a_time_but_a_price_is_not(self):
        self.assertEqual(timeref.dotted("was price inside the fvg at 13.46"), "was price inside the fvg at 13:46")
        self.assertEqual(timeref.dotted("price at 1.3346 and 13.46 pips"), "price at 1.3346 and 13.46 pips")
        w = timeref.parse("at 13.46", datetime(2026, 10, 20, 16, 5, tzinfo=timezone.utc))
        self.assertEqual(timeref.hm(w.start), "13:16")

    def test_zone_question_asked_soon_after_the_moment_uses_that_moment(self):
        import bus_runner as br
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bus_runner.py")).read()
        self.assertIn('"around": w.start + timedelta(minutes=30)', src)       # not the middle of a window cut off at "now"

    def test_real_history_has_no_1h_flip_and_a_planted_one_is_found_exactly(self):
        """The 322 real snapshots (16-20 Oct) hold no 1H leg flip, so the flip path is checked by planting one in a copy of that real history."""
        import json, copy
        import market_reconstruct as mr
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        H = [json.loads(l) for l in open(os.path.join(root, "tests_data_live", "state_history_log.jsonl")) if l.strip()]
        now = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
        res = mr.last_structure_events([], [], [], H, now)
        self.assertIsNone(res["leg_flip_1h"]); self.assertEqual(res["leg_now_1h"], "BULLISH")
        H2 = copy.deepcopy(H); k = 200
        for r in H2[k:]:
            if isinstance(r["payload"].get("macro_leg"), dict):
                r["payload"]["macro_leg"]["direction"] = "BEARISH"
        res2 = mr.last_structure_events([], [], [], H2, now)
        self.assertEqual(res2["leg_flip_1h"]["to"], "BEARISH"); self.assertEqual(res2["leg_flip_1h"]["at"], mr._t(H2[k]["ts"]))


if __name__ == "__main__":
    unittest.main()
