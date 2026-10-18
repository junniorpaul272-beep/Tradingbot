"""Run: python test_bus_quiet.py — quiet mode: /quiet, /loud, held messages, the one summary, the critical bypass,
and the fail-safe direction of every error. Quiet mode changes WHEN the user hears something, never what exists."""
import ast, json, os, shutil, sys, tempfile, unittest
from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import ledger, bus, bus_runner as br, quiet_mode as qm, inspector
import test_bus_signals as tbs

T0 = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)      # a Wednesday


def _rd(p):
    with open(p) as f:
        return f.read()


class Parse(unittest.TestCase):
    def p(self, text, now=T0):
        return qm.parse(text, now, now)

    def test_durations(self):
        for txt, mins in (("/quiet 3h", 180), ("/quiet 45m", 45), ("/quiet 1h30m", 90), ("/quiet 1h 30m", 90), ("/quiet 24h", 1440)):
            r = self.p(txt)
            self.assertEqual((r.kind, r.minutes), ("set", mins), txt)
            self.assertEqual(r.until, T0 + timedelta(minutes=mins))

    def test_nothing_is_open_ended_or_absurd(self):
        for txt in ("/quiet 25h", "/quiet 2d", "/quiet 0m", "/quiet forever", "/quiet off", "/quiet -3h", "/quiet 3", "/quiet until 25:00", "/quiet until 18:70"):
            self.assertEqual(self.p(txt).kind, "error", txt)

    def test_bare_quiet_is_usage_not_a_mute(self):
        self.assertEqual(self.p("/quiet").kind, "usage")

    def test_loud_and_bot_suffix(self):
        self.assertEqual(self.p("/loud").kind, "loud")
        self.assertEqual(self.p("/Quiet@mybot 2h").minutes, 120)
        self.assertIsNone(qm.command_of("quiet please"))
        self.assertIsNone(qm.command_of("/quietly"))

    def test_until_defaults_to_lagos_time_and_utc_is_explicit(self):
        r = self.p("/quiet until 18:00")                          # 10:00 UTC = 11:00 Lagos; 18:00 Lagos = 17:00 UTC
        self.assertEqual(r.until, datetime(2026, 10, 14, 17, 0, tzinfo=timezone.utc))
        r = self.p("/quiet until 18:00 utc")
        self.assertEqual(r.until, datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc))

    def test_until_a_time_already_passed_means_tomorrow_within_the_cap(self):
        r = self.p("/quiet until 09:00 utc")                       # 09:00 UTC has passed at 10:00 UTC
        self.assertEqual(r.until, datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(r.kind, "set")
        r = self.p("/quiet until 10:00 utc")                       # exactly now -> a full day, which is the cap, allowed
        self.assertEqual((r.kind, r.minutes), ("set", 1440))

    def test_a_duration_counts_from_when_the_user_sent_it(self):
        sent = T0 - timedelta(minutes=50)
        r = qm.parse("/quiet 1h", sent, T0)
        self.assertEqual(r.until, T0 + timedelta(minutes=10))


def rec(kind, payload, at, rid=None):
    r = ledger.make_record(kind, payload, recorded_at=at)
    if rid:
        r["event_id"] = rid
    return r


class State(unittest.TestCase):
    def test_active_until_the_end_then_not(self):
        r = [rec(qm.QUIET_SET, {"until": (T0 + timedelta(hours=1)).isoformat()}, T0, "q1")]
        self.assertTrue(qm.state(r, T0 + timedelta(minutes=59)).active)
        self.assertFalse(qm.state(r, T0 + timedelta(hours=1)).active)         # expiry needs no clear event
        self.assertFalse(qm.state(r, T0 + timedelta(hours=5)).active)

    def test_loud_ends_it_and_a_later_set_restarts_it(self):
        s1 = rec(qm.QUIET_SET, {"until": (T0 + timedelta(hours=3)).isoformat()}, T0, "q1")
        c = rec(qm.QUIET_CLEARED, {"quiet_id": "q1"}, T0 + timedelta(minutes=5))
        s2 = rec(qm.QUIET_SET, {"until": (T0 + timedelta(hours=4)).isoformat()}, T0 + timedelta(minutes=10), "q2")
        self.assertFalse(qm.state([s1, c], T0 + timedelta(minutes=6)).active)
        self.assertTrue(qm.state([s1, c, s2], T0 + timedelta(minutes=11)).active)

    def test_malformed_records_never_leave_the_user_muted(self):
        for bad in ({}, {"until": None}, {"until": "tomorrow"}, {"until": 12345}, {"until": ""}):
            r = [rec(qm.QUIET_SET, bad, T0, "q1")]
            st = qm.state(r, T0 + timedelta(minutes=1))
            self.assertFalse(st.active, bad)
        self.assertFalse(qm.state([{"event_type": qm.QUIET_SET, "payload": "garbage", "event_id": "x"}], T0).active)

    def test_an_absurd_until_is_clamped_to_the_cap(self):
        r = [rec(qm.QUIET_SET, {"until": (T0 + timedelta(days=400)).isoformat()}, T0, "q1")]
        self.assertTrue(qm.state(r, T0 + timedelta(hours=23)).active)
        self.assertFalse(qm.state(r, T0 + timedelta(hours=25)).active)

    def test_no_records_means_not_quiet(self):
        self.assertFalse(qm.state([], T0).active)


class Critical(unittest.TestCase):
    def f(self, **k):
        d = {"severity": "HIGH", "probe": "direction_conflict", "relationship": "same_writer"}
        d.update(k); return d

    def test_the_narrow_case_is_critical(self):
        self.assertTrue(qm.is_critical(self.f()))

    def test_everything_else_is_not(self):
        for k in ({"severity": "MED"}, {"severity": "LOW"}, {"probe": "timeframe_divergence"}, {"probe": "dwell"},
                  {"relationship": "consumer_of_macro_bias"}, {"relationship": "mil_transition_overdue"},
                  {"relationship": "derived_and_may_lag"}, {"relationship": None}, {"severity": None}):
            self.assertFalse(qm.is_critical(self.f(**k)), k)
        self.assertFalse(qm.is_critical(None)); self.assertFalse(qm.is_critical("HIGH"))


SUMMARY = "Sources that should agree on direction disagree: BULLISH from the MIL thesis; BEARISH from the regime. Relationship that failed: %s. Which is right is not decided here and the cause is not established."


class Flow(tbs.Base):
    """Real run() against a real ledger, signal log and integrity ledger."""

    def setUp(self):
        super().setUp()
        self.now = T0
        self.inbox_path = os.path.join(self.root, "inbox.jsonl")
        self.ilog = os.path.join(self.root, "integrity_ledger.jsonl")

    def say(self, uid, text, at=None):
        with open(self.inbox_path, "a") as f:
            f.write(json.dumps({"update_id": uid, "chat_id": "1", "text": text, "received_at": (at or self.now).isoformat()}) + "\n")

    def finding(self, rel="same_writer", sev="HIGH", probe="direction_conflict", paths=("a.b", "c.d"), now=None):
        f = inspector.finding(probe, sev, SUMMARY % rel, list(paths), {"relationship": rel, "families": ["x", "y"]})
        ledger.append(self.ilog, inspector.FINDING, f, now=now or self.now)
        return f

    def resolve(self, f, now=None):
        ledger.append(self.ilog, inspector.RESOLVED, {"fingerprint": f["fingerprint"]}, now=now or self.now)

    def kinds(self, *names):
        return [r for r in self.records() if r["event_type"] in names]

    def quiet(self, text="/quiet 3h", uid=1):
        self.say(uid, text); self.go(now=self.now)

    def at(self, minutes):
        return T0 + timedelta(minutes=minutes)

    # ---- commands
    def test_quiet_sets_a_period_with_an_explicit_end_and_says_so(self):
        self.quiet("/quiet 3h")
        st = qm.state(self.records(), self.now)
        self.assertTrue(st.active); self.assertEqual(st.until, T0 + timedelta(hours=3))
        self.assertIn("13:00 UTC", self.sent[-1]); self.assertIn("14:00 Lagos time", self.sent[-1])

    def test_a_quiet_period_that_ended_before_the_message_was_read_mutes_nothing(self):
        self.say(1, "/quiet 1h", at=T0 - timedelta(hours=2)); self.go(now=T0)
        self.assertEqual(self.kinds(qm.QUIET_SET), [])
        self.assertIn("already ended", self.sent[-1])

    def test_bad_commands_set_nothing(self):
        self.say(1, "/quiet 30h"); self.say(2, "/quiet soon"); self.go()
        self.assertEqual(self.kinds(qm.QUIET_SET), [])
        self.assertFalse(qm.state(self.records(), self.now).active)

    def test_loud_when_not_quiet_is_harmless(self):
        self.say(1, "/loud"); self.go()
        self.assertEqual(self.kinds(qm.QUIET_CLEARED), []); self.assertIn("isn't on", self.sent[-1])

    def test_help_lists_the_commands(self):
        self.assertIn("/quiet", bus.HELP_TEXT); self.assertIn("/loud", bus.HELP_TEXT)

    # ---- what is held
    def test_signals_are_held_not_sent_while_quiet_and_nothing_is_lost(self):
        self.quiet(); n = len(self.sent)
        tbs.write_signal(self.log, fired=self.now)
        s = self.go(now=self.at(5))
        self.assertEqual(len(self.sent), n)                                  # nothing went out
        self.assertEqual(s.get("signals_held"), 1)
        held = self.kinds(qm.BUS_HELD)
        self.assertEqual(len(held), 1); self.assertIn("sell", held[0]["payload"]["text"])
        self.assertEqual(self.kinds(br.BUS_SIGNAL_ANNOUNCED), [])
        self.go(now=self.at(10))                                             # a second run does not hold it twice
        self.assertEqual(len(self.kinds(qm.BUS_HELD)), 1)

    def test_a_held_signal_is_never_announced_later_as_if_it_were_new(self):
        self.quiet(); tbs.write_signal(self.log, fired=self.now); self.go(now=self.at(5))
        self.go(now=self.at(181)); self.go(now=self.at(190))
        self.assertEqual(self.kinds(br.BUS_SIGNAL_ANNOUNCED), [])
        self.assertEqual(self.kinds(br.BUS_SIGNAL_SKIPPED), [])

    def test_non_critical_integrity_findings_are_held(self):
        self.quiet()
        for rel, sev in (("consumer_of_macro_bias", "HIGH"), ("derived_and_may_lag", "MED")):
            self.finding(rel=rel, sev=sev, paths=("p." + rel, "q." + rel), now=self.at(1))
        n = len(self.sent); s = self.go(now=self.at(5))
        self.assertEqual(len(self.sent), n); self.assertEqual(s.get("integrity_held"), 2)
        self.assertEqual(s.get("critical_sent", 0), 0)
        self.go(now=self.at(10)); self.assertEqual(len(self.kinds(qm.BUS_HELD)), 2)   # held once each

    def test_a_critical_finding_breaks_through_once_with_a_plain_note(self):
        self.quiet(); n = len(self.sent)
        self.finding(now=self.at(1))
        s = self.go(now=self.at(5))
        self.assertEqual(s.get("critical_sent"), 1)
        out = self.sent[n:]
        self.assertEqual(len(out), 1); self.assertTrue(out[0].startswith("Quiet mode is on, but this is a critical"))
        self.assertIn("same_writer", out[0])
        self.go(now=self.at(10)); self.assertEqual(len(self.sent), n + 1)    # not repeated every run

    def test_a_critical_finding_with_a_failed_send_is_retried_from_stored_text(self):
        self.quiet(); self.finding(now=self.at(1)); self.ok = False
        self.go(now=self.at(5)); self.ok = True; n = len(self.sent)
        self.go(now=self.at(10))
        self.assertTrue(any(t.startswith("Quiet mode is on, but") for t in self.sent[n:]))

    # ---- what is never muted
    def test_replies_to_the_user_still_come_through(self):
        self.quiet(); self.say(2, "/now"); self.go(now=self.at(5))
        self.assertTrue(any("Quiet mode" not in t for t in self.sent[1:]))
        self.assertGreaterEqual(len(self.sent), 2)

    def test_the_users_own_price_alerts_still_fire(self):
        self.quiet(); self.say(2, "/alert 1.3400 above"); self.go(now=self.at(1))
        with open(os.path.join(self.root, "ohlc_5m_log.jsonl"), "a") as f:
            f.write(json.dumps({"datetime": (T0 + timedelta(minutes=3)).isoformat(), "open": 1.3390, "high": 1.3410, "low": 1.3385, "close": 1.3405}) + "\n")
        n = len(self.sent); self.go(now=self.at(7))
        self.assertTrue(any("Alert" in t for t in self.sent[n:]), self.sent[n:])

    # ---- the summary
    def test_one_summary_when_quiet_expires_and_never_a_second(self):
        self.quiet("/quiet 1h")
        tbs.write_signal(self.log, fired=self.now); self.go(now=self.at(5))
        self.finding(rel="consumer_of_macro_bias", sev="HIGH", now=self.at(10)); self.go(now=self.at(15))
        n = len(self.sent)
        s = self.go(now=self.at(65))
        self.assertEqual(s.get("quiet_summary"), 1)
        out = self.sent[n:]; self.assertEqual(len(out), 1, out)
        t = out[0]
        self.assertIn("Quiet mode ended", t); self.assertIn("1 signal and 1 integrity finding", t)
        self.assertIn("sell", t); self.assertIn("not now", t); self.assertIn("Integrity findings still open", t)
        self.go(now=self.at(70)); self.go(now=self.at(300))
        self.assertEqual(len(self.kinds(qm.BUS_QUIET_SUMMARY)), 1)
        self.assertEqual(len(self.sent), n + 1)                               # no duplicate summary, no duplicate announce

    def test_integrity_in_the_summary_is_not_repeated_by_the_normal_announcement(self):
        self.quiet("/quiet 1h"); self.finding(rel="consumer_of_macro_bias", sev="HIGH", now=self.at(10)); self.go(now=self.at(15))
        n = len(self.sent); s = self.go(now=self.at(65))
        self.assertEqual(s.get("announced", 0), 0)
        self.assertEqual(len(self.sent) - n, 1)

    def test_a_finding_that_opened_and_closed_while_quiet_is_still_reported(self):
        self.quiet("/quiet 1h"); f = self.finding(rel="consumer_of_macro_bias", sev="HIGH", now=self.at(10)); self.go(now=self.at(12))
        self.resolve(f, now=self.at(20)); self.go(now=self.at(25))
        n = len(self.sent); self.go(now=self.at(65))
        t = self.sent[n]
        self.assertIn("1 integrity finding", t); self.assertIn("No longer open", t); self.assertIn("direction_conflict", t)
        self.assertNotIn("BULLISH", t)                                       # closed findings are named, not re-described

    def test_an_empty_quiet_period_says_it_ended_once(self):
        self.quiet("/quiet 1h"); n = len(self.sent)
        self.go(now=self.at(65)); self.go(now=self.at(80))
        self.assertEqual(len(self.sent) - n, 1); self.assertIn("Nothing was held", self.sent[n])

    def test_loud_ends_it_now_and_summarises_what_was_held(self):
        self.quiet(); tbs.write_signal(self.log, fired=self.now); self.go(now=self.at(5))
        n = len(self.sent); self.say(2, "/loud", at=self.at(20)); s = self.go(now=self.at(20))
        self.assertFalse(qm.state(self.records(), self.at(21)).active)
        out = self.sent[n:]
        self.assertEqual(len(out), 2, out)                                    # the confirmation and ONE summary
        self.assertIn("Quiet mode ended", out[1]); self.assertIn("1 signal", out[1])

    def test_loud_with_nothing_held_sends_only_the_confirmation(self):
        self.quiet(); n = len(self.sent); self.say(2, "/loud", at=self.at(20)); self.go(now=self.at(20))
        self.assertEqual(len(self.sent) - n, 1)
        self.go(now=self.at(30)); self.assertEqual(len(self.sent) - n, 1)

    def test_signals_after_quiet_ends_are_announced_normally(self):
        self.quiet("/quiet 1h"); self.go(now=self.at(65))
        tbs.write_signal(self.log, fired=self.at(66)); n = len(self.sent)
        s = self.go(now=self.at(68))
        self.assertEqual(s.get("signals_announced"), 1); self.assertEqual(len(self.sent) - n, 1)

    def test_summary_survives_a_failed_send(self):
        self.quiet("/quiet 1h"); tbs.write_signal(self.log, fired=self.now); self.go(now=self.at(5))
        self.ok = False; self.go(now=self.at(65)); self.ok = True; n = len(self.sent)
        self.go(now=self.at(70))
        self.assertTrue(any("Quiet mode ended" in t for t in self.sent[n:]))
        self.assertEqual(len(self.kinds(qm.BUS_QUIET_SUMMARY)), 1)

    def test_a_corrupted_quiet_record_cannot_mute_signals(self):
        with open(os.path.join(self.root, br.LEDGER_NAME), "a") as f:
            f.write(json.dumps(ledger.make_record(qm.QUIET_SET, {"until": "garbage"}, recorded_at=T0)) + "\n")
        tbs.write_signal(self.log, fired=self.now); n = len(self.sent)
        s = self.go(now=self.at(2))
        self.assertEqual(s.get("signals_announced"), 1); self.assertEqual(len(self.sent) - n, 1)

    def test_flag_off_signals_are_not_touched_by_quiet_mode(self):
        self.quiet(); tbs.write_signal(self.log, fired=self.now)
        before = _rd(self.log); self.go(now=self.at(5), signals=False)
        self.assertEqual(before, _rd(self.log)); self.assertEqual(self.kinds(qm.BUS_HELD), [])


class Boundaries(unittest.TestCase):
    def test_the_engine_does_not_know_quiet_mode_exists(self):
        for d, name in [(d, n) for d in (HERE, ROOT) for n in os.listdir(d)]:
            if not name.endswith(".py") or name.startswith("test_") or name in ("bus_runner.py", "quiet_mode.py"):
                continue
            tree = ast.parse(_rd(os.path.join(d, name)))
            mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
                   {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            self.assertNotIn("quiet_mode", mods, name)

    def test_quiet_mode_imports_only_the_ledger_and_the_standard_library(self):
        tree = ast.parse(_rd(os.path.join(HERE, "quiet_mode.py")))
        mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
               {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertLessEqual(mods, {"re", "collections", "datetime", "zoneinfo", "ledger"})

    def test_scanner_live_is_untouched_by_this_feature(self):
        src = _rd(os.path.join(ROOT, "scanner_live.py"))
        for needle in ("quiet_mode", "/quiet", "/loud", "QUIET_SET"):
            self.assertNotIn(needle, src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
