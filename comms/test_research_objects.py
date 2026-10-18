"""Run: python test_research_objects.py — research engine phase 1 (objects + rules; no randomness)."""
import copy, os, sys, tempfile, unittest
from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import ledger, research_objects as ro
from ledger import LedgerError

U = lambda *a: datetime(*a, tzinfo=timezone.utc)
T = U(2026, 10, 2, 9, 0)
m = lambda n: T + timedelta(minutes=n)


def good_spec(**over):
    s = {"question": "Does my directional decision at break-and-retest entries improve expectancy?",
         "claim_under_test": "direction choice adds expectancy over random direction at the same entries",
         "instrument": "GBPUSD",
         "population": {"population_id": "POP-a1b2c3", "eligibility_rule": "break+retest on 5m inside London session", "n_observations": 187},
         "decision_under_test": "direction",
         "preserve": sorted(ro.NULL_MODELS["direction_randomization"]["must_preserve"]),
         "randomize": ["direction"], "outcome_metric": "expectancy_R", "null_model": "direction_randomization",
         "benchmarks": ["always_follow_htf"], "research_family_id": "FAM-gbpusd-breakout", "data_cutoff": "2026-09-30T21:00:00+00:00",
         "data_split": "development", "seed": 20261002}
    s.update(over); return s


class Validation(unittest.TestCase):
    def test_a_correct_spec_is_valid(self):
        self.assertEqual(ro.validate_spec(good_spec()), [])

    def test_the_headline_rule_testing_direction_but_also_randomising_entry_is_rejected(self):
        p = ro.validate_spec(good_spec(randomize=["direction", "entry_time"]))
        self.assertTrue(any("entry_time" in x and "would test something other" in x for x in p), p)

    def test_randomising_something_that_must_be_preserved_is_rejected(self):
        s = good_spec(randomize=["direction", "stop_distance"]); p = ro.validate_spec(s)
        self.assertTrue(any("declared both preserved and randomised" in x for x in p) or any("stop_distance" in x for x in p), p)

    def test_failing_to_randomise_the_thing_under_test_is_rejected(self):
        p = ro.validate_spec(good_spec(randomize=["session"]))
        self.assertTrue(any("must randomise ['direction']" in x for x in p), p)

    def test_failing_to_freeze_what_must_stay_fixed_is_rejected(self):
        s = good_spec(); s["preserve"] = [x for x in s["preserve"] if x != "costs"]
        self.assertTrue(any("costs" in x for x in ro.validate_spec(s)))

    def test_missing_fields_unknown_null_bad_split_bad_population_bad_seed(self):
        s = good_spec(); del s["claim_under_test"]; self.assertEqual(ro.validate_spec(s), ["missing field: claim_under_test"])
        self.assertIn("unknown null_model", ro.validate_spec(good_spec(null_model="vibes"))[0])
        self.assertTrue(any("data_split" in x for x in ro.validate_spec(good_spec(data_split="whenever"))))
        self.assertTrue(any("population needs" in x for x in ro.validate_spec(good_spec(population={"n": 1}))))
        self.assertTrue(any("seed" in x for x in ro.validate_spec(good_spec(seed=1.5))))
        self.assertTrue(any("benchmark" in x for x in ro.validate_spec(good_spec(benchmarks=["moon_phase"]))))
        self.assertEqual(ro.validate_spec("nope"), ["spec must be a dict"])

    def test_every_null_documents_what_it_does_not_answer(self):
        for name, nm in ro.NULL_MODELS.items():
            self.assertTrue(nm["answers"] and nm["does_not_answer"], name)
            self.assertFalse(nm["implemented"], f"{name}: nothing is simulated in phase 1")

    def test_ablation_is_not_randomness(self):
        s = good_spec(null_model="component_ablation", randomize=[],
                      preserve=sorted(ro.NULL_MODELS["component_ablation"]["must_preserve"]), decision_under_test="fib_component")
        self.assertEqual(ro.validate_spec(s), [])


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.p = os.path.join(tempfile.mkdtemp(), "u.jsonl")
        self.h = ro.draft_hypothesis(self.p, "Does break/retest have an edge?", "break+retest entries have positive expectancy", "FAM-gbpusd-breakout", now=T)

    def test_ids_are_content_addressed(self):
        self.assertEqual(self.h, ro.draft_hypothesis(self.p, "Does break/retest have an edge?", "break+retest entries have positive expectancy", "FAM-gbpusd-breakout", now=m(1)))
        h2 = ro.draft_hypothesis(self.p, "Does break/retest have an edge?", "a DIFFERENT claim", "FAM-gbpusd-breakout", now=m(2))
        self.assertNotEqual(self.h, h2)
        a, _ = ro.draft_spec(self.p, self.h, good_spec(), now=m(3))
        b, _ = ro.draft_spec(self.p, self.h, good_spec(seed=1), now=m(4))                     # only the seed differs
        c, _ = ro.draft_spec(self.p, h2, good_spec(), now=m(5))                              # same spec, different hypothesis
        self.assertEqual(len({a, b, c}), 3)                                                   # three experiments, never "edits"
        again, _ = ro.draft_spec(self.p, self.h, good_spec(), now=m(6))
        self.assertEqual(again, a)

    def test_an_invalid_spec_is_kept_as_a_draft_but_can_never_be_frozen(self):
        sid, problems = ro.draft_spec(self.p, self.h, good_spec(randomize=["direction", "entry_time"]), now=m(1))
        self.assertTrue(problems)
        with self.assertRaises(LedgerError): ro.freeze(self.p, sid, now=m(2))
        drafts = [r for r in ledger.read(self.p)[0] if r["event_type"] == ro.SPEC_DRAFTED]
        self.assertFalse(drafts[0]["payload"]["valid"])                                       # the failed attempt is part of the search record

    def test_freeze_once_only_and_unknown_specs_refused(self):
        sid, _ = ro.draft_spec(self.p, self.h, good_spec(), now=m(1))
        self.assertFalse(ro.is_frozen(self.p, sid))
        ro.freeze(self.p, sid, now=m(2)); self.assertTrue(ro.is_frozen(self.p, sid))
        with self.assertRaises(LedgerError): ro.freeze(self.p, sid, now=m(3))
        with self.assertRaises(LedgerError): ro.freeze(self.p, "X-nope", now=m(3))
        with self.assertRaises(LedgerError): ro.draft_spec(self.p, "H-unknown", good_spec(), now=m(3))

    def test_the_freeze_moment_is_reconstructable(self):
        sid, _ = ro.draft_spec(self.p, self.h, good_spec(), now=m(1)); ro.freeze(self.p, sid, now=m(10))
        recs = ledger.read(self.p)[0]
        before = ledger.as_of(recs, m(5)); after = ledger.as_of(recs, m(11))
        self.assertFalse([r for r in before if r["event_type"] == ro.SPEC_FROZEN])
        self.assertTrue([r for r in after if r["event_type"] == ro.SPEC_FROZEN])

    def test_split_access_rules(self):
        sid, _ = ro.draft_spec(self.p, self.h, good_spec(), now=m(1))
        self.assertEqual(ro.access_split(self.p, sid, "development", now=m(2)), {"first_access": True, "peek": False})
        for role in ("validation", "final_test"):
            with self.assertRaises(LedgerError): ro.access_split(self.p, sid, role, now=m(3))     # not frozen yet
        ro.freeze(self.p, sid, now=m(4))
        self.assertEqual(ro.access_split(self.p, sid, "validation", now=m(5)), {"first_access": True, "peek": False})
        self.assertEqual(ro.access_split(self.p, sid, "final_test", now=m(6)), {"first_access": True, "peek": False})
        self.assertEqual(ro.access_split(self.p, sid, "final_test", now=m(7)), {"first_access": False, "peek": True})
        with self.assertRaises(LedgerError): ro.access_split(self.p, sid, "tomorrow")

    def test_results_need_a_frozen_spec_the_same_population_and_an_effective_n(self):
        sid, _ = ro.draft_spec(self.p, self.h, good_spec(), now=m(1))
        with self.assertRaises(LedgerError): ro.record_result(self.p, sid, "POP-a1b2c3", "SUPPORTED", 40, now=m(2))   # not frozen
        ro.freeze(self.p, sid, now=m(3))
        with self.assertRaises(LedgerError): ro.record_result(self.p, sid, "POP-a1b2c3", "PROVEN", 40)
        with self.assertRaises(LedgerError): ro.record_result(self.p, sid, "POP-a1b2c3", "SUPPORTED", -1)
        with self.assertRaises(LedgerError): ro.record_result(self.p, sid, "POP-a1b2c3", "SUPPORTED", True)
        self.assertEqual(ro.record_result(self.p, sid, "POP-OTHER", "SUPPORTED", 40, now=m(4)), "INVALID_EXPERIMENT")   # not hidden, recorded
        res = [r for r in ledger.read(self.p)[0] if r["event_type"] == ro.RESULT_RECORDED][-1]["payload"]
        self.assertIn("population other than the frozen one", res["invalidated_because"])

    def test_a_final_test_peek_travels_with_the_result(self):
        sid, _ = ro.draft_spec(self.p, self.h, good_spec(), now=m(1)); ro.freeze(self.p, sid, now=m(2))
        ro.access_split(self.p, sid, "final_test", now=m(3)); ro.access_split(self.p, sid, "final_test", now=m(4))
        ro.record_result(self.p, sid, "POP-a1b2c3", "INCONCLUSIVE", 31, limitations=["small sample"], now=m(5))
        res = [r for r in ledger.read(self.p)[0] if r["event_type"] == ro.RESULT_RECORDED][-1]["payload"]
        self.assertTrue(res["final_test_peeked"]); self.assertEqual(res["effective_n"], 31)

    def test_family_accounting_counts_every_search_not_just_the_winners(self):
        ids = []
        for i in range(5):
            sid, _ = ro.draft_spec(self.p, self.h, good_spec(seed=i), now=m(10 + i)); ids.append(sid)
        ro.draft_spec(self.p, self.h, good_spec(randomize=["direction", "entry_time"], seed=99), now=m(20))   # an invalid variant also counts as a search
        ro.freeze(self.p, ids[0], now=m(30)); ro.freeze(self.p, ids[1], now=m(31))
        ro.record_result(self.p, ids[0], "POP-a1b2c3", "SUPPORTED", 50, now=m(40))
        ro.record_result(self.p, ids[1], "POP-a1b2c3", "INCONCLUSIVE", 12, now=m(41))
        f = ro.family_summary(self.p, "FAM-gbpusd-breakout")
        self.assertEqual((f["specs_drafted"], f["specs_frozen"], f["results"]), (6, 2, 2))
        self.assertEqual(f["by_status"], {"INCONCLUSIVE": 1, "SUPPORTED": 1})
        early = ro.family_summary(self.p, "FAM-gbpusd-breakout", as_of=m(15))
        self.assertEqual((early["specs_drafted"], early["specs_frozen"]), (5, 0))                  # what the Catalog knew then

    def test_cases_index(self):
        sid, _ = ro.draft_spec(self.p, self.h, good_spec(), now=m(1)); ro.freeze(self.p, sid, now=m(2))
        ro.record_result(self.p, sid, "POP-a1b2c3", "INCONCLUSIVE", 9, now=m(3))
        c = ro.cases(self.p)[0]
        self.assertEqual(c["hypothesis_id"], self.h)
        self.assertEqual(c["experiments"], [{"spec_id": sid, "valid": True, "frozen": True, "status": "INCONCLUSIVE"}])

    def test_statuses_are_about_the_experiment_not_the_strategy(self):
        self.assertIn("INCONCLUSIVE", ro.STATUSES); self.assertIn("DATA_INSUFFICIENT", ro.STATUSES)
        self.assertNotIn("PROFITABLE", ro.STATUSES); self.assertNotIn("EDGE_FOUND", ro.STATUSES)

    def test_the_ledger_stays_verifiable_and_nothing_is_ever_rewritten(self):
        sid, _ = ro.draft_spec(self.p, self.h, good_spec(), now=m(1)); ro.freeze(self.p, sid, now=m(2))
        recs = ledger.read(self.p)[0]
        self.assertEqual(ledger.verify(recs), [])
        self.assertEqual(len([r for r in recs if r["event_type"] == ro.SPEC_FROZEN]), 1)


class Boundary(unittest.TestCase):
    def test_imports_only_ledger_and_stdlib(self):
        import ast
        with open(os.path.join(HERE, "research_objects.py")) as f:
            tree = ast.parse(f.read())
        found = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import): found |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module: found.add(n.module.split(".")[0])
        self.assertLessEqual(found, {"hashlib", "json", "ledger"}, found)

    def test_no_randomness_in_phase_one(self):
        with open(os.path.join(HERE, "research_objects.py")) as f:
            src = f.read()
        self.assertNotIn("import random", src); self.assertNotIn("numpy", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
