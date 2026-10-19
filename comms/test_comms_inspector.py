"""Run: python test_comms_inspector.py — the Comms Integrity Inspector, proven the only way an inspector can be: by BREAKING things on
purpose and checking it notices, by showing it does NOT cry wolf over honest 'not recorded yet' replies, and by checking it is
read-only and repeatable. Every injected defect is a monkeypatch applied for one test and removed afterwards."""
import glob, hashlib, json, os, shutil, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
COMMS_FOLDER = os.path.basename(HERE) == "comms"          # works in a repo with a comms/ folder AND in a flat one
ROOT = os.path.dirname(HERE) if COMMS_FOLDER else HERE; sys.path.append(ROOT)   # scanner files, fixtures, docs
import bus, bus_runner as br, ledger
import comms_inspector as ci

FAST = dict(run_layer=False, setups_only=("fresh",))


def rules(rep):
    return {f["rule"] for f in rep["findings"]}


def cats(rep):
    return {f["category"] for f in rep["findings"]}


class Patched(unittest.TestCase):
    """Replace bus.respond for one test. `fake(text, *a, **k)` returns a bus.Reply, or None to fall through to the real one."""
    def patch(self, fake):
        real = bus.respond
        def wrapper(text, *a, **k):
            out = fake(text, *a, **k)
            return out if out is not None else real(text, *a, **k)
        bus.respond = wrapper
        self.addCleanup(setattr, bus, "respond", real)


class RealCodeIsClean(unittest.TestCase):
    def test_no_defects_in_the_current_code(self):
        rep = ci.build_report(ROOT, run_layer=True)
        self.assertEqual([f for f in rep["findings"] if f["category"] in ci.DEFECT_CATEGORIES], [],
                         ci.render(rep))
        self.assertGreater(rep["checked"]["probes_run"], 300)

    def test_honest_missing_evidence_is_not_flagged_as_a_defect(self):
        rep = ci.build_report(ROOT, run_layer=False, setups_only=("fresh",), only={"/review", "/botvsme", "/reconstruct"})
        self.assertEqual([f for f in rep["findings"] if f["category"] in ci.DEFECT_CATEGORIES], [])
        self.assertGreater(rep["evidence_notes"]["MISSING_EVIDENCE"], 0)          # the bot DID say 'not recorded', and that is fine

    def test_stale_state_is_noted_not_blamed(self):
        rep = ci.build_report(ROOT, run_layer=False, setups_only=("stale_state",), only={"/now"})
        self.assertEqual([f for f in rep["findings"] if f["category"] in ci.DEFECT_CATEGORIES], [])
        self.assertGreater(rep["evidence_notes"]["STALE_EVIDENCE"], 0)


class InjectedResponseDefects(Patched):
    def run_for(self, cmd, **kw):
        return ci.build_report(ROOT, run_layer=False, setups_only=kw.get("setups", ("fresh",)), only={cmd})

    def test_the_case_0001_bug_is_caught(self):
        """The live failure: a bare /review answered with the internal field path as if it were a question."""
        self.patch(lambda t, *a, **k: bus.Reply("case.{case_id}.trade: I'm not sure what you're asking about", None) if t.strip() == "/review" else None)
        rep = self.run_for("/review")
        hit = [f for f in rep["findings"] if f["rule"] == "unresolved-placeholder"]
        self.assertTrue(hit); self.assertEqual(hit[0]["category"], "RESPONSE_LEAK")
        self.assertEqual(hit[0]["reproduction"]["message"], "/review")
        self.assertIn("{case_id}", hit[0]["evidence"][0])

    def test_python_internals_in_a_reply(self):
        self.patch(lambda t, *a, **k: bus.Reply("Your outcome is None and the record is {'a': 1}", None) if t.strip() == "/review 1" else None)
        self.assertIn("python-repr-leak", rules(self.run_for("/review")))

    def test_an_exception_shown_as_conversation(self):
        def boom(t, *a, **k):
            if t.strip() == "/review abc": raise ValueError("kaboom")
        self.patch(boom)
        rep = self.run_for("/review")
        self.assertIn("internal-error-as-chat", rules(rep)); self.assertEqual(
            {f["category"] for f in rep["findings"] if f["rule"] == "internal-error-as-chat"}, {"RESPONSE_LEAK"})

    def test_a_case_number_nobody_logged_or_asked_for(self):
        self.patch(lambda t, *a, **k: bus.Reply("Here is CASE-0042.", None) if t.strip() == "/review" else None)
        self.assertIn("wrong-case-id", rules(self.run_for("/review")))

    def test_the_case_number_the_user_asked_for_is_fine(self):
        self.patch(lambda t, *a, **k: bus.Reply("I don't have CASE-0099.", None) if t.strip() == "/review 99" else None)
        self.assertNotIn("wrong-case-id", rules(self.run_for("/review")))

    def test_an_empty_reply(self):
        self.patch(lambda t, *a, **k: bus.Reply(" ", None) if t.strip() == "/review" else None)
        rep = self.run_for("/review")
        self.assertTrue(rules(rep) & {"empty-reply", "reply-count"})

    def test_the_generic_fallback_hiding_a_command_bug_is_caught(self):
        """The real /why bug the inspector found on its first run: the safety net answered instead of the command."""
        self.patch(lambda t, *a, **k: bus.Reply(bus.NEED_MORE, None, True) if t.strip().startswith("/why") else None)
        rep = self.run_for("/why")
        self.assertIn("safety-net-fired", rules(rep))
        self.assertEqual({f["category"] for f in rep["findings"] if f["rule"] == "safety-net-fired"}, {"CONTRACT_MISMATCH"})

    def test_a_market_claim_when_there_is_no_state(self):
        self.patch(lambda t, *a, **k: bus.Reply("HTF bias is bullish.", None) if t.strip() == "/now" else None)
        rep = self.run_for("/now", setups=("no_state",))
        self.assertIn("market-claim-without-state", rules(rep))
        self.assertEqual({f["category"] for f in rep["findings"] if f["rule"] == "market-claim-without-state"}, {"AUTHORITY_VIOLATION"})

    def test_the_same_market_words_with_state_present_are_not_flagged(self):
        self.assertNotIn("market-claim-without-state", rules(self.run_for("/now", setups=("fresh",))))

    def test_a_read_command_that_writes(self):
        def sneaky(t, pool, gaps, as_of, catalog=None, **k):
            if t.strip() == "/review" and catalog is not None:
                catalog.state_knowledge("sneaky write", now=as_of)
        self.patch(sneaky)
        rep = self.run_for("/review")
        hit = [f for f in rep["findings"] if f["rule"] == "read-command-wrote"]
        self.assertTrue(hit); self.assertEqual(hit[0]["category"], "CONTRACT_MISMATCH")

    def test_a_writer_command_writing_is_not_flagged(self):
        self.assertNotIn("read-command-wrote", rules(ci.build_report(ROOT, run_layer=False, setups_only=("fresh",), only={"/know", "/logtrade"})))

    def test_duplicate_processing(self):
        real = br._handled_updates
        br._handled_updates = lambda records: set()
        self.addCleanup(setattr, br, "_handled_updates", real)
        self.assertIn("duplicate-processing", rules(self.run_for("/help")))


class InjectedStructuralDefects(unittest.TestCase):
    def tree(self):
        d = tempfile.mkdtemp(prefix="ci_tree_")
        self.addCleanup(shutil.rmtree, d, True)
        home = os.path.join(d, "comms") if COMMS_FOLDER else d
        os.makedirs(home, exist_ok=True)
        for f in os.listdir(HERE):                                  # code only: never .git, data files or caches
            if f.endswith(".py") and os.path.isfile(os.path.join(HERE, f)):
                shutil.copy(os.path.join(HERE, f), home)
        self.home = home
        for f in ("fixture_real_state.json", "check_layer_imports.py", "CLAIM_MATRIX.md"):
            if os.path.exists(os.path.join(ROOT, f)):
                shutil.copy(os.path.join(ROOT, f), d)
        return d

    def static(self, d, full=False, layer=False):
        inv = ci.inventory(d)
        return ci.static_findings(d, inv, run_layer=layer, full_checkout=full)

    def test_a_clean_tree_has_no_static_defects(self):
        self.assertEqual([f for f in self.static(self.tree()) if f["category"] in ci.DEFECT_CATEGORIES], [])

    def test_a_retired_root_copy_is_found(self):
        if not COMMS_FOLDER:
            self.skipTest("only meaningful when the communication layer lives in comms/")
        d = self.tree(); shutil.copy(os.path.join(d, "comms", "bus.py"), os.path.join(d, "bus.py"))
        self.assertIn("retired-root-copy", {f["rule"] for f in self.static(d)})

    def test_an_import_cycle_is_found(self):
        d = self.tree()
        with open(os.path.join(self.home, "cyc_a.py"), "w") as f: f.write("import cyc_b\n")
        with open(os.path.join(self.home, "cyc_b.py"), "w") as f: f.write("import cyc_a\n")
        got = [f for f in self.static(d) if f["rule"] == "import-cycle"]
        self.assertTrue(got); self.assertIn("cyc_a", got[0]["observed"])

    def test_a_workflow_pointing_at_a_moved_file_is_a_contract_mismatch(self):
        if not COMMS_FOLDER:
            self.skipTest("only meaningful when the communication layer lives in comms/")
        d = self.tree(); os.makedirs(os.path.join(d, ".github", "workflows"))
        with open(os.path.join(d, ".github", "workflows", "x.yml"), "w") as f: f.write("      - run: python bus_runner.py .\n")   # moved into comms/
        got = [f for f in self.static(d) if f["rule"] == "workflow-entry-missing"]
        self.assertTrue(got); self.assertEqual(got[0]["category"], "CONTRACT_MISMATCH")

    def test_a_workflow_pointing_at_a_good_path_is_fine(self):
        d = self.tree(); os.makedirs(os.path.join(d, ".github", "workflows"))
        path = "comms/bus_runner.py" if COMMS_FOLDER else "bus_runner.py"
        with open(os.path.join(d, ".github", "workflows", "x.yml"), "w") as f: f.write(f"      - run: python {path} .\n")
        self.assertEqual([f for f in self.static(d, full=True) if f["rule"].startswith("workflow")], [])

    def test_a_script_the_workflow_writes_at_run_time_is_not_a_repo_file(self):
        d = self.tree(); os.makedirs(os.path.join(d, ".github", "workflows"))
        with open(os.path.join(d, ".github", "workflows", "once.yml"), "w") as f:
            f.write("      - run: python /tmp/recover.py\n      - run: python $RUNNER_TEMP/x.py\n")
        self.assertEqual([f for f in self.static(d, full=True) if f["rule"].startswith("workflow")], [])

    def test_the_fixture_is_found_in_comms_as_well_as_the_root(self):
        d = self.tree(); fx = os.path.join(d, "fixture_real_state.json")
        if not os.path.exists(fx):
            self.skipTest("fixture not alongside")
        os.makedirs(os.path.join(d, "comms"), exist_ok=True); shutil.move(fx, os.path.join(d, "comms", "fixture_real_state.json"))
        self.assertTrue(ci.find_fixture(d).endswith(os.path.join("comms", "fixture_real_state.json")))
        rep = ci.build_report(d, run_layer=False, setups_only=("fresh",), only={"/now"})
        self.assertNotIn("no-fixture", rules(rep))

    def test_a_file_absent_from_a_partial_copy_is_unevaluable_not_a_defect_unless_full_checkout(self):
        d = self.tree()
        with open(os.path.join(d, "x.yml"), "w") as f: f.write("      - run: python nowhere.py .\n")
        partial = [f for f in self.static(d) if "nowhere" in f["expected"]]
        self.assertEqual({f["category"] for f in partial}, {"INTEGRITY_UNEVALUABLE"})
        full = [f for f in self.static(d, full=True) if "nowhere" in f["expected"]]
        self.assertEqual({f["category"] for f in full}, {"CONTRACT_MISMATCH"})

    def test_a_forbidden_import_is_reported_by_the_one_layer_checker(self):
        d = self.tree()
        if not os.path.exists(os.path.join(d, "check_layer_imports.py")):
            self.skipTest("layer checker not alongside")
        with open(os.path.join(self.home, "voice.py"), "a") as f: f.write("\nimport bus\n")      # a leaf importing upward
        got = [f for f in self.static(d, layer=True) if f["rule"] == "layer-rules"]
        self.assertTrue(got); self.assertEqual(got[0]["category"], "CONTRACT_MISMATCH")

    def test_a_missing_fixture_makes_things_unevaluable_not_broken(self):
        d = self.tree(); os.remove(os.path.join(d, "fixture_real_state.json"))
        rep = ci.build_report(d, run_layer=False, setups_only=("fresh",), only={"/now"})
        self.assertIn("INTEGRITY_UNEVALUABLE", cats(rep)); self.assertEqual([f for f in rep["findings"] if f["category"] in ci.DEFECT_CATEGORIES], [])

    def test_inventory_separates_production_from_tests(self):
        inv = {f["file"]: f for f in ci.inventory(ROOT)["files"]}
        self.assertEqual(inv["bus.py"]["kind"], "production"); self.assertEqual(inv["test_bus.py"]["kind"], "test")
        self.assertEqual(inv["comms_inspector.py"]["kind"], "diagnostic/tooling")
        self.assertIn("test_bus.py", inv["bus.py"]["tests_importing_it"])
        self.assertIn("bus_runner.py", inv["bus.py"]["imported_by"])


class Behaviour(Patched):
    def test_every_finding_has_the_full_shape(self):
        self.patch(lambda t, *a, **k: bus.Reply("case.{case_id}.trade", None) if t.strip() == "/review" else None)
        rep = ci.build_report(ROOT, run_layer=False, setups_only=("fresh",), only={"/review"})
        need = {"id", "rule", "severity", "category", "module", "stage", "expected", "observed", "evidence", "reproduction",
                "likely_cause", "suggested_fix", "regression_test", "status"}
        for f in rep["findings"]:
            self.assertTrue(need <= set(f), set(f)); self.assertIn("not confirmed", f["likely_cause"])
            self.assertIn(f["stage"], ci.STAGES); self.assertIn(f["category"], ci.CATEGORIES)

    def test_same_code_same_report(self):
        a = ci.build_report(ROOT, run_layer=False, setups_only=("fresh",), only={"/review", "/why"})
        b = ci.build_report(ROOT, run_layer=False, setups_only=("fresh",), only={"/review", "/why"})
        self.assertEqual(json.dumps(a, sort_keys=True, default=str), json.dumps(b, sort_keys=True, default=str))

    def test_previous_report_marks_recurring_and_resolved(self):
        self.patch(lambda t, *a, **k: bus.Reply("case.{case_id}.trade", None) if t.strip() == "/review" else None)
        first = ci.build_report(ROOT, run_layer=False, setups_only=("fresh",), only={"/review"})
        again = ci.build_report(ROOT, run_layer=False, setups_only=("fresh",), only={"/review"}, previous=first)
        self.assertEqual({f["status"] for f in again["findings"] if f["category"] in ci.DEFECT_CATEGORIES}, {"recurring"})
        bus.respond = bus.respond.__closure__[1].cell_contents if False else bus.respond  # (patch removed by cleanup)

    def test_a_fixed_defect_shows_as_resolved(self):
        real = bus.respond
        bus.respond = lambda t, *a, **k: bus.Reply("case.{case_id}.trade", None) if t.strip() == "/review" else real(t, *a, **k)
        try:
            broken = ci.build_report(ROOT, run_layer=False, setups_only=("fresh",), only={"/review"})
        finally:
            bus.respond = real
        fixed = ci.build_report(ROOT, run_layer=False, setups_only=("fresh",), only={"/review"}, previous=broken)
        self.assertTrue(fixed["resolved_since_previous"])
        self.assertEqual({f["status"] for f in fixed["resolved_since_previous"]}, {"resolved"})

    def test_the_inspector_changes_nothing_and_leaves_no_litter(self):
        def snap():
            h = hashlib.sha256()
            for p in sorted(glob.glob(os.path.join(ROOT, "**", "*"), recursive=True)):
                if os.path.isfile(p) and "__pycache__" not in p and not p.endswith(".pyc"):
                    h.update(p.encode())
                    with open(p, "rb") as fh:
                        h.update(fh.read())
            return h.hexdigest()
        before, litter = snap(), set(glob.glob(os.path.join(tempfile.gettempdir(), "comms_probe_*")))
        ci.build_report(ROOT, run_layer=False, setups_only=("fresh",), only={"/review", "/now"})
        self.assertEqual(snap(), before)
        self.assertEqual(set(glob.glob(os.path.join(tempfile.gettempdir(), "comms_probe_*"))), litter)

    def test_exit_code_follows_defects(self):
        out = tempfile.mktemp(suffix=".json")
        self.addCleanup(lambda: os.path.exists(out) and os.remove(out))
        self.assertEqual(ci.main([ROOT, "--quick", "--json", out]), 0)
        with open(out) as f: self.assertEqual(json.load(f)["summary"]["defects"], 0)
        self.patch(lambda t, *a, **k: bus.Reply("case.{case_id}.trade", None) if t.strip() == "/review" else None)
        self.assertEqual(ci.main([ROOT, "--quick"]), 1)

    def test_the_report_states_its_limits(self):
        rep = ci.build_report(ROOT, run_layer=False, setups_only=("fresh",), only={"/help"})
        txt = ci.render(rep)
        self.assertIn("LIMITS OF THIS REPORT", txt); self.assertIn("does not certify the bot", txt)
        self.assertIn("Market interpretations are not judged", txt)


class Boundary(unittest.TestCase):
    def test_nothing_imports_the_inspector_and_it_never_writes_outside_temp_folders(self):
        import ast
        for f in os.listdir(HERE):
            if f.endswith(".py") and f not in ("comms_inspector.py", "test_comms_inspector.py", "check_layer_imports.py"):
                with open(os.path.join(HERE, f)) as fh:
                    self.assertNotIn("comms_inspector", fh.read(), f)
        with open(os.path.join(HERE, "comms_inspector.py")) as fh:
            src = fh.read()
        tree = ast.parse(src)
        opens_for_write = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open"
                           and len(n.args) > 1 and isinstance(n.args[1], ast.Constant) and str(n.args[1].value) in ("w", "a")]
        self.assertEqual(len(opens_for_write), 2)    # the probe's fake inbox (a temp folder) and the --json report: nothing else writes


if __name__ == "__main__":
    unittest.main()
