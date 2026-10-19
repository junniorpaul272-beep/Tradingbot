"""Run: python test_bus_review_fresh.py — REGRESSION for a live failure (2026-10-16): after /logtrade, a bare /review answered
"case.{case_id}.trade: I'm not sure what you're asking about" (an unfilled placeholder shown as if it were a question).
A freshly logged trade legitimately has no reasoning, outcome or investigation: that is missing evidence, never an
unrecognised question, and an internal field path must never reach the user."""
import os, re, sys, unittest
from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import bus, bus_runner as br, test_bus_taken as tt

FRESH = datetime(2026, 10, 1, 15, 57, 20, tzinfo=timezone.utc)      # 2 min after the fixture's scan


class Fresh(tt.tbs.Base):
    def setUp(self):
        super().setUp()
        self.inbox_path = os.path.join(self.root, "inbox.jsonl")
        self.n = 0
        self.now = FRESH

    def go(self, now=None, signals=False):
        return super().go(now=now, signals=signals)

    say = tt.Flow.say
    last = tt.Flow.last

    def logged(self):
        self.say("/logtrade GBPUSD buy 1.33483 sl 1.33387 tp 1.33675", at=self.now - timedelta(minutes=1)); self.go()

    def ask(self, text):
        self.say(text); self.go(); return self.last()

    def test_a_bare_review_after_logging_shows_the_latest_trade_not_a_placeholder(self):
        self.logged(); t = self.ask("/review")
        self.assertNotIn("{", t); self.assertNotIn("case.", t); self.assertNotIn("not sure what you're asking", t)
        self.assertIn("your latest trade, CASE-0001", t); self.assertIn("GBPUSD buy at 1.33483", t)

    def test_a_fresh_case_reports_what_is_missing_in_words_and_what_to_do_next(self):
        self.logged(); t = self.ask("/review 1")
        self.assertIn("Not recorded yet: your reasoning, the outcome", t); self.assertIn("normal for a trade you've only just logged", t)
        self.assertIn("/reason", t); self.assertIn("/outcome", t)
        self.assertNotRegex(t, r"case\.[A-Za-z{]"); self.assertNotIn("can't answer", t)

    def test_retrieval_with_the_real_case_id_works_for_both_commands(self):
        self.logged()
        for cmd in ("/review 1", "/botvsme 1", "/review", "/botvsme"):
            t = self.ask(cmd)
            self.assertNotIn("{", t, cmd); self.assertNotRegex(t, r"case\.", cmd)
            self.assertIn("Not recorded yet", t, cmd)

    def test_the_missing_sections_shrink_as_they_are_filled_in(self):
        self.logged(); self.say("/reason 1 buying the retest"); self.go()
        t = self.ask("/review 1")
        self.assertIn("buying the retest", t); self.assertIn("Not recorded yet: the outcome", t); self.assertNotIn("your reasoning,", t)

    def test_no_trade_logged_yet_is_said_plainly(self):
        t = self.ask("/review"); self.assertIn("haven't logged a trade yet", t); self.assertNotIn("{", t)

    def test_a_number_that_does_not_exist_says_so_and_names_the_latest(self):
        self.logged(); t = self.ask("/review 7")
        self.assertIn("don't have a trade with that number", t); self.assertIn("CASE-0001", t); self.assertNotIn("nothing recorded for it", t)

    def test_the_bot_plan_line_says_what_unknown_means(self):
        self.logged(); t = self.ask("/review 1")
        self.assertIn("I didn't judge whether it was a valid setup", t); self.assertNotIn("had at the time: unknown", t)

    def test_logging_twice_is_two_cases_and_review_without_a_number_follows_the_newest(self):
        self.logged(); self.say("/logtrade GBPUSD sell 1.34000 sl 1.34100 tp 1.33800", at=self.now - timedelta(seconds=30)); self.go()
        self.assertIn("your latest trade, CASE-0002", self.ask("/review"))

    def test_no_command_ever_leaks_an_unfilled_placeholder_or_a_field_path(self):
        self.logged()
        cmds = sorted(set(bus.COMMAND_INTENTS) | {"/review", "/botvsme", "/thread", "/why"})
        for c in cmds:
            t = self.ask(c)
            self.assertNotRegex(t, r"\{[a-z_]+\}", c); self.assertNotRegex(t, r"\bcase\.[A-Za-z{]", c)

    def test_a_bare_thread_gets_the_latest_alert_or_a_plain_none_yet(self):
        self.logged()
        self.assertIn("haven't set any alerts", self.ask("/thread"))
        self.say("/alert 1.40000 above"); self.go()
        t = self.ask("/thread")
        self.assertIn("your latest alert or question", t); self.assertNotIn("{", t); self.assertNotIn("which trade", t)

    def test_the_safety_net_catches_a_placeholder_that_gets_through(self):
        import bus_contract as bc
        from bus_contract import Answer
        a = bc.resolve("trade_reconstruction", [], datetime(2026, 10, 1, tzinfo=timezone.utc), params={}, gaps=[])
        rep = bus.finalize(a)
        self.assertNotIn("{", rep.text); self.assertIn("which trade", rep.text)


class Why(unittest.TestCase):
    """REGRESSION found by the comms inspector (2026-10-18): /why with no subject, or one the bot has no evidence for, was answered by
    the audit safety net ("which trade do you mean? Try /review") instead of by /why itself."""
    def ask(self, text):
        from datetime import datetime, timezone
        return bus.respond(text, [], [], datetime(2026, 10, 1, tzinfo=timezone.utc)).text

    def test_a_bare_why_says_what_it_can_explain(self):
        t = self.ask("/why")
        self.assertIn("Which part do you want the evidence for?", t); self.assertIn("/why htf.bias", t)
        self.assertNotIn("which trade", t.lower().split("or ask about a trade")[0])

    def test_an_unknown_subject_is_named_back_not_mistaken_for_a_trade(self):
        for w in ("abc", "1"):
            t = self.ask("/why " + w)
            self.assertIn(f'I don\'t have an explanation for "{w}"', t); self.assertIn("/why cautions", t)
            self.assertNotIn("I need a bit more to answer that", t)

    def test_a_real_subject_still_goes_to_the_normal_path(self):
        self.assertNotIn("I don't have an explanation", self.ask("/why htf.bias"))


if __name__ == "__main__":
    unittest.main()
