"""Run: python test_bus_investigate.py — /investigate (roadmap Step 2a). The worksheet is read back from the ledger,
asks the SAME questions of a win and a loss, never says who was right, and refuses to conclude before an outcome exists."""
import json, os, shutil, sys, tempfile, unittest
from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import ledger, bus, bus_runner as br, user_catalog as uc

T0 = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.c = uc.Catalog(os.path.join(self.d, "user_ledger.jsonl"))
        self.t = T0

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def tick(self, m=5):
        self.t = self.t + timedelta(minutes=m); return self.t

    def case(self, plan=True):
        cid = self.c.log_trade("GBPUSD", "BUY", 1.3421, 1.3398, 1.3490, self.t, now=self.t)
        if plan:
            self.c.attach_bot_plan(cid, self.t, "TRADE", {"tier_label": "TIER_2_FIB"}, now=self.t)
        return cid

    def inv(self, text):
        return bus.respond(text, [], [], self.tick(), catalog=self.c).text

    def opened(self):
        return [r for r in ledger.read(self.c.path)[0] if r["event_type"] == uc.INVESTIGATION_OPENED]

    def questions(self, txt):
        return [l for l in txt.splitlines() if l[:2] in ("1.", "2.", "3.", "4.", "5.")]


class Worksheet(Base):
    def test_unknown_case_is_said_and_nothing_is_written(self):
        t = self.inv("/investigate 9")
        self.assertIn("don't have CASE-0009", t); self.assertEqual(self.opened(), [])

    def test_bad_format_is_explained(self):
        self.case(); self.assertIn("Format", self.inv("/investigate"))

    def test_without_an_outcome_nothing_is_concluded_and_it_says_pending(self):
        self.case(); t = self.inv("/investigate 1")
        self.assertIn("PENDING", t); self.assertIn("can't compare", t); self.assertIn("/outcome", t)
        self.assertEqual([r["payload"]["status"] for r in self.opened()], ["PENDING_OUTCOME"])

    def test_a_question_before_the_outcome_is_stored_pending_verbatim_and_not_answered(self):
        self.case(); t = self.inv("/investigate 1 Was the sweep real or did I imagine it? #sweep_trap")
        self.assertIn("PENDING item, word for word", t); self.assertIn("Was the sweep real or did I imagine it? #sweep_trap", t)
        self.assertEqual(self.c.get_case("CASE-0001")["investigation"], [])          # not an investigation conclusion
        self.assertEqual(self.c.investigation_state("CASE-0001")["pending_questions"], ["Was the sweep real or did I imagine it? #sweep_trap"])

    def test_repeating_the_command_does_not_duplicate_records(self):
        self.case(); self.inv("/investigate 1"); self.inv("/investigate 1"); self.inv("/investigate 1 why?"); self.inv("/investigate 1 why?")
        self.assertEqual(len(self.opened()), 2)                                      # the bare open, then the one question; no repeats

    def test_recording_the_outcome_moves_it_to_open_once(self):
        self.case(); self.inv("/investigate 1")
        self.c.record_outcome("CASE-0001", "LOSS", self.tick(), r=-1, now=self.t)
        t = self.inv("/investigate 1"); self.inv("/investigate 1")
        self.assertEqual([r["payload"]["status"] for r in self.opened()], ["PENDING_OUTCOME", "OPEN"])
        self.assertIn("Outcome recorded: loss", t)

    def test_a_win_and_a_loss_get_exactly_the_same_questions(self):
        a = self.case(); self.c.record_outcome(a, "WIN", self.tick(), r=2, now=self.t)
        b = self.case(); self.c.record_outcome(b, "LOSS", self.tick(), r=-1, now=self.t)
        ta, tb = self.inv("/investigate 1"), self.inv("/investigate 2")
        self.assertEqual(self.questions(ta), self.questions(tb)); self.assertEqual(len(self.questions(ta)), 5)
        for t in (ta, tb):
            self.assertIn("not evidence your reasoning was right or the bot's was wrong", t)

    def test_the_worksheet_never_hands_out_a_verdict(self):
        a = self.case(); self.c.record_outcome(a, "WIN", self.tick(), r=2, now=self.t)
        self.c.add_reasoning(a, "I saw a #sweep_trap then a BOS", now=self.tick())
        t = self.inv("/investigate 1").lower()
        for bad in ("you were right", "you were wrong", "bot was wrong", "bot was right", "good trade", "bad trade", "mistake", "should have"):
            self.assertNotIn(bad, t)

    def test_a_note_after_the_outcome_is_an_investigation_note_stored_verbatim(self):
        a = self.case(); self.c.record_outcome(a, "LOSS", self.tick(), r=-1, now=self.t)
        t = self.inv("/investigate 1 I entered before the retest closed")
        self.assertIn("stored word for word as an investigation note", t)
        self.assertEqual([r["payload"]["text"] for r in self.c.get_case(a)["investigation"]], ["I entered before the retest closed"])

    def test_reasoning_written_after_the_outcome_is_flagged_in_the_worksheet(self):
        a = self.case(); self.c.add_reasoning(a, "before", now=self.tick())
        self.c.record_outcome(a, "WIN", self.tick(), r=1, now=self.t)
        self.c.add_reasoning(a, "after, with hindsight", now=self.tick())
        t = self.inv("/investigate 1")
        self.assertIn("2 entries", t); self.assertIn("1 written after the outcome was known", t)

    def test_unknown_user_concepts_stay_the_users_and_are_not_made_into_engine_facts(self):
        a = self.case(); self.c.add_reasoning(a, "classic #suport bounce with a BOS", now=self.tick())
        t = self.inv("/investigate 1")
        self.assertIn("your own terms (the engine has no definition): #suport", t)
        self.assertNotIn("terms the engine defines: suport", t)

    def test_investigating_never_changes_the_bots_plan_or_the_trade(self):
        a = self.case(); before = json.dumps(self.c.get_case(a)["bot_plan"], sort_keys=True), json.dumps(self.c.get_case(a)["trade"], sort_keys=True)
        self.c.record_outcome(a, "WIN", self.tick(), r=2, now=self.t)
        self.inv("/investigate 1 the OB was mitigated, I only see that now")
        after = json.dumps(self.c.get_case(a)["bot_plan"], sort_keys=True), json.dumps(self.c.get_case(a)["trade"], sort_keys=True)
        self.assertEqual(before, after)
        with self.assertRaises(ledger.LedgerError):                                  # and a plan dated after the entry is still refused
            self.c.attach_bot_plan(self.case(plan=False), self.tick(60), "TRADE", {}, now=self.t)

    def test_missing_bot_plan_is_said_not_invented(self):
        self.case(plan=False); t = self.inv("/investigate 1")
        self.assertIn("bot plan: none recorded", t)

    def test_as_of_an_earlier_time_the_investigation_did_not_exist(self):
        a = self.case(); self.inv("/investigate 1")
        view = ledger.view_as_of(ledger.read(self.c.path)[0], T0 + timedelta(minutes=1))
        self.assertEqual([r for r in view if r["event_type"] == uc.INVESTIGATION_OPENED], [])

    # ---- the reviewer's three checks
    def test_pending_output_carries_no_judgement_labels_or_suggested_explanations(self):
        a = self.case(); self.c.add_reasoning(a, "I saw a #sweep_trap", now=self.tick())
        t = self.inv("/investigate 1 Did the sweep cause the move").lower()
        for bad in ("because", "likely", "probably", "suggests", "indicates", "valid", "invalid", "correct", "incorrect",
                    "right", "wrong", "good", "bad", "mistake", "supports", "confirms"):
            self.assertNotRegex(t, r"\b" + bad + r"\b", bad)

    def test_reasoning_is_shown_with_when_it_was_written_relative_to_the_outcome(self):
        a = self.case(); self.c.add_reasoning(a, "before", now=self.tick())
        self.c.record_outcome(a, "WIN", self.tick(), r=1, now=self.t)
        self.c.add_reasoning(a, "after", now=self.tick())
        t = self.inv("/investigate 1")
        self.assertIn("(before the outcome)", t); self.assertIn("(after the outcome)", t)
        times = [r["recorded_at"] for r in self.c.get_case(a)["user_reasoning"]]
        self.assertEqual(times, sorted(times)); self.assertTrue(all(times))

    def test_a_question_stays_a_question_even_after_the_outcome(self):
        a = self.case(); self.c.record_outcome(a, "LOSS", self.tick(), r=-1, now=self.t)
        t = self.inv("/investigate 1 Did the retest actually hold?")
        self.assertIn("QUESTION, word for word", t); self.assertIn("a question is not a finding", t)
        self.assertEqual(self.c.get_case(a)["investigation"], [])                    # NOT stored as an investigation note
        self.assertIn("Did the retest actually hold?", self.c.investigation_state(a)["pending_questions"])

    def test_the_command_never_writes_a_conclusion_or_touches_trade_state(self):
        a = self.case(); self.c.record_outcome(a, "WIN", self.tick(), r=2, now=self.t)
        before = [r for r in ledger.read(self.c.path)[0] if r["event_type"] != uc.INVESTIGATION_OPENED]
        self.inv("/investigate 1"); self.inv("/investigate 1 Why did it work?")
        after = [r for r in ledger.read(self.c.path)[0] if r["event_type"] != uc.INVESTIGATION_OPENED]
        self.assertEqual(before, after)                                              # only status/question records were added
        self.assertEqual(self.c.get_case(a)["investigation"], [])

    def test_the_pending_response_is_built_only_from_the_fixed_template(self):
        """Stronger than scanning for words: EVERY line must match a template line, so there is nowhere for a suggested
        explanation or an implied conclusion to come from. The user's own text may appear only in the places allowed."""
        import re
        a = self.case(); self.c.add_reasoning(a, "I saw a #sweep_trap", now=self.tick())
        t = self.inv("/investigate 1 Did the sweep cause the move")
        questions = [q for _k, q in uc.Catalog.INVESTIGATION_QUESTIONS]
        allowed = [r"^Stored as a PENDING item, word for word\. Nothing is concluded before an outcome exists\.$",
                   r"^Investigation of CASE-\d{4}: [A-Z]{6} (buy|sell) at \d\.\d{5}\.$",
                   r"^No outcome is recorded yet, so this stays PENDING: I can't compare anyone's reasoning with what happened\. Record it with /outcome N win\|loss\|be \[R\]\.$",
                   r"^Evidence on file — .*\.$", r"^The same questions apply to every case:$",
                   r"^Questions on file, unanswered: Did the sweep cause the move$"] + [r"^\d\. " + re.escape(q) + "$" for q in questions]
        for line in t.splitlines():
            self.assertTrue(any(re.match(rx, line) for rx in allowed), f"line outside the template: {line!r}")
        ev = [l for l in t.splitlines() if l.startswith("Evidence on file")][0]
        self.assertRegex(ev, r"^Evidence on file — your reasoning: 1 entry; reasoning written at \d\d:\d\d UTC \(before the outcome\); bot plan: trade, as of \d\d:\d\d UTC; your own terms \(the engine has no definition\): #sweep_trap\.$")


class ThroughTheRunner(Base):
    def test_a_telegram_message_is_handled_once(self):
        shutil.copy(os.path.join(ROOT, "fixture_real_state.json"), os.path.join(self.d, "state.json")) if os.path.exists(os.path.join(ROOT, "fixture_real_state.json")) else None
        self.case()
        with open(os.path.join(self.d, "inbox.jsonl"), "w") as f:
            f.write(json.dumps({"update_id": 7, "chat_id": "1", "text": "/investigate 1", "received_at": T0.isoformat()}) + "\n")
        sent = []
        for i in range(3):
            br.run(self.d, now=T0 + timedelta(minutes=10 + i), send=lambda t: sent.append(t) or True)
        self.assertEqual(sum("Investigation of CASE-0001" in t for t in sent), 1)
        self.assertEqual(len(self.opened()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
