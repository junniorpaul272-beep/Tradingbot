"""plainwords + guard: every engine code has a registered meaning, no facts hide in the registry, the guard's hard rules hold."""
import re, sys, unittest
sys.path.insert(0, ".."); sys.path.insert(0, ".")
import plainwords as pw, guard


class Registry(unittest.TestCase):
    def test_every_engine_caution_and_watch_code_has_a_meaning(self):
        import scanner_observation as so
        for c in so.CautionCode:
            self.assertIsNotNone(pw.plain(c.value), c.value)
        for w in so.WatchCode:
            self.assertIsNotNone(pw.watch(w.value), w.value)

    def test_tiers_match_the_engine(self):
        import scanner_observation as so
        self.assertEqual(pw.TIERS, dict(so.TIER_DISPLAY_NAMES))

    def test_no_digits_except_timeframe_names(self):
        for ph in pw.phrases():
            left = re.sub(r"\b(?:1|5|15)-(?:minute|hour)\b", "", ph)
            left = re.sub(r"\bTier [123]\b", "", left)
            self.assertFalse(re.search(r"\d", left), ph)


class Guard(unittest.TestCase):
    def test_hard_rules(self):
        self.assertTrue(guard.review("").blocked)
        self.assertTrue(guard.review("Price is {price}").blocked)
        self.assertTrue(guard.review("bias is None").blocked)
        self.assertTrue(guard.review("[market_state] status ANSWER x").blocked)

    def test_user_quotes_are_not_judged(self):
        self.assertFalse(guard.review('Saved your note: "bias None today"').blocked)

    def test_raw_codes_become_words(self):
        r = guard.review("Careful: AGING_CAMPAIGN here.")
        self.assertIn("aging campaign", r.text); self.assertTrue(r.repaired)

    def test_integrity_note_moves_to_the_end(self):
        r = guard.review("Integrity alarm: x\nThe bigger picture leans up.")
        self.assertTrue(r.text.startswith("The bigger picture"))

    def test_missing_part_is_said_not_dropped(self):
        r = guard.review("Answer one.", parts=[("the last CHoCH", ""), ("now", "ok")])
        self.assertIn("couldn't answer the part about the last CHoCH", r.text)

    def test_long_text_is_cut(self):
        r = guard.review("line\n" * 2000)
        self.assertLessEqual(len(r.text), 1500)


if __name__ == "__main__":
    unittest.main()


class TestProbeGlossDrift(unittest.TestCase):
    def test_every_probe_the_inspector_can_raise_has_a_plain_gloss(self):
        import inspector, plainwords, re as _re
        src = open(inspector.__file__).read()
        names = set(_re.findall(r'finding\(\s*"([a-z_]+)"', src))
        self.assertTrue(names)
        missing = [n for n in names if not plainwords.probe_gloss(n)]
        self.assertEqual(missing, [], f"inspector raises probes with no plain wording: {missing}")
