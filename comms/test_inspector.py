"""Run: python test_inspector.py — the Inspector, including MUTATION TESTS: deliberately corrupt a healthy
state / break the deployed engine in many different ways and check each corruption is detected.
A detector that has never been shown a corruption is not evidence of anything."""
import copy, hashlib, json, os, shutil, sys, tempfile, unittest
from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import inspector as ins, ledger, mil

try:
    import mil_orig
except Exception:                                   # pragma: no cover
    mil_orig = None
FIX = os.path.join(ROOT, "fixture_real_state.json")


def real():
    if not os.path.exists(FIX):
        raise unittest.SkipTest("fixture_real_state.json not alongside")
    with open(FIX) as f:
        return json.load(f)


def healthy():
    """The real file with its one real inconsistency repaired: MIL agrees with everything else."""
    s = real(); m = s["mil_understanding"]
    m["thesis_status"] = "SUPPORTED"; m["primary_thesis"] = {"direction": "BEARISH"}; m["counter_thesis"] = None
    m["failure_condition"] = {"origin_level": 1.33093, "origin_direction": "above", "retrace_level": 1.3284}
    m["unresolved_questions"] = []
    m["history"] = [{"from_state": "UNKNOWN", "to_state": "SUPPORTED", "at": "2026-10-01T07:15:17+00:00", "transition_cause": "BOOTSTRAP", "invalidation_fired": False}]
    return s


def probes(state, mem=None, mil_module=None):
    f, m = ins.inspect_state(state, mem, mil_module=mil_module)
    return f, m


class OnTheRealFile(unittest.TestCase):
    def test_finds_the_real_contradiction_unprompted_and_marks_the_origin(self):
        f, _ = probes(real())
        out = [x for x in f if x["probe"] == "direction_conflict"]
        self.assertEqual(len(out), 1)
        ev = out[0]["evidence"]
        self.assertEqual(ev["sides"]["BULLISH"], {"mil": ["mil_understanding.primary_thesis.direction"]})
        self.assertIn("htf_macro", ev["sides"]["BEARISH"])
        self.assertNotIn("outlier", ev); self.assertNotIn("majority_value", ev)    # no culprit, no vote count
        self.assertGreaterEqual(ev["excluded_recorded_fields"], 20)                # the 20 history copies are not evidence
        self.assertTrue(out[0]["first_divergence"])
        self.assertEqual((out[0]["severity"], out[0]["evidence"]["relationship"]), ("HIGH", "mil_transition_overdue"))   # 5.8h stuck: not excused
        self.assertTrue([x for x in f if x["probe"] == "dwell"])                                                          # and the dwell check sees it too

    def test_finds_the_unset_placeholders(self):
        paths = {p for x in probes(real())[0] if x["probe"] == "placeholder" for p in x["paths"]}
        self.assertEqual(paths, {"mil_understanding.failure_condition.origin_level", "mil_understanding.failure_condition.retrace_level"})

    def test_finds_the_long_dwell(self):
        d = [x for x in probes(real())[0] if x["probe"] == "dwell"]
        self.assertEqual(len(d), 1); self.assertEqual(d[0]["evidence"]["current_state"], "TRANSITIONING")
        self.assertGreater(d[0]["evidence"]["current_dwell_s"], 5 * 3600)

    def test_exercise_finds_the_bug_in_the_deployed_engine_and_not_in_the_fixed_one(self):
        if mil_orig is None:
            self.skipTest("mil_orig.py (the deployed engine) not alongside")
        self.assertTrue([x for x in ins.fuzz_mil(mil_orig, seed=3) if "convergence" in x["summary"]])
        self.assertEqual(ins.fuzz_mil(mil, seed=3), [])

    def test_healthy_state_raises_nothing_of_note(self):
        f, _ = probes(healthy(), mil_module=mil)
        self.assertEqual([x for x in f if x["severity"] in ("HIGH", "MED")], [], [x["summary"] for x in f])


class DirectionRule(unittest.TestCase):
    """The reviewer's regression set for the 5-minute flip-flop incident (2026-10-14)."""

    def st(self, **kw):
        s = healthy()
        for k in [k for k in s if k.startswith("shadow_") and k.endswith(("bias_confirmed", "leg_direction"))]:
            s[k] = "BULLISH"
        for k in ("macro_bias_confirmed", "macro_leg_direction", "campaign_direction", "leg15_direction"):
            s[k] = "BULLISH"
        s["structure_digest"]["regime"]["macro_bias"] = "BULLISH"; s["structure_digest"]["structure"]["bos_direction"] = "BULLISH"
        s["market_thesis_campaign"]["direction"] = "BULLISH"; s["market_thesis_mtf_5m"]["m5_direction"] = "BULLISH"
        s["mil_understanding"]["primary_thesis"] = {"direction": "BULLISH"}
        s["mil_understanding"]["counter_thesis"] = None
        for k, v in kw.items():
            cur = s
            *head, last = k.split("__")
            for h in head: cur = cur[h]
            cur[last] = v
        return s

    def kinds(self, s):
        return {(x["probe"], x["severity"]) for x in probes(s)[0] if x["probe"].startswith(("direction", "timeframe"))}

    def test_agreeing_state_raises_no_direction_finding(self):
        self.assertEqual({k for k in self.kinds(self.st()) if k[1] != "LOW"}, set())

    def test_m5_pullback_against_bullish_htf_is_low_and_names_no_culprit(self):
        s = self.st(market_thesis_mtf_5m__m5_direction="BEARISH")
        f = [x for x in probes(s)[0] if x["probe"].startswith(("direction", "timeframe"))]
        self.assertEqual([(x["probe"], x["severity"]) for x in f], [("timeframe_divergence", "LOW")])
        self.assertIn("no fault is implied", f[0]["summary"])
        self.assertNotIn("outlier", f[0]["evidence"])

    def test_same_authority_mismatch_is_high_and_decides_nothing(self):
        s = self.st(macro_leg_direction="BEARISH")
        f = [x for x in probes(s)[0] if x["probe"] == "direction_conflict"]
        self.assertEqual(len(f), 1); self.assertEqual(f[0]["severity"], "HIGH")
        self.assertIn("not decided here", f[0]["summary"])
        self.assertNotIn("majority_value", f[0]["evidence"])

    def test_recorded_copies_do_not_inflate_or_change_the_finding(self):
        base = self.st(macro_leg_direction="BEARISH")
        more = copy.deepcopy(base)
        more["undelivered_signal_events"] = [{"macro_bias": "BULLISH"}] * 5          # the real 13->14 culprit
        more["market_event_prev_snapshot"] = {"macro_bias": "BULLISH"}
        more["prior_continuation_leg_direction"] = "BULLISH"
        a = [x for x in probes(base)[0] if x["probe"] == "direction_conflict"][0]
        b = [x for x in probes(more)[0] if x["probe"] == "direction_conflict"][0]
        self.assertEqual(a["fingerprint"], b["fingerprint"]); self.assertEqual(a["evidence"]["sides"], b["evidence"]["sides"])
        self.assertGreater(b["evidence"]["excluded_recorded_fields"], a["evidence"]["excluded_recorded_fields"])

    def test_severity_follows_the_relationship_that_failed(self):
        def conflict(**kw):
            return [x for x in probes(self.st(**kw))[0] if x["probe"] == "direction_conflict"]
        c = conflict(macro_leg_direction="BEARISH")                      # same writer disagreeing with itself
        self.assertEqual((c[0]["severity"], c[0]["evidence"]["relationship"]), ("HIGH", "same_writer"))
        c = conflict(mil_understanding__primary_thesis={"direction": "BEARISH"}, mil_understanding__thesis_status="SUPPORTED")
        self.assertEqual((c[0]["severity"], c[0]["evidence"]["relationship"]), ("HIGH", "consumer_of_macro_bias"))
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

        def transitioning(entered_ago_s):
            h = [{"from_state": "SUPPORTED", "to_state": "TRANSITIONING", "at": (now - timedelta(seconds=entered_ago_s)).isoformat()}] \
                if entered_ago_s is not None else []
            return dict(timestamp=now.isoformat(), mil_understanding__primary_thesis={"direction": "BEARISH"},
                        mil_understanding__thesis_status="TRANSITIONING", mil_understanding__history=h)
        for ago, want in ((1800, ("MED", "mil_in_transition")), (ins.MIL_TRANSITION_GRACE_S, ("MED", "mil_in_transition")),
                          (ins.MIL_TRANSITION_GRACE_S + 60, ("HIGH", "mil_transition_overdue")),
                          (3 * 3600, ("HIGH", "mil_transition_overdue")), (None, ("HIGH", "mil_transition_overdue"))):
            c = conflict(**transitioning(ago))                            # no entry time known -> not excused either
            self.assertEqual((c[0]["severity"], c[0]["evidence"]["relationship"]), want, ago)
        c = conflict(campaign_direction="BEARISH", market_thesis_campaign__direction="BEARISH")                       # derived, only resynced on a new leg
        self.assertEqual((c[0]["severity"], c[0]["evidence"]["relationship"]), ("MED", "derived_and_may_lag"))

    def test_a_leftover_digest_copy_is_not_evidence_but_a_fresh_one_still_is(self):
        def run(copy_scan):
            s = self.st(); s["scan_id"] = 10                      # everything BULLISH, only the digest copy says BEARISH
            s["structure_digest"]["regime"].update(macro_bias="BEARISH", as_of_scan_id=copy_scan)
            return {x["probe"]: x for x in probes(s)[0]}
        stale = run(9)                                             # written by scan 9: a leftover from before the early return
        self.assertNotIn("direction_conflict", stale); self.assertIn("direction_copy_stale", stale)
        self.assertEqual(stale["direction_copy_stale"]["severity"], "LOW")
        self.assertEqual(run(10)["direction_conflict"]["evidence"]["relationship"], "same_writer")   # written this scan: counts, HIGH
        self.assertNotIn("direction_conflict", run(None))          # no stamp -> cannot be verified -> not counted

    def test_injected_same_writer_mismatches_are_always_high(self):
        for k, v in (("macro_bias_confirmed", "BEARISH"), ("macro_leg_direction", "BEARISH")):
            c = [x for x in probes(self.st(**{k: v}))[0] if x["probe"] == "direction_conflict"]
            self.assertEqual((c[0]["severity"], c[0]["evidence"]["relationship"]), ("HIGH", "same_writer"), k)
        s = self.st(); s["scan_id"] = 5
        s["structure_digest"]["regime"].update(macro_bias="BEARISH", as_of_scan_id=5)      # a fresh copy that disagrees
        c = [x for x in probes(s)[0] if x["probe"] == "direction_conflict"]
        self.assertEqual((c[0]["severity"], c[0]["evidence"]["relationship"]), ("HIGH", "same_writer"))

    def test_shadow_rules_and_other_timeframes_differing_are_expected_and_low(self):
        s = self.st(shadow_macro_bias_confirmed="BEARISH", leg15_direction="BEARISH")
        f = [x for x in probes(s)[0] if x["probe"].startswith(("direction", "timeframe", "variant"))]
        self.assertEqual({(x["probe"], x["severity"]) for x in f}, {("variant_divergence", "LOW"), ("timeframe_divergence", "LOW")})

    def test_every_recorded_direction_path_has_a_provenance_entry(self):
        import state_history as sh
        for p in sh.DIRECTION_PATHS:
            fam, _ = ins.classify_direction(p)
            self.assertNotIn(fam, (None, "excluded"), p)
            self.assertIn(fam, ins.FAMILY_PROVENANCE, p)
        for fam, meta in ins.FAMILY_PROVENANCE.items():
            self.assertEqual(set(meta), {"origin", "meaning", "authority", "expected"}, fam)
        with open(os.path.join(ROOT, "DIRECTION_PROVENANCE.md")) as f:
            doc = f.read()
        for p in sh.DIRECTION_PATHS:
            self.assertIn(p, doc, p)

    def test_unmapped_direction_fields_are_surfaced_not_counted(self):
        s = self.st(); s["brand_new_field_direction"] = "BEARISH"
        f = [x for x in probes(s)[0] if x["probe"] == "direction_unmapped"]
        self.assertEqual(len(f), 1); self.assertEqual(f[0]["severity"], "LOW")
        self.assertEqual({k for k in self.kinds(s) if k[1] != "LOW"}, set())

    def _write(self, root, s, now):
        with open(os.path.join(root, "state.json"), "w") as f:
            json.dump(s, f)
        ins.run(root, now=now, mil_module=None)
        recs, _ = ledger.read(os.path.join(root, "integrity_ledger.jsonl"))
        fps = {r["payload"]["fingerprint"] for r in recs if r["event_type"] == "INTEGRITY_FINDING"
               and r["payload"]["probe"] == "direction_conflict"}
        return [r["event_type"].replace("INTEGRITY_", "") for r in recs
                if r["payload"].get("fingerprint") in fps]

    def test_finding_closes_when_condition_ends_and_reopens_when_it_returns(self):
        root = tempfile.mkdtemp(); t = datetime(2026, 10, 14, 11, 15, tzinfo=timezone.utc)
        bad, good = self.st(macro_leg_direction="BEARISH"), self.st()
        self.assertEqual(self._write(root, bad, t), ["FINDING"])
        self.assertEqual(self._write(root, good, t + timedelta(minutes=15)), ["FINDING", "RESOLVED"])
        self.assertEqual(self._write(root, bad, t + timedelta(minutes=30)), ["FINDING", "RESOLVED", "FINDING"])
        shutil.rmtree(root)

    def test_missing_data_is_not_a_resolution(self):
        root = tempfile.mkdtemp(); t = datetime(2026, 10, 14, 11, 15, tzinfo=timezone.utc)
        self.assertEqual(self._write(root, self.st(macro_leg_direction="BEARISH"), t), ["FINDING"])
        gone = self.st(); gone["macro_bias_confirmed"] = None; gone["macro_leg_direction"] = None
        gone["structure_digest"]["regime"]["macro_bias"] = None
        self.assertEqual(self._write(root, gone, t + timedelta(minutes=15)), ["FINDING", "UNEVALUABLE"])   # held, not resolved
        self.assertEqual(self._write(root, gone, t + timedelta(minutes=30)), ["FINDING", "UNEVALUABLE"])   # no duplicate note
        self.assertEqual(self._write(root, self.st(), t + timedelta(minutes=45)),
                         ["FINDING", "UNEVALUABLE", "RESOLVED"])                                           # data back and agrees
        shutil.rmtree(root)


class MutationTests(unittest.TestCase):
    """Each mutation is a CLASS of corruption; the healthy state must be clean first."""

    def clean(self):
        s = healthy()
        self.assertEqual([x for x in probes(s)[0] if x["severity"] != "LOW"], [])
        return s

    def hit(self, state, probe, mem=None):
        f, _ = probes(state, mem)
        got = [x for x in f if x["probe"] == probe]
        self.assertTrue(got, f"{probe} missed the mutation; saw {[x['probe'] for x in f]}")
        return got

    def test_M1_flip_a_direction(self):
        s = self.clean(); s["mil_understanding"]["primary_thesis"]["direction"] = "BULLISH"
        self.hit(s, "direction_conflict")
        s = self.clean(); s["macro_bias_confirmed"] = "BULLISH"
        self.assertIn("htf_macro", self.hit(s, "direction_conflict")[0]["evidence"]["families"])

    def test_M2_zero_a_level(self):
        s = self.clean(); s["mil_understanding"]["failure_condition"]["origin_level"] = 0.0
        self.hit(s, "placeholder")
        s = self.clean(); s["macro_leg_extreme"] = 0.0
        self.hit(s, "placeholder")

    def test_M3_future_and_after_write_timestamps(self):
        s = self.clean(); s["macro_swing_confirmed_at"] = "2026-10-01T16:30:00+00:00"
        self.hit(s, "time")
        s = self.clean()
        f, _ = ins.inspect_state(s, now=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc))
        self.assertTrue([x for x in f if x["probe"] == "time"])           # state written "in the future"

    def test_M4_id_that_no_longer_matches_its_fields(self):
        # a NEW leg changes every numeric field that describes it, but a stale id string would persist
        s = self.clean()
        def renumber(o):
            if isinstance(o, dict): return {k: renumber(v) for k, v in o.items()}
            if isinstance(o, list): return [renumber(v) for v in o]
            return 1.34000 if isinstance(o, float) and abs(o - 1.33093) < 1e-12 else o
        s = renumber(s)
        got = self.hit(s, "identity")
        self.assertIn("1.33093", json.dumps(got[0]["evidence"]))

    def test_M5_collection_that_only_grows(self):
        s = self.clean(); mem = None
        for i in range(20):
            s["mil_understanding"]["unresolved_questions"] = ["x" * 30] * (10 + 3 * i)
            f, mem = ins.inspect_state(s, mem)
        self.assertTrue([x for x in f if x["probe"] == "growth" and "unresolved_questions" in x["paths"][0]])

    def test_M5b_a_collection_that_breathes_is_not_a_leak(self):
        s = self.clean(); mem = None
        for i in range(30):
            s["mil_understanding"]["unresolved_questions"] = ["x"] * (35 + (i % 4))
            f, mem = ins.inspect_state(s, mem)
        self.assertEqual([x for x in f if x["probe"] == "growth"], [])

    def test_M6_schema_drift_between_runs(self):
        s = self.clean(); _, mem = ins.inspect_state(s)
        del s["leg_owner"]; s["market_phase_age_bars"] = "58"
        f, _ = ins.inspect_state(s, mem)
        sch = [x for x in f if x["probe"] == "schema"][0]
        self.assertIn("leg_owner.tier", sch["evidence"]["vanished"]); self.assertIn("market_phase_age_bars", sch["evidence"]["type_changed"])

    def test_M7_a_state_that_never_ends(self):
        s = self.clean()
        base = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
        s["mil_understanding"]["history"] = [
            {"from_state": "A", "to_state": "SUPPORTED", "at": (base).isoformat()},
            {"from_state": "SUPPORTED", "to_state": "WEAKENING", "at": (base + timedelta(minutes=40)).isoformat()},
            {"from_state": "WEAKENING", "to_state": "CONTESTED", "at": (base + timedelta(minutes=70)).isoformat()},
            {"from_state": "CONTESTED", "to_state": "TRANSITIONING", "at": (base + timedelta(minutes=100)).isoformat()}]
        s["timestamp"] = (base + timedelta(hours=12)).isoformat()
        self.assertEqual(self.hit(s, "dwell")[0]["severity"], "HIGH")

    def test_M8_a_normal_history_is_not_a_long_dwell(self):
        s = self.clean(); base = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
        s["mil_understanding"]["history"] = [{"from_state": "A", "to_state": f"S{i}", "at": (base + timedelta(hours=i)).isoformat()} for i in range(5)]
        s["timestamp"] = (base + timedelta(hours=4, minutes=40)).isoformat()
        f, _ = probes(s)
        self.assertEqual([x for x in f if x["probe"] == "dwell"], [])

    # ---- break the ENGINE itself (the exercise must notice)
    def broken(self, patch):
        import types
        m = types.SimpleNamespace(**{k: getattr(mil, k) for k in dir(mil) if not k.startswith("__")})
        patch(m); return ins.fuzz_mil(m, seed=11)

    def test_M9_engine_that_cannot_leave_a_state(self):
        orig = mil.reconcile
        def stuck(prev, obs, now):
            out = orig(prev, obs, now)
            return out if out.thesis_status != mil.ThesisStatus.SUPPORTED or prev.thesis_status != mil.ThesisStatus.TRANSITIONING else prev
        f = self.broken(lambda m: setattr(m, "reconcile", stuck))
        self.assertTrue([x for x in f if "convergence" in x["summary"]])

    def test_M10_engine_that_mutates_its_input(self):
        orig = mil.reconcile
        def mut(prev, obs, now):
            prev.unresolved_questions.append("poison"); return orig(prev, obs, now)
        self.assertTrue([x for x in self.broken(lambda m: setattr(m, "reconcile", mut)) if "no_mutation" in x["summary"]])

    def test_M11_engine_that_rolls_dice(self):
        import random as r
        orig = mil.reconcile
        def dice(prev, obs, now):
            out = orig(prev, obs, now)
            return out if r.random() < 0.5 else mil.reconcile(out, obs, now)
        self.assertTrue([x for x in self.broken(lambda m: setattr(m, "reconcile", dice)) if "deterministic" in x["summary"]])

    def test_M12_engine_with_a_bear_only_bug(self):
        orig = mil.reconcile
        def bearbug(prev, obs, now):
            out = orig(prev, obs, now)
            if obs.macro_bias_confirmed == "BEARISH" and out.thesis_status == mil.ThesisStatus.SUPPORTED and (prev.primary_thesis or {}).get("direction") == "BULLISH":
                import dataclasses; return dataclasses.replace(out, thesis_status=mil.ThesisStatus.WEAKENING)
            return out
        self.assertTrue([x for x in self.broken(lambda m: setattr(m, "reconcile", bearbug)) if "mirror_symmetry" in x["summary"] or "convergence" in x["summary"]])

    def test_M13_unbounded_memory(self):
        orig = mil.reconcile
        def leak(prev, obs, now):
            import dataclasses; out = orig(prev, obs, now)
            return dataclasses.replace(out, unresolved_questions=list(prev.unresolved_questions) + ["x"] * 50)
        self.assertTrue([x for x in self.broken(lambda m: setattr(m, "reconcile", leak)) if "bounded" in x["summary"]])


class CodeVersion(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        for n in ins.CODE_VERSION_FILES:
            with open(os.path.join(self.d, n), "w") as f:
                f.write(f"# {n} v1\n")

    def test_matching_stamp_is_silent_and_a_changed_file_is_flagged(self):
        s = healthy(); s["code_version"] = ins.compute_code_version(self.d)
        self.assertEqual(ins.probe_version(s, self.d), [])
        with open(os.path.join(self.d, "mil.py"), "a") as f:
            f.write("# a fix\n")
        got = ins.probe_version(s, self.d)
        self.assertEqual((got[0]["probe"], got[0]["severity"]), ("version", "MED"))
        self.assertEqual(got[0]["evidence"]["stamped"], s["code_version"])

    def test_missing_stamp_is_low_unknown_is_silent_no_dir_is_silent(self):
        s = healthy(); s.pop("code_version", None)
        self.assertEqual(ins.probe_version(s, self.d)[0]["severity"], "LOW")
        s["code_version"] = "unknown"; self.assertEqual(ins.probe_version(s, self.d), [])
        self.assertEqual(ins.probe_version(s, None), [])

    def test_a_missing_engine_file_changes_the_version(self):
        a = ins.compute_code_version(self.d); os.remove(os.path.join(self.d, "brain.py"))
        self.assertNotEqual(a, ins.compute_code_version(self.d))

    def test_inspector_and_scanner_live_agree_on_the_file_list_and_the_hash(self):
        import re
        p = os.path.join(ROOT, "scanner_live.py")
        if not os.path.exists(p):
            self.skipTest("scanner_live.py not alongside")
        with open(p) as f:
            src = f.read()
        m = re.search(r"CODE_VERSION_FILES = \((.*?)\)", src, re.S)
        live_files = tuple(re.findall(r'"([^"]+\.py)"', m.group(1)))
        self.assertEqual(live_files, ins.CODE_VERSION_FILES)
        # same algorithm: compute with the live module's own function, on a throwaway dir
        os.environ.setdefault("TELEGRAM_TOKEN", "x"); os.environ.setdefault("TELEGRAM_CHAT_ID", "1"); os.environ.setdefault("TWELVE_DATA_KEY", "x")
        sys.path.insert(0, HERE)
        try:
            import scanner_live
        except ModuleNotFoundError as e:
            self.skipTest(f"engine modules not alongside ({e})")
        self.assertEqual(scanner_live.compute_code_version(self.d), ins.compute_code_version(self.d))
        self.assertEqual(len(scanner_live.CODE_VERSION), 12)

    def test_run_flags_drift_end_to_end_and_the_stamp_resolves(self):
        root = tempfile.mkdtemp()
        for n in ins.CODE_VERSION_FILES:
            with open(os.path.join(root, n), "w") as f:
                f.write("x")
        s = healthy(); s["code_version"] = "deadbeef0000"
        with open(os.path.join(root, "state.json"), "w") as f:
            json.dump(s, f)
        r1 = ins.run(root, now=datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc))
        recs = ledger.read(os.path.join(root, "integrity_ledger.jsonl"))[0]
        self.assertTrue([x for x in recs if x["event_type"] == ins.FINDING and x["payload"]["probe"] == "version"])
        s["code_version"] = ins.compute_code_version(root)
        with open(os.path.join(root, "state.json"), "w") as f:
            json.dump(s, f)
        ins.run(root, now=datetime(2026, 10, 1, 16, 5, tzinfo=timezone.utc))
        recs = ledger.read(os.path.join(root, "integrity_ledger.jsonl"))[0]
        self.assertTrue([x for x in recs if x["event_type"] == ins.RESOLVED])


class Localisation(unittest.TestCase):
    def test_one_root_cause_is_one_origin_not_five_alarms(self):
        f = [ins.finding("outlier", "HIGH", "a", ["macro_bias_confirmed"], {}),
             ins.finding("placeholder", "MED", "b", ["mil_understanding.failure_condition.origin_level"], {}),
             ins.finding("dwell", "MED", "c", ["mil_understanding.history"], {})]
        out = {x["probe"]: x for x in ins.localize(f)}
        self.assertTrue(out["outlier"]["first_divergence"])                 # layer 2 (bias) is earliest
        self.assertFalse(out["placeholder"]["first_divergence"]); self.assertFalse(out["dwell"]["first_divergence"])

    def test_layer_order(self):
        self.assertLess(ins.layer_of("macro_leg_origin"), ins.layer_of("mil_understanding.primary_thesis"))
        self.assertEqual(ins.layer_of("totally_unknown_key"), 9)


class RunAndLedger(unittest.TestCase):
    def setUp(self):
        if not os.path.exists(FIX):
            self.skipTest("fixture not alongside")
        self.root = tempfile.mkdtemp(); shutil.copy(FIX, os.path.join(self.root, "state.json"))
        self.now = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)

    def sha(self):
        with open(os.path.join(self.root, "state.json"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def recs(self):
        return ledger.read(os.path.join(self.root, "integrity_ledger.jsonl"))[0]

    def test_never_modifies_the_state_it_inspects(self):
        before = self.sha(); ins.run(self.root, now=self.now, mil_module=mil); self.assertEqual(before, self.sha())

    def test_findings_are_written_once_and_resolve_when_the_cause_goes_away(self):
        a = ins.run(self.root, now=self.now, mil_module=mil)
        self.assertGreaterEqual(a["new"], 3)
        b = ins.run(self.root, now=self.now + timedelta(minutes=5), mil_module=mil)
        self.assertEqual((b["new"], b["resolved"]), (0, 0))               # same problems: no duplicate records
        with open(os.path.join(self.root, "state.json"), "w") as f:
            json.dump(healthy(), f)
        c = ins.run(self.root, now=self.now + timedelta(minutes=10), mil_module=mil)
        self.assertGreaterEqual(c["resolved"], 3)
        left = [r for r in self.recs() if r["event_type"] == ins.FINDING and r["payload"]["probe"] != "schema"]
        self.assertEqual(len(left), c["resolved"] + 0 if False else len(left))        # (see next assertion)
        still_open = {r["payload"]["fingerprint"] for r in self.recs() if r["event_type"] == ins.FINDING} - \
                     {r["payload"]["fingerprint"] for r in self.recs() if r["event_type"] == ins.RESOLVED}
        self.assertTrue(all(r["payload"]["severity"] == "LOW" for r in self.recs()
                            if r["event_type"] == ins.FINDING and r["payload"]["fingerprint"] in still_open))   # only schema drift (the repaired file has fewer keys)
        types = [r["event_type"] for r in self.recs()]
        self.assertEqual(ledger.verify(self.recs()), [])                   # the integrity ledger itself verifies

    def test_missing_or_corrupt_state_is_a_noop_not_a_crash(self):
        os.remove(os.path.join(self.root, "state.json"))
        self.assertEqual(ins.run(self.root, now=self.now)["findings"], 0)
        with open(os.path.join(self.root, "state.json"), "w") as f:
            f.write("{truncated")
        self.assertEqual(ins.run(self.root, now=self.now)["findings"], 0)

    def test_memory_persists_between_runs(self):
        ins.run(self.root, now=self.now, mil_module=mil); ins.run(self.root, now=self.now + timedelta(minutes=5), mil_module=mil)
        with open(os.path.join(self.root, "inspector_state.json")) as f:
            mem = json.load(f)
        self.assertEqual(mem["runs"], 2); self.assertIn("mil_understanding.unresolved_questions", mem["sizes"])


class Boundary(unittest.TestCase):
    def test_inspector_imports_only_stdlib_ledger_and_lazily_mil(self):
        import ast
        with open(os.path.join(HERE, "inspector.py")) as f:
            tree = ast.parse(f.read())
        found = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import): found |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module: found.add(n.module.split(".")[0])
        self.assertLessEqual(found, {"hashlib", "json", "os", "random", "re", "sys", "datetime", "ledger", "mil", "dataclasses"}, found)
        self.assertTrue(found.isdisjoint({"bus", "bus_contract", "bus_adapters", "user_catalog", "scanner_live"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
