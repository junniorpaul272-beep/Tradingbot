import json
import os
import shutil
import tempfile
import unittest

from bot_router import Bot, Router, build_default_router


def fake_bot_handler(name, known):
    return lambda text: f"{name} got {text}" if text.split()[0] in known else None


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.state = os.path.join(self.d, "router", "state.json")
        self.bots = [Bot("gbpusd", "GBPUSD", ("gbp",), handler=None, free_text=True),
                     Bot("btc", "BTC", ("btcusd",), handler=fake_bot_handler("BTC", {"/taken", "/stats", "/help"}))]

    def router(self, bots=None):
        return Router(bots or self.bots, self.state, default="gbpusd")

    def test_default_is_gbpusd_and_everything_passes_through_untouched(self):
        r = self.router()
        self.assertEqual(r.active(), "gbpusd")
        for text in ("/now", "/shadow exp3", "what is happening", "/closed 1.2650"):
            x = r.route(text)
            self.assertFalse(x.handled)
            self.assertEqual(x.passthrough_text, text)                 # byte-identical: GBPUSD behaves as before
        self.assertFalse(os.path.exists(self.state))                   # routing alone never writes state

    def test_explicit_bot_name_reaches_that_bot_without_switching(self):
        r = self.router()
        x = r.route("/taken btc 98250")
        self.assertTrue(x.handled)
        self.assertEqual(x.reply, "BTC got /taken 98250")              # the name is stripped before the bot sees it
        self.assertEqual(r.active(), "gbpusd")
        x = r.route("/status gbp")
        self.assertFalse(x.handled)
        self.assertEqual(x.passthrough_text, "/status")
        x = r.route("/btcusd")                                         # a bare name is not a command
        self.assertFalse(x.handled)

    def test_switching_persists_and_puts_the_other_bot_on_hold(self):
        r = self.router()
        x = r.route("/bot btc")
        self.assertIn("Active bot: BTC", x.reply)
        self.assertIn("Signals from every bot keep arriving", x.reply)
        self.assertEqual(json.load(open(self.state))["active"], "btc")
        r2 = self.router()                                             # a fresh process remembers
        self.assertEqual(r2.active(), "btc")
        self.assertEqual(r2.route("/stats").reply, "BTC got /stats")
        x = r2.route("how is gbpusd")
        self.assertTrue(x.handled)
        self.assertIn("on hold", x.reply)
        x = r2.route("/shadow")
        self.assertTrue(x.handled)
        self.assertIn("BTC has no /shadow", x.reply)
        self.assertIn("/shadow gbp", x.reply)
        x = r2.route("/shadow gbp")
        self.assertFalse(x.handled)
        self.assertEqual(x.passthrough_text, "/shadow")
        self.assertIn("Active bot: GBPUSD", r2.route("/switch gbp").reply)
        self.assertFalse(r2.route("/now").handled)

    def test_cancel_returns_to_default_and_never_shadows_the_default_bots_own_cancel(self):
        r = self.router()
        x = r.route("/cancel")                                         # on the default bot: untouched passthrough
        self.assertFalse(x.handled)
        self.assertEqual(x.passthrough_text, "/cancel")
        self.assertFalse(os.path.exists(self.state))
        r.route("/bot btc")
        x = r.route("/cancel")
        self.assertTrue(x.handled)
        self.assertIn("Back to the default bot: GBPUSD", x.reply)
        self.assertEqual(r.active(), "gbpusd")
        self.assertEqual(self.router().active(), "gbpusd")             # persisted
        self.assertFalse(r.route("/now").handled)                      # commands reach GBPUSD again
        r.route("/bot btc")
        self.assertTrue(r.route("/cancel@SomeBot").handled)            # Telegram's /cancel@botname form
        self.assertEqual(r.active(), "gbpusd")

    def test_bad_input_changes_nothing(self):
        r = self.router()
        self.assertIn("don't know a bot called 'xyz'", r.route("/bot xyz").reply)
        self.assertEqual(r.active(), "gbpusd")
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w") as f:
            f.write("{broken")
        self.assertEqual(r.active(), "gbpusd")                         # corrupt state falls back to the default
        with open(self.state, "w") as f:
            json.dump({"active": "deleted_bot"}, f)
        self.assertEqual(r.active(), "gbpusd")

    def test_help_gets_the_switch_footer(self):
        r = self.router()
        r.route("/bot btc")
        x = r.route("/help")
        self.assertIn("BTC got /help", x.reply)
        self.assertIn("/bot: show or switch", x.reply)

    def test_a_third_bot_is_one_line(self):
        bots = self.bots + [Bot("eurusd", "EURUSD", ("eu",), handler=fake_bot_handler("EUR", {"/stats"}))]
        r = self.router(bots)
        self.assertEqual(r.route("/stats eu").reply, "EUR got /stats")
        self.assertEqual(r.route("/stats btcusd").reply, "BTC got /stats")
        r.route("/bot eu")
        self.assertEqual(r.route("/stats").reply, "EUR got /stats")
        self.assertIn("BTC (", r.route("/bot nope").reply)
        self.assertFalse(r.route("/now gbp").handled)

    def test_ambiguous_or_reserved_names_are_rejected(self):
        with self.assertRaises(ValueError):
            Router([Bot("a", "A", ("x",)), Bot("b", "B", ("x",))], self.state)
        with self.assertRaises(ValueError):
            Router([Bot("bot", "Bot", ())], self.state)
        with self.assertRaises(ValueError):
            Router([], self.state)

    def test_default_router_wires_the_real_btc_commands_to_gbpusd_passthrough(self):
        r = build_default_router()
        r.state_path = self.state                                      # never touch the real state file
        self.assertEqual(r.active(), "gbpusd")
        self.assertFalse(r.route("/now").handled)
        x = r.route("/help btc")
        self.assertTrue(x.handled)
        self.assertIn("/taken [price]", x.reply)
        self.assertIn("/bot: show or switch", x.reply)
        self.assertEqual(r.active(), "gbpusd")


if __name__ == "__main__":
    unittest.main(verbosity=2)
