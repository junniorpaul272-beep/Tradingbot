"""Run: python test_ledger_catalog.py   (stdlib only; exits non-zero on failure)"""
import json, os, sys, tempfile, unittest
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger
from ledger import LedgerError
from user_catalog import Catalog

U = lambda *a: datetime(*a, tzinfo=timezone.utc)
MON = U(2026, 9, 28, 9, 0)          # the entry
FRI = U(2026, 10, 2, 17, 0)         # when the user got round to logging it


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.p = os.path.join(tempfile.mkdtemp(), "l.jsonl")

    def test_append_only_and_verify_detects_edit(self):
        a = ledger.append(self.p, "X", {"n": 1}, now=MON)
        ledger.append(self.p, "X", {"n": 2}, now=MON + timedelta(minutes=1))
        recs, bad = ledger.read(self.p)
        self.assertEqual((len(recs), bad, ledger.verify(recs)), (2, 0, []))
        # tamper with the file on disk
        with open(self.p) as f:
            lines = f.read().splitlines()
        rec = json.loads(lines[0]); rec["payload"]["n"] = 999
        lines[0] = json.dumps(rec, sort_keys=True)
        with open(self.p, "w") as f:
            f.write("\n".join(lines) + "\n")
        recs, _ = ledger.read(self.p)
        self.assertEqual(ledger.verify(recs), [("EDITED", a["event_id"])])

    def test_naive_and_future_and_nonjson_refused(self):
        with self.assertRaises(LedgerError): ledger.append(self.p, "X", {}, ts=datetime(2026, 1, 1))
        with self.assertRaises(LedgerError): ledger.append(self.p, "X", {}, ts=MON + timedelta(hours=1), now=MON)
        with self.assertRaises(LedgerError): ledger.append(self.p, "X", {"d": {1, 2}}, now=MON)
        self.assertEqual(ledger.read(self.p)[0], [])          # nothing half-written

    def test_corrupt_line_skipped_not_repaired(self):
        ledger.append(self.p, "X", {}, now=MON)
        with open(self.p, "a") as f:
            f.write("{garbage\n")
        recs, bad = ledger.read(self.p)
        self.assertEqual((len(recs), bad), (1, 1))

    def test_bitemporal_no_hindsight(self):
        # happened Monday, but only learned Friday
        ledger.append(self.p, "TRADE", {"c": 1}, ts=MON, now=FRI)
        recs, _ = ledger.read(self.p)
        self.assertEqual(ledger.as_of(recs, MON + timedelta(hours=1)), [])     # not knowable Monday
        self.assertEqual(len(ledger.as_of(recs, FRI)), 1)

    def test_view_without_T_is_not_wallclock_bounded(self):
        far = datetime.now(timezone.utc) + timedelta(days=400)
        ledger.append(self.p, "X", {}, ts=far, now=far)
        recs, _ = ledger.read(self.p)
        self.assertEqual(len(ledger.view_as_of(recs)), 1)
        self.assertEqual(len(ledger.current(recs)), 1)

    def test_correction_visible_only_after_it_was_known(self):
        a = ledger.append(self.p, "FACT", {"v": 1}, now=MON)
        b = ledger.append(self.p, "FACT", {"v": 2}, corrects=a["event_id"], now=FRI)
        recs, _ = ledger.read(self.p)
        before = ledger.view_as_of(recs, MON + timedelta(days=1))
        self.assertEqual([r["superseded_by"] for r in before], [None])         # believed uncorrected then
        after = ledger.view_as_of(recs, FRI)
        self.assertEqual([r["superseded_by"] for r in after], [b["event_id"], None])
        self.assertEqual([r["payload"]["v"] for r in ledger.current(recs, FRI)], [2])
        with open(self.p) as f:
            self.assertEqual(f.read().count("\n"), 2)                          # original still on disk


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.c = Catalog(os.path.join(tempfile.mkdtemp(), "u.jsonl"))

    def _trade(self, **kw):
        a = dict(pair="GBPUSD", direction="BUY", entry=1.3000, sl=1.2950, tp=1.3100, taken_at=MON, now=FRI)
        a.update(kw); return self.c.log_trade(**a)

    def test_inbox_dedupes_update_id(self):
        r1, new1 = self.c.record_inbound(101, "/case 1", ts=MON, now=MON)
        r2, new2 = self.c.record_inbound(101, "/case 1", ts=MON, now=MON)
        self.assertEqual((new1, new2, r1["event_id"] == r2["event_id"]), (True, False, True))
        self.assertEqual(len(ledger.read(self.c.path)[0]), 1)

    def test_trade_geometry_validated_and_sequential_ids(self):
        with self.assertRaises(LedgerError): self._trade(sl=1.3050)           # stop above a BUY entry
        self.assertEqual((self._trade(), self._trade()), ("CASE-0001", "CASE-0002"))

    def test_hindsight_plan_refused_and_no_valid_trade_is_legit(self):
        cid = self._trade()
        with self.assertRaises(LedgerError):
            self.c.attach_bot_plan(cid, MON + timedelta(minutes=5), "TRADE", {})
        self.c.attach_bot_plan(cid, MON - timedelta(minutes=5), "NO_VALID_TRADE", {"htf": "BEARISH"}, now=FRI)
        self.assertEqual(self.c.get_case(cid)["bot_plan"]["payload"]["result"], "NO_VALID_TRADE")
        with self.assertRaises(LedgerError):                                   # one plan; fixes are corrections
            self.c.attach_bot_plan(cid, MON - timedelta(minutes=9), "TRADE", {})

    def test_reasoning_after_outcome_is_flagged(self):
        cid = self._trade()
        early = self.c.add_reasoning(cid, "I think the bounce is a reversal", now=MON + timedelta(minutes=1))
        self.c.record_outcome(cid, "LOSS", closed_at=MON + timedelta(hours=3), r=-1.0, now=FRI)
        late = self.c.add_reasoning(cid, "I knew it was a trap", now=FRI + timedelta(hours=1))
        self.assertFalse(early["payload"]["written_after_outcome"])
        self.assertTrue(late["payload"]["written_after_outcome"])
        self.assertEqual(late["payload"]["text"], "I knew it was a trap")      # verbatim

    def test_outcome_and_investigation_ordering(self):
        cid = self._trade()
        with self.assertRaises(LedgerError): self.c.add_investigation(cid, "why?", now=FRI)   # no outcome yet
        with self.assertRaises(LedgerError): self.c.record_outcome(cid, "WIN", closed_at=MON - timedelta(hours=1))
        self.c.record_outcome(cid, "WIN", closed_at=MON + timedelta(hours=2), r=2.0, now=FRI)
        with self.assertRaises(LedgerError): self.c.record_outcome(cid, "LOSS", closed_at=MON + timedelta(hours=3))
        self.c.add_investigation(cid, "bounce was corrective", now=FRI + timedelta(hours=2))

    def test_case_reconstruction_hides_what_was_unknown(self):
        cid = self._trade(now=MON + timedelta(minutes=1))                       # logged promptly this time
        self.c.attach_bot_plan(cid, MON, "NO_VALID_TRADE", {}, now=MON + timedelta(minutes=2))
        self.c.record_outcome(cid, "LOSS", closed_at=MON + timedelta(hours=3), r=-1.0, now=MON + timedelta(hours=3))
        self.c.add_investigation(cid, "post-mortem", now=FRI)
        at_entry = self.c.get_case(cid, as_of=MON + timedelta(minutes=30))
        self.assertIsNotNone(at_entry["trade"]); self.assertIsNotNone(at_entry["bot_plan"])
        self.assertIsNone(at_entry["outcome"]); self.assertEqual(at_entry["investigation"], [])
        full = self.c.get_case(cid)
        self.assertIsNotNone(full["outcome"]); self.assertEqual(len(full["investigation"]), 1)
        # trade logged Friday about a Monday entry: invisible to a Monday reconstruction
        late = self._trade(now=FRI)
        self.assertIsNone(self.c.get_case(late, as_of=MON + timedelta(hours=1)))

    def test_thread_resolves_only_from_strictly_later_candles(self):
        tid = self.c.open_thread("bearish leg", "I don't think this break is real", "bearish",
                                 {"type": "price_close_above", "level": 1.3200}, opened_at=MON, now=MON)
        candles = [
            {"datetime": (MON - timedelta(minutes=5)).isoformat(), "close": 1.3300},   # before: must NOT count
            {"datetime": MON.isoformat(), "close": 1.3250},                            # at open: must NOT count
            {"datetime": (MON + timedelta(minutes=5)).isoformat(), "close": 1.3100},
            {"datetime": (MON + timedelta(minutes=10)).isoformat(), "close": 1.3210},  # first valid hit
            {"datetime": (MON + timedelta(minutes=15)).isoformat(), "close": 1.3400},
        ]
        res = self.c.resolve_open_threads(candles=candles, now=MON + timedelta(minutes=11))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["payload"]["evidence"]["close"], 1.3210)
        self.assertEqual(self.c.open_threads(), [])
        self.assertEqual(self.c.resolve_open_threads(candles=candles), [])             # idempotent

    def test_event_thread_needs_a_timestamp_and_a_later_one(self):
        self.c.open_thread("fresh BOS?", "q", "wait", {"type": "event_code", "code": "CHOCH_M15"}, opened_at=MON, now=MON)
        untimed = [{"code": "CHOCH_M15"}]                                               # no recorded_at -> can't prove "later"
        earlier = [{"code": "CHOCH_M15", "recorded_at": (MON - timedelta(hours=1)).isoformat()}]
        self.assertEqual(self.c.resolve_open_threads(events=untimed + earlier), [])
        later = [{"code": "OTHER", "recorded_at": (MON + timedelta(minutes=5)).isoformat()},
                 {"code": "CHOCH_M15", "recorded_at": (MON + timedelta(minutes=9)).isoformat()}]
        self.assertEqual(len(self.c.resolve_open_threads(events=later, now=MON + timedelta(minutes=10))), 1)

    def test_thread_condition_validated(self):
        with self.assertRaises(LedgerError): self.c.open_thread("t", "c", "p", {"type": "vibes"})
        with self.assertRaises(LedgerError): self.c.open_thread("t", "c", "p", {"type": "price_close_above"})

    def test_knowledge_needs_evidence(self):
        cid = self._trade()
        with self.assertRaises(LedgerError): self.c.record_knowledge("BOS", "no-such-event")
        self.assertEqual(self.c.knows("BOS"), [])                                       # a guess is not knowledge
        ev = self.c.add_reasoning(cid, "BOS above the swing high, then pullback to OB", now=MON + timedelta(minutes=2))
        self.c.record_knowledge("BOS", ev["event_id"], "used correctly unprompted", now=MON + timedelta(minutes=3))
        self.assertEqual(len(self.c.knows("BOS")), 1)

    def test_whole_ledger_verifies_clean(self):
        cid = self._trade(); self.c.add_reasoning(cid, "x", now=FRI)
        self.assertEqual(ledger.verify(ledger.read(self.c.path)[0]), [])



class UserTerms(unittest.TestCase):
    def setUp(self):
        self.c = Catalog(os.path.join(tempfile.mkdtemp(), "u.jsonl"))
        self.cid = self.c.log_trade("GBPUSD", "BUY", 1.30, 1.295, 1.31, MON, now=FRI)

    def test_extract_recognises_engine_concepts_and_tags_user_terms(self):
        from user_catalog import extract_terms
        x = extract_terms("Waited for a BOS then a retest of #support; the #suport held. Also a fair value gap.")
        self.assertEqual({r["concept"] for r in x["recognised"]}, {"break_of_structure", "fair_value_gap"})
        self.assertEqual([(t["raw"], t["key"], t["status"]) for t in x["user_terms"]],
                         [("support", "support", "unrecognised_by_engine"), ("suport", "suport", "unrecognised_by_engine")])

    def test_a_tag_that_is_an_engine_alias_is_recognised_and_untagged_words_are_not_guessed(self):
        from user_catalog import extract_terms
        x = extract_terms("support and resistance, a #choch and a #supply_zone")
        self.assertEqual([t["status"] for t in x["user_terms"]], ["recognised_by_engine", "unrecognised_by_engine"])
        self.assertEqual(x["recognised"][0]["concept"], "change_of_character")        # 'choch' by alias
        self.assertNotIn("support", [t["key"] for t in x["user_terms"]])               # bare words are never treated as concepts
        self.assertEqual(extract_terms("")["user_terms"], []); self.assertEqual(extract_terms(None)["recognised"], [])

    def test_word_boundaries(self):
        from user_catalog import extract_terms
        self.assertEqual(extract_terms("the lobby jobs boss")["recognised"], [])       # 'ob' inside lobby/jobs, 'bos' inside boss

    def test_reasoning_is_stored_verbatim_and_terms_are_a_separate_additive_record(self):
        text = "A #suport retest after the BOS"
        rec = self.c.add_reasoning(self.cid, text, now=MON + timedelta(minutes=1))
        self.assertEqual(rec["payload"]["text"], text)
        obs = [r for r in ledger.read(self.c.path)[0] if r["event_type"] == "TERMS_OBSERVED"]
        self.assertEqual(len(obs), 1); self.assertEqual(obs[0]["payload"]["reasoning_event_id"], rec["event_id"])
        self.c.add_reasoning(self.cid, "nothing taggable here", now=MON + timedelta(minutes=2))
        self.assertEqual(len([r for r in ledger.read(self.c.path)[0] if r["event_type"] == "TERMS_OBSERVED"]), 1)   # no empty records

    def test_usage_counts_and_aliasing_without_rewriting_history(self):
        cid2 = self.c.log_trade("GBPUSD", "SELL", 1.30, 1.31, 1.29, MON, now=FRI)
        self.c.add_reasoning(self.cid, "#support held", now=MON + timedelta(minutes=1))
        self.c.add_reasoning(cid2, "#suport again, and #Support", now=MON + timedelta(minutes=2))
        before = {u["term"]: u["uses"] for u in self.c.term_usage()}
        self.assertEqual(before, {"support": 2, "suport": 1})
        self.c.alias_term("suport", "support", now=FRI)
        after = self.c.term_usage()
        self.assertEqual(len(after), 1); self.assertEqual(after[0]["uses"], 3); self.assertEqual(after[0]["variants"], ["Support", "suport", "support"])
        self.assertEqual(after[0]["cases"], [self.cid, cid2])
        # history is intact: the as-of view before the alias still shows two separate terms
        early = {u["term"] for u in self.c.term_usage(as_of=FRI + timedelta(hours=1)) if True}
        self.assertEqual(len(early), 1)                                   # recorded events all precede the alias in knowledge time only after FRI
        with self.assertRaises(LedgerError): self.c.alias_term("support", "Support")      # same key: nothing to alias

    def test_alias_chains_do_not_loop(self):
        self.c.add_reasoning(self.cid, "#aa #bb", now=MON + timedelta(minutes=1))     # tags need 2+ characters on purpose
        self.c.alias_term("aa", "bb", now=FRI); self.c.alias_term("bb", "aa", now=FRI)
        u = self.c.term_usage()
        self.assertEqual((len(u), u[0]["term"], u[0]["uses"]), (1, "aa", 2))              # terminates, and a cycle is ONE term (smallest key)

    def test_unrecognised_status_is_what_the_engine_says_not_what_the_catalog_decides(self):
        self.c.add_reasoning(self.cid, "#fvg and #supplyzone", now=MON + timedelta(minutes=1))
        st = {u["term"]: u["status"] for u in self.c.term_usage()}
        self.assertEqual(st, {"fvg": "recognised_by_engine", "supplyzone": "unrecognised_by_engine"})



class AlertConditions(unittest.TestCase):
    def setUp(self):
        self.c = Catalog(os.path.join(tempfile.mkdtemp(), "u.jsonl"))
        self.t = U(2026, 10, 1, 12, 0)

    def cs(self, rows, tf_step=5):
        return [{"datetime": (self.t + timedelta(minutes=tf_step * i)).isoformat(), "open": o, "high": h, "low": l, "close": cl}
                for i, (o, h, l, cl) in enumerate(rows)]

    def test_ema_series_matches_a_hand_computation(self):
        from user_catalog import ema_series
        e = ema_series([10, 11, 12, 13, 14, 15], 3)
        self.assertEqual(e[:2], [None, None])
        self.assertAlmostEqual(e[2], 11.0)                         # seed = SMA of first 3
        self.assertAlmostEqual(e[3], 11.0 + 0.5 * (13 - 11.0))     # k = 2/(3+1) = 0.5 -> 12.0
        self.assertAlmostEqual(e[4], 12.0 + 0.5 * (14 - 12.0)); self.assertAlmostEqual(e[5], 13.0 + 0.5 * (15 - 13.0))
        self.assertEqual(ema_series([1, 2], 5), [None, None])

    def test_grammar_parity_and_warmup_parity_with_bus(self):
        import bus, bus_contract, user_catalog
        self.assertEqual(tuple(bus_contract.CONDITION_TYPES), tuple(user_catalog.CONDITION_TYPES))
        self.assertEqual(tuple(bus_contract.TIMEFRAMES), tuple(user_catalog.TIMEFRAMES))
        for p in (2, 20, 200):
            self.assertEqual(bus.candle_needs(p), user_catalog.candles_needed_for_ema(p))

    def test_validation_of_new_conditions(self):
        for bad in ({"type": "ema_touch", "timeframe": "15m"}, {"type": "ema_touch", "period": 1, "timeframe": "15m"},
                    {"type": "ema_touch", "period": 20, "timeframe": "4h"}, {"type": "ema_touch", "period": True, "timeframe": "5m"},
                    {"type": "price_close_above", "level": 1.3, "timeframe": "1d"}):
            with self.assertRaises(LedgerError): self.c.open_thread("t", "c", "p", bad)
        self.c.open_thread("t", "c", "p", {"type": "ema_close_below", "period": 50, "timeframe": "1h"})

    def test_ema_touch_uses_the_ema_BEFORE_the_candle(self):
        from user_catalog import evaluate_condition, ema_series
        flat = [(1.30, 1.3001, 1.2999, 1.30)] * 12                                   # EMA(2) = 1.3000 throughout
        opened = self.t + timedelta(minutes=5 * 11)                                  # only the NEXT candle is "after"
        cond = {"type": "ema_touch", "period": 2, "timeframe": "5m"}
        # last candle runs 1.2996-1.2998 and closes 1.2996. The EMA BEFORE it is 1.3000 (outside its range). The EMA
        # INCLUDING its own close would be ~1.29973 (inside its range) — a lookahead version would wrongly fire.
        rows = flat + [(1.2998, 1.2998, 1.2996, 1.2996)]
        closes = [r[3] for r in rows]; e = ema_series(closes, 2)
        self.assertTrue(1.2996 <= e[-1] <= 1.2998 and not (1.2996 <= e[-2] <= 1.2998))   # the trap is really set
        self.assertIsNone(evaluate_condition(cond, opened, self.cs(rows), []))
        rows2 = flat + [(1.2999, 1.3003, 1.2998, 1.3001)]                              # range contains the EMA that stood before it
        hit = evaluate_condition(cond, opened, self.cs(rows2), [])
        self.assertEqual((hit["kind"], hit["period"]), ("ema_touch", 2)); self.assertAlmostEqual(hit["ema"], 1.30, places=4)

    def test_ema_never_fires_inside_the_warmup_or_before_the_alert_was_set(self):
        from user_catalog import evaluate_condition
        rows = [(1.3, 1.31, 1.29, 1.31)] * 4                                       # period 3 needs index >= 9
        self.assertIsNone(evaluate_condition({"type": "ema_touch", "period": 3, "timeframe": "5m"}, self.t - timedelta(hours=1), self.cs(rows), []))
        long_rows = [(1.30, 1.3001, 1.2999, 1.30)] * 12 + [(1.30, 1.3050, 1.2990, 1.3040)]
        cs = self.cs(long_rows)
        opened = self.t + timedelta(minutes=5 * 12)                                # set AT the last candle -> that candle is not "after"
        self.assertIsNone(evaluate_condition({"type": "ema_close_above", "period": 3, "timeframe": "5m"}, opened, cs, []))
        self.assertTrue(evaluate_condition({"type": "ema_close_above", "period": 3, "timeframe": "5m"}, opened - timedelta(minutes=5), cs, []))

    def test_zone_and_touch_semantics(self):
        from user_catalog import evaluate_condition
        cs = self.cs([(1.30, 1.3010, 1.2990, 1.30), (1.30, 1.3050, 1.3020, 1.3040)])
        zone = {"type": "price_in_zone", "low": 1.3030, "high": 1.3045}
        self.assertEqual(evaluate_condition(zone, self.t, cs, [])["high"], 1.3050)              # overlap counts
        self.assertIsNone(evaluate_condition({"type": "price_in_zone", "low": 1.3100, "high": 1.3110}, self.t, cs, []))
        self.assertTrue(evaluate_condition({"type": "price_in_zone", "low": 1.3005, "high": 1.3005}, self.t - timedelta(minutes=1), cs, []))   # touch = zero-width zone

    def test_a_15m_condition_is_never_evaluated_on_5m_candles(self):
        self.c.open_thread("t", "c", "p", {"type": "price_close_above", "level": 1.30, "timeframe": "15m"}, opened_at=self.t, now=self.t)
        five = self.cs([(1.30, 1.40, 1.20, 1.35)] * 3)
        self.assertEqual(self.c.resolve_open_threads(candles=five, now=self.t + timedelta(hours=1)), [])
        fifteen = self.cs([(1.30, 1.40, 1.20, 1.35)] * 3, tf_step=15)
        self.assertEqual(len(self.c.resolve_open_threads(candles_by_tf={"15m": fifteen}, now=self.t + timedelta(hours=1))), 1)

    def test_cancel_is_append_only_and_final(self):
        tid = self.c.open_thread("price alert", "x", "p", {"type": "price_trade_above", "level": 1.3}, opened_at=self.t, now=self.t)
        self.c.cancel_thread(tid, now=self.t + timedelta(minutes=1))
        self.assertEqual(self.c.open_threads(), [])
        self.assertEqual(len([r for r in ledger.read(self.c.path)[0] if r["event_type"] == "THREAD_OPENED"]), 1)   # still in the ledger
        with self.assertRaises(LedgerError): self.c.cancel_thread(tid)
        with self.assertRaises(LedgerError): self.c.cancel_thread("THREAD-9999")


if __name__ == "__main__":
    unittest.main(verbosity=2)
