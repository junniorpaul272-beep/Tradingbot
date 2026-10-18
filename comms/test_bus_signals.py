"""Run: python test_bus_signals.py — fired signals through BUS, behind the BUS_SIGNALS flag.
Report-only: the engine, the legacy alert and every trade decision are untouched; these tests pin that, and compare
the BUS alert with the legacy alert's facts, the missing-evidence behaviour and the flag-off behaviour."""
import ast, json, os, re, shutil, sys, tempfile, unittest, uuid
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import ledger, bus, bus_contract as bc, bus_runner as br, bus_adapters as ba

FIX = os.path.join(ROOT, "fixture_real_state.json")
LIVE = os.path.join(ROOT, "scanner_live.py")
def _rd(p, mode="r"):
    with open(p, mode) as f:
        return f.read()


def _append(p, r):
    with open(p, "a") as f:
        f.write(json.dumps(r, default=str) + "\n")
    return True


FIRED = datetime(2026, 10, 14, 10, 55, 30, tzinfo=timezone.utc)
CAUTIONS = [{"code": "OB_MITIGATED", "sentence": "Order block already mitigated"},
            {"code": "LEG_AGING", "sentence": "3 breaks into this leg - getting old"}]


def write_signal(path, cautions=CAUTIONS, risk_result=None, bias="BEARISH", fired=FIRED, **over):
    """Writes a record with the REAL _record_undelivered_signal, so the BUS is tested against the real shape."""
    src = _rd(LIVE); L = src.splitlines()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_record_undelivered_signal")
    g = dict(uuid=uuid, PAIR="GBPUSD", SIGNAL_EVENT_LOG_FILE=path, SIGNAL_EVENT_STATUS_UNDELIVERED="FIRED_RISK_GATE_PASSED_UNDELIVERED",
             SIGNAL_EVENT_STATE_MIRROR_CAP=200, apply_leg_ownership=lambda s, d: None,
             append_permanent_jsonl=_append)
    exec("\n".join(L[fn.lineno - 1:fn.end_lineno]), g)
    res = NS(tier_label="TIER_2_FIB", direction="SELL", score=7, tier_rating="B", reason="Retest of the 0.618 zone after a 15m change of character",
             breakdown={}, conviction=None,
             risk={"band": "MEDIUM", "reasons": ["Counter to the 4h swing", "Spread normal"], "recommendation": "Consider a slightly wider stop"})
    rr = risk_result if risk_result is not None else {"entry": 1.31234, "sl": 1.31534, "tp": 1.30634, "risk_pips": 30.0,
                                                         "reward_pips": 60.0, "target_r": 2.0, "size_mult": 0.5}
    ctx = NS(low_atr_warning=True, very_low_atr_warning=False)
    state = {"scan_id": 41, "market_thesis_current_state": "Bearish Expansion", "macro_bias_stale": False}
    g["_record_undelivered_signal"](state, {}, {}, res, rr, ctx, {}, bias, cautions, fired, "leg-9", fired)


class Base(unittest.TestCase):
    def setUp(self):
        if not (os.path.exists(FIX) and os.path.exists(LIVE)):
            self.skipTest("fixture / scanner_live.py not alongside")
        self.root = tempfile.mkdtemp()
        shutil.copy(FIX, os.path.join(self.root, "state.json"))
        self.log = os.path.join(self.root, "signal_event_log.jsonl")
        self.sent = []; self.ok = True
        self.now = FIRED + timedelta(minutes=3)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def send(self, t):
        self.sent.append(t); return self.ok

    def go(self, now=None, signals=True):
        return br.run(self.root, now=now or self.now, send=self.send, signals=signals)

    def records(self):
        p = os.path.join(self.root, br.LEDGER_NAME)
        return ledger.read(p)[0] if os.path.exists(p) else []

    def claim(self):
        rows = [json.loads(l) for l in _rd(self.log).splitlines()]
        return ba.adapt_signal_events(rows).claims[0]

    def text_of(self, claim):
        ans = bc.resolve("signal_alert", [claim], self.now, gaps=[])
        rep = bus.finalize(ans)
        return rep


class Parity(Base):
    """The BUS alert states the facts the legacy alert did."""

    def test_every_fact_the_legacy_alert_carries_is_in_the_bus_alert(self):
        write_signal(self.log)
        rep = self.text_of(self.claim())
        self.assertFalse(rep.used_fallback, rep.violations)
        t = rep.text
        for s in ("sell", "TIER_2_FIB", "10:55 UTC", "bearish", "1.31234", "1.31534", "30.0 pips", "1.30634", "60.0 pips",
                  "Reward-to-risk 2", "Suggested size 0.5x", "Risk-at-hand: medium", "Counter to the 4h swing", "Spread normal",
                  "Consider a slightly wider stop", "Retest of the 0.618 zone", "flagged low ATR",
                  "Order block already mitigated", "3 breaks into this leg - getting old"):
            self.assertIn(s, t, s)
        self.assertNotRegex(t.lower(), r"\b(take|enter now|you should|buy now|sell now)\b")      # states what was recorded; never instructs

    def test_the_legacy_alert_still_reads_the_same_fields_so_this_comparison_cannot_go_stale(self):
        src = _rd(LIVE)
        a = src.index("telegram_ok = send_telegram(\n        \"🚨 *SMC SIGNAL")
        legacy = src[a:a + 2600]
        for key in ("risk_result['entry']", "risk_result['sl']", "risk_result['risk_pips']", "risk_result['tp']",
                    "risk_result['reward_pips']", "risk_result['target_r']", "risk_result['size_mult']", "macro_bias",
                    "result.tier_label", "result.direction", "risk_line", "low_atr_line", "caution_line", "evidence_line", "timeline_line"):
            self.assertIn(key, legacy, f"legacy alert no longer references {key}: update the BUS alert and this list together")

    def test_what_the_record_cannot_support_is_said_not_invented(self):
        write_signal(self.log)
        t = self.text_of(self.claim()).text
        self.assertIn("position size in account terms and the signal timeline", t)         # legacy extras that are not recorded


class MissingEvidence(Base):
    def test_a_record_without_prices_or_cautions_reports_the_gaps_and_invents_nothing(self):
        write_signal(self.log, risk_result={"target_r": 2.0})
        row = json.loads(_rd(self.log)); row.pop("snapshot")                  # a record whose context snapshot is absent
        with open(self.log, "w") as f:
            f.write(json.dumps(row) + "\n")
        rep = self.text_of(self.claim())
        self.assertFalse(rep.used_fallback, rep.violations)
        self.assertIn("Not in the record: entry, stop, target.", rep.text)
        self.assertIn("Whether any cautions applied isn't in the record.", rep.text)
        self.assertNotRegex(rep.text, r"Entry \d|Stop \d|Target \d")
        self.assertNotIn("No cautions were recorded", rep.text)

    def test_the_real_writer_records_no_cautions_as_an_empty_list(self):
        write_signal(self.log, cautions=None)          # the engine stores `list(caution_codes or [])`, so None and [] are the same on disk
        self.assertIn("No cautions were recorded with it.", self.text_of(self.claim()).text)

    def test_an_empty_cautions_list_is_stated_as_none_recorded_not_as_unknown(self):
        write_signal(self.log, cautions=[])
        self.assertIn("No cautions were recorded with it.", self.text_of(self.claim()).text)

    def test_bias_missing_is_a_gap(self):
        write_signal(self.log, bias=None)
        self.assertIn("1H bias at the time isn't in the record.", self.text_of(self.claim()).text)

    def test_unsupported_wording_cannot_pass_the_audit(self):
        write_signal(self.log)
        c = self.claim()
        ans = bc.resolve("signal_alert", [c], self.now, gaps=[])
        self.assertTrue(bc.audit_text("The engine thinks momentum is strong and this setup is fresh.", ans))   # BUS may not add its own claims
        self.assertTrue(bc.audit_text("Entry 1.99999.", ans))


class Runner(Base):
    def test_flag_off_writes_nothing_and_sends_nothing(self):
        write_signal(self.log)
        before = sorted(os.listdir(self.root))
        self.go(signals=False)
        self.assertEqual(self.sent, [])
        self.assertEqual([r for r in self.records() if "SIGNAL" in r["event_type"]], [])
        self.assertEqual(sorted(os.listdir(self.root)), before if not os.path.exists(os.path.join(self.root, br.LEDGER_NAME)) else sorted(os.listdir(self.root)))
        self.assertFalse(br.signals_flag({}))
        self.assertFalse(br.signals_flag({"BUS_SIGNALS": ""}) or br.signals_flag({"BUS_SIGNALS": "0"}) or br.signals_flag({"BUS_SIGNALS": "no"}))
        self.assertTrue(br.signals_flag({"BUS_SIGNALS": "1"}) and br.signals_flag({"BUS_SIGNALS": "true"}))

    def test_flag_on_announces_once_and_never_again(self):
        write_signal(self.log)
        s = self.go()
        self.assertEqual(s.get("signals_announced"), 1)
        self.assertEqual(len([t for t in self.sent if "recorded a sell signal" in t]), 1)
        self.go(now=self.now + timedelta(minutes=5)); self.go(now=self.now + timedelta(minutes=10))
        self.assertEqual(len([t for t in self.sent if "recorded a sell signal" in t]), 1)           # a re-run never repeats it
        self.assertEqual(len([r for r in self.records() if r["event_type"] == br.BUS_SIGNAL_ANNOUNCED]), 1)

    def test_a_failed_send_is_retried_from_the_stored_text_without_re_announcing(self):
        write_signal(self.log)
        self.ok = False; self.go()
        self.ok = True; self.go(now=self.now + timedelta(minutes=2))
        said = [t for t in self.sent if "recorded a sell signal" in t]
        self.assertEqual(len(said), 2); self.assertEqual(said[0], said[1])
        self.assertEqual(len([r for r in self.records() if r["event_type"] == br.BUS_SIGNAL_ANNOUNCED]), 1)

    def test_turning_the_flag_on_never_replays_old_signals(self):
        write_signal(self.log, fired=FIRED - timedelta(hours=5))
        s = self.go()
        self.assertEqual(s.get("signals_announced"), None)
        self.assertEqual([t for t in self.sent if "signal" in t.lower() and "recorded" in t], [])
        sk = [r for r in self.records() if r["event_type"] == br.BUS_SIGNAL_SKIPPED]
        self.assertEqual(len(sk), 1); self.assertIn("older", sk[0]["payload"]["reason"])          # recorded, not silently dropped

    def test_a_flood_is_capped_per_run_and_the_rest_wait(self):
        for i in range(5):
            write_signal(self.log, fired=FIRED + timedelta(seconds=i))
        s = self.go(now=FIRED + timedelta(minutes=2))
        self.assertEqual(s["signals_announced"], br.SIGNAL_MAX_PER_RUN)
        s2 = self.go(now=FIRED + timedelta(minutes=4))
        self.assertEqual(s2["signals_announced"], 2)

    def test_the_log_and_the_engine_files_are_never_modified(self):
        write_signal(self.log)
        snap = {n: _rd(os.path.join(self.root, n), "rb") for n in ("state.json", "signal_event_log.jsonl")}
        self.go()
        for n, b in snap.items():
            self.assertEqual(_rd(os.path.join(self.root, n), "rb"), b, n)

    def test_a_broken_signal_log_does_not_fail_the_step(self):
        with open(self.log, "w") as f:
            f.write("{not json\n")
        s = self.go()
        self.assertEqual(s["errors"], 0); self.assertEqual(self.sent, [])


class Boundaries(unittest.TestCase):
    def test_workflow_flag_defaults_off_and_the_engine_is_untouched(self):
        y = _rd(os.path.join(ROOT, "live-scan.yml"))
        self.assertIn("BUS_SIGNALS: ${{ vars.BUS_SIGNALS }}", y)
        self.assertNotIn("BUS_SIGNALS: 1", y); self.assertNotIn("BUS_SIGNALS: '1'", y)
        if os.path.exists(LIVE):
            src = _rd(LIVE)
            self.assertIn("_ENGINE_ONLY_MODE = True", src)                                          # the engine's own switch is unchanged
            imported = {a.name.split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names}
            imported |= {n.module.split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom) and n.module}
            self.assertFalse(imported & {"bus", "bus_runner", "bus_contract", "bus_adapters", "user_catalog"})   # the engine never imports BUS
            self.assertNotIn("BUS_SIGNALS", src)

    def test_the_signal_intent_and_subject_are_registered_with_a_producer(self):
        self.assertIn("signal_alert", bc.INTENTS)
        self.assertIn("signal.undelivered", ba.ADAPTER_PRODUCES)


if __name__ == "__main__":
    unittest.main()
