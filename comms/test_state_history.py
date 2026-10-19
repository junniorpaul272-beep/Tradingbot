"""Run: python test_state_history.py -- the per-scan state history: what is written, what is never written,
and that a past moment is answered from the past (never from today's state.json)."""
import ast, copy, json, os, re, shutil, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import ledger, state_history as sh, bus_adapters as ba, bus_contract as bc, bus_runner as br, bus

FIX = os.path.join(ROOT, "fixture_real_state.json")
U = lambda *a: datetime(*a, tzinfo=timezone.utc)
T0 = U(2026, 10, 14, 9, 0)
m = lambda n: T0 + timedelta(minutes=n)


def base_state(i=0, **over):
    """A synthetic but realistically shaped state for scan i (5 minutes apart)."""
    t = m(5 * i)
    s = {"scan_id": 100 + i, "market_data_timestamp": t.isoformat(), "timestamp": (t + timedelta(seconds=3)).isoformat(),
         "code_version": "abc123abc123", "structure_digest": {"regime": {"macro_bias": "BULLISH", "macro_bias_stale": False}},
         "market_phase": "expansion", "market_phase_age_bars": 30 + i, "market_thesis_current_state": "Bullish Expansion",
         "market_thesis_failure_risk": "LOW", "market_thesis_volatility_hint": "flat", "market_thesis_break_count": 1,
         "market_leg_age_status": {"status": "DEVELOPING", "engine_phase": "expansion", "aging_reason": None, "break_count": 1, "dist_in_atr": 0.4},
         "macro_leg_direction": "BULLISH", "macro_leg_origin": 1.32022, "macro_leg_extreme": 1.32755, "macro_leg_origin_time": "2026-10-13T12:00:00+00:00",
         "leg_owner": {"leg_id": "BULLISH|1.32022|1.32755", "tier": "TIER_3_STRUCTURE", "status": "WATCHING", "upgraded": False,
                       "claimed_at": (t + timedelta(seconds=2)).isoformat()},
         "mil_understanding": {"thesis_status": "SUPPORTED", "primary_thesis": {"direction": "BULLISH"}, "counter_thesis": None}}
    s.update(over); return s


def _rd(p, js=False):
    with open(p) as f:
        return json.load(f) if js else f.read()


class Root:
    def setUp(self):
        self.root = tempfile.mkdtemp(); self.log = os.path.join(self.root, sh.LOG_NAME)

    def put(self, state):
        with open(os.path.join(self.root, "state.json"), "w") as f:
            json.dump(state, f)

    def recs(self):
        return ledger.read(self.log)[0]


class Writing(Root, unittest.TestCase):
    def test_a_scan_is_recorded_once_with_both_times_and_a_hash(self):
        self.put(base_state(0)); r = sh.record(self.root, now=m(1))
        self.assertEqual(r["status"], "recorded")
        rec = self.recs()[0]
        self.assertEqual((rec["event_type"], rec["scan_id"], rec["ts"]), (sh.SNAPSHOT, 100, m(0).isoformat()))
        self.assertEqual(rec["recorded_at"], m(1).isoformat())             # observed != recorded
        self.assertEqual(ledger.verify(self.recs()), [])
        self.assertEqual(rec["payload"]["state_hash"], sh.state_hash(base_state(0)))
        self.assertEqual(rec["payload"]["payload_schema"], sh.PAYLOAD_SCHEMA)

    def test_rerunning_the_same_scan_never_duplicates(self):
        self.put(base_state(0))
        self.assertEqual(sh.record(self.root, now=m(1))["status"], "recorded")
        self.assertEqual(sh.record(self.root, now=m(2))["status"], "duplicate")
        self.assertEqual(sh.record(self.root, now=m(9))["status"], "duplicate")
        self.assertEqual(len(self.recs()), 1)
        self.put(base_state(1)); self.assertEqual(sh.record(self.root, now=m(6))["status"], "recorded")
        self.assertEqual(len(self.recs()), 2)

    def test_a_scan_that_returned_early_leaves_state_untouched_and_adds_nothing(self):
        self.put(base_state(0)); sh.record(self.root, now=m(1))
        self.assertEqual(sh.record(self.root, now=m(6))["status"], "duplicate")     # next 5-minute run, state not rewritten

    def test_nothing_is_written_for_missing_corrupt_or_untimed_state(self):
        self.assertEqual(sh.record(self.root, now=m(1))["status"], "no_state")
        with open(os.path.join(self.root, "state.json"), "w") as f:
            f.write("{truncated")
        self.assertEqual(sh.record(self.root, now=m(1))["status"], "no_state")
        s = base_state(0); s.pop("market_data_timestamp"); self.put(s)
        self.assertEqual(sh.record(self.root, now=m(1))["status"], "unusable_state")
        self.put(base_state(0, market_data_timestamp="not-a-time"))
        self.assertEqual(sh.record(self.root, now=m(1))["status"], "unusable_state")
        self.put(base_state(0, market_data_timestamp="2026-10-14T09:00:00"))         # naive: ambiguous, refused
        self.assertEqual(sh.record(self.root, now=m(1))["status"], "unusable_state")
        self.assertFalse(os.path.exists(self.log))

    def test_a_future_dated_state_is_refused_not_recorded(self):
        self.put(base_state(0)); r = sh.record(self.root, now=m(-30))
        self.assertEqual(r["status"], "refused"); self.assertFalse(os.path.exists(self.log))

    def test_absent_is_null_never_false_or_zero(self):
        s = base_state(0); s.pop("leg_owner"); s["macro_leg_direction"] = None; s["market_leg_age_status"] = None
        s["structure_digest"] = {"regime": {"macro_bias": "SIDEWAYS"}}
        p = sh.extract(s)
        self.assertIsNone(p["leg_owner"]); self.assertIsNone(p["macro_leg"]); self.assertIsNone(p["leg_age_status"])
        self.assertIsNone(p["htf_bias"]); self.assertIsNone(p["htf_bias_stale"])      # unrecognised bias is not guessed

    def test_the_payload_carries_no_engine_prose(self):
        s = base_state(0, market_thesis_trend_health="Healthy - 1 break(s)", market_thesis_narrative="long prose")
        txt = json.dumps(sh.extract(s))
        self.assertNotIn("Healthy", txt); self.assertNotIn("long prose", txt)

    def test_the_real_fixture_extracts(self):
        if not os.path.exists(FIX):
            self.skipTest("fixture not alongside")
        p = sh.extract(_rd(FIX, True))
        self.assertEqual((p["htf_bias"], p["mil_status"], p["mil_primary_direction"]), ("BEARISH", "TRANSITIONING", "BULLISH"))
        self.assertIsNone(p["leg_age_status"])                                          # that file predates the field: absent, not invented


class Reconstruction(Root, unittest.TestCase):
    def fill(self, n=4):
        for i in range(n):
            self.put(base_state(i)); sh.record(self.root, now=m(5 * i + 1))

    def test_an_earlier_view_does_not_change_when_the_current_state_changes(self):
        self.fill(3)
        before = sh.snapshot_as_of(self.recs(), m(6))
        self.put(base_state(3, market_phase="exhaustion", **{"structure_digest": {"regime": {"macro_bias": "BEARISH", "macro_bias_stale": False}}}))
        sh.record(self.root, now=m(16))
        after = sh.snapshot_as_of(self.recs(), m(6))
        self.assertEqual(before["record"]["event_id"], after["record"]["event_id"])
        self.assertEqual(after["record"]["payload"]["htf_bias"], "BULLISH")             # the past still says what it said then
        self.assertEqual(sh.snapshot_as_of(self.recs(), m(17))["record"]["payload"]["htf_bias"], "BEARISH")

    def test_no_hindsight_a_record_written_after_T_is_invisible_at_T(self):
        self.fill(1)
        self.assertIsNotNone(sh.snapshot_as_of(self.recs(), m(1)))
        self.assertIsNone(sh.snapshot_as_of(self.recs(), m(0)))                         # observed at m(0) but only recorded at m(1)

    def test_missing_history_is_none_never_a_fabricated_state(self):
        self.assertIsNone(sh.snapshot_as_of([], m(5)))
        self.fill(2)
        self.assertIsNone(sh.snapshot_as_of(self.recs(), m(-60)))
        res = ba.adapt_state_history(ledger.as_of(self.recs(), m(-60)))
        self.assertEqual((res.claims, res.gaps), ([], []))
        a = bc.resolve("market_state", res.claims, m(-60), gaps=res.gaps)
        self.assertEqual(a.status, "NO_EVIDENCE")

    def test_a_correction_keeps_the_original_and_is_only_seen_after_it_was_known(self):
        self.fill(2)
        orig = self.recs()[1]
        fix = sh.append_correction(self.log, orig["event_id"], {"htf_bias": "BEARISH"}, "bias was mis-read", now=m(50))
        self.assertEqual(ledger.verify(self.recs()), [])
        self.assertEqual(len(self.recs()), 3)                                           # original still on disk
        at_before = sh.snapshot_as_of(self.recs(), m(40))["record"]
        self.assertEqual((at_before["event_id"], at_before["payload"]["htf_bias"]), (orig["event_id"], "BULLISH"))
        at_after = sh.snapshot_as_of(self.recs(), m(51))["record"]
        self.assertEqual((at_after["event_id"], at_after["payload"]["htf_bias"]), (fix["event_id"], "BEARISH"))
        self.assertEqual(at_after["payload"]["correction_reason"], "bias was mis-read")
        self.assertEqual(at_after["ts"], orig["ts"])                                    # the state did not move in time
        with self.assertRaises(ledger.LedgerError): sh.append_correction(self.log, orig["event_id"], {}, "")
        with self.assertRaises(ledger.LedgerError): sh.append_correction(self.log, "nope", {}, "why")


class Adapter(Root, unittest.TestCase):
    def fill(self, states):
        for i, s in enumerate(states):
            self.put(s); sh.record(self.root, now=m(5 * i + 1))
        return ledger.read(self.log)[0]

    def test_claims_have_real_authorities_and_snapshot_times(self):
        recs = self.fill([base_state(0)])
        res = ba.adapt_state_history(recs); self.assertEqual(res.errors, [])
        c = {x.subject: x for x in res.claims}
        self.assertEqual(c["htf.bias"].value, "BULLISH")
        self.assertEqual(c["leg.owner"].value, {"leg_id": "BULLISH|1.32022|1.32755", "tier": "TIER_3_STRUCTURE", "status": "WATCHING", "upgraded": False})
        self.assertEqual(c["leg.age_status"].value["status"], "DEVELOPING")
        self.assertEqual(c["htf.bias"].observed_at, m(0)); self.assertEqual(c["htf.bias"].recorded_at, m(1))
        self.assertNotIn("thesis.primary", c)                                           # current-only on purpose

    def test_value_shapes_match_adapt_state_so_history_never_conflicts_with_now(self):
        s = base_state(0)
        recs = self.fill([s]); h = {x.subject: x for x in ba.adapt_state_history(recs).claims}
        cur = {x.subject: x for x in ba.adapt_state(s).claims}
        for subj in ("htf.bias", "thesis.status", "thesis.phase", "leg.macro", "leg.owner"):
            self.assertEqual(h[subj].value, cur[subj].value, subj)

    def test_what_changed_sees_a_real_flip_between_two_snapshots(self):
        flip = base_state(1, **{"structure_digest": {"regime": {"macro_bias": "BEARISH", "macro_bias_stale": False}}})
        recs = self.fill([base_state(0), flip])
        res = ba.adapt_state_history(recs)
        a = bc.resolve("what_changed", res.claims, m(8), gaps=res.gaps)
        got = [(i.label, i.claim.value) for i in a.items if i.claim.subject == "htf.bias"]
        self.assertEqual(got, [("PREVIOUS", "BULLISH"), ("CURRENT", "BEARISH")])
        early = bc.resolve("what_changed", res.claims, m(2), gaps=res.gaps)             # before the flip was recorded
        self.assertEqual([(i.label, i.claim.value) for i in early.items if i.claim.subject == "htf.bias"], [("UNCHANGED", "BULLISH")])

    def test_a_released_leg_owner_becomes_a_gap_not_a_stale_owner(self):
        gone = base_state(1); gone.pop("leg_owner")
        recs = self.fill([base_state(0), gone])
        res = ba.adapt_state_history(recs)
        a = bc.resolve("market_state", res.claims, m(8), gaps=res.gaps)
        self.assertIn("leg.owner", [x.subject for x in a.missing])
        self.assertNotIn("leg.owner", [i.claim.subject for i in a.items])

    def test_stale_bias_and_unknown_mil_are_gaps_not_values(self):
        recs = self.fill([base_state(0, **{"structure_digest": {"regime": {"macro_bias": "BULLISH", "macro_bias_stale": True}}},
                                    mil_understanding={"thesis_status": "UNKNOWN", "primary_thesis": None, "counter_thesis": None})])
        res = ba.adapt_state_history(recs)
        g = {x.subject: x.state for x in res.gaps}
        self.assertEqual((g["htf.bias"], g["thesis.status"]), ("STALE", "UNKNOWN"))
        self.assertNotIn("htf.bias", [c.subject for c in res.claims])

    def test_a_correction_supersedes_inside_the_adapter(self):
        recs = self.fill([base_state(0)])
        sh.append_correction(self.log, recs[0]["event_id"], {"htf_bias": "BEARISH"}, "fix", now=m(30))
        recs = ledger.read(self.log)[0]
        self.assertEqual({c.subject: c.value for c in ba.adapt_state_history(recs).claims}["htf.bias"], "BEARISH")
        self.assertEqual({c.subject: c.value for c in ba.adapt_state_history(ledger.as_of(recs, m(10))).claims}["htf.bias"], "BULLISH")

    def test_snapshot_event_name_is_pinned(self):
        self.assertEqual(ba.STATE_SNAPSHOT, sh.SNAPSHOT); self.assertEqual(ba.BIAS_VALUES, sh.BIAS_VALUES)


class Runner(Root, unittest.TestCase):
    def test_collect_adds_only_strictly_older_snapshots_and_never_conflicts(self):
        from user_catalog import Catalog
        for i in range(3):
            self.put(base_state(i)); sh.record(self.root, now=m(5 * i + 1))
        cur = base_state(2)                                                             # state.json is the latest scan
        self.put(cur)
        cat = Catalog(os.path.join(self.root, br.LEDGER_NAME))
        adapted, _ = br.collect(self.root, cat, m(20))
        a = bc.resolve("market_state", adapted.claims, m(20), gaps=adapted.gaps)
        self.assertEqual(a.conflicts, ())
        ts = [c.observed_at for c in adapted.claims if c.ref.startswith("state_history_log.jsonl")]
        self.assertTrue(ts and max(ts) < m(10))                                         # the newest snapshot (== current) is excluded
        r = bus.respond("/changed", adapted.claims, adapted.gaps, m(20))
        self.assertFalse(r.used_fallback, r.violations)

    def test_old_history_beyond_the_window_is_not_read_and_a_missing_log_is_fine(self):
        from user_catalog import Catalog
        self.put(base_state(0)); sh.record(self.root, now=m(1))
        cat = Catalog(os.path.join(self.root, br.LEDGER_NAME))
        adapted, _ = br.collect(self.root, cat, m(1) + timedelta(days=30))
        self.assertFalse([c for c in adapted.claims if c.ref.startswith("state_history_log.jsonl")])
        os.remove(self.log)
        adapted, _ = br.collect(self.root, cat, m(20)); self.assertTrue(adapted.claims)   # no log, no crash

    def test_read_tail_is_whole_lines_and_says_when_it_truncated(self):
        for i in range(60):
            self.put(base_state(i)); sh.record(self.root, now=m(5 * i + 1))
        full, _ = ledger.read(self.log)
        part, bad, trunc = ledger.read_tail(self.log, 8000)
        self.assertTrue(trunc and bad == 0 and 0 < len(part) < len(full))
        self.assertEqual([r["event_id"] for r in part], [r["event_id"] for r in full[-len(part):]])
        self.assertFalse(ledger.read_tail(self.log, 10_000_000)[2])
        self.assertEqual(ledger.read_tail(os.path.join(self.root, "none.jsonl")), ([], 0, False))


class MomentumHistory(unittest.TestCase):
    def test_raw_readings_are_recorded_verbatim_without_labels_and_absent_stays_null(self):
        import momentum_read as mr
        from datetime import datetime, timedelta, timezone
        t0 = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        cs = [{"t": t0 + i * timedelta(minutes=15), "o": 1.3 + i * 0.0003, "h": 1.3004 + i * 0.0003, "l": 1.2996 + i * 0.0003, "c": 1.3 + (i + 1) * 0.0003} for i in range(60)]
        m = mr.compute(cs, t0 + 60 * timedelta(minutes=15)); m["as_of_scan_id"] = 7
        s = {"market_data_timestamp": "2026-10-14T11:15:00+00:00", "scan_id": 7, "momentum_state": m}
        got = sh.extract(s)["momentum"]
        self.assertEqual(got["direction"]["value"], m["direction"]["value"])
        self.assertEqual(got["expansion"]["status"], m["expansion"]["status"])
        self.assertTrue(got["current"]); self.assertEqual(got["threshold_version"], "v0-unfrozen")
        self.assertNotIn("label", got["direction"]); self.assertNotIn("provisional_label", got["direction"])
        s["scan_id"] = 8
        self.assertFalse(sh.extract(s)["momentum"]["current"])                      # a leftover record is marked as such
        del s["momentum_state"]
        self.assertIsNone(sh.extract(s)["momentum"])


class DirectionHistory(unittest.TestCase):
    def test_per_timeframe_directions_are_recorded_and_copies_are_not(self):
        s = {"market_data_timestamp": "2026-10-14T11:15:00+00:00", "macro_bias_confirmed": "BULLISH",
             "market_thesis_mtf_5m": {"m5_direction": "BEARISH"},
             "market_event_prev_snapshot": {"macro_bias": "BULLISH"}, "undelivered_signal_events": [{"macro_bias": "BULLISH"}]}
        d = sh.extract(s)["direction_fields"]
        self.assertEqual(d["market_thesis_mtf_5m.m5_direction"], "BEARISH")
        self.assertEqual(d["macro_bias_confirmed"], "BULLISH")
        self.assertIsNone(d["leg15_direction"])                       # absent stays null, never invented
        self.assertEqual(set(d), set(sh.DIRECTION_PATHS))
        self.assertFalse([p for p in d if "prev_snapshot" in p or "undelivered" in p or "history" in p])


class Mutations(Root, unittest.TestCase):
    """A guard that has never been shown the corruption it guards against is not evidence."""

    def test_M1_without_dedupe_a_rerun_duplicates(self):
        orig = sh._is_duplicate
        try:
            sh._is_duplicate = lambda path, key: False
            self.put(base_state(0)); sh.record(self.root, now=m(1)); sh.record(self.root, now=m(2))
            self.assertEqual(len(self.recs()), 2)                                      # the mutant duplicates ...
        finally:
            sh._is_duplicate = orig
        shutil.rmtree(self.root); self.setUp()
        self.put(base_state(0)); sh.record(self.root, now=m(1)); sh.record(self.root, now=m(2))
        self.assertEqual(len(self.recs()), 1)                                          # ... the real one does not

    def test_M2_stamping_ts_with_the_write_time_would_leak_hindsight_and_is_caught(self):
        self.put(base_state(0)); sh.record(self.root, now=m(30))                      # the scan ran at m(0), written at m(30)
        rec = self.recs()[0]
        self.assertEqual(rec["ts"], m(0).isoformat())
        self.assertIsNone(sh.snapshot_as_of(self.recs(), m(10)))                       # not knowable at m(10)
        mutant = dict(rec, ts=rec["recorded_at"])
        self.assertIsNone(sh.snapshot_as_of([mutant], m(10)))                          # (also invisible: both times m(30))
        self.assertNotEqual(mutant["ts"], base_state(0)["market_data_timestamp"])      # but the observation time is now wrong: detectable

    def test_M3_overwriting_instead_of_correcting_is_caught_by_the_hash(self):
        self.put(base_state(0)); sh.record(self.root, now=m(1))
        with open(self.log) as f:
            line = json.loads(f.readline())
        line["payload"]["htf_bias"] = "BEARISH"
        with open(self.log, "w") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")
        self.assertEqual([p[0] for p in ledger.verify(self.recs())], ["EDITED"])


class Boundary(unittest.TestCase):
    def test_state_history_imports_only_ledger_and_stdlib_and_nothing_imports_it(self):
        t = ast.parse(_rd(os.path.join(HERE, "state_history.py"))); found = set()
        for n in ast.walk(t):
            if isinstance(n, ast.Import): found |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module: found.add(n.module.split(".")[0])
        self.assertLessEqual(found, {"hashlib", "json", "os", "sys", "datetime", "ledger"}, found)
        for name in ("bus.py", "bus_adapters.py", "bus_runner.py", "scanner_live.py", "inspector.py", "mil.py"):
            p = os.path.join(HERE, name) if os.path.exists(os.path.join(HERE, name)) else os.path.join(ROOT, name)
            if os.path.exists(p):
                self.assertNotIn("import state_history", _rd(p), name)

    def test_workflow_runs_it_after_the_scan_and_commits_and_validates_the_log(self):
        p = next((os.path.join(d, "live-scan.yml") for d in (ROOT, os.path.join(ROOT, ".github", "workflows"), HERE)
                  if os.path.exists(os.path.join(d, "live-scan.yml"))), os.path.join(ROOT, "live-scan.yml"))
        if not os.path.exists(p):
            self.skipTest("live-scan.yml not alongside")
        y = _rd(p)
        self.assertLess(y.index("python scanner_live.py"), y.index("python comms/state_history.py ."))
        self.assertLess(y.index("python comms/state_history.py ."), y.index("python comms/bus_runner.py ."))
        self.assertEqual(y.count("state_history_log.jsonl; do"), 2)                    # validated AND committed
        step = y[y.index("name: Record state history"):y.index("name: Inspect integrity")]
        self.assertNotIn("if: always()", step); self.assertNotIn("if: failure()", step)  # a failed scan skips it


if __name__ == "__main__":
    unittest.main(verbosity=2)
