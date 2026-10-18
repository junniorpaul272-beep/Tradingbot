"""Run: python test_bus_know.py — /know (roadmap Step 2b): recording what the user says they know. Raw words verbatim,
engine vocabulary linked only where it has a definition, unknown terms stay the user's, nothing becomes demonstrated
knowledge or a detected setup, and later interpretation never overwrites the original."""
import json, os, shutil, sys, tempfile, unittest
from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import ledger, bus, bus_runner as br, bus_adapters as ba, user_catalog as uc

T0 = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.c = uc.Catalog(os.path.join(self.d, "user_ledger.jsonl")); self.t = T0

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def say(self, text):
        self.t = self.t + timedelta(minutes=1)
        return bus.respond(text, [], [], self.t, catalog=self.c).text

    def recs(self, kind):
        return [r for r in ledger.read(self.c.path)[0] if r["event_type"] == kind]


class Recording(Base):
    def test_the_raw_statement_is_kept_word_for_word_with_time_and_no_case(self):
        said = "I waited for a break and retest of support"
        t = self.say("/know " + said)
        r = self.recs(uc.KNOWLEDGE_STATED)
        self.assertEqual(len(r), 1); self.assertEqual(r[0]["payload"]["text"], said); self.assertIsNone(r[0]["payload"]["case_id"])
        self.assertEqual(r[0]["recorded_at"], ledger._iso(self.t))
        self.assertIn("word for word", t)

    def test_a_concept_the_engine_does_not_define_is_not_detected_or_defined(self):
        t = self.say("/know I waited for a break and retest of support")
        self.assertIn("doesn't mean the engine detected anything", t)
        self.assertNotIn("The engine has definitions for", t)                         # nothing in that sentence is an engine concept
        self.assertEqual(self.recs(uc.TERMS_OBSERVED), [])

    def test_a_recognised_word_links_to_the_engine_vocabulary_without_claiming_your_use_matches(self):
        t = self.say("/know I only take a BOS after a sweep")
        self.assertIn("break of structure", t); self.assertIn("liquidity sweep", t)
        self.assertIn("not that your use of it matches", t)

    def test_your_own_tagged_terms_stay_yours_with_no_definition(self):
        t = self.say("/know I trust #suport more than #fib")
        self.assertIn("#suport", t); self.assertIn("they stay yours", t)
        terms = {x["term"]: x["status"] for x in self.c.term_usage()}
        self.assertEqual(terms["suport"], "unrecognised_by_engine")

    def test_it_is_never_demonstrated_knowledge(self):
        self.say("/know I understand order blocks")
        self.assertEqual(self.c.knows("order_block"), []); self.assertEqual(self.recs(uc.KNOWLEDGE_DEMONSTRATED), [])
        self.assertIn("not treated as demonstrated knowledge", self.say("/know I understand fvg"))

    def test_a_statement_can_be_tied_to_a_case_and_the_case_must_exist(self):
        self.c.log_trade("GBPUSD", "BUY", 1.3421, 1.3398, 1.3490, self.t, now=self.t)
        t = self.say("/know case 1 I saw a failed breakdown then a reclaim")
        self.assertIn("about CASE-0001", t)
        self.assertEqual(self.recs(uc.KNOWLEDGE_STATED)[0]["payload"]["case_id"], "CASE-0001")
        n = len(ledger.read(self.c.path)[0])
        self.assertIn("I didn't record that", self.say("/know case 9 nothing here")); self.assertEqual(len(ledger.read(self.c.path)[0]), n)

    def test_a_statement_that_starts_with_a_number_is_not_mistaken_for_a_case(self):
        self.say("/know 1h bias matters more than 5m")
        self.assertIsNone(self.recs(uc.KNOWLEDGE_STATED)[0]["payload"]["case_id"])

    def test_empty_and_bare_commands_write_nothing(self):
        self.assertIn("Format", self.say("/know")); self.assertEqual(self.recs(uc.KNOWLEDGE_STATED), [])

    def test_later_statements_add_records_and_never_overwrite_the_first(self):
        self.say("/know support is where price bounced twice"); first = json.dumps(self.recs(uc.KNOWLEDGE_STATED)[0], sort_keys=True)
        self.say("/know actually support is a zone, not a line")
        r = self.recs(uc.KNOWLEDGE_STATED)
        self.assertEqual(len(r), 2); self.assertEqual(json.dumps(r[0], sort_keys=True), first)
        self.assertTrue(ledger.verify(ledger.read(self.c.path)[0]) == [])

    def test_list_reads_back_the_words_as_written(self):
        self.say("/know first thing"); self.say("/know second #thing")
        t = self.say("/know list")
        self.assertIn("first thing", t); self.assertIn("second #thing", t)
        self.assertEqual(len(self.recs(uc.KNOWLEDGE_STATED)), 2)                      # listing writes nothing

    def test_as_of_an_earlier_time_the_statement_did_not_exist(self):
        self.say("/know something")
        self.assertEqual(self.c.statements(as_of=T0), [])

    def test_term_counts_and_the_bus_adapter_tolerate_statements_with_no_case(self):
        self.say("/know #suport again"); self.say("/know #suport once more")
        u = {x["term"]: x for x in self.c.term_usage()}["suport"]
        self.assertEqual((u["uses"], u["cases"]), (2, []))
        recs = ledger.read(self.c.path)[0]
        claim = ba.adapt_terms(recs).claims[0].value["terms"][0]
        self.assertEqual((claim["term"], claim["uses"], claim["n_cases"]), ("suport", 2, 0))

    def test_reasoning_terms_and_statement_terms_count_together(self):
        cid = self.c.log_trade("GBPUSD", "BUY", 1.3421, 1.3398, 1.3490, self.t, now=self.t)
        self.c.add_reasoning(cid, "bounce off #suport", now=self.t); self.say("/know I like #suport")
        u = {x["term"]: x for x in self.c.term_usage()}["suport"]
        self.assertEqual((u["uses"], u["cases"]), (2, [cid]))

    def test_an_engine_word_inside_your_own_tag_does_not_make_the_engine_define_your_term(self):
        t = self.say("/know I watch for a #sweep_trap")
        self.assertIn("#sweep_trap", t); self.assertNotIn("The engine has definitions for", t)
        self.assertEqual(uc.extract_terms("a #sweep_trap")["recognised"], [])
        self.assertEqual([c["concept"] for c in uc.extract_terms("a #fib bounce")["recognised"]], ["fib_pocket"])     # a tag that IS an alias still links


class ThroughTheRunner(Base):
    def test_a_telegram_message_is_recorded_once(self):
        with open(os.path.join(self.d, "inbox.jsonl"), "w") as f:
            f.write(json.dumps({"update_id": 7, "chat_id": "1", "text": "/know I wait for a retest", "received_at": T0.isoformat()}) + "\n")
        sent = []
        for i in range(3):
            br.run(self.d, now=T0 + timedelta(minutes=10 + i), send=lambda t: sent.append(t) or True)
        self.assertEqual(len(self.recs(uc.KNOWLEDGE_STATED)), 1)
        self.assertEqual(sum("Recorded, word for word" in t for t in sent), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
