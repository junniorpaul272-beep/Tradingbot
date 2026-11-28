"""
bot_router.py: one Telegram chat, several bots, one switch.

What it does
  * /bot            shows the active bot.   /bot btc   /bot gbp   switches it.   (/switch works the same)
  * /cancel         while a non-default bot is active, goes back to the default bot (GBPUSD). On the default bot it
                    is NOT intercepted, so a /cancel the scanner already has keeps working exactly as before.
  * A plain command (/stats, /status, /taken ...) goes to the ACTIVE bot only. The other bots' commands and
    conversations are on hold.
  * Adding the bot's name to a command sends it to that bot without switching: /taken btc 98250, /now gbp.
  * Free text (not a command) goes to the active bot if it takes free text, otherwise a one-line hold notice is sent.
  * SIGNALS ARE NEVER ROUTED. This module only sees messages YOU send; every bot keeps sending its own alerts through
    its own code, whichever bot is active.

Adding a bot later = one more Bot(...) in the list. A bot whose handler is None is a PASSTHROUGH: the router hands
the text back untouched (minus the bot name, if you typed one) for that bot's existing command code to deal with.
That is how GBPUSD plugs in without any change to its commands.

Wiring into the existing Telegram command dispatcher (a few lines, placed before the old command handling):

    from router.bot_router import build_default_router
    router = build_default_router()
    route = router.route(message_text)
    if route.handled:
        if route.reply:
            send_telegram(route.reply)
        return                         # BTC (or the router itself) dealt with it
    message_text = route.passthrough_text   # GBPUSD carries on exactly as before, with this text

State: router/state.json holds the active bot. It is written only by this module.
"""
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Bot:
    name: str                       # canonical short name, e.g. "btc"
    title: str                      # how it is shown, e.g. "BTC"
    aliases: tuple                  # every word that means this bot, e.g. ("btc", "btcusd")
    handler: object = None          # handler(text) -> reply or None; None = passthrough
    free_text: bool = False         # does it take messages that are not commands?


@dataclass
class Route:
    handled: bool                   # True: reply (if any) is the answer, do nothing else
    reply: str = None
    passthrough_text: str = None    # when not handled: the text the bot's own code should process
    bot: str = None                 # which bot the message was for


_CMD = re.compile(r"^/(\w+)(?:@\w+)?\s*(.*)$", re.S)


class Router:
    SWITCH = ("bot", "switch")
    RESERVED = SWITCH + ("cancel",)

    def __init__(self, bots, state_path, default=None):
        if not bots:
            raise ValueError("at least one bot is needed")
        self.bots = {b.name: b for b in bots}
        self.alias = {}
        for b in bots:
            for a in (b.name, *b.aliases):
                if a.lower() in self.alias and self.alias[a.lower()] != b.name:
                    raise ValueError(f"the word '{a}' belongs to two bots")
                self.alias[a.lower()] = b.name
        for s in self.RESERVED:
            if s in self.alias:
                raise ValueError(f"'{s}' is reserved for the switch")
        self.state_path = state_path
        self.default = default if default in self.bots else bots[0].name

    # -- state
    def active(self):
        try:
            with open(self.state_path) as f:
                name = json.load(f).get("active")
        except (FileNotFoundError, json.JSONDecodeError):
            name = None
        return name if name in self.bots else self.default

    def set_active(self, name):
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump({"active": name, "changed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                      f, indent=1)

    # -- routing
    def route(self, text):
        text = (text or "").strip()
        m = _CMD.match(text)
        if not m:
            return self._free_text(text)
        cmd, args = m.group(1).lower(), m.group(2).split()
        if cmd in self.SWITCH:
            return self._switch(args)
        if cmd == "cancel" and self.active() != self.default:
            return self._cancel()
        target, rest = None, []
        for a in args:
            if target is None and a.lower() in self.alias:
                target = self.alias[a.lower()]
            else:
                rest.append(a)
        bot = self.bots[target or self.active()]
        if bot.handler is None:  # passthrough
            out = text if target is None else "/" + cmd + (" " + " ".join(rest) if rest else "")
            return Route(False, passthrough_text=out, bot=bot.name)
        reply = bot.handler("/" + cmd + (" " + " ".join(rest) if rest else ""))
        if reply is None:
            return Route(True, reply=self._unknown(cmd, bot), bot=bot.name)
        if cmd == "help":
            reply += "\n\n" + self._switch_help(bot)
        return Route(True, reply=reply, bot=bot.name)

    def _free_text(self, text):
        bot = self.bots[self.active()]
        if bot.handler is None and bot.free_text:
            return Route(False, passthrough_text=text, bot=bot.name)
        talkers = [b.title for b in self.bots.values() if b.free_text and b.name != bot.name]
        who = " and ".join(talkers) if talkers else "the other bots"
        return Route(True, bot=bot.name, reply=(
            f"{bot.title} is the active bot and does not take free text, so {who} conversations are on hold. "
            f"Signals still arrive. /bot {self._first_alias(self._talker_first(bot))} switches."))

    def _talker_first(self, bot):
        """First other bot that takes free text (for the hold notice), else the first other bot."""
        others = self._others(bot)
        return next((b.name for b in others if b.free_text), others[0].name if others else bot.name)

    def _first_alias(self, name):
        b = self.bots[name]
        return min((b.name, *b.aliases), key=len)

    def _others(self, bot):
        return [b for b in self.bots.values() if b.name != bot.name]

    def _switch_help(self, bot):
        others = self._others(bot)
        ex = self._first_alias(others[0].name) if others else None
        line = f"/bot: show or switch the active bot (now {self.bots[self.active()].title}). /cancel: back to {self.bots[self.default].title}."
        if ex:
            line += f" Add a bot's name to reach it without switching, e.g. /status {ex}."
        return line

    def _unknown(self, cmd, bot):
        others = self._others(bot)
        hint = f" To send it to {others[0].title} instead, add its name: /{cmd} {self._first_alias(others[0].name)}." \
            if others else ""
        return f"{bot.title} has no /{cmd}. /help {self._first_alias(bot.name)} lists its commands.{hint}"

    def _cancel(self):
        left = self.bots[self.active()]
        self.set_active(self.default)
        d = self.bots[self.default]
        return Route(True, bot=d.name, reply=(
            f"Back to the default bot: {d.title}. Plain commands go to {d.title} again; {left.title} is on hold. "
            f"Signals from every bot keep arriving. /bot {self._first_alias(left.name)} switches back."))

    def _switch(self, args):
        if not args:
            a = self.bots[self.active()]
            others = ", ".join(b.title for b in self._others(a)) or "none"
            return Route(True, bot=a.name, reply=(
                f"Active bot: {a.title}. Plain commands go to {a.title}; on hold: {others}. "
                f"Signals from every bot keep arriving. /bot {self._first_alias(self._others(a)[0].name)} "
                f"switches." if self._others(a) else f"Active bot: {a.title}."))
        name = self.alias.get(args[0].lower())
        if name is None:
            known = ", ".join(f"{b.title} ({self._first_alias(b.name)})" for b in self.bots.values())
            return Route(True, reply=f"I don't know a bot called '{args[0]}'. Bots: {known}.")
        prev = self.active()
        self.set_active(name)
        b = self.bots[name]
        others = self._others(b)
        tail = (f" To reach {others[0].title} without switching, add its name, e.g. "
                f"/status {self._first_alias(others[0].name)}.") if others else ""
        head = f"Active bot: {b.title}." if prev != name else f"{b.title} was already the active bot."
        return Route(True, bot=name, reply=(
            f"{head} Plain commands now go to {b.title}"
            + (f"; {', '.join(o.title for o in others)} is on hold." if others else ".")
            + " Signals from every bot keep arriving." + tail))


def build_default_router(repo_root=None, display_tz="Africa/Lagos"):
    """GBPUSD (passthrough) + BTC, with state in router/state.json and the BTC bot in btc_bot/."""
    root = repo_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    btc_dir = os.path.join(root, "btc_bot")
    if btc_dir not in sys.path:
        sys.path.insert(0, btc_dir)
    from btc_commands import BtcCommands  # noqa: E402 (needs the path set above)
    btc = BtcCommands(os.path.join(btc_dir, "data"), os.path.join(btc_dir, "accounts.json"), display_tz)
    bots = [Bot("gbpusd", "GBPUSD", ("gbp",), handler=None, free_text=True),
            Bot("btc", "BTC", ("btcusd",), handler=btc.handle)]
    return Router(bots, os.path.join(root, "router", "state.json"), default="gbpusd")
