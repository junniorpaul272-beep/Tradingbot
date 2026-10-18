"""Run: python test_bus_taken.py — /taken and /skip (roadmap Phase A): the user's answer to a fired signal.
The signal stays the engine's record; the Catalog stores only what the USER did, and /taken logs a case whose
bot-plan side is the signal AS IT FIRED. Nothing is ever double-logged, and no trade is promoted automatically."""
import ast, json, os, sys, unittest
from datetime import datetime, timezone, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import ledger, bus_runner as br, user_catalog as uc
import test_bus_signals as tbs

FIRED = tbs.FIRED


def _rd(p):
    with open(p) as f:
        return f.read()


class Flow(tbs.Base):
    def setUp(self):
        super().setUp()
        self.inbox_path = os.path.join(self.root, "inbox.jsonl")
        self.n = 0

    def go(self, now=None, signals=False):          # announcements off unless a test asks: the last message is then the command's reply
        return super().go(now=now, signals=signals)

    def say(self, text, at=None):
        self.n += 1
        with open(self.inbox_path, "a") as f:
            f.write(json.dumps({"update_id": 1000 + self.n, "chat_id": "1", "text": text, "received_at": (at or self.now).isoformat()}) + "\n")

    def cat(self):
        return uc.Catalog(os.path.join(self.root, br.LEDGER_NAME))

    def of(self, kind):
        return [r for r in self.records() if r["event_type"] == kind]

    def last(self):
        return self.sent[-1]

    # ---- /taken
    def test_taken_logs_a_case_from_the_signal_with_the_signal_as_the_bots_plan(self):
        tbs.write_signal(self.log); self.say("/taken"); self.go()
        self.assertIn("Logged CASE-0001", self.last()); self.assertIn("1.31234", self.last())
        case = self.cat().get_case("CASE-0001")
        t = case["trade"]["payload"]
        self.assertEqual((t["direction"], t["entry"], t["sl"], t["tp"]), ("SELL", 1.31234, 1.31534, 1.30634))
        plan = case["bot_plan"]["payload"]
        self.assertEqual(plan["result"], "TRADE"); self.assertEqual(plan["plan_as_of"], ledger._iso(FIRED))
        self.assertEqual(plan["facts"]["tier_label"], "TIER_2_FIB")           # the signal as it fired, not today's market
        r = self.of(uc.SIGNAL_RESPONDED)
        self.assertEqual(len(r), 1); self.assertEqual(r[0]["payload"]["response"], "TAKEN"); self.assertEqual(r[0]["payload"]["case_id"], "CASE-0001")

    def test_a_fill_price_is_stored_and_the_signals_own_entry_is_kept_on_the_bots_side(self):
        tbs.write_signal(self.log); self.say("/taken 1.31200"); self.go()
        c = self.cat().get_case("CASE-0001")
        self.assertEqual(c["trade"]["payload"]["entry"], 1.312)
        self.assertEqual(c["bot_plan"]["payload"]["facts"]["entry"], 1.31234)
        self.assertIn("You filled at 1.31200", self.last())

    def test_a_fill_on_the_wrong_side_of_the_stop_is_refused_and_the_signal_stays_open(self):
        tbs.write_signal(self.log); self.say("/taken 1.31600"); self.go()    # a SELL cannot enter above its own stop
        self.assertIn("I didn't log it", self.last())
        self.assertEqual(self.of(uc.TRADE_LOGGED), []); self.assertEqual(self.of(uc.SIGNAL_RESPONDED), [])
        self.say("/taken"); self.go(); self.assertIn("Logged CASE-0001", self.last())

    def test_garbage_after_taken_is_not_guessed(self):
        tbs.write_signal(self.log); self.say("/taken maybe"); self.go()
        self.assertIn("Format", self.last()); self.assertEqual(self.of(uc.TRADE_LOGGED), [])

    def test_answering_twice_never_double_logs(self):
        tbs.write_signal(self.log); self.say("/taken"); self.say("/taken"); self.go()
        self.assertEqual(len(self.of(uc.TRADE_LOGGED)), 1)
        self.assertIn("no fired signal waiting", self.last())

    def test_no_signal_means_nothing_is_written(self):
        self.say("/taken"); self.say("/skip"); self.go()
        self.assertEqual(self.sent.count("There is no fired signal waiting for an answer."), 2)
        self.assertEqual(self.of(uc.TRADE_LOGGED) + self.of(uc.SIGNAL_RESPONDED), [])

    def test_a_record_without_prices_cannot_be_logged_for_you(self):
        tbs.write_signal(self.log, risk_result={"target_r": 2.0}); self.say("/taken"); self.go()
        self.assertIn("doesn't carry an entry", self.last()); self.assertEqual(self.of(uc.TRADE_LOGGED), [])

    def test_a_message_stamped_before_the_signal_fired_is_refused(self):
        tbs.write_signal(self.log); self.say("/taken", at=FIRED - timedelta(minutes=5)); self.go()
        self.assertIn("before that signal fired", self.last()); self.assertEqual(self.of(uc.TRADE_LOGGED), [])

    def test_the_user_can_still_log_the_case_they_took_hours_later(self):
        tbs.write_signal(self.log); self.say("/taken", at=FIRED + timedelta(hours=3)); self.go(now=FIRED + timedelta(hours=3, minutes=1))
        self.assertIn("Logged CASE-0001", self.last())
        self.assertEqual(self.cat().get_case("CASE-0001")["trade"]["ts"], ledger._iso(FIRED + timedelta(hours=3)))

    # ---- reviewer's edge cases: interruption and invalid input
    def test_an_interrupted_taken_is_completed_on_retry_not_repeated(self):
        for broken in ("attach_bot_plan", "respond_to_signal"):
            with self.subTest(broken=broken):
                self.setUp(); tbs.write_signal(self.log); self.say("/taken")
                real = getattr(uc.Catalog, broken)
                calls = {"n": 0}
                def boom(*a, **k):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise RuntimeError("crash mid-command")
                    return real(*a, **k)
                setattr(uc.Catalog, broken, boom)
                try:
                    self.go()                                    # first attempt dies partway through
                finally:
                    setattr(uc.Catalog, broken, real)
                # the user sends it again (the crashed attempt answered with an error and was marked handled)
                self.say("/taken"); self.go(now=self.now + timedelta(minutes=5))
                self.assertEqual(len(self.of(uc.TRADE_LOGGED)), 1, broken)       # never a second case
                self.assertEqual(len(self.of(uc.BOT_PLAN_ATTACHED)), 1, broken)
                self.assertEqual(len(self.of(uc.SIGNAL_RESPONDED)), 1, broken)
                self.assertIn("CASE-0001", self.last())
                self.tearDown()

    def test_invalid_fills_are_rejected_with_nothing_written(self):
        tbs.write_signal(self.log)
        for bad in ("nan", "inf", "-1.3", "0", "1.31600", "1e999"):
            self.say(f"/taken {bad}")
        self.go()
        self.assertEqual(self.of(uc.TRADE_LOGGED), []); self.assertEqual(self.of(uc.BOT_PLAN_ATTACHED), [])
        self.assertEqual(self.of(uc.SIGNAL_RESPONDED), [])
        self.say("/taken"); self.go(); self.assertIn("Logged CASE-0001", self.last())      # still answerable afterwards

    # ---- /skip
    def test_skip_records_the_decision_and_the_note_verbatim_without_a_case(self):
        tbs.write_signal(self.log); self.say("/skip too close to NFP #news"); self.go()
        r = self.of(uc.SIGNAL_RESPONDED)
        self.assertEqual((r[0]["payload"]["response"], r[0]["payload"]["case_id"], r[0]["payload"]["note"]), ("SKIPPED", None, "too close to NFP #news"))
        self.assertEqual(self.of(uc.TRADE_LOGGED), []); self.assertIn("skipped", self.last())

    def test_skip_then_taken_does_not_resurrect_it(self):
        tbs.write_signal(self.log); self.say("/skip"); self.say("/taken"); self.go()
        self.assertEqual(self.of(uc.TRADE_LOGGED), []); self.assertIn("no fired signal waiting", self.last())

    def test_with_two_open_signals_the_newest_is_answered_and_the_other_is_untouched(self):
        tbs.write_signal(self.log, fired=FIRED - timedelta(hours=1)); tbs.write_signal(self.log, fired=FIRED)
        self.say("/skip"); self.go()
        self.assertIn("1 older unanswered", self.last())
        answered = {r["payload"]["signal_fired_at"] for r in self.of(uc.SIGNAL_RESPONDED)}
        self.assertEqual(answered, {ledger._iso(FIRED)})

    # ---- delivery and boundaries
    def test_a_failed_send_is_retried_without_logging_twice(self):
        tbs.write_signal(self.log); self.say("/taken"); self.ok = False; self.go()
        self.ok = True; self.go(now=self.now + timedelta(minutes=5))
        self.assertEqual(len(self.of(uc.TRADE_LOGGED)), 1)
        self.assertGreaterEqual(sum("Logged CASE-0001" in t for t in self.sent), 2)

    def test_it_works_with_signal_reporting_switched_off(self):
        tbs.write_signal(self.log); self.say("/taken"); self.go(signals=False)
        self.assertIn("Logged CASE-0001", self.last())

    def test_the_announcement_tells_you_how_to_answer(self):
        tbs.write_signal(self.log); self.go(signals=True)
        self.assertIn("/taken", self.sent[-1]); self.assertIn("/skip", self.sent[-1])

    def test_review_shows_the_machine_side_as_it_fired(self):
        tbs.write_signal(self.log); self.say("/taken"); self.say("/botvsme 1"); self.go()
        self.assertIn("What the bot had at the time: trade", self.sent[-1])      # a bot plan exists and /botvsme can read it


class CatalogLevel(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(); self.c = uc.Catalog(os.path.join(self.d, "l.jsonl"))

    def tearDown(self):
        import shutil; shutil.rmtree(self.d, ignore_errors=True)

    def test_validation(self):
        with self.assertRaises(ledger.LedgerError): self.c.respond_to_signal("s1", "MAYBE", now=FIRED)
        with self.assertRaises(ledger.LedgerError): self.c.respond_to_signal("s1", "TAKEN", case_id="CASE-0009", now=FIRED)
        with self.assertRaises(ledger.LedgerError): self.c.respond_to_signal("s1", "TAKEN", now=FIRED)
        with self.assertRaises(ledger.LedgerError): self.c.respond_to_signal("s1", "SKIPPED", case_id="CASE-0001", now=FIRED)
        self.c.respond_to_signal("s1", "SKIPPED", now=FIRED)
        with self.assertRaises(ledger.LedgerError): self.c.respond_to_signal("s1", "SKIPPED", now=FIRED)

    def test_responses_are_bitemporal(self):
        self.c.respond_to_signal("s1", "SKIPPED", now=FIRED)
        self.assertEqual(self.c.signal_responses(as_of=FIRED - timedelta(minutes=1)), {})
        self.assertIn("s1", self.c.signal_responses(as_of=FIRED + timedelta(minutes=1)))


class Boundaries(unittest.TestCase):
    def test_the_old_confirmation_flow_cannot_swallow_the_commands_any_more(self):
        src = _rd(os.path.join(ROOT, "scanner_live.py"))
        self.assertNotIn('cmd in ("/taken", "taken")', src); self.assertNotIn('cmd in ("/skip", "skip")', src)

    def test_nothing_in_the_engine_imports_the_new_code(self):
        for d, name in [(d, n) for d in (HERE, ROOT) for n in os.listdir(d)]:
            if name.endswith(".py") and not name.startswith("test_") and name not in ("bus_runner.py", "user_catalog.py"):
                tree = ast.parse(_rd(os.path.join(d, name)))
                mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
                       {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
                if name in ("scanner_live.py", "mil.py", "min_scanner.py", "scanner_observation.py", "scanner_common.py"):
                    self.assertNotIn("user_catalog", mods, name); self.assertNotIn("bus_runner", mods, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
