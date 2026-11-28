"""Tests for the statistics, journal, BTC commands, history download and the gap-free recording."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from unittest import mock

import btc_bank
import btc_commands
import btc_history
import btc_report
import btc_stats
import test_btc_bot as base
import turtle_btc
from btc_engine import Bar, Config, run_both

HERE = base.HERE
SAMPLE_ACCOUNTS = os.path.join(HERE, "accounts.json")


def T(pnl, risk, c):
    return {"pnl": pnl, "risk": risk, "open_t": c - 3600, "close_t": c}


def tmpdir(test):
    d = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, d, True)
    return d


def jsonl_lines(path):
    with open(path) as f:
        return [json.loads(x) for x in f if x.strip()]


# --------------------------------------------------------------------------- statistics
class Stats(unittest.TestCase):
    def test_headline_numbers_are_exactly_right(self):
        trades = [T(2, 1, 1000), T(-1, 1, 2000), T(3, 1, 3000), T(-1, 1, 4000), T(-1, 1, 5000), T(1, 1, 6000)]
        s = btc_stats.compute_stats(trades, 100)
        self.assertEqual((s["n"], s["wins"], s["losses"], s["flat"]), (6, 3, 3, 0))
        self.assertAlmostEqual(s["win_rate"], 0.5)
        self.assertAlmostEqual(s["gross_profit"], 6)
        self.assertAlmostEqual(s["gross_loss"], -3)
        self.assertAlmostEqual(s["profit_factor"], 2.0)
        self.assertAlmostEqual(s["total_r"], 3.0)
        self.assertAlmostEqual(s["avg_r"], 0.5)
        self.assertAlmostEqual(s["avg_win_r"], 2.0)
        self.assertAlmostEqual(s["avg_loss_r"], -1.0)
        self.assertEqual((s["best_r"], s["worst_r"]), (3, -1))
        self.assertAlmostEqual(s["payoff"], 2.0)
        self.assertAlmostEqual(s["max_dd_r"], 2.0)                    # R curve 2,1,4,3,2,3: peak 4 -> trough 2
        self.assertEqual((s["max_win_streak"], s["max_loss_streak"]), (1, 2))
        self.assertAlmostEqual(s["end_balance"], 103)
        self.assertAlmostEqual(s["return_pct"], 3.0)
        self.assertAlmostEqual(s["max_dd_usd"], 2.0)                  # balance 102,101,104,103,102,103
        self.assertAlmostEqual(s["max_dd_pct"], 2 / 104 * 100)
        self.assertAlmostEqual(s["avg_hold_h"], 1.0)

    def test_profit_factor_in_r_differs_from_money_when_risks_differ(self):
        s = btc_stats.compute_stats([T(4, 2, 1000), T(-1, 1, 2000)])
        self.assertAlmostEqual(s["profit_factor"], 4.0)               # money: 4 won / 1 lost
        self.assertAlmostEqual(s["profit_factor_r"], 2.0)             # R: +2R won / 1R lost
        self.assertAlmostEqual(btc_stats.compute_stats([T(2, 1, 1000), T(-1, 1, 2000), T(3, 1, 3000)] )
                               ["profit_factor_r"], 5.0)

    def test_undefined_numbers_are_none_never_zero(self):
        empty = btc_stats.compute_stats([])
        self.assertEqual(empty["n"], 0)
        self.assertIsNone(empty["profit_factor"])
        self.assertIsNone(empty["avg_r"])
        winners = btc_stats.compute_stats([T(1, 1, 1000), T(2, 1, 2000)])
        self.assertIsNone(winners["profit_factor"])                   # no losing trade: not infinity, not 0
        self.assertIsNone(winners["avg_loss_r"])
        self.assertIsNone(winners["payoff"])
        self.assertIsNone(winners["profit_factor_r"])
        self.assertIn("no losing trade yet", btc_report.stats_block("t", winners))
        nr = btc_stats.compute_stats([T(1, None, 1000), T(-1, 1, 2000)])
        self.assertEqual(nr["r_known"], 1)
        self.assertAlmostEqual(nr["total_r"], -1.0)                   # only the trade with a risk counts
        self.assertAlmostEqual(nr["profit_factor"], 1.0)

    def test_small_samples_are_flagged(self):
        txt = btc_report.stats_block("t", btc_stats.compute_stats([T(1, 1, 1000), T(-1, 1, 2000)]))
        self.assertIn("far too few", txt)

    def test_segments_pair_entries_with_exits(self):
        ev = [
            {"kind": "ENTRY", "side": "LONG", "bar_time": 1000, "price": 100.0, "active_stop": 95.0, "event_id": "a"},
            {"kind": "CLOSE", "side": "LONG", "bar_time": 2000, "price": 110.0, "flat": False, "event_id": "b"},
            {"kind": "EXIT", "side": "LONG", "bar_time": 2500, "price": 90.0, "flat": True, "event_id": "c"},
            {"kind": "ENTRY", "side": "SHORT", "bar_time": 3000, "price": 100.0, "active_stop": 105.0, "event_id": "d"},
            {"kind": "EXIT", "side": "SHORT", "bar_time": 4000, "price": 107.0, "flat": False, "event_id": "e"},
        ]
        segs = btc_stats.segments_from_events(ev)
        self.assertEqual(len(segs), 2)                                # the flat EXIT held nothing, so it is no trade
        self.assertAlmostEqual(btc_stats.trade_r(segs[0]), 2.0)
        self.assertAlmostEqual(btc_stats.trade_r(segs[1]), -1.4)

    def test_segments_match_the_engines_own_trades_and_account_r_matches_strategy_r(self):
        for seed in range(4):
            bars = base.random_walk(seed)
            cfg = Config()
            lg, sh, evs, _ = run_both(bars, cfg, keep_last=0)
            segs = btc_stats.segments_from_events(evs)
            engine_trades = lg.trades + sh.trades
            self.assertEqual(len(segs), len(engine_trades))
            self.assertEqual(sorted((round(t["entry"], 6), round(t["exit"], 6)) for t in segs),
                             sorted((round(t["entry_price"], 6), round(t["exit_price"], 6)) for t in engine_trades))
            # an account that takes every trade must show exactly the strategy's R, whatever its size
            rich = btc_bank.validate_accounts({"accounts": [{"name": "Rich", "starting_balance": 1e9,
                                                             "risk_pct": 0.001, "max_lot": 1e6, "min_lot": 0.00001,
                                                             "lot_step": 0.00001, "max_margin_pct": 100,
                                                             "leverage": 1000}]})
            bank = btc_bank.Bank(rich, None)
            bank.ensure_accounts()
            for e in evs:
                bank.commit(bank.plan(e)[1])
            acct_r = sorted(round(btc_stats.trade_r(t), 6) for t in bank.closed_trades())
            seg_r = sorted(round(btc_stats.trade_r(t), 6) for t in segs)
            self.assertEqual(acct_r, seg_r)

    def test_csv_export(self):
        d = tmpdir(self)
        path = os.path.join(d, "t.csv")
        btc_stats.write_closed_csv(path, [{**T(2, 1, 1000), "side": "LONG", "entry": 100, "exit": 102}])
        with open(path) as f:
            rows = f.read().splitlines()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].startswith("account,side,open_utc"))
        self.assertIn(",2.0,", rows[1])


# --------------------------------------------------------------------------- journal and commands
class Commands(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir(self)
        self.clock = [2_000_000_000]
        self.c = btc_commands.BtcCommands(self.d, SAMPLE_ACCOUNTS, clock=lambda: self.clock[0])

    def write_latest(self, price):
        with open(os.path.join(self.d, "latest.json"), "w") as f:
            json.dump({"asof": self.clock[0], "cfg": asdict(Config()),
                       "statuses": {"LONG": {"last_close": price}, "SHORT": {"last_close": price}}}, f)

    def sig(self, kind, side, t, price, stop=None, flat=False):
        rec = {"event_id": f"{side}:{kind}:{t}", "side": side, "kind": kind, "bar_time": t, "close_time": t + 1800,
               "price": price, "active_stop": stop, "flat": flat}
        with open(os.path.join(self.d, "trades.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")

    def test_full_long_flow_with_refusals_and_undo(self):
        self.assertIn("No BTC signal has been recorded", self.c.handle("/taken"))
        self.sig("ENTRY", "LONG", 1000, 100000, 95000)
        self.assertIn("typo", self.c.handle("/taken 9825"))
        self.assertIn("Send only one price", self.c.handle("/taken 100000 100100"))
        self.assertIn("didn't understand", self.c.handle("/taken abc"))
        r = self.c.handle("/taken 100,250")
        self.assertIn("Recorded", r)
        self.assertIn("100,250", r)
        self.assertIn("1R is 5,250", r)
        self.assertIn("already recorded as taken", self.c.handle("/taken"))
        self.assertIn("has not signalled an exit", self.c.handle("/closed"))
        self.write_latest(101000)                                      # where the market is now
        self.assertIn("typo", self.c.handle("/closed 150000"))
        r = self.c.handle("/closed 105000")
        self.assertIn("+0.90R", r)
        self.assertIn("1 closed, total +0.9R", r)
        self.assertIn("You have no taken BTC trade open", self.c.handle("/closed 105000"))
        j = self.c.handle("/journal")
        self.assertIn("took long at 100,250", j)
        self.assertIn("closed long at 105,000, +0.90R", j)
        self.assertIn("Undone: your /closed", self.c.handle("/undo"))
        self.assertIn("+0.71R", self.c.handle("/closed 104000"))      # open again, closed at a different price
        self.assertIn("Undone: your /closed", self.c.handle("/undo"))
        self.assertIn("Undone: your /taken long", self.c.handle("/undo"))
        self.assertEqual(self.c.handle("/undo"), "Nothing to undo.")
        self.assertIn("Recorded", self.c.handle("/taken"))             # can be taken again after an undo

    def test_win_closes_the_last_taken_trade_as_a_winner_and_refuses_a_loss(self):
        self.assertIn("no taken BTC trade open", self.c.handle("/win"))
        self.sig("ENTRY", "LONG", 1000, 100000, 95000)
        self.c.handle("/taken 100000")
        self.assertIn("No market price is recorded", self.c.handle("/win"))
        self.write_latest(98000)                                       # market below the fill: not a win
        r = self.c.handle("/win")
        self.assertIn("would not be a win", r)
        self.assertEqual(len(self.c.journal().open_taken()), 1)        # nothing was recorded
        self.assertIn("would not be a win", self.c.handle("/win 99000"))
        self.write_latest(105000)
        r = self.c.handle("/win")                                      # no exit yet: latest price, and it says so
        self.assertIn("+1.00R", r)
        self.assertIn("latest recorded price", r)
        self.assertIn("Undone: your /closed", self.c.handle("/undo"))
        self.assertIn("+0.60R", self.c.handle("/win 103000"))          # your own fill
        # a bot exit that was a loss cannot be filed as a win
        self.sig("ENTRY", "LONG", 5000, 100000, 95000)
        self.c.handle("/taken 100000")
        self.sig("EXIT", "LONG", 6000, 95000, 95000)
        self.assertIn("would not be a win", self.c.handle("/win"))
        self.assertIn("-1.00R", self.c.handle("/closed"))

    def test_win_works_for_a_short(self):
        self.sig("ENTRY", "SHORT", 1000, 100000, 105000)
        self.c.handle("/taken")
        self.assertIn("would not be a win", self.c.handle("/win 101000"))
        self.assertIn("+0.60R", self.c.handle("/win 97000"))

    def test_closed_uses_the_bots_exit_price_and_says_so(self):
        self.sig("ENTRY", "LONG", 1000, 100000, 95000)
        self.c.handle("/taken 100000")
        self.sig("CLOSE", "LONG", 5000, 103000, 95000)
        r = self.c.handle("/closed")
        self.assertIn("bot's exit price", r)
        self.assertIn("+0.60R", r)

    def test_short_r_sign_and_stale_signal_is_not_takeable(self):
        self.sig("ENTRY", "SHORT", 1000, 100000, 105000)
        self.c.handle("/taken")
        self.assertIn("+0.60R", self.c.handle("/closed 97000"))
        self.sig("EXIT", "SHORT", 1500, 97500, 105000)                 # the bot has now left the short too
        self.sig("ENTRY", "LONG", 9000, 100000, 95000)
        self.sig("EXIT", "LONG", 9500, 94000, 95000)
        r = self.c.handle("/taken")
        self.assertIn("No BTC signal is open", r)
        self.assertIn("already left it at 94,000", r)

    def test_fill_beyond_the_exit_is_refused_unless_forced(self):
        self.sig("ENTRY", "LONG", 1000, 100000, 99500)
        self.assertIn("already beyond the exit", self.c.handle("/taken 99000"))
        self.assertIn("Recorded", self.c.handle("/taken 99000 force"))

    def test_the_commands_only_write_the_journal(self):
        self.sig("ENTRY", "LONG", 1000, 100000, 95000)
        before = sorted(os.listdir(self.d))
        self.c.handle("/stats")
        self.c.handle("/bank")
        self.c.handle("/status")
        self.assertEqual(sorted(os.listdir(self.d)), before)           # reading never creates or changes files
        self.c.handle("/taken")
        self.assertEqual(sorted(set(os.listdir(self.d)) - set(before)), ["journal.jsonl"])

    def test_unknown_and_non_commands_return_none(self):
        self.assertIsNone(self.c.handle("/shadow"))
        self.assertIsNone(self.c.handle("hello"))
        self.assertIsNone(self.c.handle(""))
        self.assertIsNotNone(self.c.handle("/taken@MyBot"))            # Telegram appends the bot's name in groups

    def test_taken_stats_appear_in_the_results(self):
        self.sig("ENTRY", "LONG", 1000, 100000, 95000)
        self.c.handle("/taken 100000")
        self.c.handle("/closed 110000")
        out = self.c.handle("/stats")
        self.assertIn("Your taken trades: your own fills", out)
        self.assertIn("Total +2.0R", out)

    def test_commands_work_on_the_files_a_real_run_produced(self):
        d, bars, start, full, sent, env = base.simulate_cached()
        data = tmpdir(self)
        shutil.rmtree(data)
        shutil.copytree(env["DATA_DIR"], data)                         # never write into the shared simulation
        c = btc_commands.BtcCommands(data, SAMPLE_ACCOUNTS, clock=lambda: bars[-1].t + 1800 + 60)
        outs = {n: c.handle(f"/{n}") for n in c.command_names()}
        for name, text in outs.items():
            self.assertIsInstance(text, str, name)
            self.assertLess(len(text), 4000, name)
            self.assertNotIn("None", text, name)
            self.assertNotIn("nan", text.lower().replace("finance", ""), name)
        self.assertIn("Long:", outs["status"])
        self.assertIn("Demo accounts", outs["status"])
        self.assertIn("Strategy: every signal", outs["stats"])
        self.assertIn("R = one unit of risk", outs["stats"])
        for name in ("Acct-5k", "Acct-2k", "Acct-10k", "Acct-25k-Long", "Acct-50usd"):
            self.assertIn(name, outs["bank"])
        self.assertIn("minimum lot was too big", outs["bank"])          # the $5 account explains why it never trades
        self.assertIn("BTC technical read", outs["technical"])
        self.assertIn("BTC fundamental read", outs["fundamental"])
        self.assertIn("/taken", outs["help"])


# --------------------------------------------------------------------------- history
def make_history(days, now):
    n = int(days * 48)
    return [Bar(now - (n - i) * 1800, 100 + i * 0.01, 101 + i * 0.01, 99 + i * 0.01, 100 + i * 0.01)
            for i in range(n)]


def provider(bars, log=None):
    """A fake Twelve Data: newest `outputsize` candles at or before end_date, in descending order like the real one."""
    def fetch(end, n):
        if log is not None:
            log.append(end)
        end_t = None if end is None else int(datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
                                             .replace(tzinfo=timezone.utc).timestamp())
        rows = [b for b in bars if end_t is None or b.t <= end_t]
        return list(reversed(rows[-n:]))
    return fetch


class History(unittest.TestCase):
    NOW = 1_800_000_000 - 1_800_000_000 % 1800

    def test_pages_backwards_without_duplicates_and_stops_when_covered(self):
        bars = make_history(300, self.NOW)
        ends, sleeps = [], []
        got, rep = btc_history.download(provider(bars, ends), 200, now=self.NOW, sleep=sleeps.append)
        ts = [b.t for b in got]
        self.assertEqual(ts, sorted(set(ts)))
        self.assertGreaterEqual(rep["days_covered"], 199.5)
        self.assertGreaterEqual(got[0].t, self.NOW - 200 * 86400)      # trimmed to what was asked for
        self.assertEqual(rep["pages"], 2)                              # 5000 candles is ~104 days, so 2 pages
        self.assertEqual(sleeps, [8])                                  # polite pause between pages only
        self.assertIsNone(ends[0])
        first_page_earliest = bars[-5000].t
        self.assertEqual(ends[1], datetime.fromtimestamp(first_page_earliest - 1, timezone.utc)
                         .strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(len(got), len({b.t for b in got}))

    def test_short_provider_history_and_empty_provider_are_reported(self):
        got, rep = btc_history.download(provider(make_history(60, self.NOW)), 200, now=self.NOW, sleep=lambda s: None)
        self.assertEqual(rep["pages"], 1)
        self.assertIn("no candles older", rep["why"])
        self.assertAlmostEqual(rep["days_covered"], 60, delta=0.1)
        got, rep = btc_history.download(lambda e, n: [], 200, now=self.NOW)
        self.assertEqual(got, [])

    def test_csv_round_trip_merge_and_atomic_write(self):
        d = tmpdir(self)
        path = os.path.join(d, "sub", "h.csv")
        a = make_history(3, self.NOW)
        btc_history.save_csv(path, a)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["h.csv"])  # no half-written .tmp left behind
        back = btc_history.load_csv(path)
        self.assertEqual([(b.t, b.o, b.c) for b in back], [(b.t, b.o, b.c) for b in a])
        newer = [Bar(a[-1].t, 1, 2, 0.5, 1.5), Bar(a[-1].t + 1800, 1, 2, 0.5, 1.5)]
        merged = btc_history.merge(a, newer)
        self.assertEqual(len(merged), len(a) + 1)
        self.assertEqual(merged[len(a) - 1].c, 1.5)                     # later list wins for the same candle


# --------------------------------------------------------------------------- runner: CLI modes, folders, gaps
class Anchor(unittest.TestCase):
    def test_live_signals_do_not_change_when_the_fetch_window_slides(self):
        d = tmpdir(self)
        path = os.path.join(d, "h.csv")
        import math
        import random
        r, px, bars = random.Random(7), 50000.0, []
        for i in range(14000):
            c = px * math.exp(r.gauss(0.0002, 0.004))
            bars.append(Bar(1736121600 + i * 1800, px, max(px, c) * 1.001, min(px, c) * 0.999, c))
            px = c
        seen, cfg = {}, Config()
        for end in range(5000, 14000, 211):
            fetched = bars[end - 5000:end]                   # what a fresh 5000-candle fetch would return
            got = turtle_btc.anchored_bars(path, fetched, True)
            for e in run_both(got, cfg)[2]:
                self.assertAlmostEqual(seen.setdefault(e.event_id, e.price), e.price, places=9)
        self.assertGreater(len(seen), 4)
        last = end
        self.assertEqual([(e.event_id, e.price) for e in run_both(got, cfg)[2]],
                         [(e.event_id, e.price) for e in run_both(bars[:last], cfg)[2]])

    def test_dry_run_and_status_never_write_and_a_broken_csv_is_survivable(self):
        d = tmpdir(self)
        path = os.path.join(d, "h.csv")
        fetched = make_history(3, 1_800_000_000)
        turtle_btc.anchored_bars(path, fetched, False)
        self.assertFalse(os.path.exists(path))
        with open(path, "w") as f:
            f.write("garbage")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(turtle_btc.anchored_bars(path, fetched, True), fetched)


class Runner(unittest.TestCase):
    def run_cli(self, argv, env):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = turtle_btc.main(argv)
            except SystemExit as e:
                rc = e.code
        return rc, buf.getvalue()

    def env(self, d, **kw):
        return {"DATA_DIR": os.path.join(d, "data"), "ACCOUNTS_FILE": SAMPLE_ACCOUNTS,
                "TWELVE_DATA_API_KEY": "x", **kw}

    def test_backtest_prints_full_stats_writes_csvs_and_never_touches_the_live_ledger(self):
        d = tmpdir(self)
        csv_path = os.path.join(d, "h.csv")
        btc_history.save_csv(csv_path, base.random_walk(3, days=150))
        rc, out = self.run_cli(["--backtest", "--csv", csv_path], self.env(d))
        self.assertEqual(rc, 0)
        for needle in ("BTC replay of historical candles", "Strategy: every signal", "profit factor", "Total ",
                       "Acct-5k", "Acct-50usd", "R = one unit of risk"):
            self.assertIn(needle, out)
        data = os.path.join(d, "data")
        self.assertEqual(sorted(os.listdir(data)), ["backtest"])        # nothing but the replay files
        self.assertEqual(sorted(os.listdir(os.path.join(data, "backtest"))), ["account_trades.csv",
                                                                              "strategy_trades.csv"])
        with open(os.path.join(data, "backtest", "strategy_trades.csv")) as f:
            self.assertGreater(len(f.read().splitlines()), 5)

    def test_backtest_without_history_says_what_to_do(self):
        d = tmpdir(self)
        rc, _ = self.run_cli(["--backtest"], self.env(d))
        self.assertIn("--download-history", str(rc))

    def test_download_history_cli_saves_and_merges(self):
        d = tmpdir(self)
        bars = make_history(400, int(datetime.now(timezone.utc).timestamp()) // 1800 * 1800)

        def fake_fetch(key, sym, iv, outputsize=5000, now=None, end_date=None):
            return list(reversed(provider(bars)(end_date, outputsize)))

        with mock.patch.object(turtle_btc, "fetch_bars", fake_fetch), \
                mock.patch.object(btc_history.time, "sleep", lambda s: None):
            rc, out = self.run_cli(["--download-history", "150"], self.env(d))
            self.assertEqual(rc, 0)
            self.assertIn("Saved", out)
            path = os.path.join(d, "data", "history_30m.csv")
            first = btc_history.load_csv(path)
            self.assertGreaterEqual((first[-1].t - first[0].t) / 86400, 149)
            rc, out = self.run_cli(["--download-history", "150"], self.env(d))
            again = btc_history.load_csv(path)
            self.assertEqual(len(again), len({b.t for b in again}))     # merging never duplicates candles

    def test_paths_do_not_depend_on_the_working_directory(self):
        here = os.getcwd()
        other = tmpdir(self)
        env = {k: v for k, v in os.environ.items() if k not in ("DATA_DIR", "ACCOUNTS_FILE")}
        try:
            os.chdir(other)
            with mock.patch.dict(os.environ, env, clear=True):
                p = turtle_btc.paths()
        finally:
            os.chdir(here)
        self.assertEqual(p["accounts"], os.path.join(HERE, "accounts.json"))
        self.assertTrue(os.path.exists(p["accounts"]))
        self.assertEqual(p["data"], os.path.join(HERE, "data"))

    def test_a_pause_is_recorded_without_alerts_and_leaves_no_stuck_positions(self):
        start = 48 * 25
        gap = (start + 504, start + 840)                               # off from day 10.5 to day 17.5, when signals fire
        d, bars, start, full, sent, env = base.simulate(gap=gap)
        expected = [e for e in full if e.close_time > bars[start - 1].t + 1800]
        trades = jsonl_lines(env["TRADES_FILE"])
        self.assertEqual([t["event_id"] for t in trades], [e.event_id for e in expected])   # no gaps, no repeats
        stale = [t for t in trades if not t["alerted"]]
        self.assertGreater(len(stale), 5, "the pause should have produced signals to catch up on")
        self.assertTrue(all(t["bar_time"] + 1800 < bars[gap[1] - 1].t for t in stale))
        catch = [s for s in sent if "while the bot was not running" in s]
        self.assertEqual(len(catch), 1)
        alerts = [s for s in sent if s.startswith("BTC Turtle: ") and "while the bot" not in s]
        self.assertEqual(len(alerts), len(trades) - len(stale))
        # the ledger must equal a straight replay of every signal, so no position can be left open by the gap
        accts, _ = btc_bank.load_accounts(SAMPLE_ACCOUNTS)
        mem = btc_bank.Bank(accts, None)
        mem.ensure_accounts()
        for e in expected:
            mem.commit(mem.plan(e)[1])
        disk = btc_bank.Bank(accts, env["LEDGER_FILE"])
        self.assertEqual({k: round(v, 6) for k, v in disk.balances.items()},
                         {k: round(v, 6) for k, v in mem.balances.items()})
        self.assertEqual(set(disk.open), set(mem.open))
        ids = [e["id"] for e in jsonl_lines(env["LEDGER_FILE"])]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
