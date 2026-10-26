"""Run: python test_bot_menu.py — the Telegram "/" menu and /help cannot drift from the commands the bot really answers.
Every menu entry is a real command; every command BUS or the runner handles is in /help; /help has ONE author (BUS)."""
import os, re, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import bus, bus_runner as br, quiet_mode as qm, set_bot_commands as sbc

def rd(p):
    with open(p) as f:
        return f.read()


LIVE = rd(os.path.join(ROOT, "scanner_live.py"))
RUNNER_CMDS = {"/taken", "/skip", "/quiet", "/loud", "/reconstruct"}


def legacy_commands():
    return set(re.findall(r'"(/[a-z]+)"', "\n".join(l for l in LIVE.splitlines() if "elif cmd in (" in l or "if cmd in (" in l)))


class Menu(unittest.TestCase):
    def test_every_menu_entry_is_a_command_the_bot_answers(self):
        legacy = legacy_commands()
        for c, _d in sbc.MENU:
            cmd = "/" + c
            ok = (cmd in bus.ACT_COMMANDS or cmd in bus.COMMAND_INTENTS or cmd in bus.HELP_COMMANDS or cmd in RUNNER_CMDS or cmd in legacy)
            self.assertTrue(ok, f"{cmd} is in the menu but nothing answers it")

    def test_telegram_limits(self):
        names = [c for c, _ in sbc.MENU]
        self.assertEqual(len(names), len(set(names))); self.assertLessEqual(len(names), 100)
        for c, d in sbc.MENU:
            self.assertRegex(c, r"^[a-z0-9_]{1,32}$"); self.assertTrue(3 <= len(d) <= 256, c)
        p = sbc.payload(); self.assertEqual(p["scope"], {"type": "all_private_chats"})   # the group's menu is not touched

    def test_the_menu_no_longer_offers_commands_that_were_retired_or_confusing(self):
        names = {c for c, _ in sbc.MENU}
        for gone in ("marketintent", "confirm", "undo", "win", "loss", "biasab"):
            self.assertNotIn(gone, names)

    def test_the_menu_includes_the_new_commands(self):
        names = {c for c, _ in sbc.MENU}
        for new in ("taken", "skip", "know", "ask", "investigate", "reconstruct", "quiet", "loud", "help"):
            self.assertIn(new, names)

    def test_the_script_never_prints_the_token(self):
        src = rd(os.path.join(HERE, "set_bot_commands.py"))
        self.assertNotRegex(src, r"print\(f?[\"'][^)]*\{token")


class Help(unittest.TestCase):
    def test_every_command_the_bot_handles_is_in_help(self):
        cmds = set(bus.ACT_COMMANDS) | set(bus.COMMAND_INTENTS) | RUNNER_CMDS
        cmds -= {"/change"}                                          # /change is an alias of /changed
        missing = [c for c in sorted(cmds) if c not in bus.HELP_TEXT]
        self.assertEqual(missing, [], f"commands the bot answers but /help does not mention: {missing}")

    def test_every_menu_command_is_in_help(self):
        for c, _ in sbc.MENU:
            self.assertIn("/" + c, bus.HELP_TEXT)

    def test_help_fits_in_one_telegram_message(self):
        self.assertLess(len(bus.HELP_TEXT), br.TELEGRAM_LIMIT)

    def test_help_start_and_commands_all_reach_the_one_help(self):
        for c in ("/help", "/start", "/commands", "/bushelp", "/menu", "/help@ValleyBot"):
            self.assertEqual(bus.route(c).kind, "help", c)

    def test_the_old_scanner_help_is_retired_so_there_is_one_author(self):
        self.assertNotIn("HELP_TEXT", LIVE.replace("# The personal-chat /help list lives in bus.py (HELP_TEXT", ""))
        self.assertNotRegex(LIVE, r'cmd in \("/help"')
        self.assertIn("format_group_help", LIVE)                      # the group's own help is untouched

    def test_help_says_how_replies_arrive(self):
        self.assertIn("when the scanner runs", bus.HELP_TEXT)

    def test_old_commands_are_listed_only_if_they_still_exist(self):
        legacy = legacy_commands()
        old = re.search(r"OLDER COMMANDS.*?\n(.*?)\n\n", bus.HELP_TEXT, re.S).group(1)
        for c in re.findall(r"/[a-z]+", old):
            self.assertIn(c, legacy, f"/help lists {c} as an older command but the scanner no longer handles it")


if __name__ == "__main__":
    unittest.main()
