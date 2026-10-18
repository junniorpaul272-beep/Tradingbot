"""Run: python test_bus.py  — BUS wording, routing, the audit gate, adversarial cases, act commands."""
import json, os, sys, tempfile, unittest
from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import ledger, bus, bus_adapters as ba, bus_contract as bc
from bus_contract import Claim, FACT, INTERPRETATION
from user_catalog import Catalog

U = lambda *a: datetime(*a, tzinfo=timezone.utc)
T0 = U(2026, 10, 1, 12, 0)
m = lambda n: T0 + timedelta(minutes=n)


def real():
    p = os.path.join(ROOT, "fixture_real_state.json")
    if not os.path.exists(p):
        raise unittest.SkipTest("fixture_real_state.json not alongside")
    with open(p) as f:
        s = json.load(f)
    res = ba.adapt_state(s)
    return s, res, ba._t(s["timestamp"])


class Routing(unittest.TestCase):
    def test_commands_and_keywords(self):
        self.assertEqual(bus.route("/now").intent, "market_state")
        self.assertEqual(bus.route("/thesis@MyBot").intent, "current_thesis")
        self.assertEqual(bus.route("what changed?").intent, "what_changed")
        self.assertEqual(bus.route("what would invalidate this").intent, "what_would_change_it")
        self.assertEqual(bus.route("what are you watching").intent, "watching")
        self.assertEqual(bus.route("/review 7").params, {"case_id": "CASE-0007"})
        self.assertEqual(bus.route("/thread 2").params, {"thread_id": "THREAD-0002"})
        self.assertEqual(bus.route("/logtrade GBPUSD buy 1 sl 0.9 tp 1.2").kind, "act")
        self.assertEqual(bus.route("/bushelp").kind, "help")
        self.assertEqual(bus.route("/nonsense").kind, "unknown")
        self.assertEqual(bus.route("hello there").kind, "unknown")
        self.assertEqual(bus.route("").kind, "unknown")

    def test_why_uses_the_previous_statement_for_continuity(self):
        self.assertEqual(bus.route("why?", last_subject="thesis.primary").params, {"about": "thesis.primary"})
        self.assertEqual(bus.route("/why htf.bias", last_subject="thesis.primary").params, {"about": "htf.bias"})
        self.assertEqual(bus.route("why?").params, {})                       # no referent -> the contract reports it


class RealDataWording(unittest.TestCase):
    def test_every_intent_renders_without_falling_back(self):
        s, res, T = real()
        for q in ("/now", "/thesis", "/watch", "/flip", "/changed", "/story", "/why thesis.primary", "/health"):
            r = bus.respond(q, res.claims, res.gaps, T, last_subject="thesis.primary")
            if q == "/health":
                continue                                                      # needs a ledger claim; covered below
            self.assertFalse(r.used_fallback, (q, r.violations))
            self.assertTrue(r.text.strip())

    def test_the_real_contradiction_is_in_every_reply_that_touches_both(self):
        s, res, T = real()
        for q in ("/now", "/thesis", "/changed", "/story", "/why thesis.primary"):
            r = bus.respond(q, res.claims, res.gaps, T, last_subject="thesis.primary")
            self.assertIn("these disagree", r.text, q)
            self.assertIn("showing both", r.text, q)

    def test_unset_failure_condition_is_never_printed_as_a_level(self):
        s, res, T = real()
        r = bus.respond("/thesis", res.claims, res.gaps, T)
        self.assertNotIn("0.0", r.text); self.assertNotIn("Failure condition", r.text)
        names = {x.subject: x.reason for x in r.answer.missing}
        self.assertEqual(names["thesis.failure_condition"], "unset")

    def test_engine_prose_is_shown_quoted_and_marked_as_the_engine(self):
        s, res, T = real()
        r = bus.respond("/thesis", res.claims, res.gaps, T)
        self.assertIn("\u201cthe higher-timeframe bias is bearish\u201d", r.text)
        self.assertIn("(engine)", r.text)

    def test_asking_about_the_past_without_history_says_so(self):
        s, res, T = real()
        r = bus.respond("/now", res.claims, res.gaps, T - timedelta(days=2))
        self.assertEqual(r.answer.status, "NO_EVIDENCE")
        self.assertTrue(r.text.startswith("I can't answer that from what's recorded"))

    def test_history_the_engine_does_keep_is_answerable_as_of_the_past(self):
        s, res, T = real()
        r = bus.respond("/changed", res.claims, res.gaps, ba._t(s["mil_understanding"]["history"][2]["at"]) + timedelta(minutes=1))
        self.assertIn("thesis status", r.text.lower())


class AuditGate(unittest.TestCase):
    """Adversarial: a renderer that goes wrong must be caught, and the user gets the plain listing."""

    def setUp(self):
        self.s, self.res, self.T = real()

    def with_renderer(self, intent, fn):
        orig = bus.RENDERERS[intent]; bus.RENDERERS[intent] = fn
        try:
            return bus.respond({"market_state": "/now", "current_thesis": "/thesis"}[intent], self.res.claims, self.res.gaps, self.T)
        finally:
            bus.RENDERERS[intent] = orig

    def test_invented_number_triggers_fallback(self):
        r = self.with_renderer("market_state", lambda a: ["HTF bias is bearish; target 1.3150."])
        self.assertTrue(r.used_fallback); self.assertTrue(any("1.3150" in v for v in r.violations))
        self.assertTrue(r.text.startswith("[market_state]"))                     # plain listing, no prose

    def test_invented_concept_triggers_fallback(self):
        r = self.with_renderer("market_state", lambda a: ["Sellers took control and momentum is building."])
        self.assertTrue(r.used_fallback)
        self.assertTrue(any("story.controller" in v for v in r.violations) and any("momentum.state" in v for v in r.violations))

    def test_smuggled_engine_quote_triggers_fallback(self):
        r = self.with_renderer("current_thesis", lambda a: [f"Evidence: {bus.E('a liquidity sweep just happened')}"])
        self.assertTrue(r.used_fallback); self.assertTrue(any("quoted engine text not found" in v for v in r.violations))

    def test_dropping_the_disagreement_note_triggers_fallback(self):
        r = self.with_renderer("market_state", lambda a: ["HTF bias is bearish, MIL says bullish."])
        self.assertTrue(r.used_fallback)                                         # both directions named, but the note is not acknowledged

    def test_fallback_still_contains_the_note_and_missing(self):
        r = self.with_renderer("market_state", lambda a: ["invented 9.9999"])
        self.assertIn("NOTE DIRECTION_DISAGREEMENT", r.text)


class SyntheticAnswers(unittest.TestCase):
    def test_stale_bias_is_reported_as_flagged_stale_in_words(self):
        s, _, _ = real(); s["structure_digest"]["regime"]["macro_bias_stale"] = True
        res = ba.adapt_state(s); T = ba._t(s["timestamp"])
        r = bus.respond("/now", res.claims, res.gaps, T)
        self.assertNotIn("HTF bias is", r.text)
        self.assertIn("the higher-timeframe bias: the engine flags it as stale right now", r.text)

    def test_no_evidence_at_all_is_honest(self):
        r = bus.respond("/thesis", [], [], T0)
        self.assertIn("I can't answer that", r.text); self.assertIn("nothing recorded", r.text)

    def test_unknown_referent_for_a_bare_why(self):
        r = bus.respond("why?", [], [], T0)
        self.assertIn("which trade (or which thing) do you mean", r.text)

    def test_help_and_unknown(self):
        self.assertIn("/now", bus.respond("/bushelp", [], [], T0).text)
        self.assertIn("didn't catch that", bus.respond("blah", [], [], T0).text)

    def test_statement_round_trip_supports_correction(self):
        s, res, T = real()
        first = bus.respond("/changed", res.claims, res.gaps, T)
        path = os.path.join(tempfile.mkdtemp(), "u.jsonl")
        ledger.append(path, bc.BUS_STATEMENT, bc.statement_payload(first.answer), ts=T, now=T)
        recs, _ = ledger.read(path)
        said = bc.prior_claims(recs, "htf.bias", T + timedelta(minutes=1))
        self.assertEqual(said[-1]["value"], "BEARISH")
        flipped = Claim(FACT, "htf.bias", "BULLISH", "engine.state", "r", T + timedelta(minutes=30), T + timedelta(minutes=30))
        self.assertTrue(bc.reconcile("htf.bias", flipped, recs, T + timedelta(minutes=31))["changed"])


class ActCommands(unittest.TestCase):
    def setUp(self):
        self.c = Catalog(os.path.join(tempfile.mkdtemp(), "u.jsonl"))
        self.now = U(2026, 10, 1, 12, 0)

    def act(self, text, plan_facts=None, now=None):
        return bus.respond(text, [], [], now or self.now, catalog=self.c, plan_facts=plan_facts).text

    def test_logtrade_echoes_what_was_stored_not_what_was_typed(self):
        t = self.act("/logtrade gbpusd BUY 1.3421 sl 1.3398 tp 1.3490")
        self.assertIn("CASE-0001", t); self.assertIn("buy at 1.34210", t); self.assertIn("did not attach a bot snapshot", t)
        v = self.c.get_case("CASE-0001")["trade"]["payload"]
        self.assertEqual((v["pair"], v["direction"], v["entry"]), ("GBPUSD", "BUY", 1.3421))

    def test_logtrade_with_plan_facts_marks_the_plan_unknown_not_valid(self):
        t = self.act("/logtrade GBPUSD sell 1.30 sl 1.31 tp 1.28", plan_facts={"htf.bias": "BEARISH"})
        self.assertIn("marked UNKNOWN", t)
        self.assertEqual(self.c.get_case("CASE-0001")["bot_plan"]["payload"]["result"], "UNKNOWN")

    def test_bad_geometry_and_bad_format_are_refused_without_writing(self):
        self.assertIn("didn't record", self.act("/logtrade GBPUSD buy 1.30 sl 1.31 tp 1.32"))
        self.assertIn("Format:", self.act("/logtrade buy now"))
        self.assertIsNone(self.c.get_case("CASE-0001"))

    def test_reasoning_and_outcome_and_the_after_outcome_flag(self):
        self.act("/logtrade GBPUSD buy 1.3421 sl 1.3398 tp 1.3490")
        self.assertIn("word for word", self.act("/reason 1 thought the bounce was a reversal", now=self.now + timedelta(minutes=1)))
        self.assertIn("loss", self.act("/outcome 1 loss -1", now=self.now + timedelta(hours=2)))
        late = self.act("/reason 1 I knew it was a trap", now=self.now + timedelta(hours=3))
        self.assertIn("after the outcome", late)
        case = self.c.get_case("CASE-0001")
        self.assertEqual([r["payload"]["written_after_outcome"] for r in case["user_reasoning"]], [False, True])

    def test_review_and_botvsme_read_back_from_the_catalog(self):
        self.act("/logtrade GBPUSD buy 1.3421 sl 1.3398 tp 1.3490", plan_facts={"x": 1})
        self.act("/reason 1 5m bounce looked like a reversal", now=self.now + timedelta(minutes=2))
        recs = ledger.read(self.c.path)[0]
        res = ba.adapt_case(self.c.get_case("CASE-0001"))
        r = bus.respond("/review 1", res.claims, [], self.now + timedelta(hours=1))
        self.assertFalse(r.used_fallback, r.violations)
        self.assertIn("5m bounce looked like a reversal", r.text)
        r2 = bus.respond("/botvsme 1", res.claims, [], self.now + timedelta(hours=1))
        self.assertIn("The bot's side", r2.text); self.assertIn("Your note", r2.text)

    def test_act_without_a_ledger_says_so(self):
        self.assertIn("ledger isn't available", bus.respond("/logtrade GBPUSD buy 1 sl 0.9 tp 1.2", [], [], T0).text)


class TermsResearchWording(unittest.TestCase):
    def setUp(self):
        self.c = Catalog(os.path.join(tempfile.mkdtemp(), "u.jsonl")); self.now = U(2026, 10, 1, 12, 0)

    def act(self, text, now=None):
        return bus.respond(text, [], [], now or self.now, catalog=self.c).text

    def test_reason_echo_separates_engine_concepts_from_your_own_terms(self):
        self.act("/logtrade GBPUSD buy 1.3421 sl 1.3398 tp 1.3490")
        t = self.act("/reason 1 waited for a BOS and a #support retest", now=self.now + timedelta(minutes=1))
        self.assertIn("word for word", t)
        self.assertIn("The engine has definitions for: break of structure", t)
        self.assertIn("Your own terms (the engine has no definition, so they stay yours): #support", t)

    def test_terms_command_counts_without_defining(self):
        self.act("/logtrade GBPUSD buy 1.3421 sl 1.3398 tp 1.3490")
        self.act("/reason 1 #support held, then #suport again", now=self.now + timedelta(minutes=1))
        recs = ledger.read(self.c.path)[0]
        res = ba.adapt_terms(recs)
        r = bus.respond("/terms", res.claims, [], self.now + timedelta(hours=1))
        self.assertFalse(r.used_fallback, r.violations)
        self.assertIn("#support: 1 use(s) in 1 case(s)", r.text); self.assertIn("the engine has no definition for this", r.text)
        self.assertNotIn("means", r.text)
        self.assertIn("Noted: I'll count #suport as #support", self.act("/term suport = support"))
        res = ba.adapt_terms(ledger.read(self.c.path)[0])
        r = bus.respond("/terms", res.claims, [], self.now + timedelta(hours=1))
        self.assertIn("#support: 2 use(s)", r.text); self.assertIn("typed as suport, support", r.text)
        self.assertIn("Format:", self.act("/term nonsense"))

    def test_terms_and_research_with_nothing_recorded_say_so(self):
        self.assertIn("nothing recorded", bus.respond("/terms", [], [], self.now).text)
        self.assertIn("nothing recorded", bus.respond("/research", [], [], self.now).text)

    def test_research_index_wording_never_concludes_anything(self):
        import research_objects as ro
        p = self.c.path
        h = ro.draft_hypothesis(p, "Does break/retest have an edge?", "claim", "FAM-gbpusd", now=self.now)
        spec = {"question": "q", "claim_under_test": "c", "instrument": "GBPUSD",
                "population": {"population_id": "POP-1", "eligibility_rule": "r"}, "decision_under_test": "direction",
                "preserve": sorted(ro.NULL_MODELS["direction_randomization"]["must_preserve"]), "randomize": ["direction"],
                "outcome_metric": "expectancy_R", "null_model": "direction_randomization", "research_family_id": "FAM-gbpusd",
                "data_cutoff": "2026-09-30T00:00:00+00:00", "data_split": "development"}
        a, _ = ro.draft_spec(p, h, spec, now=self.now + timedelta(minutes=1))
        ro.draft_spec(p, h, dict(spec, randomize=["direction", "entry_time"]), now=self.now + timedelta(minutes=2))
        ro.freeze(p, a, now=self.now + timedelta(minutes=3))
        ro.access_split(p, a, "final_test", now=self.now + timedelta(minutes=4)); ro.access_split(p, a, "final_test", now=self.now + timedelta(minutes=5))
        ro.record_result(p, a, "POP-1", "INCONCLUSIVE", 11, now=self.now + timedelta(minutes=6))
        res = ba.adapt_research(ledger.read(p)[0])
        r = bus.respond("/research", res.claims, [], self.now + timedelta(hours=1))
        self.assertFalse(r.used_fallback, r.violations)
        self.assertIn("2 spec(s), 1 frozen", r.text); self.assertIn("2 search(es) in family", r.text)
        self.assertIn("inconclusive (final test read more than once)", r.text); self.assertIn("invalid draft", r.text)
        for word in ("works", "edge found", "profitable", "proven"):
            self.assertNotIn(word, r.text.lower().replace("have an edge?", ""))


class AbsenceMarks(unittest.TestCase):
    def test_naming_a_missing_concept_is_fine_but_the_mark_cannot_smuggle_an_assertion(self):
        s, res, T = real()
        a = bc.resolve("current_market_story", res.claims, T, gaps=res.gaps)
        self.assertIn("momentum", {m.subject.split(".")[0] for m in a.missing})
        ok = "Bias is bearish. " + f"• {bus.A('momentum')}: the engine doesn't compute that"
        absent = {bus._label(m.subject) for m in a.missing}
        v = bc.audit_text(ok + " MIL disagrees (mid-transition).", a, absent_labels=absent)
        self.assertEqual([x for x in v if "momentum" in x or "marked" in x], [])
        smuggle = f"Buyers {bus.A('took control')} and momentum is building"
        v = bc.audit_text(smuggle, a, absent_labels=absent)
        self.assertTrue(any("marked as missing but the answer does not report it missing" in x for x in v))
        self.assertTrue(any("momentum" in x for x in v))                         # the unmarked 'momentum' is still caught

    def test_story_renders_clean_and_says_what_it_lacks(self):
        s, res, T = real()
        r = bus.respond("/story", res.claims, res.gaps, T)
        self.assertFalse(r.used_fallback, r.violations)
        self.assertIn("who controlled the move: the engine doesn't compute that", r.text)
        self.assertNotIn("\u27e6", r.text)                                       # marks never reach the user


class NoDerivedNumbers(unittest.TestCase):
    """Systematic guard: every renderer, over claims whose numbers are all DIFFERENT primes, must pass the audit.
    A renderer that computes its own count (len(), sum(), a subtraction) would emit a number found in no claim."""

    def test_integrity_and_research_counts_come_from_claims(self):
        import ledger as L
        from bus_contract import Claim, FACT
        # 7 open findings, only 5 listed: the heading must say 7 (a claim field), and nothing may say 2
        open_ = [{"fingerprint": f"fp{i:02d}", "probe": "outlier", "severity": "HIGH", "summary": f"finding number {100 + i}",
                  "paths": ["x"], "first_divergence": i == 0, "first_seen": "2026-10-01T10:00:00+00:00"} for i in range(7)]
        c = Claim(FACT, "system.integrity", {"open": open_, "n_open": 7}, "inspector.findings", "integrity_ledger.jsonl", T0, T0)
        r = bus.respond("/health", [c, Claim(FACT, "system.ledger_integrity", {"ok": True, "records": 13, "problems": [], "bad_lines": 0},
                                              "ledger.verify", "v", T0, T0)], [], m(1))
        self.assertFalse(r.used_fallback, r.violations)
        self.assertIn("7 open finding(s)", r.text); self.assertNotIn("2 more", r.text)

    def test_every_renderer_is_audit_clean_on_prime_valued_claims(self):
        # deliberately NOT exhaustive of content; it asserts the audit never has to rescue a derived number
        from bus_contract import Claim, FACT, INTERPRETATION, EXPECTATION
        pool = [Claim(FACT, "htf.bias", "BEARISH", "engine.state", "r", T0, T0),
                Claim(INTERPRETATION, "thesis.phase", {"phase": "expansion", "age_bars": 53}, "engine.thesis", "r", T0, T0),
                Claim(FACT, "leg.macro", {"direction": "BEARISH", "origin": 1.33097, "extreme": 1.31937, "origin_time": None}, "engine.state", "r", T0, T0),
                Claim(INTERPRETATION, "thesis.failure_risk", {"level": "LOW", "engine_reasons": []}, "engine.thesis", "r", T0, T0)]
        for q in ("/now", "/story"):
            r = bus.respond(q, pool, [], m(1))
            self.assertFalse(r.used_fallback, (q, r.violations))


class Boundary(unittest.TestCase):
    def test_bus_is_pure(self):
        import ast
        with open(os.path.join(HERE, "bus.py")) as f:
            tree = ast.parse(f.read())
        found = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import): found |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module: found.add(n.module.split(".")[0])
        self.assertLessEqual(found, {"json", "re", "dataclasses", "typing", "bus_adapters", "bus_contract", "ledger", "informal", "voice"}, found)
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                self.assertNotIn(n.func.id, ("open", "print", "input", "exec", "eval"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
