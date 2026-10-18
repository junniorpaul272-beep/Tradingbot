"""Run: python test_case_reconstruct.py — Step 2d: the case reconstruction is an EVIDENCE AUDIT.
It uses only records known at the cutoff, never today's state, keeps the bot's plan / your decision / the outcome /
the investigation apart, reports every gap with why and what it blocks, and writes nothing."""
import hashlib, inspect, json, os, re, shutil, sys, tempfile, unittest


def rd(p, mode="r"):
    with open(p, mode) as f:
        return f.read()


def wr(p, text):
    with open(p, "w") as f:
        f.write(text)

from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import ledger, case_reconstruct as cr, user_catalog as uc, state_history as sh, bus_runner as br

T = datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc)          # entry
M = timedelta(minutes=1)
OUT_AT = T + 90 * M


def snap_payload(bias="BEARISH", phase="EXPANSION", **over):
    p = {"payload_schema": 1, "snapshot_key": "k" + bias + phase, "state_hash": "h" + bias + phase, "code_version": "abc",
         "market_data_timestamp": "x", "htf_bias": bias, "market_phase": phase, "thesis_state": "Bearish Expansion",
         "failure_risk": "LOW", "mil_status": "ACTIVE", "leg_owner": {"leg_id": "L1", "tier": 2}, "leg_age_status": {"status": "FRESH"},
         "macro_leg": {"direction": "BEARISH"}, "regime_digest_current": True, "htf_bias_stale": False}
    p.update(over)
    return p


class Env(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cpath = os.path.join(self.d, "user_ledger.jsonl")
        self.hpath = os.path.join(self.d, "state_history_log.jsonl")
        self.cat = uc.Catalog(self.cpath)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def snap(self, ts, recorded=None, **kw):
        return ledger.append(self.hpath, sh.SNAPSHOT, snap_payload(**kw), ts=ts, now=recorded or ts + M * 0.5)

    def trade(self, **kw):
        kw.setdefault("signal_event_id", "sig-1")
        return self.cat.log_trade("GBPUSD", "SELL", 1.3123, 1.3153, 1.3063, T, now=T + timedelta(seconds=20), **kw)

    def plan(self, cid, facts=None, at=None, now=None):
        return self.cat.attach_bot_plan(cid, at or T - 5 * M, "TRADE", {"entry": 1.3123, "tier_label": "TIER_2"} if facts is None else facts,
                                        now=now or T + timedelta(seconds=25))

    def full(self):
        self.snap(T - 5 * M, bias="BEARISH")
        cid = self.trade(); self.plan(cid)
        self.cat.add_reasoning(cid, "bearish bias, retest of the zone", now=T + 2 * M)
        self.cat.record_outcome(cid, "LOSS", OUT_AT, r=-1.0, now=OUT_AT)
        return cid

    def rep(self, cid="CASE-0001", **kw):
        recs, bad = ledger.read(self.cpath)
        hist = ledger.read(self.hpath)[0] if os.path.exists(self.hpath) else None
        kw.setdefault("history", hist)
        h = kw.pop("history")
        return cr.reconstruct(cid, recs, h, catalog_bad_lines=len(bad) if hasattr(bad, "__len__") else bad, **kw)

    def gap(self, rep, item):
        return [g for g in rep["gaps"] if g["item"] == item]


class Required(Env):
    def test_a_complete_case_reconstructs_only_from_historical_records(self):
        cid = self.full()
        self.snap(T + 30 * M, bias="BULLISH")                      # a LATER snapshot with a different story: never used
        r = self.rep(cid)
        self.assertEqual((r["evidence_coverage"], r["outcome_status"], r["historical_reconstruction"]), ("complete", "known", "trustworthy"))
        self.assertEqual(r["market_state"]["values"]["htf_bias"], "BEARISH")
        self.assertEqual(r["market_state"]["snapshot"]["ts"], ledger._iso(T - 5 * M))
        self.assertEqual(r["gaps"] and [g for g in r["gaps"] if g["item"] == "historical market state"], [])

    def test_missing_historical_state_stays_missing_even_when_current_state_has_the_fields(self):
        cid = self.full_without_snapshot()
        wr(self.hpath, "")                               # a history log exists, with nothing recorded for that time
        with open(os.path.join(self.d, "state.json"), "w") as f:
            json.dump({"structure_digest": {"regime": {"macro_bias": "BULLISH"}}, "market_phase": "EXPANSION"}, f)
        r = self.rep(cid)
        self.assertEqual(r["market_state"]["status"], "MISSING"); self.assertIsNone(r["market_state"]["values"])
        self.assertTrue(self.gap(r, "historical market state")); self.assertNotIn("BULLISH", json.dumps(r))
        params = set(inspect.signature(cr.reconstruct).parameters)
        self.assertEqual(params, {"case_id", "catalog_records", "history_records", "known_at", "catalog_bad_lines"})   # no door for today's state

    def full_without_snapshot(self):
        cid = self.trade(); self.plan(cid)
        self.cat.add_reasoning(cid, "reason", now=T + 2 * M); self.cat.record_outcome(cid, "WIN", OUT_AT, r=2.0, now=OUT_AT)
        return cid

    def test_post_outcome_reasoning_is_never_presented_as_pre_trade(self):
        self.snap(T - 5 * M); cid = self.trade(); self.plan(cid)
        self.cat.record_outcome(cid, "LOSS", OUT_AT, r=-1.0, now=OUT_AT)
        self.cat.add_reasoning(cid, "I knew it would fail", now=OUT_AT + 10 * M)
        r = self.rep(cid)
        self.assertEqual(r["four_records"]["user_decision"]["reasoning"], {"pre_entry": 0, "in_trade": 0, "post_outcome": 1})
        item = next(i for i in r["inventory"] if i["key"] == "reasoning")
        self.assertEqual(item["status"], "MISSING")
        g = self.gap(r, "reasoning before the outcome")[0]
        self.assertEqual(g["state"], cr.LATE); self.assertNotEqual(r["evidence_coverage"], "complete")
        self.assertEqual(r["outcome_status"], "known")

    def test_reasoning_written_after_entry_is_not_called_pre_entry(self):
        cid = self.full(); r = self.rep(cid)
        self.assertEqual(r["four_records"]["user_decision"]["reasoning"]["in_trade"], 1)
        self.assertEqual(next(i for i in r["inventory"] if i["key"] == "reasoning")["status"], "PARTIAL")
        self.assertTrue(self.gap(r, "reasoning before entry"))

    def test_happened_at_and_recorded_at_at_the_cutoff(self):
        cid = self.full()
        self.snap(T - 2 * M, recorded=T + 5 * M, bias="BULLISH")        # happened before entry, only RECORDED after it
        r = self.rep(cid)
        self.assertEqual(r["market_state"]["snapshot"]["ts"], ledger._iso(T - 5 * M))      # the late one is not pre-entry knowledge
        self.assertEqual(len(r["market_state"]["late_recorded"]), 1); self.assertTrue(self.gap(r, "snapshot recorded in time"))
        early = self.rep(cid, known_at=T + 10 * M)                      # rewind knowledge to before the outcome was written
        self.assertEqual(early["outcome_status"], "pending"); self.assertEqual(early["cutoffs"]["known_at"], ledger._iso(T + 10 * M))
        self.assertEqual(self.rep(cid, known_at=OUT_AT + M)["outcome_status"], "known")

    def test_a_missing_outcome_stays_pending_and_nothing_is_inferred_from_price(self):
        self.snap(T - 5 * M); cid = self.trade(); self.plan(cid)
        r = self.rep(cid)
        self.assertEqual(r["outcome_status"], "pending"); self.assertTrue(self.gap(r, "recorded outcome"))
        self.assertIn("/outcome", self.gap(r, "recorded outcome")[0]["resolvable"])
        src = rd(os.path.join(HERE, "case_reconstruct.py"))
        self.assertFalse(re.search(r"^\s*(import|from)\s+\w*(candle|price|ohlc)", src, re.M | re.I))
        self.cat.record_outcome(cid, "OPEN", OUT_AT, now=OUT_AT)
        self.assertEqual(self.rep(cid)["outcome_status"], "pending")      # recorded as still open is still pending

    def test_a_known_outcome_with_missing_reasoning_is_known_but_incompletely_covered(self):
        self.snap(T - 5 * M); cid = self.trade(); self.plan(cid)
        self.cat.record_outcome(cid, "WIN", OUT_AT, r=2.0, now=OUT_AT)
        r = self.rep(cid)
        self.assertEqual(r["outcome_status"], "known"); self.assertEqual(r["evidence_coverage"], "partial")
        self.assertTrue(self.gap(r, "reasoning before the outcome"))

    def test_an_outcome_without_r_is_partial_and_the_source_is_not_claimed(self):
        self.snap(T - 5 * M); cid = self.trade(); self.plan(cid); self.cat.record_outcome(cid, "WIN", OUT_AT, now=OUT_AT)
        r = self.rep(cid); self.assertEqual(r["outcome_status"], "partial")
        o = next(i for i in r["inventory"] if i["key"] == "outcome")["detail"]
        self.assertIn("not where the result came from", o)

    def test_reconstruction_is_read_only_and_repeatable(self):
        cid = self.full()
        before = {p: hashlib.sha256(rd(p, "rb")).hexdigest() for p in (self.cpath, self.hpath)}
        a, b = self.rep(cid), self.rep(cid)
        after = {p: hashlib.sha256(rd(p, "rb")).hexdigest() for p in (self.cpath, self.hpath)}
        self.assertEqual(before, after); self.assertEqual(a, b)
        self.assertEqual(cr.render(a), cr.render(b))

    def test_conflicting_records_are_flagged_not_resolved(self):
        cid = self.full()
        recs, _ = ledger.read(self.cpath)
        o = next(r for r in recs if r["event_type"] == uc.OUTCOME_RECORDED)
        dup = ledger.make_record(uc.OUTCOME_RECORDED, {"case_id": cid, "result": "WIN", "r": 3.0}, ts=OUT_AT, recorded_at=OUT_AT + M)
        with open(self.cpath, "a") as f:
            f.write(json.dumps(dup) + "\n")
        r = self.rep(cid)
        self.assertEqual(r["outcome_status"], "unavailable"); self.assertTrue(any("disagree" in x for x in r["integrity"]))
        self.assertIsNone(r["four_records"]["outcome"]["ref"])

    def test_two_plans_or_two_trades_are_conflicts(self):
        cid = self.full()
        dup = ledger.make_record(uc.BOT_PLAN_ATTACHED, {"case_id": cid, "plan_as_of": ledger._iso(T - M), "result": "TRADE", "facts": {"a": 1}}, ts=T - M, recorded_at=T + M)
        with open(self.cpath, "a") as f:
            f.write(json.dumps(dup) + "\n")
        r = self.rep(cid)
        self.assertEqual(next(i for i in r["inventory"] if i["key"] == "bot_plan")["status"], "CONFLICT")
        self.assertNotEqual(r["evidence_coverage"], "complete")
        t2 = ledger.make_record(uc.TRADE_LOGGED, {"case_id": cid, "pair": "GBPUSD", "direction": "BUY", "entry": 1.0, "sl": 0.9, "tp": 1.2}, ts=T, recorded_at=T + M)
        with open(self.cpath, "a") as f:
            f.write(json.dumps(t2) + "\n")
        self.assertEqual(self.rep(cid)["status"], "CONFLICT")

    def test_an_edited_record_is_flagged_and_an_edited_snapshot_is_not_trusted(self):
        cid = self.full()
        lines = rd(self.cpath).splitlines()
        i = next(k for k, l in enumerate(lines) if uc.OUTCOME_RECORDED in l)
        rec = json.loads(lines[i]); rec["payload"]["r"] = 9.0; lines[i] = json.dumps(rec)
        wr(self.cpath, "\n".join(lines) + "\n")
        r = self.rep(cid)
        self.assertEqual(r["outcome_status"], "unavailable"); self.assertTrue(any("hash" in x for x in r["integrity"]))
        h = rd(self.hpath).splitlines(); rec = json.loads(h[0]); rec["payload"]["htf_bias"] = "BULLISH"
        wr(self.hpath, json.dumps(rec) + "\n")
        r = self.rep(cid)
        self.assertEqual(r["market_state"]["status"], "CONFLICT"); self.assertIsNone(r["market_state"]["values"])
        self.assertEqual(r["historical_reconstruction"], "unavailable")

    def test_two_disagreeing_snapshots_for_one_moment_are_not_resolved(self):
        self.snap(T - 5 * M, bias="BEARISH"); self.snap(T - 5 * M, bias="BULLISH")
        cid = self.trade(); self.plan(cid)
        r = self.rep(cid)
        self.assertEqual(r["market_state"]["status"], "CONFLICT"); self.assertIsNone(r["market_state"]["values"])

    def test_a_record_with_an_unreadable_time_is_set_aside_and_said_so(self):
        cid = self.full()
        bad = {"event_id": "bad-1", "event_type": uc.REASONING_LOGGED, "ts": "not a time", "recorded_at": "nope", "payload": {"case_id": cid, "text": "x"}}
        with open(self.cpath, "a") as f:
            f.write(json.dumps(bad) + "\n")
        r = self.rep(cid)
        self.assertTrue(any("bad-1" in x and "unreadable timestamp" in x for x in r["integrity"]))
        self.assertNotEqual(r["evidence_coverage"], "complete")

    def test_the_report_names_its_sources_cutoff_and_a_reproducible_fingerprint(self):
        cid = self.full(); r = self.rep(cid)
        types = [s["event_type"] for s in r["sources"]]
        for t in (uc.TRADE_LOGGED, uc.BOT_PLAN_ATTACHED, uc.REASONING_LOGGED, uc.OUTCOME_RECORDED, sh.SNAPSHOT):
            self.assertIn(t, types)
        self.assertTrue(r["cutoffs"]["entry_anchor"]); self.assertTrue(r["cutoffs"]["entry_anchor_basis"])
        self.assertEqual(self.rep(cid)["inputs_fingerprint"], r["inputs_fingerprint"])
        self.assertNotEqual(self.rep(cid, known_at=T + 10 * M)["inputs_fingerprint"], r["inputs_fingerprint"])

    def test_every_gap_says_what_was_expected_why_what_it_blocks_and_whether_it_can_be_fixed(self):
        self.snap(T - 5 * M); cid = self.trade()
        r = self.rep(cid)
        self.assertTrue(r["gaps"])
        for g in r["gaps"]:
            self.assertTrue(g["expected"] and g["state"] and g["prevents"] and g["resolvable"], g)


class Details(Env):
    def test_the_four_records_stay_distinct(self):
        cid = self.full(); self.cat.add_investigation(cid, "I took it against the 4h swing", now=OUT_AT + 5 * M)
        r = self.rep(cid); f = r["four_records"]
        self.assertEqual(set(f), {"bot_plan", "user_decision", "outcome", "investigation"})
        self.assertEqual(f["investigation"]["notes"], 1)
        refs = [f["bot_plan"]["ref"]["event_id"], f["user_decision"]["ref"]["event_id"], f["outcome"]["ref"]["event_id"]]
        self.assertEqual(len(set(refs)), 3)
        self.assertNotIn("4h swing", json.dumps(r))           # the investigation's words are not copied into the evidence

    def test_a_stale_snapshot_is_flagged_and_not_used(self):
        self.snap(T - 90 * M); cid = self.trade(); self.plan(cid)
        r = self.rep(cid)
        self.assertEqual(r["market_state"]["status"], "STALE"); self.assertEqual(self.gap(r, "historical market state")[0]["state"], cr.STALE)
        self.assertEqual(r["historical_reconstruction"], "partial"); self.assertEqual(r["evidence_coverage"], "insufficient")

    def test_history_that_began_after_entry_is_never_recorded_not_stale(self):
        self.snap(T + 20 * M); cid = self.trade(); self.plan(cid)
        r = self.rep(cid)
        self.assertEqual(self.gap(r, "historical market state")[0]["state"], cr.NEVER_RECORDED)
        self.assertTrue(any("began after" in x for x in r["market_state"]["flags"]))

    def test_an_unreadable_history_log_is_unavailable_not_empty(self):
        cid = self.full(); r = self.rep(cid, history=None)
        self.assertEqual(r["market_state"]["status"], "UNAVAILABLE"); self.assertEqual(r["historical_reconstruction"], "unavailable")

    def test_null_fields_in_a_snapshot_are_gaps_not_values(self):
        self.snap(T - 5 * M, market_phase=None, failure_risk=None); cid = self.trade(); self.plan(cid)
        r = self.rep(cid)
        self.assertEqual(set(r["market_state"]["missing_fields"]), {"market_phase", "failure_risk"})
        self.assertTrue(self.gap(r, "snapshot field market_phase")); self.assertIsNone(r["market_state"]["values"]["market_phase"])

    def test_a_command_processed_late_withholds_the_market_state(self):
        self.snap(T - 5 * M); cid = self.trade(signal_event_id=None); self.plan(cid)       # a /logtrade with no send time stored
        r = self.rep(cid)
        self.assertEqual(r["cutoffs"]["entry_anchor_basis"], "processing_time"); self.assertIsNone(r["market_state"]["values"])
        self.assertTrue(self.gap(r, "exact entry time")); self.assertEqual(r["historical_reconstruction"], "unavailable")

    def test_a_logtrade_with_its_send_time_is_anchored_to_that_time(self):
        self.snap(T - 5 * M)
        cid = self.cat.log_trade("GBPUSD", "SELL", 1.3123, 1.3153, 1.3063, T + 40 * M, now=T + 40 * M, reported_at=T)
        self.cat.attach_bot_plan(cid, T - 5 * M, "TRADE", {"entry": 1.3}, now=T + 40 * M)
        r = self.rep(cid)
        self.assertEqual(r["cutoffs"]["entry_anchor"], ledger._iso(T)); self.assertEqual(r["market_state"]["status"], "PRESENT")

    def test_a_plan_recorded_after_the_close_is_not_treated_as_the_plan_before_the_result(self):
        self.snap(T - 5 * M); cid = self.trade()
        self.cat.record_outcome(cid, "LOSS", OUT_AT, r=-1.0, now=OUT_AT)
        self.plan(cid, now=OUT_AT + 20 * M)
        r = self.rep(cid)
        self.assertEqual(next(i for i in r["inventory"] if i["key"] == "bot_plan")["status"], "PARTIAL")
        self.assertTrue(self.gap(r, "bot plan recorded before the outcome"))

    def test_a_plan_with_no_facts_is_not_evidence(self):
        self.snap(T - 5 * M); cid = self.trade(); self.plan(cid, facts={})
        r = self.rep(cid)
        self.assertTrue(self.gap(r, "facts in the bot plan")); self.assertEqual(r["four_records"]["bot_plan"]["status"], "PARTIAL")

    def test_a_fill_that_differs_from_the_signals_entry_is_reported_without_judgement(self):
        self.snap(T - 5 * M)
        cid = self.cat.log_trade("GBPUSD", "SELL", 1.3120, 1.3153, 1.3063, T, now=T, signal_event_id="s")
        self.plan(cid)
        r = self.rep(cid)
        self.assertTrue(r["four_records"]["user_decision"]["fill"]["differs"])
        self.assertIn("differs", next(i for i in r["inventory"] if i["key"] == "user_decision")["detail"])

    def test_unknown_or_future_cases(self):
        self.assertEqual(self.rep("CASE-0009")["status"], "NO_SUCH_CASE")
        cid = self.full()
        self.assertEqual(self.rep(cid, known_at=T - 10 * M)["status"], "NO_SUCH_CASE")

    def test_unreadable_ledger_lines_are_stated(self):
        cid = self.full()
        with open(self.cpath, "a") as f:
            f.write("{ not json\n")
        self.assertTrue(any("unreadable line" in x for x in self.rep(cid)["integrity"]))

    def test_the_render_states_gaps_and_limits_and_never_judges(self):
        self.snap(T - 5 * M); cid = self.trade()
        t = cr.render(self.rep(cid))
        for s in ("coverage INSUFFICIENT", "Today's state is never used", "Missing or uncertain", "Cannot be established",
                  "an outcome alone cannot show that", "fingerprint"):
            self.assertIn(s, t, s)
        self.assertNotRegex(t.lower(), r"\b(good trade|bad trade|you should|mistake|correct decision)\b")

    def test_event_names_match_their_owners(self):
        for n in ("TRADE_LOGGED", "REASONING_LOGGED", "BOT_PLAN_ATTACHED", "OUTCOME_RECORDED", "INVESTIGATION_ADDED",
                  "INVESTIGATION_OPENED", "KNOWLEDGE_STATED"):
            self.assertEqual(getattr(cr, n), getattr(uc, n))
        self.assertEqual(cr.SNAPSHOT, sh.SNAPSHOT); self.assertEqual(cr.SNAPSHOT_SCHEMA, sh.PAYLOAD_SCHEMA)
        self.assertEqual(cr.OUTCOMES, uc.OUTCOMES)


class Through(unittest.TestCase):
    """The Telegram command, end to end through the runner."""
    def setUp(self):
        import test_bus_taken as tt, test_bus_signals as tbs
        self.tt, self.tbs = tt, tbs
        self.f = tt.Flow("test_taken_logs_a_case_from_the_signal_with_the_signal_as_the_bots_plan")
        self.f.setUp()

    def tearDown(self):
        self.f.tearDown()

    def test_reconstruct_replies_from_records_and_writes_no_case_records(self):
        f = self.f
        self.tbs.write_signal(f.log); f.say("/taken"); f.go()
        before = [(r["event_type"], r["hash"]) for r in f.records() if r["event_type"] in
                  ("TRADE_LOGGED", "BOT_PLAN_ATTACHED", "OUTCOME_RECORDED", "REASONING_LOGGED", "SIGNAL_RESPONDED")]
        f.say("/reconstruct 1 detail"); f.go(); first = f.last()
        f.say("/reconstruct 1 detail"); f.go()
        after = [(r["event_type"], r["hash"]) for r in f.records() if r["event_type"] in
                 ("TRADE_LOGGED", "BOT_PLAN_ATTACHED", "OUTCOME_RECORDED", "REASONING_LOGGED", "SIGNAL_RESPONDED")]
        self.assertEqual(before, after)
        self.assertIn("Reconstruction of CASE-0001", first); self.assertIn("Today's state is never used", first)
        self.assertIn("Historical market state: Unavailable", first)       # no history log exists in this scenario: said, not filled
        self.assertEqual(f.last(), first)                                   # a repeat gives the same report
        f.say("/reconstruct 1"); f.go(); plain = f.last()                   # the everyday version says the same things in words
        self.assertIn("CASE-0001", plain); self.assertIn("Where I'm short", plain)
        self.assertIn("never today's market", plain); self.assertNotIn("Reconstruction of CASE-0001", plain)

    def test_bad_format_and_unknown_case(self):
        f = self.f
        f.say("/reconstruct"); f.go(); self.assertIn("What do you want me to go back over?", f.last())
        f.say("/reconstruct 7"); f.go(); self.assertIn("I don't have CASE-0007", f.last())

    def test_a_logtrade_read_long_after_it_was_sent_gets_no_as_of_now_snapshot(self):
        f = self.f
        f.now = datetime(2026, 10, 1, 15, 57, 20, tzinfo=timezone.utc)      # the engine's state IS fresh now; the message is not
        f.say("/logtrade GBPUSD buy 1.3421 sl 1.3398 tp 1.3490", at=f.now - timedelta(minutes=45)); f.go()
        self.assertIn("I did not attach a bot snapshot", f.last())
        t = f.of("TRADE_LOGGED")[0]["payload"]
        self.assertEqual(t["reported_at"], ledger._iso(f.now - timedelta(minutes=45)))

    def test_a_fresh_logtrade_keeps_its_send_time_and_the_snapshot(self):
        f = self.f
        f.now = datetime(2026, 10, 1, 15, 57, 20, tzinfo=timezone.utc)      # 2 min after the fixture's scan, as test_bus_runner does
        f.say("/logtrade GBPUSD buy 1.3421 sl 1.3398 tp 1.3490", at=f.now - timedelta(minutes=1)); f.go()
        self.assertIn("attached what the engine had right now", f.last())
        self.assertEqual(f.of("TRADE_LOGGED")[0]["payload"]["reported_at"], ledger._iso(f.now - timedelta(minutes=1)))


if __name__ == "__main__":
    unittest.main()
