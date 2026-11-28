import atexit
import contextlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import btc_bank
import btc_report
import btc_scan
import turtle_btc
from btc_engine import NY, Bar, Config, deadline_for, run_both
from turtle_btc import only_closed

STEP = 1800
HERE = os.path.dirname(os.path.abspath(__file__))


def ny_epoch(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=NY).timestamp())


def flat_bars(start, end, o=100, h=101, l=99, c=100):
    out, t = [], start
    while t < end:
        out.append(Bar(t, o, h, l, c))
        t += STEP
    return out


def scenario(extra):
    """Flat 100 (H101/L99) from Sun 4 Jan 2026 17:00 NY to Wed 11 Feb 10:00 NY, then `extra` bars."""
    return flat_bars(ny_epoch(2026, 1, 4, 17), ny_epoch(2026, 2, 11, 10)) + extra


def seq(start_t, count, o, h, l, c):
    return [Bar(start_t + i * STEP, o, h, l, c) for i in range(count)]


def run(bars, side="LONG", **kw):
    cfg = Config(**kw)
    lg, sh, evs, snaps = run_both(bars, cfg)
    return cfg, (lg if side == "LONG" else sh), [e for e in evs if e.side == side]


def kinds(evs):
    return [(e.kind, datetime.fromtimestamp(e.t, NY).strftime("%a %H:%M"), round(e.price, 2)) for e in evs]


def random_walk(seed, days=140):
    rnd = random.Random(seed)
    t = ny_epoch(2026, 1, 4, 17)
    price, out, drift = 60000.0, [], 0.0
    for i in range(days * 48):
        if i % (48 * 12) == 0:
            drift = rnd.uniform(-0.0006, 0.0006)
        o = price
        c = o * (1 + drift + rnd.gauss(0, 0.004))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.002)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, 0.002)))
        out.append(Bar(t + i * STEP, o, h, l, c))
        price = c
    return out


# --------------------------------------------------------------------------- engine: long
def _lines(path):
    with open(path) as f:
        return f.read().splitlines()


class LongSide(unittest.TestCase):
    def test_day_trading_cycle(self):
        t = ny_epoch(2026, 2, 11, 10)
        extra = [Bar(t, 100, 105, 100, 104)]                                   # Wed 10:00 breakout
        extra += seq(t + STEP, (ny_epoch(2026, 2, 13, 11) - t - STEP) // STEP, 104, 105, 103, 104)
        extra += [Bar(ny_epoch(2026, 2, 13, 11), 104, 104, 90, 92)]            # Fri 11:00 crash through the exit
        extra += seq(ny_epoch(2026, 2, 13, 11, 30), 12, 92, 93, 91, 92)
        cfg, eng, evs = run(scenario(extra))
        self.assertEqual(kinds(evs), [
            ("ENTRY", "Wed 10:00", 101.0),     # max(band 101, open 100); stop 97 = 101 - 2*2
            ("CLOSE", "Wed 16:00", 104.0),     # scheduled close, at the candle close
            ("REENTRY", "Wed 18:00", 104.0),   # open 104 >= max(stop 97, exit band 99)
            ("CLOSE", "Thu 16:00", 104.0),
            ("REENTRY", "Thu 18:00", 104.0),
            ("EXIT", "Fri 11:00", 99.0),       # min(open 104, max(stop 97, band 99))
        ])
        self.assertAlmostEqual(evs[0].atr, 2.0)
        self.assertAlmostEqual(evs[0].stop, 97.0)
        self.assertAlmostEqual(evs[0].entry_level, 101.0)
        self.assertAlmostEqual(evs[0].exit_level, 99.0)
        self.assertAlmostEqual(evs[0].active_stop, 99.0)
        self.assertFalse(eng.signal_long)
        self.assertEqual(len(eng.trades), 3)
        self.assertEqual(eng.trades[-1]["kind"], "EXIT")

    def test_breakout_during_the_break_enters_at_reopen(self):
        t = ny_epoch(2026, 2, 11, 17, 30)
        extra = flat_bars(ny_epoch(2026, 2, 11, 10), t) + [Bar(t, 100, 105, 100, 104)] + seq(t + STEP, 6, 104, 105, 103, 104)
        cfg, eng, evs = run(scenario(extra))
        self.assertEqual([e.kind for e in evs], ["REENTRY"])
        self.assertEqual(datetime.fromtimestamp(evs[0].t, NY).strftime("%a %H:%M"), "Wed 18:00")

    def test_swing_mode_has_no_scheduled_close(self):
        t = ny_epoch(2026, 2, 11, 10)
        cfg, eng, evs = run(scenario([Bar(t, 100, 105, 100, 104)] + seq(t + STEP, 200, 104, 105, 103, 104)),
                            day_trading=False)
        self.assertEqual([e.kind for e in evs], ["ENTRY"])
        self.assertTrue(eng.held)

    def test_stop_hit_same_candle_as_breakout(self):
        t = ny_epoch(2026, 2, 11, 10)
        cfg, eng, evs = run(scenario([Bar(t, 100, 105, 90, 92)] + seq(t + STEP, 4, 92, 93, 91, 92)))
        self.assertEqual([e.kind for e in evs], ["ENTRY", "EXIT"])
        self.assertAlmostEqual(evs[1].price, 97.0)

    def test_no_signals_during_warmup(self):
        t = ny_epoch(2026, 1, 4, 17)
        bars = flat_bars(t, t + 10 * 86400) + [Bar(t + 10 * 86400, 100, 500, 100, 400)]
        lg, sh, evs, _ = run_both(bars, Config())
        self.assertEqual(evs, [])
        self.assertFalse(lg.status()["ready"])


# --------------------------------------------------------------------------- engine: short
class ShortSide(unittest.TestCase):
    def test_breakdown_cycle_mirrors_the_long_rules(self):
        t = ny_epoch(2026, 2, 11, 10)
        extra = [Bar(t, 100, 100, 95, 96)]                                      # Wed 10:00 breaks the 20-day low (99)
        extra += seq(t + STEP, (ny_epoch(2026, 2, 13, 11) - t - STEP) // STEP, 96, 97, 95, 96)
        extra += [Bar(ny_epoch(2026, 2, 13, 11), 96, 110, 96, 108)]             # Fri 11:00 spike through the exit
        extra += seq(ny_epoch(2026, 2, 13, 11, 30), 12, 108, 109, 107, 108)
        cfg, eng, evs = run(scenario(extra), side="SHORT")
        self.assertEqual([e.kind for e in evs][:3], ["ENTRY", "CLOSE", "REENTRY"])
        entry = evs[0]
        self.assertEqual(entry.side, "SHORT")
        self.assertAlmostEqual(entry.price, 99.0)            # min(band 99, open 100)
        self.assertAlmostEqual(entry.stop, 103.0)            # 99 + 2 * ATR(2)
        self.assertAlmostEqual(entry.exit_level, 101.0)      # highest high of the last 10 days
        self.assertAlmostEqual(entry.active_stop, 101.0)     # the LOWER (tighter) of stop and band
        self.assertEqual(evs[-1].kind, "EXIT")
        self.assertAlmostEqual(evs[-1].price, 101.0)         # max(open, min(stop, band)) = 101
        self.assertEqual(eng.trades[-1]["side"], "SHORT")
        last = eng.trades[-1]                                  # re-entered at 96, stopped at 101: a LOSING short
        self.assertAlmostEqual(last["exit_price"] - last["entry_price"], 5.0)

    def test_status_reports_real_prices_not_mirrored_ones(self):
        t = ny_epoch(2026, 2, 11, 10)
        lg, sh, evs, _ = run_both(scenario([Bar(t, 100, 100, 95, 96)] + seq(t + STEP, 4, 96, 97, 95, 96)), Config())
        st = sh.status()
        self.assertTrue(st["ready"])
        self.assertGreater(st["last_close"], 0)
        self.assertGreater(st["exit_level"], st["entry_level"])   # 10-day high above the 20-day low
        self.assertGreater(st["stop"], 96)                      # a short's stop is ABOVE the market

    def test_both_sides_never_trigger_together_in_a_quiet_market(self):
        bars = scenario(seq(ny_epoch(2026, 2, 11, 10), 100, 100, 101, 99, 100))
        lg, sh, evs, _ = run_both(bars, Config())
        self.assertEqual(evs, [])


# --------------------------------------------------------------------------- helpers
class Helpers(unittest.TestCase):
    def test_deadline_skips_weekend(self):
        cfg = Config()
        self.assertEqual(deadline_for(ny_epoch(2026, 2, 13, 12), cfg), ny_epoch(2026, 2, 13, 16, 30))
        self.assertEqual(deadline_for(ny_epoch(2026, 2, 13, 18), cfg), ny_epoch(2026, 2, 16, 16, 30))
        self.assertEqual(deadline_for(ny_epoch(2026, 2, 15, 18), cfg), ny_epoch(2026, 2, 16, 16, 30))

    def test_dst_keeps_local_clock(self):
        cfg = Config()
        self.assertEqual(deadline_for(ny_epoch(2026, 3, 6, 10), cfg), ny_epoch(2026, 3, 6, 16, 30))
        self.assertEqual(deadline_for(ny_epoch(2026, 3, 9, 10), cfg), ny_epoch(2026, 3, 9, 16, 30))

    def test_only_closed_drops_forming_candle(self):
        bars = [Bar(0, 1, 1, 1, 1), Bar(1800, 1, 1, 1, 1), Bar(3600, 1, 1, 1, 1)]
        self.assertEqual(len(only_closed(bars, 30, now=3600 + 100)), 2)
        self.assertEqual(len(only_closed(bars, 30, now=3600 + 1800)), 3)

    def test_long_messages_split_under_the_telegram_limit(self):
        sent = []

        class R:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            sent.append(req.data.decode())
            return R()

        text = "\n\n".join("para %d " % i + "x" * 900 for i in range(12))
        with mock.patch.object(turtle_btc.urllib.request, "urlopen", fake_urlopen):
            self.assertTrue(turtle_btc.send_telegram("t", "c", text))
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(s) < 4900 for s in sent))


# --------------------------------------------------------------------------- invariants on random walks
class RandomWalkInvariants(unittest.TestCase):
    def test_never_holds_through_the_break_or_weekend(self):
        total = 0
        for seed in range(5):
            cfg = Config()
            lg, sh, evs, _ = run_both(random_walk(seed), cfg)
            cm, om = cfg.close_h * 60 + cfg.close_min, cfg.open_h * 60 + cfg.open_min
            for tr in lg.trades + sh.trades:
                total += 1
                s = tr["entry_t"]
                while s < tr["exit_t"]:
                    loc = datetime.fromtimestamp(s, NY)
                    mins, dow = loc.hour * 60 + loc.minute, loc.weekday()
                    ok = ((mins < cm or mins >= om) and dow != 5 and not (dow == 6 and mins < om)
                          and not (dow == 4 and mins >= cm))
                    self.assertTrue(ok, f"seed {seed}: held through {loc}")
                    s += STEP
        self.assertGreater(total, 0)

    def test_each_side_alternates_entry_and_leave(self):
        shorts = 0
        for seed in range(5):
            _, _, evs, _ = run_both(random_walk(seed), Config())
            for side in ("LONG", "SHORT"):
                held = False
                for e in (x for x in evs if x.side == side):
                    if e.kind in ("ENTRY", "REENTRY"):
                        self.assertFalse(held, f"{side} seed {seed}: entry while already held")
                        held = True
                        shorts += side == "SHORT"
                    elif e.kind == "CLOSE":
                        self.assertTrue(held, f"{side} seed {seed}: close while flat")
                        held = False
                    elif e.kind == "EXIT" and not e.flat:
                        self.assertTrue(held, f"{side} seed {seed}: exit while flat")
                        held = False
        self.assertGreater(shorts, 0, "no short ever opened in the random walks; short side untested")

    def test_stops_sit_on_the_right_side_of_entry_and_exit_prices_respect_them(self):
        for seed in range(5):
            _, _, evs, _ = run_both(random_walk(seed), Config())
            for e in evs:
                if e.kind == "ENTRY":
                    self.assertTrue(e.stop < e.price if e.side == "LONG" else e.stop > e.price)
                if e.kind == "EXIT" and not e.flat:
                    if e.side == "LONG":
                        self.assertLessEqual(e.price, e.active_stop + 1e-6)
                    else:
                        self.assertGreaterEqual(e.price, e.active_stop - 1e-6)

    def test_same_candle_event_ids_are_unique(self):
        for seed in range(5):
            _, _, evs, _ = run_both(random_walk(seed), Config())
            ids = [e.event_id for e in evs]
            self.assertEqual(len(ids), len(set(ids)))


# --------------------------------------------------------------------------- scan
class Scan(unittest.TestCase):
    def test_resample_returns_only_complete_hours(self):
        bars = [Bar(i * STEP, 1, 2 + i, 0, 1) for i in range(5)]      # two full hours + half an hour
        h = btc_scan.resample(bars, 3600)
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0].h, 3)

    def test_structure_break_that_holds_gives_buyers_control(self):
        # a swing high at 110 followed by a close at 112, then price stays above 110
        highs = [100, 101, 102, 110, 103, 102, 101, 104, 112, 113, 114, 115]
        bars = [Bar(i * 3600, h - 1, h, h - 2, h - 0.5) for i, h in enumerate(highs)]
        st = btc_scan.structure_read(bars)
        self.assertEqual(st["last_break"]["dir"], "up")
        self.assertTrue(st["last_break"]["held"])
        self.assertEqual(btc_scan._control(st["last_break"]), "buyers")

    def test_break_that_fails_is_contested(self):
        highs = [100, 101, 102, 110, 103, 102, 101, 104, 112, 108, 107, 106, 105]
        bars = [Bar(i * 3600, h - 1, h, h - 2, h - 0.5) for i, h in enumerate(highs)]
        st = btc_scan.structure_read(bars)
        self.assertEqual(btc_scan._control(st["last_break"]), "contested")

    def test_few_candles_are_unavailable_not_guessed(self):
        bars = flat_bars(ny_epoch(2026, 1, 4, 17), ny_epoch(2026, 1, 5, 17))
        scan = btc_scan.build_scan(bars, None, None, bars[-1].t + STEP)
        self.assertEqual(scan["fundamental"]["status"], "unavailable")
        self.assertEqual(scan["technical"]["frames"]["15M"]["status"], "unavailable")
        txt = btc_report.fundamental_report(scan, "x")
        self.assertIn("Not checked", txt)

    def test_full_scan_is_json_and_reports_are_clean(self):
        bars = random_walk(2, days=100)
        lg, sh, _, snaps = run_both(bars, Config())
        asof = bars[-1].t + STEP
        scan = btc_scan.build_scan(bars, None, None, asof, strategy=snaps[asof])
        json.dumps(scan)
        self.assertEqual(scan["fundamental"]["status"], "ok")
        self.assertEqual(scan["fundamental"]["sentiment"]["status"], "unavailable")
        tech = btc_report.technical_report(scan, "stamp")
        fund = btc_report.fundamental_report(scan, "stamp")
        for txt in (tech, fund):
            self.assertNotIn("pip", txt.lower())
            self.assertNotIn("None", txt)
            self.assertNotIn("nan", txt.lower())
            self.assertLess(len(txt), 3800)
        self.assertIn("not checked", tech.lower())        # the 5M / 15M frames were not supplied
        ctx = "\n".join(btc_report.context_lines(scan, "LONG"))
        self.assertIn("information only", ctx)

    def test_scan_never_looks_past_its_asof(self):
        bars = random_walk(2, days=100)
        cut = len(bars) - 200
        a = btc_scan.build_scan(bars[:cut], None, None, bars[cut - 1].t + STEP)
        b = btc_scan.build_scan(bars, None, None, bars[cut - 1].t + STEP)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


# --------------------------------------------------------------------------- bank
ACCT = {"name": "T", "starting_balance": 10000}


def acct(**kw):
    return btc_bank.validate_accounts({"accounts": [{**ACCT, **kw}]})[0]


class Bank(unittest.TestCase):
    def test_validation_rejects_wrong_inputs(self):
        v = btc_bank.validate_accounts
        with self.assertRaises(btc_bank.AccountsError):
            v({"accounts": [{"name": str(i), "starting_balance": 1000} for i in range(6)]})
        self.assertEqual(len(v({"accounts": [{"name": str(i), "starting_balance": 1000} for i in range(5)]})), 5)
        for bad in ({"risk_precent": 1}, {"risk_pct": 50}, {"risk_pct": 0}, {"sides": "up"}, {"leverage": 0},
                    {"starting_balance": -5}, {"max_lot": 0.001}, {"enabled": "yes"}, {"min_lot_policy": "x"},
                    {"name": "a:b"}):
            with self.assertRaises(btc_bank.AccountsError, msg=str(bad)):
                v({"accounts": [{**ACCT, **bad}]})
        with self.assertRaises(btc_bank.AccountsError):
            v({"accounts": [{"name": "A", "starting_balance": 1}, {"name": "a", "starting_balance": 1}]})
        with self.assertRaises(btc_bank.AccountsError):
            v({"accounts": [{"name": "A"}]})

    def test_load_accounts_never_raises(self):
        d = tempfile.mkdtemp()
        self.assertEqual(btc_bank.load_accounts(os.path.join(d, "nope.json")), ([], None))
        bad = os.path.join(d, "bad.json")
        with open(bad, "w") as f:
            f.write("{not json")
        accts, problem = btc_bank.load_accounts(bad)
        self.assertEqual(accts, [])
        self.assertIn("not valid JSON", problem)
        shutil.rmtree(d)

    def test_the_sample_accounts_file_is_valid(self):
        accts, problem = btc_bank.load_accounts(os.path.join(HERE, "accounts.json"))
        self.assertIsNone(problem)
        self.assertEqual(len(accts), 5)

    def test_sizing_rounds_down_and_reports_the_real_risk(self):
        r = btc_bank.size_position(acct(), 10000, "LONG", 100000, 94000)
        self.assertEqual((r["status"], r["lots"]), ("OK", 0.01))             # raw 0.0166 -> 0.01
        self.assertAlmostEqual(r["actual_risk"], 60.0)
        self.assertAlmostEqual(r["actual_risk_pct"], 0.6)
        r = btc_bank.size_position(acct(), 100000, "SHORT", 100000, 106000)   # stop above entry works too
        self.assertAlmostEqual(r["lots"], 0.16)
        self.assertLessEqual(r["actual_risk"], 1000.0)

    def test_below_min_skips_and_says_what_the_minimum_would_risk(self):
        r = btc_bank.size_position(acct(), 500, "LONG", 100000, 94000)
        self.assertEqual((r["status"], r["lots"]), ("BELOW_MIN", 0.0))
        self.assertAlmostEqual(r["fmt"]["min_risk_pct"], 12.0)
        text = btc_report.account_lines([{"action": "skip", "account": "T", "status": "BELOW_MIN", "fmt": r["fmt"]}])[0]
        self.assertIn("12.0%", text)

    def test_round_up_policy(self):
        r = btc_bank.size_position(acct(min_lot_policy="round_up"), 500, "LONG", 100000, 94000)
        self.assertEqual((r["status"], r["lots"]), ("MIN_LOT_ROUNDED_UP", 0.01))
        self.assertAlmostEqual(r["actual_risk_pct"], 12.0)

    def test_caps(self):
        r = btc_bank.size_position(acct(max_lot=1.0), 1_000_000, "LONG", 100000, 94000)
        self.assertEqual((r["status"], r["lots"]), ("MAX_LOT", 1.0))
        r = btc_bank.size_position(acct(risk_pct=10, leverage=1, max_margin_pct=50), 10000, "LONG", 100000, 94000)
        self.assertEqual((r["status"], r["lots"]), ("MARGIN_LIMITED", 0.05))
        r = btc_bank.size_position(acct(leverage=1, max_margin_pct=1), 10000, "LONG", 100000, 94000)
        self.assertEqual(r["status"], "MARGIN_TOO_SMALL")

    def test_side_and_degenerate_inputs(self):
        self.assertEqual(btc_bank.size_position(acct(sides="long"), 10000, "SHORT", 100, 90)["status"], "SIDE_OFF")
        self.assertEqual(btc_bank.size_position(acct(), 10000, "LONG", 100, 100)["status"], "NO_DISTANCE")
        self.assertEqual(btc_bank.size_position(acct(), 0, "LONG", 100, 90)["status"], "NO_BALANCE")

    def _event(self, kind, side, price, stop, t, flat=False):
        from btc_engine import Event
        return Event(kind, t, t + STEP, price, 0, 0, stop, 1, stop, False, flat, side)

    def test_open_close_pnl_for_both_sides_and_idempotence(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "ledger.jsonl")
        accts = [acct(name="A"), acct(name="B", sides="long")]
        bank = btc_bank.Bank(accts, path)
        bank.ensure_accounts()
        bank.ensure_accounts()                                                  # twice: still one opening each
        with open(path) as f:
            self.assertEqual(len(f.read().splitlines()), 2)

        ev = self._event("ENTRY", "LONG", 100000, 94000, 1000)
        res, entries = bank.plan(ev)
        self.assertEqual([r["action"] for r in res], ["open", "open"])
        bank.commit(entries)
        res2, entries2 = bank.plan(ev)                                          # same event again: nothing new
        self.assertEqual(entries2, [])
        self.assertEqual([r["action"] for r in res2], ["open", "open"])
        bank.commit(entries)                                                    # committing twice changes nothing
        self.assertEqual(len(bank.entries), 4)

        res, entries = bank.plan(self._event("CLOSE", "LONG", 106000, 94000, 9000))
        bank.commit(entries)
        self.assertAlmostEqual(bank.balances["A"], 10060.0)                    # +6000 * 0.01 lots
        self.assertEqual(bank.open, {})

        short_open = self._event("ENTRY", "SHORT", 100000, 106000, 20000)
        res, entries = bank.plan(short_open)
        self.assertEqual([(r["account"], r["action"], r["status"]) for r in res],
                         [("A", "open", "OK"), ("B", "skip", "SIDE_OFF")])
        bank.commit(entries)
        res, entries = bank.plan(self._event("EXIT", "SHORT", 94000, 106000, 30000))
        bank.commit(entries)
        self.assertGreater(bank.balances["A"], 10060)                          # a short that fell made money

        again = btc_bank.Bank(accts, path)                                      # replaying the file gives the same state
        self.assertEqual(again.balances, bank.balances)
        self.assertEqual(again.open, bank.open)
        shutil.rmtree(d)

    def test_flat_exit_and_close_without_position_do_nothing(self):
        bank = btc_bank.Bank([acct()], None)
        bank.ensure_accounts()
        for ev in (self._event("EXIT", "LONG", 90, 95, 1, flat=True), self._event("CLOSE", "LONG", 90, 95, 2)):
            res, entries = bank.plan(ev)
            self.assertEqual((res, entries), ([], []))

    def test_second_entry_while_open_is_skipped(self):
        bank = btc_bank.Bank([acct()], None)
        bank.ensure_accounts()
        _, e = bank.plan(self._event("ENTRY", "LONG", 100000, 94000, 1))
        bank.commit(e)
        res, e = bank.plan(self._event("REENTRY", "LONG", 100000, 94000, 2))
        self.assertEqual(res[0]["status"], "ALREADY_OPEN")

    def test_dry_run_reads_the_ledger_but_never_writes(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "ledger.jsonl")
        bank = btc_bank.Bank([acct()], path, write=False)
        bank.ensure_accounts()
        self.assertFalse(os.path.exists(path))
        self.assertIn("T", bank.balances)
        shutil.rmtree(d)


# --------------------------------------------------------------------------- messages
class Messages(unittest.TestCase):
    def test_every_event_kind_renders_for_both_sides(self):
        for seed in range(5):
            bars = random_walk(seed)
            cfg = Config()
            lg, sh, evs, snaps = run_both(bars, cfg)
            seen = set()
            for e in evs:
                scan = btc_scan.build_scan([b for b in bars if b.t + STEP <= e.close_time], None, None, e.close_time,
                                           strategy=snaps.get(e.close_time))
                txt = btc_report.format_event(e, cfg, "Africa/Lagos", context=btc_report.context_lines(scan, e.side))
                self.assertIn(f"({btc_report.SIDE[e.side]['word']}", txt)
                self.assertNotIn("None", txt)
                seen.add((e.side, e.kind))
        self.assertTrue({("LONG", "ENTRY"), ("SHORT", "ENTRY"), ("LONG", "CLOSE"), ("SHORT", "EXIT")} <= seen
                        or len(seen) >= 6, f"too few kinds exercised: {seen}")

    def test_status_shows_both_sides(self):
        bars = random_walk(1, days=60)
        lg, sh, _, _ = run_both(bars, Config())
        txt = btc_report.format_status({"LONG": lg.status(), "SHORT": sh.status()}, Config())
        self.assertIn("Long:", txt)
        self.assertIn("Short:", txt)


# --------------------------------------------------------------------------- the whole bot, one run per candle
def simulate(fail_at=None, report_hour="-1", accounts_file=None, gap=None, days=52):
    d = tempfile.mkdtemp()
    atexit.register(shutil.rmtree, d, True)
    if accounts_file is None:
        accounts_file = os.path.join(d, "accounts.json")
        shutil.copy(os.path.join(HERE, "accounts.json"), accounts_file)
    bars = random_walk(1, days=days)
    start = 48 * 25
    full_bars_cfg = Config()
    _, _, full, _ = run_both(bars, full_bars_cfg)
    state = {"k": start}
    sent, calls = [], {"n": 0}

    def fake_send(token, chat, text):
        calls["n"] += 1
        if fail_at is not None and calls["n"] == fail_at:
            return False
        sent.append(text)
        return True

    def fake_fetch(key, sym, iv, outputsize=5000, now=None, end_date=None):
        return bars[:state["k"]] if iv == 30 else []

    data = os.path.join(d, "data")
    env = {"TWELVE_DATA_API_KEY": "x", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c",
           "DATA_DIR": data, "ACCOUNTS_FILE": accounts_file, "MAX_CATCHUP_HOURS": "6",
           "SCAN_EVERY_MIN": "60", "DAILY_REPORT_HOUR_UTC": report_hour,
           # convenience paths for the assertions (the bot itself only reads DATA_DIR)
           "TRADES_FILE": os.path.join(data, "trades.jsonl"), "SCANS_FILE": os.path.join(data, "scans.jsonl"),
           "LEDGER_FILE": os.path.join(data, "bank_ledger.jsonl")}
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(turtle_btc, "fetch_bars", fake_fetch), \
            mock.patch.object(turtle_btc, "send_telegram", fake_send), \
            mock.patch.object(turtle_btc._time, "time", lambda: bars[state["k"] - 1].t + STEP + 60), \
            contextlib.redirect_stderr(io.StringIO()):
        for k in range(start, len(bars) + 1):
            if gap and gap[0] <= k < gap[1]:
                continue          # the bot was not running
            state["k"] = k
            turtle_btc.main([])
    return d, bars, start, full, sent, env



_SIM = {}


def simulate_cached(**kw):
    """Run the slow simulation once per parameter set. Callers must COPY the data folder before writing to it."""
    key = tuple(sorted(kw.items()))
    if key not in _SIM:
        _SIM[key] = simulate(**kw)
    return _SIM[key]


class LiveLoop(unittest.TestCase):
    """Simulates the cron job: one run per candle, state, trades, scans and ledger persisted between runs."""

    _simulate = staticmethod(simulate)

    def _expected_events(self, bars, start, full):
        first_close = bars[start - 1].t + STEP
        return [e for e in full if e.close_time > first_close]

    def test_no_duplicates_no_misses_and_everything_is_stored(self):
        d, bars, start, full, sent, env = self._simulate()
        expected = self._expected_events(bars, start, full)
        self.assertGreater(len(expected), 4, "scenario produced too few events to be meaningful")
        self.assertTrue(sent[0].startswith("BTC Turtle bot is online"))
        self.assertEqual(len(sent) - 1, len(expected))
        for text, e in zip(sent[1:], expected):
            self.assertIn(f"({btc_report.SIDE[e.side]['word']}", text)
            self.assertIn("Context (information only", text)

        trades = [json.loads(x) for x in _lines(env["TRADES_FILE"])]
        self.assertEqual([t["event_id"] for t in trades], [e.event_id for e in expected])

        scans = [json.loads(x) for x in _lines(env["SCANS_FILE"])]
        asofs = [s["asof"] for s in scans]
        self.assertEqual(len(asofs), len(set(asofs)), "the same candle was scanned and stored twice")
        self.assertEqual(scans[0]["tag"], "first_run")
        self.assertGreater(sum(1 for s in scans if s["tag"] == "scheduled"), 10)
        self.assertEqual(sum(1 for s in scans if s["tag"].startswith("event:")), len(expected))

        # the ledger must equal an in-memory replay of exactly the events the bot alerted
        accts, _ = btc_bank.load_accounts(env["ACCOUNTS_FILE"])
        mem = btc_bank.Bank(accts, None)
        mem.ensure_accounts()
        for e in expected:
            mem.commit(mem.plan(e)[1])
        disk = btc_bank.Bank(accts, env["LEDGER_FILE"])
        self.assertEqual({k: round(v, 6) for k, v in disk.balances.items()},
                         {k: round(v, 6) for k, v in mem.balances.items()})
        ids = [json.loads(x)["id"] for x in _lines(env["LEDGER_FILE"])]
        self.assertEqual(len(ids), len(set(ids)), "a posting was written twice")
        self.assertTrue(any(s["skips"] for s in disk.summary()) or any(s["trades"] for s in disk.summary()))

    def test_failed_send_is_retried_and_nothing_is_lost_or_doubled(self):
        d, bars, start, full, sent, env = self._simulate(fail_at=3)     # 1 = online, 2 = first alert, 3 fails
        expected = self._expected_events(bars, start, full)
        self.assertEqual(len(sent) - 1, len(expected))
        trades = [json.loads(x) for x in _lines(env["TRADES_FILE"])]
        self.assertEqual([t["event_id"] for t in trades], [e.event_id for e in expected])
        ids = [json.loads(x)["id"] for x in _lines(env["LEDGER_FILE"])]
        self.assertEqual(len(ids), len(set(ids)))

    def test_daily_report_is_sent_once(self):
        d, bars, start, full, sent, env = self._simulate(report_hour="0")
        self.assertEqual(sum(1 for s in sent if s.startswith("BTC technical read")), 1)
        self.assertEqual(sum(1 for s in sent if s.startswith("BTC fundamental read")), 1)

    def test_broken_accounts_file_still_sends_alerts_and_says_why(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        bad = os.path.join(d, "accounts.json")
        with open(bad, "w") as f:
            json.dump({"accounts": [{"name": "X", "starting_balance": 100, "risk_precent": 1}]}, f)
        d2, bars, start, full, sent, env = self._simulate(accounts_file=bad)
        expected = self._expected_events(bars, start, full)
        self.assertEqual(len(sent) - 1, len(expected))
        self.assertTrue(all("Position sizes were not calculated" in s for s in sent[1:]))
        self.assertIn("unknown setting", sent[1])
        self.assertFalse(os.path.exists(env["LEDGER_FILE"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
