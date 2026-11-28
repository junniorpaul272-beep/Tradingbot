"""
btc_commands.py: the BTC bot's commands. No network and no market data: it reads the files the scheduled runner
stored (trades.jsonl, bank_ledger.jsonl, scans.jsonl, latest.json) and writes ONLY the journal (journal.jsonl).
It returns text; sending that text to Telegram is the caller's job (the router / the scanner's dispatcher).

Commands (the bot name "btc" is added by the router; /taken btc works while GBPUSD is the active bot):
  /taken [price]     I entered the open BTC signal on my demo (price = my real fill)
  /win [price]       my last taken trade won: closes it at the bot's exit (or the latest price, or your price)
  /closed [price]    I closed my taken trade (price = my real fill; optional once the bot has signalled the exit)
  /undo              cancel the latest /taken or /closed (no redo: just record it again)
  /journal           my recent taken / closed trades
  /status            both sides of the strategy, the demo accounts, and what is waiting on me
  /stats             profit factor, R, drawdown: strategy, my taken trades, every demo account
  /bank              the demo accounts with their numbers
  /technical  /fundamental   the latest stored scans
  /help

Poka-yoke: a price that is more than 5% away from where the market was is refused (a dropped digit would wreck the
record), a fill already beyond the exit is refused, and /closed will not guess a price when the bot has not signalled
an exit. Add the word "force" to override a refusal on purpose.
"""
import json
import os
import re
import time
from dataclasses import fields
from datetime import datetime
from zoneinfo import ZoneInfo

import btc_bank
import btc_report
import btc_stats
from btc_engine import Config
from btc_journal import Journal, exit_after, last_jsonl, open_signals, read_jsonl, taken_trades

PRICE_TOLERANCE = 0.05
STALE_STATUS_S = 2 * 3600


def parse_args(tokens):
    """-> (price or None, force, error text or None)"""
    price, force = None, False
    for tok in tokens:
        if tok.lower() == "force":
            force = True
            continue
        try:
            v = float(tok.replace(",", "").replace("$", ""))
        except ValueError:
            return None, False, f"I didn't understand '{tok}'. Send a price like 98250, or nothing."
        if v <= 0:
            return None, False, "A price has to be above zero."
        if price is not None:
            return None, False, "Send only one price."
        price = v
    return price, force, None


def results_text(data_dir, accounts, title, stamp):
    """The full results message built from the stored files (used by /stats and the runner's --stats)."""
    signals = read_jsonl(os.path.join(data_dir, "trades.jsonl"))
    segs = btc_stats.segments_from_events(signals)
    strategy = btc_stats.compute_stats(segs)
    by_side = {k: btc_stats.compute_stats(v) for k, v in btc_stats.split_by(segs, "side").items()}
    pairs = account_pairs(data_dir, accounts)
    j = Journal(os.path.join(data_dir, "journal.jsonl"))
    taken, waiting = None, None
    if j.active():
        taken = btc_stats.compute_stats(taken_trades(j))
        open_n = len(j.open_taken())
        waiting = [f"{open_n} taken trade(s) still open: not counted until you /closed btc them."] if open_n else None
    return btc_report.stats_report(title, stamp, strategy, by_side, pairs, taken, waiting)


def account_pairs(data_dir, accounts):
    if not accounts:
        return []
    bank = btc_bank.Bank(accounts, os.path.join(data_dir, "bank_ledger.jsonl"), write=False)
    bank.ensure_accounts()  # in memory only
    return [(row, btc_stats.compute_stats(bank.closed_trades(row["name"]), row["start"])) for row in bank.summary()]


class BtcCommands:
    NAME = "btc"

    def __init__(self, data_dir, accounts_path, display_tz="Africa/Lagos", clock=time.time):
        self.dir = data_dir
        self.accounts_path = accounts_path
        self.tz = display_tz
        self.clock = clock
        self.table = {"taken": self.c_taken, "closed": self.c_closed, "win": self.c_win, "undo": self.c_undo, "journal": self.c_journal,
                      "status": self.c_status, "stats": self.c_stats, "bank": self.c_bank,
                      "technical": self.c_technical, "fundamental": self.c_fundamental, "help": self.c_help}

    # -- plumbing
    def path(self, name):
        return os.path.join(self.dir, name)

    def journal(self):
        return Journal(self.path("journal.jsonl"))

    def signals(self):
        return read_jsonl(self.path("trades.jsonl"))

    def latest(self):
        try:
            with open(self.path("latest.json")) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def accounts(self):
        accts, problem = btc_bank.load_accounts(self.accounts_path)
        return accts, problem

    def when(self, epoch):
        return btc_report.when(epoch, self.tz)

    def word(self, side):
        return btc_report.SIDE[side]["word"]

    def handle(self, text):
        """The reply text, or None when this is not a BTC command (the router decides what to say then)."""
        m = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)$", (text or "").strip(), re.S)
        if not m:
            return None
        fn = self.table.get(m.group(1).lower())
        return None if fn is None else fn(m.group(2).split())

    def command_names(self):
        return sorted(self.table)

    # -- /taken
    def c_taken(self, args):
        price, force, err = parse_args(args)
        if err:
            return err
        signals = self.signals()
        if not signals:
            return "No BTC signal has been recorded yet, so there is nothing to take."
        j = self.journal()
        opens = open_signals(signals)
        fresh = [s for s in opens if not j.is_taken(s["event_id"])]
        if not fresh:
            if opens:
                s = opens[0]
                return (f"Your BTC {self.word(s['side'])} from {self.when(s['bar_time'])} is already recorded as "
                        f"taken. Send /closed btc when you close it.")
            entries = [s for s in signals if s["kind"] in ("ENTRY", "REENTRY")]
            if not entries:
                return "No BTC entry signal has been recorded yet."
            last = entries[-1]
            ex = exit_after(signals, last)
            gone = f"; the bot already left it at {btc_report.money(ex['price'])}" if ex else ""
            return (f"No BTC signal is open right now. The latest was a {self.word(last['side'])} "
                    f"{last['kind'].lower()} at {btc_report.money(last['price'])} ({self.when(last['bar_time'])})"
                    f"{gone}. Nothing to take.")
        sig = fresh[0]
        fill = price if price is not None else sig["price"]
        if price is not None and not force and abs(price / sig["price"] - 1) > PRICE_TOLERANCE:
            return (f"{btc_report.money(price)} is {abs(price / sig['price'] - 1) * 100:.0f}% away from the signal "
                    f"price {btc_report.money(sig['price'])}: probably a typo. Send it again, or add the word "
                    f"force if it is right.")
        stop = sig["active_stop"]
        if not force and ((sig["side"] == "LONG" and fill <= stop) or (sig["side"] == "SHORT" and fill >= stop)):
            return (f"A {self.word(sig['side'])} filled at {btc_report.money(fill)} is already beyond the exit "
                    f"({btc_report.money(stop)}), so it would be stopped at once. Check the price, or add force.")
        j.take(sig, fill)
        risk = abs(fill - stop)
        lines = [f"Recorded: you took the BTC {self.word(sig['side'])} at {btc_report.money(fill)}"
                 + (f" (signal price {btc_report.money(sig['price'])})." if price is not None else
                    " (the signal price; add your real fill, like /taken btc 98250, for realistic results)."),
                 f"Exit to respect: {btc_report.money(stop)}. 1R is {btc_report.money(risk)} "
                 f"({risk / fill * 100:.1f}% of price).",
                 "When you close it, send /closed btc."]
        if len(fresh) > 1:
            lines.append("Two BTC signals were open; I recorded the newest.")
        return "\n".join(lines)

    # -- /closed
    def c_win(self, args):
        """Same as /closed, but it must be a winner: a loss is refused so a mistaken /win cannot hide one."""
        return self.c_closed(args, win=True)

    def c_closed(self, args, win=False):
        price, force, err = parse_args(args)
        if err:
            return err
        j = self.journal()
        open_t = j.open_taken()
        if not open_t:
            return "You have no taken BTC trade open. /taken btc records one."
        t = open_t[-1]
        signals = self.signals()
        rec = next((s for s in signals if s["event_id"] == t["event_id"]), None)
        ex = exit_after(signals, rec) if rec else None
        used_bot, note = False, None
        if price is None and not ex and win:
            latest = self.latest()
            if not latest:
                return "No market price is recorded yet. Send /win btc <your price>."
            fill, ref, used_bot = latest["statuses"]["LONG"]["last_close"], None, False
            note = (f"The bot has not signalled an exit yet, so I used the latest recorded price "
                    f"({btc_report.money(fill)}). Send /undo, then /win btc <your price> for your real fill.")
        elif price is None:
            if not ex:
                return ("The bot has not signalled an exit for that trade yet. If you closed it early, "
                        "send /closed btc <your price>.")
            fill, ref, used_bot = ex["price"], ex["price"], True
        else:
            fill = price
            latest = self.latest()
            # compare with where the market really was; with nothing to compare against, do not pretend to check
            ref = ex["price"] if ex else (latest["statuses"]["LONG"]["last_close"] if latest else None)
            if not force and ref and abs(price / ref - 1) > PRICE_TOLERANCE:
                return (f"{btc_report.money(price)} is {abs(price / ref - 1) * 100:.0f}% away from where BTC was "
                        f"({btc_report.money(ref)}): probably a typo. Send it again, or add force if it is right.")
        if win and (fill - t["fill"]) * (1 if t["side"] == "LONG" else -1) <= 0:
            return (f"That would not be a win: your BTC {self.word(t['side'])} from {btc_report.money(t['fill'])} "
                    f"closing at {btc_report.money(fill)} is a "
                    f"{'loss' if fill != t['fill'] else 'break-even'}. Nothing was recorded. "
                    f"Use /closed btc if it really closed there.")
        close_t = ex.get("close_time", ex["bar_time"] + 1800) if ex else int(self.clock())
        j.close(t, fill, ex["price"] if ex else None, close_t)
        d = 1 if t["side"] == "LONG" else -1
        risk = abs(t["fill"] - t["stop"])
        r = d * (fill - t["fill"]) / risk if risk > 0 else None
        done = taken_trades(j)
        total = [btc_stats.trade_r(x) for x in done]
        total_r = sum(x for x in total if x is not None)
        lines = [f"Closed: your BTC {self.word(t['side'])} {btc_report.money(t['fill'])} → "
                 f"{btc_report.money(fill)}, {'n/a' if r is None else f'{r:+.2f}R'}."]
        if note:
            lines.append(note)
        if used_bot:
            lines.append("That used the bot's exit price, not your own fill. Send /undo, then "
                         "/closed btc <your price> if yours differed.")
        lines.append(f"Your taken trades so far: {len(done)} closed, total {total_r:+.1f}R.")
        if len(open_t) > 1:
            lines.append(f"You still have {len(open_t) - 1} other taken trade(s) open.")
        return "\n".join(lines)

    # -- /undo, /journal
    def c_undo(self, args):
        j = self.journal()
        e = j.undo()
        if e is None:
            return "Nothing to undo."
        if e["type"] == "taken":
            return (f"Undone: your /taken {self.word(e['side'])} at {btc_report.money(e['fill'])}. "
                    f"It no longer counts.")
        t = next(x for x in j.entries if x["id"] == e["taken_id"])
        return (f"Undone: your /closed on the {self.word(t['side'])} from {btc_report.money(t['fill'])}. "
                f"That trade is open again.")

    def c_journal(self, args):
        j = self.journal()
        act = j.active()[-8:]
        if not act:
            return "Your BTC journal is empty. /taken btc records a trade."
        lines = ["Your recent BTC journal"]
        for e in act:
            ts = datetime.fromisoformat(e["ts"]).astimezone(ZoneInfo(self.tz)).strftime("%a %d %b %H:%M")
            if e["type"] == "taken":
                lines.append(f"{ts}: took {self.word(e['side'])} at {btc_report.money(e['fill'])}")
            else:
                t = next(x for x in j.entries if x["id"] == e["taken_id"])
                d = 1 if t["side"] == "LONG" else -1
                risk = abs(t["fill"] - t["stop"])
                r = f", {d * (e['fill'] - t['fill']) / risk:+.2f}R" if risk > 0 else ""
                lines.append(f"{ts}: closed {self.word(t['side'])} at {btc_report.money(e['fill'])}{r}")
        return "\n".join(lines)

    # -- /status
    def c_status(self, args):
        latest = self.latest()
        if not latest:
            return "No BTC run has been recorded yet."
        cfg = Config(**{k: v for k, v in latest["cfg"].items() if k in {f.name for f in fields(Config)}})
        parts = [btc_report.format_status(latest["statuses"], cfg, None)]
        age = self.clock() - latest["asof"]
        stamp = f"As of the candle that closed {self.when(latest['asof'])}."
        if age > STALE_STATUS_S:
            stamp += f" That was {age / 3600:.0f} hours ago: the bot may not be running."
        parts.append(stamp)
        signals = self.signals()
        j = self.journal()
        waiting = []
        for s in open_signals(signals):
            if not j.is_taken(s["event_id"]):
                waiting.append(f"Waiting on you: the bot has been in a {self.word(s['side'])} since "
                               f"{self.when(s['bar_time'])} at {btc_report.money(s['price'])}. "
                               f"/taken btc if you took it.")
        for t in j.open_taken():
            rec = next((s for s in signals if s["event_id"] == t["event_id"]), None)
            if rec and exit_after(signals, rec):
                waiting.append(f"Waiting on you: the bot has signalled out of your {self.word(t['side'])}. "
                               f"Send /closed btc.")
            else:
                waiting.append(f"Your taken {self.word(t['side'])} from {btc_report.money(t['fill'])} is open.")
        if waiting:
            parts.append("\n".join(waiting))
        accts, problem = self.accounts()
        if accts:
            bank = btc_bank.Bank(accts, self.path("bank_ledger.jsonl"), write=False)
            bank.ensure_accounts()
            parts.append("\n".join(bank.summary_lines() + self._warnings(bank)))
        return "\n\n".join(parts)

    def _warnings(self, bank):
        latest = self.latest()
        st = latest["statuses"]["LONG"] if latest else {}
        return bank.warnings(st.get("last_close"), st.get("atr"))

    # -- /stats, /bank
    def c_stats(self, args):
        accts, _ = self.accounts()
        latest = self.latest()
        stamp = f"Recorded up to {self.when(latest['asof'])}" if latest else "No run recorded yet"
        return results_text(self.dir, accts, "BTC results", stamp)

    def c_bank(self, args):
        accts, problem = self.accounts()
        if problem:
            return f"Accounts: {problem}."
        latest = self.latest()
        stamp = f"Recorded up to {self.when(latest['asof'])}" if latest else "No run recorded yet"
        text = btc_report.bank_report(stamp, account_pairs(self.dir, accts))
        bank = btc_bank.Bank(accts, self.path("bank_ledger.jsonl"), write=False)
        bank.ensure_accounts()
        warn = self._warnings(bank)
        return text + ("\n\n" + "\n".join(warn) if warn else "")

    # -- /technical, /fundamental
    def _scan(self):
        scan = last_jsonl(self.path("scans.jsonl"))
        if not scan:
            return None, None
        stamp = f"As of the candle that closed {self.when(scan['asof'])}"
        if self.clock() - scan["asof"] > 3 * 3600:
            stamp += f" ({(self.clock() - scan['asof']) / 3600:.0f} hours ago)"
        return scan, stamp

    def c_technical(self, args):
        scan, stamp = self._scan()
        return "No BTC scan has been stored yet." if scan is None else btc_report.technical_report(scan, stamp)

    def c_fundamental(self, args):
        scan, stamp = self._scan()
        return "No BTC scan has been stored yet." if scan is None else btc_report.fundamental_report(scan, stamp)

    def c_help(self, args):
        return ("BTC commands (add the word btc when GBPUSD is the active bot, e.g. /taken btc)\n\n"
                "/taken [price]: you entered the open signal on your demo (add your real fill)\n"
                "/win [price]: your last taken trade won (closes it at the bot's exit or the latest price)\n"
                "/closed [price]: you closed your taken trade (win or loss)\n"
                "/undo: cancel the latest /taken or /closed\n"
                "/journal: your recent taken and closed trades\n"
                "/status: both sides, the demo accounts, and what is waiting on you\n"
                "/stats: profit factor, R, drawdown for the strategy, your trades and each account\n"
                "/bank: the demo accounts\n"
                "/technical and /fundamental: the latest stored scans")
