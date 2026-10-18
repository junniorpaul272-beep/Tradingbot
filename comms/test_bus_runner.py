"""Run: python test_bus_runner.py — the runner end to end, over the REAL state.json fixture, with a fake Telegram."""
import json, os, shutil, sys, tempfile, unittest
from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import ledger, bus_runner as br, bus_adapters as ba
from user_catalog import Catalog

FIX = os.path.join(ROOT, "fixture_real_state.json")


class Base(unittest.TestCase):
    def setUp(self):
        if not os.path.exists(FIX):
            self.skipTest("fixture_real_state.json not alongside")
        self.root = tempfile.mkdtemp()
        shutil.copy(FIX, os.path.join(self.root, "state.json"))
        with open(FIX) as f:
            self.persisted = ba._t(json.load(f)["timestamp"])
        self.now = self.persisted + timedelta(minutes=2)        # asked 2 minutes after the scan wrote state
        self.sent = []
        self.ok = True

    def send(self, text):
        self.sent.append(text)
        return self.ok

    def inbox(self, *msgs):
        with open(os.path.join(self.root, "inbox.jsonl"), "a") as f:
            for uid, text in msgs:
                f.write(json.dumps({"update_id": uid, "chat_id": "1", "text": text, "received_at": self.now.isoformat()}) + "\n")

    def run_(self, **kw):
        return br.run(self.root, now=kw.pop("now", self.now), send=self.send)

    def records(self):
        return ledger.read(os.path.join(self.root, br.LEDGER_NAME))[0]


class Flow(Base):
    def test_a_question_is_answered_logged_and_delivered_once(self):
        self.inbox((101, "/now"))
        s = self.run_()
        self.assertEqual((s["answered"], s["delivered"], s["errors"]), (1, 1, 0))
        self.assertIn("HTF bias is bearish", self.sent[0]); self.assertIn("these disagree", self.sent[0])
        types = [r["event_type"] for r in self.records()]
        self.assertEqual(types.count("MESSAGE_RECEIVED"), 1); self.assertEqual(types.count("BUS_REPLY"), 1)
        self.assertEqual(types.count("BUS_DELIVERED"), 1); self.assertEqual(types.count("BUS_STATEMENT"), 1)
        # running again changes nothing: no second reply, no second statement
        self.run_()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual([r["event_type"] for r in self.records()].count("BUS_REPLY"), 1)

    def test_free_text_and_continuity(self):
        self.inbox((1, "/thesis"), (2, "why?"))
        self.run_()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("MIL's primary thesis is bullish", self.sent[1])        # "why" resolved from the previous statement
        self.assertIn("these disagree", self.sent[1])

    def test_order_is_by_update_id_not_file_order(self):
        self.inbox((9, "/watch"), (3, "/now"))
        self.run_()
        self.assertIn("HTF bias", self.sent[0]); self.assertIn("Watching", self.sent[1])

    def test_unknown_message_gets_a_help_pointer(self):
        self.inbox((5, "good morning"))
        self.run_()
        self.assertIn("/help", self.sent[0])

    def test_stale_state_is_reported_not_shown_as_current(self):
        self.inbox((7, "/now"))
        s = self.run_(now=self.persisted + timedelta(hours=3))
        self.assertIn("too old to call current", self.sent[0])
        self.assertNotIn("Data as of", self.sent[0])


class Resilience(Base):
    def test_failed_send_is_retried_from_stored_text_and_never_re_executes(self):
        self.inbox((11, "/logtrade GBPUSD buy 1.3421 sl 1.3398 tp 1.3490"))
        self.ok = False
        s = self.run_()
        self.assertEqual((s["answered"], s["delivered"], s["failed"]), (1, 0, 1))
        self.ok = True
        s = self.run_(now=self.now + timedelta(minutes=5))
        self.assertEqual((s["resent"], s["delivered"]), (1, 1))
        self.assertEqual(self.sent[0], self.sent[1])                          # identical text re-sent
        trades = [r for r in self.records() if r["event_type"] == "TRADE_LOGGED"]
        self.assertEqual(len(trades), 1)                                      # the trade was logged ONCE

    def test_delivery_attempts_are_capped(self):
        self.inbox((12, "/now")); self.ok = False
        for i in range(6):
            self.run_(now=self.now + timedelta(minutes=i * 5))
        fails = [r for r in self.records() if r["event_type"] == "BUS_DELIVERY_FAILED"]
        self.assertEqual(len(fails), br.MAX_DELIVERY_ATTEMPTS)

    def test_a_crashing_handler_replies_with_an_error_and_does_not_jam_the_queue(self):
        import bus
        orig = bus.respond
        def boom(text, *a, **k):
            if text == "/now": raise RuntimeError("kaboom")
            return orig(text, *a, **k)
        bus.respond = boom
        try:
            self.inbox((20, "/now"), (21, "/watch"))
            s = self.run_()
        finally:
            bus.respond = orig
        self.assertEqual((s["errors"], s["answered"]), (1, 1))
        self.assertIn("I hit an error", self.sent[0]); self.assertIn("Watching", self.sent[1])
        self.run_()
        self.assertEqual(len(self.sent), 2)                                   # the bad message is not retried forever

    def test_no_sender_composes_and_logs_without_crashing(self):
        self.inbox((30, "/now"))
        s = br.run(self.root, now=self.now, send=None)
        self.assertEqual((s["answered"], s["delivered"], s["failed"]), (1, 0, 1))

    def test_corrupt_inbox_lines_are_skipped(self):
        with open(os.path.join(self.root, "inbox.jsonl"), "w") as f:
            f.write("{not json\n")
        self.inbox((40, "/now"))
        self.assertEqual(self.run_()["answered"], 1)

    def test_empty_inbox_is_a_noop(self):
        self.assertEqual(self.run_(), {"answered": 0, "resent": 0, "delivered": 0, "failed": 0, "errors": 0})
        self.assertFalse(os.path.exists(os.path.join(self.root, br.LEDGER_NAME)) and self.records())


class CatalogThroughTheRunner(Base):
    def test_log_review_reason_outcome_cycle(self):
        self.inbox((50, "/logtrade GBPUSD sell 1.3200 sl 1.3260 tp 1.3100"))
        self.run_()
        self.assertIn("Logged CASE-0001", self.sent[0]); self.assertIn("attached what the engine had right now", self.sent[0])
        self.inbox((51, "/reason 1 expected the order block to hold"))
        self.run_(now=self.now + timedelta(minutes=1))
        self.inbox((52, "/outcome 1 win 2"))
        self.run_(now=self.now + timedelta(hours=2))
        self.inbox((53, "/botvsme 1"), (54, "/review 1"))
        self.run_(now=self.now + timedelta(hours=3))
        txt = "\n".join(self.sent)
        self.assertIn("The bot's side: I attached what the engine had when you logged it", txt)
        self.assertIn("expected the order block to hold", txt)
        self.assertIn("Outcome: win, 2.0R", txt)
        case = Catalog(os.path.join(self.root, br.LEDGER_NAME)).get_case("CASE-0001")
        self.assertEqual(case["bot_plan"]["payload"]["facts"]["htf.bias"], "BEARISH")   # the engine's side, as it stood

    def test_old_state_means_no_bot_snapshot_is_attached(self):
        self.inbox((60, "/logtrade GBPUSD buy 1.30 sl 1.29 tp 1.32"))
        self.run_(now=self.persisted + timedelta(hours=2))
        self.assertIn("did not attach a bot snapshot", self.sent[0])

    def test_health_reports_ledger_integrity_and_detects_tampering(self):
        self.inbox((70, "/logtrade GBPUSD buy 1.30 sl 1.29 tp 1.32"), (71, "/health"))
        self.run_()
        self.assertIn("untouched", self.sent[1])
        p = os.path.join(self.root, br.LEDGER_NAME)
        with open(p) as f:
            lines = f.read().splitlines()
        rec = json.loads(lines[0]); rec["payload"]["text"] = "edited"
        lines[0] = json.dumps(rec, sort_keys=True)
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        self.inbox((72, "/health"))
        self.run_(now=self.now + timedelta(minutes=1))
        self.assertIn("PROBLEMS", self.sent[-1])


class AlertsThroughBus(Base):
    def candles(self, *rows, tf="5m"):
        name = {"5m": "ohlc_5m_log.jsonl", "15m": "ohlc_15m_log.jsonl", "1h": "ohlc_1h_log.jsonl"}[tf]
        with open(os.path.join(self.root, name), "a") as f:
            for t, hi, lo, *cl in rows:
                f.write(json.dumps({"datetime": t.isoformat(), "open": 1.33, "high": hi, "low": lo, "close": cl[0] if cl else 1.33}) + "\n")

    def test_level_alert_fires_once_only_from_candles_after_it_was_set(self):
        self.candles((self.now - timedelta(hours=1), 1.3450, 1.3300))          # already crossed BEFORE: must not fire
        self.inbox((1, "/alert 1.3400 above"))
        self.run_()
        self.assertIn("Alert THREAD-0001 is set", self.sent[0]); self.assertIn("price trades up through 1.34000", self.sent[0])
        self.assertIn("07:00", self.sent[0])                                     # the scan-window limitation is stated, not hidden
        self.run_(now=self.now + timedelta(minutes=5))
        self.assertEqual(len(self.sent), 1)
        self.candles((self.now + timedelta(minutes=10), 1.3402, 1.3380))
        s = self.run_(now=self.now + timedelta(minutes=12))
        self.assertEqual(s.get("alerts"), 1)
        self.assertIn("Alert hit", self.sent[-1]); self.assertIn("price trades up through 1.34000 (high 1.34020)", self.sent[-1])
        self.run_(now=self.now + timedelta(minutes=20))
        self.assertEqual(sum("Alert hit" in t for t in self.sent), 1)

    def test_below_touch_zone_and_close_forms(self):
        self.inbox((1, "/alert 1.3200 below"), (2, "/alert touch 1.3300"), (3, "/alert zone 1.3380 1.3400"),
                   (4, "/alert close above 1.3410 15m"), (5, "/alert nonsense"))
        self.run_()
        for i, frag in enumerate(["trades down through 1.32000", "touches 1.33000", "into the zone 1.33800–1.34000", "closes above 1.34100 on the 15m"]):
            self.assertIn(frag, self.sent[i])
        self.assertIn("couldn't read that alert", self.sent[4])
        self.candles((self.now + timedelta(minutes=3), 1.3300, 1.3199))                # fires the below + the touch (range holds 1.3300)
        self.candles((self.now + timedelta(minutes=3), 1.3395, 1.3370), tf="5m") if False else None
        self.assertEqual(self.run_(now=self.now + timedelta(minutes=6)).get("alerts"), 2)
        # the 15m-close alert must NOT be evaluated on 5m candles
        self.candles((self.now + timedelta(minutes=8), 1.3500, 1.3399, 1.3420))
        s = self.run_(now=self.now + timedelta(minutes=10))
        self.assertEqual(s.get("alerts"), 1)                                             # the zone (5m range overlaps); close-15m still waits
        self.candles((self.now + timedelta(minutes=15), 1.3430, 1.3390, 1.3420), tf="15m")
        self.assertEqual(self.run_(now=self.now + timedelta(minutes=17)).get("alerts"), 1)
        self.assertIn("closes above 1.34100 on the 15m", self.sent[-1])

    def test_ema_alert_needs_enough_history_then_fires_on_a_close_above(self):
        self.inbox((1, "/alert ema 5 5m above")); self.run_()
        self.assertIn("I can't set that yet", self.sent[0]); self.assertIn("needs at least 16 candles", self.sent[0]); self.assertIn("I have 0", self.sent[0])
        # 20 flat candles at 1.3300 (EMA settles at 1.33), all BEFORE the alert is set
        base = self.now - timedelta(minutes=5 * 25)
        self.candles(*[(base + timedelta(minutes=5 * i), 1.3302, 1.3298, 1.3300) for i in range(20)])
        self.inbox((2, "/alert ema 5 5m above")); self.run_()
        self.assertIn("is set", self.sent[-1]); self.assertIn("closes above the EMA 5", self.sent[-1])
        self.candles((self.now + timedelta(minutes=5), 1.3320, 1.3298, 1.3315))          # close well above the ~1.3300 EMA
        self.assertEqual(self.run_(now=self.now + timedelta(minutes=7)).get("alerts"), 1)
        self.assertIn("EMA was", self.sent[-1]); self.assertIn("closed 1.33150", self.sent[-1])

    def test_alerts_list_and_cancel(self):
        self.inbox((1, "/alert 1.3400 above"), (2, "/alert zone 1.31 1.32"), (3, "/alerts"))
        self.run_()
        self.assertIn("You have 2 open alert(s)", self.sent[2]); self.assertIn("THREAD-0001: tell you when price trades up through 1.34000", self.sent[2])
        self.inbox((4, "/alert cancel 1"), (5, "/alerts"))
        self.run_(now=self.now + timedelta(minutes=1))
        self.assertIn("THREAD-0001 cancelled", self.sent[3]); self.assertIn("You have 1 open alert(s)", self.sent[4])
        self.assertNotIn("1.34000", self.sent[4])
        self.candles((self.now + timedelta(minutes=5), 1.3500, 1.3300))                     # would have fired alert 1
        self.assertEqual(self.run_(now=self.now + timedelta(minutes=7)).get("alerts", 0), 0)  # cancelled alerts never fire
        self.inbox((6, "/alert cancel 1")); self.run_(now=self.now + timedelta(minutes=8))
        self.assertIn("I didn't record that", self.sent[-1])                               # already finished

    def test_empty_alert_list_is_a_known_empty_after_the_first_alert(self):
        self.inbox((1, "/alerts")); self.run_()
        self.assertIn("nothing recorded", self.sent[0])                                      # never set one: no claim
        self.inbox((2, "/alert 1.34 above"), (3, "/alert cancel 1"), (4, "/alerts")); self.run_(now=self.now + timedelta(minutes=1))
        self.assertIn("You have no open alerts", self.sent[-1])

    def test_alert_message_survives_a_failed_send(self):
        self.inbox((1, "/alert 1.3400 above")); self.run_()
        self.candles((self.now + timedelta(minutes=3), 1.3410, 1.3390))
        self.ok = False; self.run_(now=self.now + timedelta(minutes=6))
        self.ok = True; s = self.run_(now=self.now + timedelta(minutes=12))
        self.assertGreaterEqual(s["resent"], 1)
        self.assertTrue(any("Alert hit" in t for t in self.sent))


class IntegrityThroughBus(Base):
    """The Inspector's findings reach the user: in answers, and once proactively."""

    def run_inspector(self, now):
        import inspector, mil
        return inspector.run(self.root, now=now, mil_module=mil)

    def test_open_alarm_is_in_every_market_answer_and_announced_exactly_once(self):
        self.run_inspector(self.now)                                    # the real file has real findings
        self.inbox((1, "/now"), (2, "/thesis"))
        s = self.run_()
        self.assertEqual(s.get("announced"), 1)
        ans = [t for t in self.sent if "Integrity alarm" in t]
        self.assertGreaterEqual(len(ans), 3)                            # both answers AND the announcement
        self.assertIn("Sources that should agree on direction disagree: BULLISH from the MIL thesis", "\n".join(self.sent))
        self.assertTrue(any("earliest layer affected" in t for t in self.sent))
        # an unchanged set of findings is never re-announced
        self.run_inspector(self.now + timedelta(minutes=5))
        s2 = self.run_(now=self.now + timedelta(minutes=5))
        self.assertEqual(s2.get("announced", 0), 0)

    def test_all_clear_is_announced_once_after_findings_resolve(self):
        self.run_inspector(self.now); self.run_()
        with open(os.path.join(self.root, "state.json"), "w") as f:
            json.dump(__import__("test_inspector").healthy(), f)
        self.run_inspector(self.now + timedelta(minutes=10))
        self.run_inspector(self.now + timedelta(minutes=15))           # the schema-drift LOW from the repair settles
        s = self.run_(now=self.now + timedelta(minutes=15))
        self.assertEqual(s.get("announced"), 1)
        self.assertIn("all clear", self.sent[-1])
        self.assertEqual(self.run_(now=self.now + timedelta(minutes=20)).get("announced", 0), 0)   # and is not repeated

    def test_low_severity_findings_never_ping(self):
        import inspector
        path = os.path.join(self.root, "integrity_ledger.jsonl")
        f = inspector.finding("schema", "LOW", "2 key(s) vanished", ["x"], {})
        ledger.append(path, inspector.FINDING, f, now=self.now)
        self.assertEqual(self.run_().get("announced", 0), 0)
        self.assertEqual(self.sent, [])

    def test_a_system_that_was_never_wrong_says_nothing(self):
        with open(os.path.join(self.root, "state.json"), "w") as f:
            json.dump(__import__("test_inspector").healthy(), f)
        self.run_inspector(self.now)
        self.assertEqual(self.run_().get("announced", 0), 0); self.assertEqual(self.sent, [])

    def test_health_command_lists_the_findings(self):
        self.run_inspector(self.now); self.inbox((9, "/health")); self.run_()
        txt = [t for t in self.sent if t.startswith("Ledger") or "Integrity alarm" in t]
        self.assertTrue(any("placeholder" in t and "origin_level" in t for t in self.sent))

    def test_announcement_survives_a_failed_send(self):
        self.run_inspector(self.now); self.ok = False; self.run_()
        self.ok = True; s = self.run_(now=self.now + timedelta(minutes=5))
        self.assertGreaterEqual(s["resent"], 1)
        self.assertTrue(any("Integrity alarm" in t for t in self.sent[1:]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
