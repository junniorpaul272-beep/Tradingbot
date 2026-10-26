"""Run: python test_bus_contract.py  (stdlib only). Includes the eight
communication-integrity tests from the planning docs."""
import os, sys, tempfile, unittest
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger, bus_contract as bc
from bus_contract import Claim, ClaimError, FACT, INTERPRETATION, EXPECTATION

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
m = lambda n: T0 + timedelta(minutes=n)


def fact(subject, value, t=0, auth="engine.state", ref="state.x", **kw):
    return Claim(FACT, subject, value, auth, ref, m(t), m(t), **kw)


def interp(subject, value, t=0, auth="engine.mil", ref="state.mil", **kw):
    return Claim(INTERPRETATION, subject, value, auth, ref, m(t), m(t), **kw)


class ClaimObject(unittest.TestCase):
    def test_no_lineage_no_claim(self):
        with self.assertRaises(ClaimError): Claim(FACT, "htf.bias", "BULLISH", "engine.state", "", T0, T0)
        with self.assertRaises(ClaimError): Claim(FACT, "htf.bias", "BULLISH", "nobody", "state.x", T0, T0)
        with self.assertRaises(ClaimError): Claim(FACT, "made.up.subject", 1, "engine.state", "state.x", T0, T0)

    def test_kinds_are_never_blurred(self):
        with self.assertRaises(ClaimError): Claim(FACT, "thesis.primary", {"d": "BULL"}, "engine.mil", "r", T0, T0)       # mil can't assert FACT
        with self.assertRaises(ClaimError): Claim(INTERPRETATION, "htf.bias", "BULLISH", "engine.state", "r", T0, T0)      # state can't interpret

    def test_expectation_needs_a_machine_checkable_condition(self):
        with self.assertRaises(ClaimError): Claim(EXPECTATION, "thesis.failure_condition", {"level": 1.31}, "engine.mil", "r", T0, T0)
        with self.assertRaises(ClaimError): Claim(EXPECTATION, "thesis.failure_condition", {}, "engine.mil", "r", T0, T0,
                                                  condition={"type": "price_close_below"})
        ok = Claim(EXPECTATION, "thesis.failure_condition", {"level": 1.3199}, "engine.mil", "r", T0, T0,
                   condition={"type": "price_close_below", "level": 1.3199})
        self.assertTrue(ok.claim_id.startswith("c_"))
        with self.assertRaises(ClaimError): fact("htf.bias", "BULLISH", condition={"type": "event_code", "code": "X"})

    def test_cannot_be_known_before_it_happened(self):
        with self.assertRaises(ClaimError): Claim(FACT, "htf.bias", "BULLISH", "engine.state", "r", m(10), m(0))

    def test_claim_id_is_content_hash_and_stable(self):
        a, b = fact("htf.bias", "BULLISH"), fact("htf.bias", "BULLISH")
        self.assertEqual(a.claim_id, b.claim_id)
        self.assertNotEqual(a.claim_id, fact("htf.bias", "BEARISH").claim_id)
        with self.assertRaises(Exception): a.value = "BEARISH"          # frozen

    def test_registered_condition_grammar_matches_the_catalog(self):
        import user_catalog
        self.assertEqual(tuple(bc.CONDITION_TYPES), tuple(user_catalog.CONDITION_TYPES))


class IntegrityTests(unittest.TestCase):
    """The eight tests from the planning docs, against this contract."""

    def test_1_no_unsupported_claim(self):
        a = bc.resolve("current_thesis", [interp("thesis.primary", {"direction": "BULLISH"}),
                                          interp("thesis.status", "SUPPORTED")], m(5))
        self.assertEqual(a.status, "ANSWER")
        self.assertEqual(bc.audit_text("Still bullish, thesis supported.", a), [])
        bad = bc.audit_text("The leg is aging and momentum is fading.", a)
        self.assertEqual(len(bad), 2); self.assertTrue(any("leg.age_status" in x for x in bad))

    def test_2_no_duplicated_interpretation(self):
        # (a) structurally: one subject, one owner — a second authority cannot even construct a competing bias
        with self.assertRaises(ClaimError):
            fact("htf.bias", "BEARISH", auth="engine.leg_owner", ref="b")
        # (b) if the SAME owner contradicts itself at the same instant, it is reported, never silently resolved
        pool = [fact("htf.bias", "BULLISH", ref="a"), fact("htf.bias", "BEARISH", ref="b")]
        a = bc.resolve("what_changed", pool, m(5))
        self.assertEqual(len(a.conflicts), 1)
        self.assertEqual(a.items, ())                    # neither side silently picked
        self.assertEqual(a.status, "NO_EVIDENCE")

    def test_3_number_consistency(self):
        a = bc.resolve("what_would_change_it", [Claim(EXPECTATION, "thesis.failure_condition", {"level": 1.31990}, "engine.mil",
                                                      "r", T0, T0, condition={"type": "price_close_below", "level": 1.3199})], m(1))
        self.assertEqual(bc.audit_text("A close below 1.3199 invalidates it.", a), [])
        self.assertEqual(bc.audit_text("A close below 1.3196 invalidates it.", a)[0], "number 1.3196 appears in the text but in no claim")
        self.assertEqual(bc.audit_text("Watching the 15-minute chart at 09:05 for a close below 1.3199.", a), [])   # timeframe/clock not mistaken for data

    def test_4_temporal_integrity_no_future_and_no_stale_as_current(self):
        late = fact("htf.bias", "BEARISH", t=30)
        early = fact("htf.bias", "BULLISH", t=0)
        a = bc.resolve("what_changed", [early, late], m(10))                 # the Bearish read didn't exist yet
        self.assertEqual([i.claim.value for i in a.items], ["BULLISH"])
        stale = bc.resolve("integrity", [fact("market.data_timestamp", "x", t=0),
                                         Claim(FACT, "system.ledger_integrity", {"ok": True}, "ledger.verify", "r", m(59), m(59))], m(60))
        self.assertTrue(any(x.reason == "stale" for x in stale.missing))     # 60 min old data is not "current"
        with self.assertRaises(ClaimError):                                  # object-level: can't hold a later claim
            bc.Answer("integrity", {}, m(1), (bc.Item("CURRENT", late),), (), (), "ANSWER")
        a_prev = bc.resolve("what_changed", [early, fact("htf.bias", "BEARISH", t=5)], m(10))
        self.assertEqual([i.label for i in a_prev.items], ["PREVIOUS", "CURRENT"])

    def test_5_conversation_continuity_why_needs_a_referent_and_a_basis(self):
        self.assertEqual(bc.resolve("why", [], m(1), params={}).missing[0].reason, "unknown_referent")
        pool = [interp("thesis.primary", {"direction": "BEARISH"})]
        self.assertEqual(bc.resolve("why", pool, m(1), params={"about": "thesis.primary"}).missing[0].reason, "no_basis")
        pool = [interp("thesis.primary", {"direction": "BEARISH"}, basis=("HTF CHoCH at 1.3350",))]
        a = bc.resolve("why", pool, m(1), params={"about": "thesis.primary"})
        self.assertEqual(a.status, "ANSWER")
        self.assertEqual(bc.audit_text("Bearish because of the HTF CHoCH at 1.3350.", a), [])

    def test_6_user_knowledge_is_a_claim_with_evidence_not_an_assumption(self):
        known = Claim(FACT, "user.knows.BOS", {"evidence": "ev-1"}, "catalog.user_ledger", "user_ledger.jsonl#ev-1", T0, T0)
        self.assertEqual(known.subject, "user.knows.BOS")
        self.assertTrue(bc.spec_for("user.knows.BOS")[1].source)            # exists in the registry, sourced

    def test_7_correction_reconcile_against_what_bus_actually_said(self):
        path = os.path.join(tempfile.mkdtemp(), "u.jsonl")
        said = bc.resolve("what_changed", [fact("htf.bias", "BULLISH", t=0)], m(5))
        ledger.append(path, bc.BUS_STATEMENT, bc.statement_payload(said), ts=m(5), now=m(5))
        recs, _ = ledger.read(path)
        now_claim = fact("htf.bias", "BEARISH", t=40)
        r = bc.reconcile("htf.bias", now_claim, recs, m(45))
        self.assertEqual((r["previously_said"], r["changed"]), ("BULLISH", True))
        self.assertEqual(bc.reconcile("htf.bias", now_claim, recs, m(1))["previously_said"], None)   # not yet said at m(1)

    def test_8_silence(self):
        c = fact("htf.bias", "BULLISH")
        a = bc.resolve("what_changed", [c], m(5))
        self.assertEqual(a.items[0].label, "UNCHANGED")
        self.assertEqual(bc.decide_speech(a, "system_event").decision, "NO_MESSAGE")
        empty = bc.resolve("what_changed", [], m(5))
        self.assertEqual(bc.decide_speech(empty, "system_event"), bc.Speech("NO_MESSAGE", "NOTHING_NEW"))
        self.assertEqual(bc.decide_speech(empty, "user_asked").decision, "SPEAK_MISSING")   # asked => never silent
        changed = bc.resolve("what_changed", [c, fact("htf.bias", "BEARISH", t=3)], m(5))
        self.assertEqual(bc.decide_speech(changed, "system_event").decision, "SPEAK")
        told = [i.claim.claim_id for i in changed.items]
        self.assertEqual(bc.decide_speech(changed, "system_event", already_communicated=told).reason, "ALREADY_KNOWN")


class AuditRobustness(unittest.TestCase):
    def test_random_record_ids_never_trip_the_number_check(self):
        """Regression: ~3% of random uuids used to make the audit fall back (the timeframe stripper
        mangled the id and left a phantom negative number)."""
        import uuid, bus
        bad = []
        for _ in range(1500):
            u = str(uuid.uuid4())
            c = Claim(FACT, "system.ledger_integrity", {"ok": False, "records": 10, "problems": [["EDITED", u]], "bad_lines": 0},
                      "ledger.verify", "ledger.verify(x)", T0, T0)
            r = bus.finalize(bc.resolve("integrity", [c], m(1)))
            if r.used_fallback:
                bad.append((u, r.violations))
        self.assertEqual(bad[:2], [])

    def test_an_identifier_not_in_any_claim_is_refused(self):
        a = bc.resolve("current_thesis", [interp("thesis.primary", {"direction": "BULLISH"}), interp("thesis.status", "SUPPORTED")], m(5))
        v = bc.audit_text("Bullish. See record 7d8ac61e-879d-42a4-a331-9aeed85c6ca5.", a)
        self.assertTrue(any("identifier" in x for x in v))

    def test_claim_id_quoted_from_the_answer_is_allowed(self):
        a = bc.resolve("current_thesis", [interp("thesis.primary", {"direction": "BULLISH"}), interp("thesis.status", "SUPPORTED")], m(5))
        self.assertEqual(bc.audit_text(f"Bullish (claim {a.claims[0].claim_id}), supported.", a), [])

    def test_hyphenated_tokens_are_not_numbers(self):
        a = bc.resolve("current_thesis", [interp("thesis.primary", {"direction": "BULLISH"}), interp("thesis.status", "SUPPORTED")], m(5))
        self.assertEqual(bc.audit_text("Bullish, supported (m15-bos, h4-fvg).", a), [])


class GapTests(unittest.TestCase):
    """Missing != false != zero."""

    def gap(self, subject, state, t=0, auth="engine.mil", **kw):
        return bc.Gap(subject, state, auth, "state.x", m(t), m(t), **kw)

    def test_gap_is_validated_like_a_claim(self):
        with self.assertRaises(ClaimError): bc.Gap("made.up", "UNSET", "engine.mil", "r", T0, T0)
        with self.assertRaises(ClaimError): bc.Gap("thesis.primary", "ZERO", "engine.mil", "r", T0, T0)
        with self.assertRaises(ClaimError): bc.Gap("thesis.primary", "UNSET", "engine.state", "r", T0, T0)   # wrong owner
        with self.assertRaises(ClaimError): bc.Gap("thesis.primary", "UNSET", "engine.mil", "", T0, T0)

    def test_each_state_surfaces_as_its_own_reason_never_as_a_value(self):
        for state, reason in (("UNSET", "unset"), ("UNKNOWN", "unknown"), ("NOT_PROVIDED", "not_provided"), ("STALE", "flagged_stale")):
            a = bc.resolve("what_would_change_it", [], m(5), gaps=[self.gap("thesis.failure_condition", state)])
            self.assertEqual(a.items, ())
            self.assertEqual([x.reason for x in a.missing if x.subject == "thesis.failure_condition"], [reason])

    def test_newer_gap_supersedes_older_claim_and_newer_claim_supersedes_older_gap(self):
        cond = lambda t: Claim(EXPECTATION, "thesis.failure_condition", {"level": 1.3199}, "engine.mil", "r", m(t), m(t),
                               condition={"type": "price_close_below", "level": 1.3199})
        a = bc.resolve("what_would_change_it", [cond(0)], m(10), gaps=[self.gap("thesis.failure_condition", "UNSET", t=5)])
        self.assertEqual(a.items, ())                                  # the level was cleared; the old one is not "current"
        a = bc.resolve("what_would_change_it", [cond(8)], m(10), gaps=[self.gap("thesis.failure_condition", "UNSET", t=5)])
        self.assertEqual(len(a.items), 1)

    def test_gap_from_the_future_is_invisible(self):
        a = bc.resolve("what_would_change_it", [], m(1), gaps=[self.gap("thesis.failure_condition", "UNSET", t=30)])
        self.assertEqual([x.reason for x in a.missing if x.subject == "thesis.failure_condition"], ["no_evidence"])


class ContractBehaviour(unittest.TestCase):
    def test_a_missing_required_slot_is_partial_never_guessed(self):
        pool = [interp("thesis.primary", {"d": "BULL"})]
        a = bc.resolve("current_thesis", pool, m(2))
        self.assertEqual(a.status, "PARTIAL")
        self.assertEqual([x.reason for x in a.missing if x.required], ["no_evidence"])   # thesis.status absent, reported

    def test_slot_kind_restriction_blocks_wrong_kind(self):
        # a slot that demands FACT will not accept an INTERPRETATION even for the same subject
        slot = bc.Slot("thesis.primary", (FACT,))
        orig = bc.INTENTS["current_thesis"]
        try:
            bc.INTENTS["_probe"] = bc.Intent("_probe", (slot,))
            a = bc.resolve("_probe", [interp("thesis.primary", {"d": "BULL"})], m(2))
        finally:
            bc.INTENTS.pop("_probe", None)
        self.assertEqual(a.missing[0].reason, "wrong_kind")

    def test_unmapped_subjects_are_reported_as_engine_work_not_filled(self):
        a = bc.resolve("current_market_story", [fact("htf.bias", "BULLISH"), interp("thesis.primary", {"d": "BULL"}),
                                                interp("thesis.status", "SUPPORTED"),
                                                interp("thesis.phase", {"phase": "expansion"}, auth="engine.thesis")], m(2))
        self.assertEqual(a.status, "ANSWER")                       # the unmapped parts are optional slots now...
        reasons = {x.subject: x.reason for x in a.missing}
        self.assertEqual(reasons["story.controller"], "engine_does_not_provide")        # ...but still REPORTED, never filled
        self.assertEqual(reasons["story.counter_attempt"], "engine_does_not_provide")
        self.assertEqual(reasons["momentum.state"], "engine_does_not_provide")
        self.assertTrue(any("story.controller" in x for x in bc.audit_text("Buyers controlled the session.", a)))

    def test_case_intents_keep_machine_and_user_views_separate(self):
        pool = [Claim(FACT, "case.CASE-0001.bot_plan", {"result": "NO_VALID_TRADE"}, "catalog.user_ledger", "u#1", T0, T0),
                Claim(FACT, "case.CASE-0001.user_reasoning", {"text": "looked like a reversal"}, "catalog.user_ledger", "u#2", T0, T0)]
        a = bc.resolve("bot_vs_user", pool, m(1), params={"case_id": "CASE-0001"})
        self.assertEqual(a.status, "ANSWER")
        self.assertEqual({i.claim.subject for i in a.items}, {"case.CASE-0001.bot_plan", "case.CASE-0001.user_reasoning"})
        self.assertFalse(any("compar" in i.claim.subject for i in a.items))        # no comparison claim exists in BUS

    def test_coverage_report_names_the_engine_gaps(self):
        rep = bc.coverage_report()
        self.assertIn("current_market_story", rep["intents_blocked_by_engine_work"])
        self.assertEqual({n for n, _ in rep["unmapped"]},
                         {"thesis.expectations", "momentum.state", "story.controller", "story.counter_attempt"})
        self.assertTrue(any(n == "htf.bias" for n, _ in rep["mapped"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
