"""Run: python test_bus_questions.py — research questions from Telegram (roadmap Step 2c, stages 1 and 2).
Stage 1 captures the question verbatim. Stage 2 stores PROPOSED definitions beside the original wording, versioned.
Nothing is answered, run, frozen or concluded, and a casual message can never become an experiment or an engine fact."""
import json, os, shutil, sys, tempfile, unittest
from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import ledger, bus, bus_runner as br, user_catalog as uc, research_objects as ro

T0 = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
Q = "Does a failed breakdown followed by a reclaim predict continuation?"


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.c = uc.Catalog(os.path.join(self.d, "user_ledger.jsonl")); self.t = T0

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def say(self, text):
        self.t = self.t + timedelta(minutes=1)
        return bus.respond(text, [], [], self.t, catalog=self.c).text

    def recs(self, *kinds):
        return [r for r in ledger.read(self.c.path)[0] if r["event_type"] in kinds]

    def all_defined(self, n=1):
        for f, t in (("claim", "a reclaim after a failed breakdown is followed by higher closes"), ("outcome", "next 4 15m candles close above the reclaim close"),
                     ("population", "GBPUSD 15m breakdowns during London"), ("baseline", "always follow the 1h bias"), ("null_model", "direction_randomization")):
            self.say(f"/define {n} {f}: {t}")


class Stage1(Base):
    def test_the_question_is_captured_word_for_word_with_time_and_source(self):
        t = self.say("/ask " + Q)
        r = self.recs(uc.RESEARCH_QUESTION_CAPTURED)
        self.assertEqual(len(r), 1); self.assertEqual(r[0]["payload"]["text"], Q); self.assertEqual(r[0]["payload"]["question_id"], "Q-0001")
        self.assertEqual((r[0]["payload"]["source"], r[0]["recorded_at"]), ("telegram", ledger._iso(self.t)))
        self.assertIn("Captured as Q-0001, word for word", t)

    def test_nothing_is_answered_run_defined_or_frozen(self):
        t = self.say("/ask " + Q)
        self.assertIn("I have not answered it, run anything or defined any of its words", t)
        self.assertEqual(self.recs(ro.HYPOTHESIS_DRAFTED, ro.SPEC_DRAFTED, ro.SPEC_FROZEN, ro.RESULT_RECORDED, uc.RESEARCH_DEFINITION_PROPOSED), [])
        self.assertEqual(self.c.question_view("Q-0001")["stage"], "CAPTURED")

    def test_the_users_words_are_not_interpreted(self):
        t = self.say("/ask " + Q).lower()
        for w in ("failed breakdown", "reclaim", "continuation"):
            self.assertNotIn(w + " means", t); self.assertNotIn(w + " is", t)
        self.assertNotIn("the engine has definitions for", t)                        # none of those words is an engine concept

    def test_engine_vocabulary_is_linked_without_claiming_the_same_meaning(self):
        t = self.say("/ask Does a BOS after a sweep continue?")
        self.assertIn("break of structure", t); self.assertIn("not that your use of it matches", t)

    def test_asking_the_same_thing_again_does_not_duplicate(self):
        self.say("/ask " + Q); t = self.say("/ask   " + Q.upper().replace("DOES", "does"))
        self.assertIn("already asked that: it is Q-0001", t); self.assertEqual(len(self.recs(uc.RESEARCH_QUESTION_CAPTURED)), 1)

    def test_empty_ask_writes_nothing(self):
        self.assertIn("Format", self.say("/ask")); self.assertEqual(self.recs(uc.RESEARCH_QUESTION_CAPTURED), [])

    def test_ids_are_sequential_and_listed(self):
        self.say("/ask first question?"); self.say("/ask second question?")
        t = self.say("/questions")
        self.assertIn("Q-0001", t); self.assertIn("Q-0002", t); self.assertIn("second question?", t)


class Stage2(Base):
    def setUp(self):
        super().setUp(); self.say("/ask " + Q)

    def test_a_definition_is_stored_as_a_proposal_beside_the_original_wording(self):
        t = self.say("/define 1 outcome: next 4 15m candles close above the entry")
        self.assertIn("proposed definition v1", t); self.assertIn(f"“{Q}”", t)
        self.assertIn("A definition here is a proposal", t)
        self.assertEqual(self.c.question_view("Q-0001")["definitions"]["outcome"]["latest"], "next 4 15m candles close above the entry")

    def test_a_changed_definition_is_a_new_version_and_the_old_one_is_kept(self):
        self.say("/define 1 outcome: version one"); t = self.say("/define 1 outcome: version two")
        self.assertIn("v2", t); self.assertIn("[2 versions kept]", t)
        texts = [r["payload"]["text"] for r in self.recs(uc.RESEARCH_DEFINITION_PROPOSED)]
        self.assertEqual(texts, ["version one", "version two"])

    def test_the_same_definition_again_adds_nothing(self):
        self.say("/define 1 claim: x"); n = len(ledger.read(self.c.path)[0])
        self.assertIn("already on file as v1", self.say("/define 1 claim: x")); self.assertEqual(len(ledger.read(self.c.path)[0]), n)

    def test_your_own_words_can_be_given_a_definition(self):
        t = self.say("/define 1 term failed breakdown: price closes below the swing low then back above within 3 candles")
        self.assertIn("your definition of “failed breakdown” (proposed)", t)
        self.assertIn("failed breakdown", self.c.question_view("Q-0001")["terms"])

    def test_the_null_model_must_be_a_real_one(self):
        n = len(ledger.read(self.c.path)[0])
        self.assertIn("null model must be one of", self.say("/define 1 null_model: coin flip")); self.assertEqual(len(ledger.read(self.c.path)[0]), n)
        self.assertIn("proposed definition v1", self.say("/define 1 null: direction_randomization"))

    def test_unknown_question_part_or_empty_text_write_nothing(self):
        n = len(ledger.read(self.c.path)[0])
        for bad in ("/define 9 claim: x", "/define 1 mood: x", "/define 1 claim:", "/define", "/define 1 term : x"):
            self.say(bad)
        self.assertEqual(len(ledger.read(self.c.path)[0]), n)

    def test_stage_reports_only_how_far_definition_has_got(self):
        self.assertEqual(self.c.question_view("Q-0001")["stage"], "CAPTURED")
        self.say("/define 1 claim: x"); self.assertEqual(self.c.question_view("Q-0001")["stage"], "DEFINING")
        self.all_defined(); v = self.c.question_view("Q-0001")
        self.assertEqual((v["stage"], v["missing"]), ("READY_TO_FORMALISE", []))

    def test_even_fully_defined_nothing_is_frozen_run_or_concluded(self):
        self.all_defined(); t = self.say("/question 1")
        self.assertIn("Nothing has been run, frozen or concluded", t)
        self.assertEqual(self.recs(ro.HYPOTHESIS_DRAFTED, ro.SPEC_DRAFTED, ro.SPEC_FROZEN, ro.RESULT_RECORDED, ro.SPLIT_ACCESSED), [])
        for bad in ("supported", "not supported", "confirmed", "proven", "works"):
            self.assertNotIn(bad, t.lower())

    def test_missing_parts_are_listed_and_never_filled_in(self):
        self.say("/define 1 claim: x"); t = self.say("/question 1")
        self.assertIn("Not defined yet: what to measure, which cases count, what to compare against, null model", t)

    def test_viewing_writes_nothing(self):
        n = len(ledger.read(self.c.path)[0]); self.say("/question 1"); self.say("/questions"); self.assertEqual(len(ledger.read(self.c.path)[0]), n)

    def test_as_of_an_earlier_time_the_definition_did_not_exist(self):
        self.say("/define 1 claim: x")
        self.assertEqual(self.c.question_view("Q-0001", as_of=T0 + timedelta(seconds=90))["definitions"], {})


class Boundaries(Base):
    def test_the_null_model_list_matches_research_objects(self):
        self.assertEqual(set(uc.KNOWN_NULL_MODELS), set(ro.NULL_MODELS))

    def test_a_research_question_never_touches_trades_or_cases(self):
        cid = self.c.log_trade("GBPUSD", "BUY", 1.3421, 1.3398, 1.3490, self.t, now=self.t)
        before = json.dumps(self.c.get_case(cid), sort_keys=True)
        self.say("/ask " + Q); self.say("/define 1 claim: x")
        self.assertEqual(before, json.dumps(self.c.get_case(cid), sort_keys=True))

    def test_a_telegram_message_is_captured_once(self):
        with open(os.path.join(self.d, "inbox.jsonl"), "w") as f:
            f.write(json.dumps({"update_id": 7, "chat_id": "1", "text": "/ask " + Q, "received_at": T0.isoformat()}) + "\n")
        sent = []
        for i in range(3):
            br.run(self.d, now=T0 + timedelta(minutes=10 + i), send=lambda t: sent.append(t) or True)
        self.assertEqual(len(self.recs(uc.RESEARCH_QUESTION_CAPTURED)), 1)
        self.assertEqual(sum("Captured as Q-0001" in t for t in sent), 1)

    def test_the_existing_research_index_is_unaffected(self):
        self.say("/ask " + Q)
        import bus_adapters as ba
        self.assertEqual(ba.adapt_research(ledger.read(self.c.path)[0]).errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
