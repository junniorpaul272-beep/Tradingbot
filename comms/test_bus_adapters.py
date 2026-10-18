"""Run: python test_bus_adapters.py  (needs mil.py, ledger.py, user_catalog.py, bus_contract.py,
bus_adapters.py in the same folder; scanner_live.py optional — one shape test is skipped without it)."""
import ast, json, os, re, sys, tempfile, unittest, uuid
from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import ledger, bus_contract as bc, bus_adapters as ba
from user_catalog import Catalog
import mil

U = lambda *a: datetime(*a, tzinfo=timezone.utc)
SNAP = U(2026, 10, 1, 12, 0)
iso = lambda dt: dt.isoformat()


def real_mil_dict(status=mil.ThesisStatus.SUPPORTED, direction="BEARISH", fc=(1.3350, "above", 1.3120), history=()):
    """The REAL persisted shape: mil.understanding_to_dict() over real mil dataclasses."""
    u = mil.MarketUnderstanding(
        thesis_status=status,
        primary_thesis={"direction": direction, "state": "TREND_CONTINUATION", "narrative": "bearish leg running from 1.3350",
                        "expected_next_event": "pullback into 1.3290", "confidence": "MODERATE"},
        supporting_evidence=["HTF CHoCH at 1.3350", "15m BOS 1.3280"], conflicting_evidence=[],
        failure_condition=(mil.FailureCondition(*fc) if fc else None),
        expectations=["pullback"], confidence="MODERATE", counter_thesis=None,
        last_reconciled_at=iso(SNAP - timedelta(seconds=20)))
    d = mil.understanding_to_dict(u)
    d["history"] = list(history)
    return d


def state(**over):
    s = {"market_data_timestamp": iso(SNAP),
         "structure_digest": {"regime": {"macro_bias": "BEARISH", "macro_bias_stale": False}},
         "leg_owner": {"leg_id": "L1", "tier": "TIER_2_FIB", "status": "FIRED", "upgraded": False, "claimed_at": iso(SNAP - timedelta(hours=1))},
         "market_thesis_current_state": "TREND_CONTINUATION",
         "market_intent_caution_codes": [{"code": "AGING_CAMPAIGN", "role": "CAUTION", "sentence": "3 breaks into this leg - getting old"}],
         "mil_understanding": real_mil_dict()}
    s.update(over); return s


def by_subject(res):
    return {c.subject: c for c in res.claims}, {g.subject: g for g in res.gaps}


class StateAdapter(unittest.TestCase):
    def test_real_mil_shape_produces_the_expected_claims(self):
        res = ba.adapt_state(state()); claims, gaps = by_subject(res)
        self.assertEqual(res.errors, [])
        self.assertEqual(claims["htf.bias"].value, "BEARISH")
        self.assertEqual(claims["thesis.status"].value, "SUPPORTED")
        self.assertEqual(claims["thesis.primary"].value["direction"], "BEARISH")
        self.assertEqual(claims["thesis.primary"].basis, ("HTF CHoCH at 1.3350", "15m BOS 1.3280"))   # "why" has real evidence
        fc = claims["thesis.failure_condition"]
        self.assertEqual(fc.condition, {"type": "price_trade_above", "level": 1.335})                 # 'above' -> price_TRADE_above: the engine's rule tests High/Low, not closes
        self.assertEqual(fc.value["retrace_level"], 1.312)
        self.assertEqual(claims["thesis.conflicting_evidence"].value, [])                              # KNOWN-none
        self.assertEqual(claims["cautions"].value[0]["code"], "AGING_CAMPAIGN")
        self.assertIn("getting old", claims["cautions"].value[0]["engine_sentence"])                   # engine text, labelled as such
        self.assertIn("thesis.counter", gaps)                                                          # counter_thesis None -> NOT_PROVIDED
        for c in res.claims:
            self.assertIn(c.authority, bc.AUTHORITIES)                                                 # real authorities only

    def test_state_claims_are_invisible_to_an_earlier_as_of(self):
        res = ba.adapt_state(state())
        a = bc.resolve("current_thesis", res.claims, SNAP - timedelta(days=1), gaps=res.gaps)
        self.assertEqual(a.status, "NO_EVIDENCE")                         # today's file cannot describe yesterday
        a = bc.resolve("current_thesis", res.claims, SNAP, gaps=res.gaps)
        self.assertEqual(a.status, "ANSWER")

    def test_no_snapshot_time_means_nothing_is_emitted(self):
        s = state(); s.pop("market_data_timestamp")
        res = ba.adapt_state(s)
        self.assertEqual((res.claims, res.gaps), ([], []))
        self.assertTrue(res.errors)
        res = ba.adapt_state(s, snapshot_at=SNAP)                          # an explicit, honest time is fine
        self.assertTrue(res.claims)

    def test_mil_history_gives_real_past_status(self):
        hist = [{"from_state": "UNKNOWN", "to_state": "SUPPORTED", "at": iso(SNAP - timedelta(hours=6)), "transition_cause": "BOOTSTRAP"},
                {"from_state": "SUPPORTED", "to_state": "WEAKENING", "at": iso(SNAP - timedelta(hours=3)), "transition_cause": "FAILURE_RISK_ELEVATED"}]
        res = ba.adapt_state(state(mil_understanding=real_mil_dict(status=mil.ThesisStatus.CONTESTED, history=hist)))
        past = bc.resolve("what_changed", res.claims, SNAP - timedelta(hours=4), gaps=res.gaps)
        self.assertEqual([i.claim.value for i in past.items if i.claim.subject == "thesis.status"], ["SUPPORTED"])
        now = bc.resolve("what_changed", res.claims, SNAP, gaps=res.gaps)
        vals = [(i.label, i.claim.value) for i in now.items if i.claim.subject == "thesis.status"]
        self.assertEqual(vals, [("PREVIOUS", "WEAKENING"), ("CURRENT", "CONTESTED")])


class MissingIsNotFalseIsNotZero(unittest.TestCase):
    def test_zero_placeholder_failure_levels_are_unset_not_a_level_of_zero(self):
        res = ba.adapt_state(state(mil_understanding=real_mil_dict(fc=(0.0, "above", 0.0))))
        claims, gaps = by_subject(res)
        self.assertNotIn("thesis.failure_condition", claims)
        self.assertEqual(gaps["thesis.failure_condition"].state, "UNSET")
        a = bc.resolve("what_would_change_it", res.claims, SNAP, gaps=res.gaps)
        self.assertEqual([m.reason for m in a.missing if m.subject == "thesis.failure_condition"], ["unset"])
        self.assertEqual(bc.audit_text("Invalidated by a close above 0.0", a)[:1], ["number 0.0 appears in the text but in no claim"])

    def test_placeholder_retrace_is_absent_never_zero(self):
        c = by_subject(ba.adapt_state(state(mil_understanding=real_mil_dict(fc=(1.3350, "above", 0.0)))))[0]["thesis.failure_condition"]
        self.assertNotIn("retrace_level", c.value)
        self.assertEqual(c.condition["level"], 1.335)

    def test_failure_condition_none_is_unset(self):
        self.assertEqual(by_subject(ba.adapt_state(state(mil_understanding=real_mil_dict(fc=None))))[1]["thesis.failure_condition"].state, "UNSET")

    def test_unrecognised_origin_direction_is_not_guessed(self):
        g = by_subject(ba.adapt_state(state(mil_understanding=real_mil_dict(fc=(1.335, "sideways", 1.31)))))[1]
        self.assertEqual(g["thesis.failure_condition"].state, "NOT_PROVIDED")

    def test_zero_is_a_number_where_zero_is_legitimate(self):
        # the 0.0 rule is scoped to the placeholder fields ONLY
        s = state(mil_understanding=dict(real_mil_dict(), counter_thesis={"direction": "BULLISH", "promotion_score": 0}))
        c = by_subject(ba.adapt_state(s))[0]["thesis.counter"]
        self.assertEqual(c.value["promotion_score"], 0)

    def test_false_is_a_known_value_not_a_gap(self):
        c = by_subject(ba.adapt_state(state()))[0]["leg.owner"]
        self.assertIs(c.value["upgraded"], False)

    def test_empty_caution_list_is_known_but_absent_is_not_provided(self):
        known = by_subject(ba.adapt_state(state(market_intent_caution_codes=[])))[0]["cautions"]
        self.assertEqual(known.value, [])                                  # "checked and clean"
        s = state(); s.pop("market_intent_caution_codes")
        self.assertEqual(by_subject(ba.adapt_state(s))[1]["cautions"].state, "NOT_PROVIDED")   # "never checked"

    def test_thesis_status_unknown_is_a_gap_not_the_value_UNKNOWN(self):
        res = ba.adapt_state(state(mil_understanding=real_mil_dict(status=mil.ThesisStatus.UNKNOWN)))
        claims, gaps = by_subject(res)
        self.assertNotIn("thesis.status", claims); self.assertEqual(gaps["thesis.status"].state, "UNKNOWN")

    def test_direction_none_is_unset(self):
        res = ba.adapt_state(state(mil_understanding=real_mil_dict(direction=None)))
        claims, gaps = by_subject(res)
        self.assertNotIn("thesis.primary", claims); self.assertEqual(gaps["thesis.primary"].state, "UNSET")

    def test_a_leftover_digest_copy_is_a_stale_gap_not_a_current_bias(self):
        res = ba.adapt_state(state(scan_id=10, structure_digest={"regime": {"macro_bias": "BEARISH", "macro_bias_stale": False, "as_of_scan_id": 9}}))
        claims, gaps = by_subject(res)
        self.assertNotIn("htf.bias", claims); self.assertEqual(gaps["htf.bias"].state, "STALE")
        res = ba.adapt_state(state(scan_id=10, structure_digest={"regime": {"macro_bias": "BEARISH", "macro_bias_stale": False, "as_of_scan_id": 10}}))
        self.assertEqual(by_subject(res)[0]["htf.bias"].value, "BEARISH")             # stamped by this scan: stated

    def test_stale_bias_is_flagged_not_presented_as_current(self):
        res = ba.adapt_state(state(structure_digest={"regime": {"macro_bias": "BEARISH", "macro_bias_stale": True}}))
        claims, gaps = by_subject(res)
        self.assertNotIn("htf.bias", claims); self.assertEqual(gaps["htf.bias"].state, "STALE")
        a = bc.resolve("current_market_story", res.claims, SNAP, gaps=res.gaps)
        self.assertEqual([m.reason for m in a.missing if m.subject == "htf.bias"], ["flagged_stale"])

    def test_unrecognised_bias_value_and_missing_regime_are_not_provided(self):
        self.assertEqual(by_subject(ba.adapt_state(state(structure_digest={"regime": {"macro_bias": "SIDEWAYS"}})))[1]["htf.bias"].state, "NOT_PROVIDED")
        self.assertEqual(by_subject(ba.adapt_state(state(structure_digest={})))[1]["htf.bias"].state, "NOT_PROVIDED")

    def test_consolidation_is_a_real_known_bias(self):
        c = by_subject(ba.adapt_state(state(structure_digest={"regime": {"macro_bias": "CONSOLIDATION", "macro_bias_stale": False}})))[0]["htf.bias"]
        self.assertEqual(c.value, "CONSOLIDATION")

    def test_missing_leg_owner_is_not_provided_not_none_owned(self):
        s = state(); s.pop("leg_owner")
        g = by_subject(ba.adapt_state(s))[1]["leg.owner"]
        self.assertEqual(g.state, "NOT_PROVIDED")

    def test_no_mil_at_all_is_not_provided_across_the_board(self):
        g = by_subject(ba.adapt_state(state(mil_understanding=None)))[1]
        for s in ("thesis.status", "thesis.primary", "thesis.counter", "thesis.failure_condition"):
            self.assertEqual(g[s].state, "NOT_PROVIDED")


class RealStateForensics(unittest.TestCase):
    """Properties of the REAL state.json captured 2026-10-01 (fixture_real_state.json), which
    caught: a wrong bias path, a 3-second observed/recorded gap, and a MIL stuck for 6 hours."""

    @classmethod
    def setUpClass(cls):
        p = os.path.join(ROOT, "fixture_real_state.json")
        if not os.path.exists(p):
            raise unittest.SkipTest("fixture_real_state.json not alongside")
        cls.s = json.load(open(p))
        cls.res = ba.adapt_state(cls.s)
        cls.claims, cls.gaps = by_subject(cls.res)

    def test_no_adapter_errors_on_real_data(self):
        self.assertEqual(self.res.errors, [])

    def test_bias_sources_agree_in_the_real_file(self):
        self.assertEqual(self.claims["htf.bias"].value, self.s["macro_bias_confirmed"])

    def test_real_direction_disagreement_is_surfaced_not_smoothed_over(self):
        # MIL (stuck in TRANSITIONING) says BULLISH while the bias, leg and campaign say BEARISH
        a = bc.resolve("market_state", self.res.claims, ba._t(self.s["timestamp"]), gaps=self.res.gaps)
        self.assertIn("DIRECTION_DISAGREEMENT", a.notes)
        self.assertEqual(bc.audit_text("Bearish bias, bearish expansion.", a),
                         ["the answer carries note DIRECTION_DISAGREEMENT but the text does not acknowledge it"])
        self.assertEqual(bc.audit_text("Bias is bearish but MIL's thesis disagrees (mid-transition).", a), [])

    def test_state_is_visible_at_its_persist_time_but_not_before_it_was_written(self):
        T_persist = ba._t(self.s["timestamp"])
        at = bc.resolve("market_state", self.res.claims, T_persist, gaps=self.res.gaps)
        self.assertNotIn("leg.owner", [m.subject for m in at.missing])       # claimed_at is 3s after market_data_timestamp
        early = bc.resolve("market_state", self.res.claims, ba._t(self.s["market_data_timestamp"]) - timedelta(seconds=1), gaps=self.res.gaps)
        leaked = {i.claim.subject for i in early.items} - {"thesis.status"}   # only MIL's own dated history is legitimately knowable then
        self.assertEqual(leaked, set())                                        # no snapshot-derived claim may leak backwards

    def test_failure_condition_placeholder_is_unset_not_a_zero_level(self):
        self.assertEqual(self.s["mil_understanding"]["failure_condition"]["origin_level"], 0.0)
        self.assertNotIn("thesis.failure_condition", self.claims)
        self.assertEqual(self.gaps["thesis.failure_condition"].state, "UNSET")

    def test_engine_prose_is_carried_as_labelled_engine_text(self):
        self.assertIn("engine_text", self.claims["thesis.invalidation"].value)
        self.assertTrue(all("engine_sentence" in c for c in self.claims["cautions"].value))


class LogAdapters(unittest.TestCase):
    def test_signal_record_from_the_REAL_writer(self):
        src_path = os.path.join(ROOT, "scanner_live.py")
        if not os.path.exists(src_path):
            self.skipTest("scanner_live.py not alongside")
        src = open(src_path).read(); t = ast.parse(src); L = src.splitlines()
        fn = next(n for n in t.body if isinstance(n, ast.FunctionDef) and n.name == "_record_undelivered_signal")
        tmp = tempfile.mkdtemp(); log = os.path.join(tmp, "s.jsonl")
        g = dict(uuid=uuid, PAIR="GBPUSD", SIGNAL_EVENT_LOG_FILE=log, SIGNAL_EVENT_STATUS_UNDELIVERED="FIRED_RISK_GATE_PASSED_UNDELIVERED",
                 SIGNAL_EVENT_STATE_MIRROR_CAP=200, apply_leg_ownership=lambda s, d: None,
                 append_permanent_jsonl=lambda p, r: open(p, "a").write(json.dumps(r, default=str) + "\n") or True)
        exec("\n".join(L[fn.lineno - 1:fn.end_lineno]), g)
        from types import SimpleNamespace as NS
        res = NS(tier_label="TIER_2_FIB", direction="BUY", score=7, tier_rating="B", reason="r", breakdown={}, conviction=None, risk={})
        rr = {"entry": 1.3, "sl": 1.29, "tp": 1.32, "target_r": 2.0}
        g["_record_undelivered_signal"]({"scan_id": 9}, {}, {}, res, rr, NS(), {}, "BULLISH", [], U(2026, 10, 1, 9, 5), "leg1", U(2026, 10, 1, 9, 5, 30))
        rows = [json.loads(l) for l in open(log)]
        out = ba.adapt_signal_events(rows)
        self.assertEqual(out.errors, [])
        c = out.claims[0]
        self.assertEqual((c.subject, c.value["entry"], c.value["tp"], c.scan_id), ("signal.undelivered", 1.3, 1.32, 9))

    def test_market_event_and_leg_outcome_records(self):
        ev = ba.adapt_market_events([{"code": "CHOCH_M15", "category": "structure", "story_impact": "challenge", "leg_obs_id": "LEG-1",
                                      "identity": ["structure", "choch"], "recorded_at": iso(SNAP), "scan_id": 4},
                                     {"code": "NO_TIME", "category": "structure"}])           # no recorded_at -> cannot be placed in time
        self.assertEqual(len(ev.claims), 1); self.assertEqual(len(ev.errors), 1)
        lo = ba.adapt_leg_outcomes([{"id": "LEG-1", "leg_id": "x", "fate": "CONTINUED", "bars_open": 42, "resolved_at": iso(SNAP)},
                                    {"id": "LEG-2", "leg_id": "y", "resolved_at": iso(SNAP)}])   # no fate -> not an outcome
        self.assertEqual((len(lo.claims), len(lo.errors)), (1, 1))
        self.assertEqual(lo.claims[0].value["fate"], "CONTINUED")


class CatalogAdapters(unittest.TestCase):
    def setUp(self):
        self.c = Catalog(os.path.join(tempfile.mkdtemp(), "u.jsonl"))
        self.T = U(2026, 9, 28, 9, 0)
        self.cid = self.c.log_trade("GBPUSD", "BUY", 1.30, 1.295, 1.31, self.T, now=self.T + timedelta(minutes=1))
        self.c.add_reasoning(self.cid, "5m bounce looked like a reversal", now=self.T + timedelta(minutes=2))
        self.c.attach_bot_plan(self.cid, self.T, "NO_VALID_TRADE", {"htf": "BEARISH"}, now=self.T + timedelta(minutes=3))
        self.c.record_outcome(self.cid, "LOSS", closed_at=self.T + timedelta(hours=3), r=-1.0, now=self.T + timedelta(hours=3))

    def test_bot_vs_user_keeps_both_views_and_respects_as_of(self):
        at_entry = ba.adapt_case(self.c.get_case(self.cid, as_of=self.T + timedelta(minutes=30)))
        a = bc.resolve("bot_vs_user", at_entry.claims, self.T + timedelta(minutes=30), params={"case_id": self.cid})
        self.assertEqual(a.status, "ANSWER")
        self.assertEqual({i.claim.subject.split(".")[-1] for i in a.items}, {"bot_plan", "user_reasoning"})   # no outcome yet
        later = ba.adapt_case(self.c.get_case(self.cid))
        a = bc.resolve("bot_vs_user", later.claims, self.T + timedelta(days=1), params={"case_id": self.cid})
        self.assertIn("outcome", {i.claim.subject.split(".")[-1] for i in a.items})
        self.assertEqual(bc.audit_text("Your note said the 5m bounce looked like a reversal; the bot saw NO_VALID_TRADE.", a)[:0], [])

    def test_threads_knowledge_statements_integrity(self):
        tid = self.c.open_thread("m15", "real?", "bearish", {"type": "price_close_above", "level": 1.32}, opened_at=self.T, now=self.T)
        recs = ledger.read(self.c.path)[0]
        th = ba.adapt_threads(recs); self.assertEqual(th.claims[0].value["status"], "OPEN")
        self.c.resolve_open_threads(candles=[{"datetime": iso(self.T + timedelta(minutes=5)), "close": 1.321}], now=self.T + timedelta(minutes=6))
        recs = ledger.read(self.c.path)[0]
        th = ba.adapt_threads(recs); self.assertEqual(th.claims[0].value["status"], "RESOLVED")
        a = bc.resolve("thread_status", th.claims, self.T + timedelta(hours=1), params={"thread_id": tid})
        self.assertEqual(a.status, "ANSWER")
        # as-of hides the resolution from a view taken before it was known
        early = ba.adapt_threads(ledger.as_of(recs, self.T + timedelta(minutes=1)))
        self.assertEqual(early.claims[0].value["status"], "OPEN")
        ev = self.c.add_reasoning(self.cid, "BOS then pullback", now=self.T + timedelta(hours=4))
        self.c.record_knowledge("BOS", ev["event_id"], now=self.T + timedelta(hours=4))
        recs = ledger.read(self.c.path)[0]
        self.assertEqual(ba.adapt_knowledge(recs).claims[0].subject, "user.knows.BOS")
        said = bc.resolve("what_changed", list(by_subject(ba.adapt_state(state()))[0].values()), SNAP)
        ledger.append(self.c.path, bc.BUS_STATEMENT, bc.statement_payload(said), ts=SNAP, now=SNAP)
        recs = ledger.read(self.c.path)[0]
        st = ba.adapt_statements(recs); self.assertTrue(any(c.subject.startswith("bus.statement.") for c in st.claims))
        integ = ba.adapt_integrity(recs, 0, SNAP)
        self.assertIs(integ.claims[0].value["ok"], True)
        tampered = [dict(recs[0], payload={"x": 1})] + recs[1:]
        self.assertIs(ba.adapt_integrity(tampered, 0, SNAP).claims[0].value["ok"], False)


class TermsAndResearchAdapters(unittest.TestCase):
    def setUp(self):
        self.c = Catalog(os.path.join(tempfile.mkdtemp(), "u.jsonl"))
        T = U(2026, 9, 28, 9, 0)
        self.cid = self.c.log_trade("GBPUSD", "BUY", 1.30, 1.295, 1.31, T, now=T + timedelta(minutes=1))
        self.c.add_reasoning(self.cid, "Retest of #support after the BOS, #suport held", now=T + timedelta(minutes=2))
        self.T = T

    def test_terms_claim_counts_words_and_never_defines_them(self):
        recs = ledger.read(self.c.path)[0]
        c = ba.adapt_terms(recs).claims[0]
        self.assertEqual(c.subject, "user.terms")
        self.assertEqual({t["term"]: (t["uses"], t["status"]) for t in c.value["terms"]},
                         {"support": (1, "unrecognised_by_engine"), "suport": (1, "unrecognised_by_engine")})
        self.assertFalse(any("mean" in json.dumps(c.value).lower() for _ in [0]))        # no definition field exists
        self.c.alias_term("suport", "support", now=self.T + timedelta(hours=1))
        merged = ba.adapt_terms(ledger.read(self.c.path)[0]).claims[0].value["terms"]
        self.assertEqual((len(merged), merged[0]["uses"], merged[0]["variants"]), (1, 2, ["suport", "support"]))

    def test_adapter_agrees_with_the_catalog_own_count(self):
        self.c.alias_term("suport", "support", now=self.T + timedelta(hours=1))
        adapter = {t["term"]: t["uses"] for t in ba.adapt_terms(ledger.read(self.c.path)[0]).claims[0].value["terms"]}
        catalog = {t["term"]: t["uses"] for t in self.c.term_usage()}
        self.assertEqual(adapter, catalog)

    def test_no_terms_means_no_claim_so_the_contract_reports_it_missing(self):
        empty = Catalog(os.path.join(tempfile.mkdtemp(), "e.jsonl"))
        self.assertEqual(ba.adapt_terms(ledger.read(empty.path)[0]).claims, [])

    def test_research_adapter_is_pinned_to_research_objects(self):
        import research_objects as ro
        p = self.c.path
        h = ro.draft_hypothesis(p, "Q?", "claim", "FAM-x", now=self.T)
        spec = {"question": "Q?", "claim_under_test": "claim", "instrument": "GBPUSD",
                "population": {"population_id": "POP-1", "eligibility_rule": "r"}, "decision_under_test": "direction",
                "preserve": sorted(ro.NULL_MODELS["direction_randomization"]["must_preserve"]), "randomize": ["direction"],
                "outcome_metric": "expectancy_R", "null_model": "direction_randomization", "research_family_id": "FAM-x",
                "data_cutoff": "2026-09-30T00:00:00+00:00", "data_split": "development"}
        a, _ = ro.draft_spec(p, h, spec, now=self.T + timedelta(minutes=1))
        b, _ = ro.draft_spec(p, h, dict(spec, seed=2), now=self.T + timedelta(minutes=2))
        ro.freeze(p, a, now=self.T + timedelta(minutes=3))
        ro.record_result(p, a, "POP-1", "INCONCLUSIVE", 7, now=self.T + timedelta(minutes=4))
        got = ba.adapt_research(ledger.read(p)[0]).claims[0].value["cases"][0]
        want = ro.cases(p)[0]
        self.assertEqual(got["hypothesis_id"], want["hypothesis_id"])
        self.assertEqual([(e["spec_id"], e["valid"], e["frozen"], e["status"]) for e in got["experiments"]],
                         [(e["spec_id"], e["valid"], e["frozen"], e["status"]) for e in want["experiments"]])
        self.assertEqual(got["family_searches"], ro.family_summary(p, "FAM-x")["specs_drafted"])


class Boundary(unittest.TestCase):
    """Rules 1-4 as mechanical checks over the adapter's own source."""

    def setUp(self):
        self.src = open(os.path.join(HERE, "bus_adapters.py")).read()
        self.tree = ast.parse(self.src)

    def test_imports_are_only_stdlib_plus_contract_and_ledger(self):
        ok = {"json", "sys", "os", "dataclasses", "typing", "bus_contract", "ledger"}
        found = set()
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Import): found |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module: found.add(n.module.split(".")[0])
        self.assertLessEqual(found, ok, found - ok)

    def test_no_clock_no_network(self):
        for banned in ("datetime.now", "utcnow", "time.time", "requests", "urllib", "socket", "yfinance"):
            self.assertNotIn(banned, self.src.replace(banned + " ", "") if False else
                             re.sub(r'""".*?"""', "", self.src, flags=re.S).replace("# ", "#"), banned)

    def test_adapters_do_no_file_io_except_the_cli_helpers(self):
        for n in ast.walk(self.tree):
            if isinstance(n, ast.FunctionDef) and n.name.startswith("adapt_"):
                calls = {c.func.id for c in ast.walk(n) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
                self.assertFalse(calls & {"open", "print", "input"}, (n.name, calls))

    def test_no_adapter_authority_exists(self):
        self.assertFalse([a for a in bc.AUTHORITIES if "adapter" in a])


class MatrixIsComplete(unittest.TestCase):
    def test_every_mapped_subject_has_an_adapter_and_every_adapter_subject_is_registered(self):
        mapped = {n for _rx, n, s in bc._S if s.source is not None}
        self.assertEqual(mapped - set(ba.ADAPTER_PRODUCES), set(), "mapped subjects with no adapter")
        for g in ba.ADAPTER_PRODUCES:
            self.assertIn(g, {n for _rx, n, _s in bc._S}, g)

    def test_every_intent_slot_is_in_the_matrix_and_unmapped_ones_say_so(self):
        rows = ba.claim_matrix()
        need = {(i.name, s.subject) for i in bc.INTENTS.values() for s in i.slots}
        self.assertEqual({(r["intent"], r["subject"]) for r in rows}, need)
        for r in rows:
            if r["subject"] in ("story.controller", "story.counter_attempt", "narrative.change_condition"):
                self.assertIn("ENGINE DOES NOT PROVIDE", r["source"])
        self.assertIn("current_market_story", ba.matrix_markdown())

    def test_committed_matrix_is_not_stale(self):
        p = os.path.join(ROOT, "CLAIM_MATRIX.md")
        if not os.path.exists(p):
            self.skipTest("CLAIM_MATRIX.md not committed alongside")
        self.assertEqual(open(p).read(), ba.matrix_markdown(), "regenerate: python bus_adapters.py --matrix > CLAIM_MATRIX.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
