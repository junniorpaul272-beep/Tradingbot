"""Property tests: the maths checked against independent brute-force recomputation on many random cases."""
import random
import unittest

import btc_bank
import btc_scan
import btc_stats
from btc_engine import Bar


def acct(rng):
    while True:
        try:
            return _acct(rng)
        except btc_bank.AccountsError:
            continue                      # an impossible combination (e.g. max_lot below min_lot) is correctly refused


def _acct(rng):
    return btc_bank.validate_accounts({"accounts": [{
        "name": "x", "starting_balance": 1000,
        "risk_pct": rng.choice([0.25, 0.5, 1, 2, 3.5]), "contract_size": rng.choice([1, 0.1, 0.01]),
        "min_lot": rng.choice([0.01, 0.1, 1]), "max_lot": rng.choice([0.5, 5, 100]),
        "lot_step": rng.choice([0.01, 0.1]), "leverage": rng.choice([1, 20, 100, 500]),
        "max_margin_pct": rng.choice([10, 50, 90]), "sides": rng.choice(["both", "long", "short"]),
        "min_lot_policy": rng.choice(["skip", "round_up"])}]})[0]


class Sizing(unittest.TestCase):
    def test_sizing_obeys_every_limit_and_is_the_largest_allowed(self):
        rng = random.Random(11)
        opened = skipped = 0
        for _ in range(20000):
            a = acct(rng)
            bal = rng.choice([5, 50, 500, 2000, 10000, 25000, 1e6]) * rng.uniform(0.5, 1.5)
            side = rng.choice(["LONG", "SHORT"])
            entry = rng.uniform(1000, 150000)
            dist = entry * rng.choice([1e-5, 1e-3, 0.01, 0.03, 0.2]) * rng.uniform(0.2, 3)
            stop = entry - dist if side == "LONG" else entry + dist
            r = btc_bank.size_position(a, bal, side, entry, stop)
            if r["status"] in ("SIDE_OFF", "NO_BALANCE", "NO_DISTANCE", "BELOW_MIN", "MARGIN_TOO_SMALL"):
                skipped += 1
                self.assertEqual(r["lots"], 0)
                continue
            opened += 1
            lots, step = r["lots"], a["lot_step"]
            self.assertAlmostEqual(lots / step, round(lots / step), places=6)          # a whole number of steps
            self.assertGreaterEqual(lots + 1e-9, a["min_lot"])
            self.assertLessEqual(lots, a["max_lot"] + 1e-9)
            margin = lots * entry * a["contract_size"] / a["leverage"]
            self.assertLessEqual(margin, bal * a["max_margin_pct"] / 100 * (1 + 1e-9))   # margin cap
            per_lot = abs(entry - stop) * a["contract_size"]
            self.assertAlmostEqual(r["actual_risk"], lots * per_lot, delta=1e-6 * max(1, r["actual_risk"]))
            self.assertAlmostEqual(r["actual_risk_pct"], r["actual_risk"] / bal * 100, places=6)
            if r["status"] != "MIN_LOT_ROUNDED_UP":
                self.assertLessEqual(r["actual_risk"], bal * a["risk_pct"] / 100 * (1 + 1e-7))  # never over-risk
            # largest allowed: one more step would break the risk, max_lot or margin limit
            more = lots + step
            breaks = (more * per_lot > bal * a["risk_pct"] / 100 * (1 + 1e-9) or more > a["max_lot"] + 1e-9
                      or more * entry * a["contract_size"] / a["leverage"] > bal * a["max_margin_pct"] / 100)
            if r["status"] != "MIN_LOT_ROUNDED_UP":
                self.assertTrue(breaks, (a, bal, entry, stop, r))
        self.assertGreater(opened, 1000)
        self.assertGreater(skipped, 1000)

    def test_hand_checked_examples(self):
        a = btc_bank.validate_accounts({"accounts": [{"name": "a", "starting_balance": 10000, "risk_pct": 1}]})[0]
        r = btc_bank.size_position(a, 10000, "LONG", 100000.0, 98000.0)   # risk $100, $2000/lot -> 0.05 lot
        self.assertEqual(r["status"], "OK")
        self.assertAlmostEqual(r["lots"], 0.05)
        self.assertAlmostEqual(r["actual_risk"], 100.0)
        r = btc_bank.size_position(a, 10000, "SHORT", 100000.0, 102000.0)
        self.assertAlmostEqual(r["lots"], 0.05)
        r = btc_bank.size_position(a, 500, "LONG", 100000.0, 98000.0)    # risk $5 = 0.0025 lot, below the 0.01 minimum
        self.assertEqual(r["status"], "BELOW_MIN")
        r = btc_bank.size_position(a, 5, "LONG", 100000.0, 98000.0)      # margin allows only 0.0025 lot
        self.assertEqual(r["status"], "MARGIN_TOO_SMALL")


class Warnings(unittest.TestCase):
    def test_an_account_that_cannot_open_trades_is_warned_about_with_the_fix(self):
        accts = btc_bank.validate_accounts({"accounts": [
            {"name": "Acct-50usd", "starting_balance": 50, "risk_pct": 1},
            {"name": "Acct-10k", "starting_balance": 10000, "risk_pct": 1},
            {"name": "Acct-50-round", "starting_balance": 50, "risk_pct": 1, "min_lot_policy": "round_up"},
            {"name": "Off", "starting_balance": 50, "risk_pct": 1, "enabled": False}]})
        bank = btc_bank.Bank(accts)
        bank.ensure_accounts()
        w = bank.warnings(100000.0, 2000.0)                    # a typical exit is 4000 away
        self.assertEqual(len(w), 1)
        self.assertIn("Acct-50usd", w[0])
        self.assertIn("No risk setting can fix this", w[0])    # $50 at 0.01 lot would need ~80% risk
        self.assertEqual(bank.warnings(None, 2000.0), [])      # no data, no warning
        # a broker with smaller lots (0.001) makes the same account tradable, and the warning goes away
        accts2 = btc_bank.validate_accounts({"accounts": [
            {"name": "Acct-50usd", "starting_balance": 50, "risk_pct": 2, "min_lot": 0.0001,
                                    "lot_step": 0.0001}]})
        b2 = btc_bank.Bank(accts2)
        b2.ensure_accounts()
        self.assertEqual(b2.warnings(100000.0, 2000.0), [])
        accts3 = btc_bank.validate_accounts({"accounts": [
            {"name": "Mid", "starting_balance": 50, "risk_pct": 2, "min_lot": 0.001, "lot_step": 0.001}]})
        b3 = btc_bank.Bank(accts3)
        b3.ensure_accounts()
        self.assertIn("raise risk_pct to about 8.0%", b3.warnings(100000.0, 2000.0)[0])


class StatsMaths(unittest.TestCase):
    def test_stats_match_a_brute_force_recomputation(self):
        rng = random.Random(5)
        for _ in range(300):
            n = rng.randint(1, 40)
            trades = []
            t = 0
            for _ in range(n):
                risk = rng.uniform(100, 3000)
                pnl = rng.choice([-1, -1, 1, 2.5, -0.4, 0]) * risk * rng.uniform(0.5, 1.2)
                trades.append({"pnl": pnl, "risk": risk, "open_t": t, "close_t": t + 3600 * rng.randint(1, 30)})
                t += 3600 * 40
            s = btc_stats.compute_stats(trades, start_balance=10000)
            pn = [x["pnl"] for x in trades]
            rr = [x["pnl"] / x["risk"] for x in trades]
            self.assertEqual(s["n"], n)
            self.assertAlmostEqual(s["net_pnl"], sum(pn))
            self.assertAlmostEqual(s["total_r"], sum(rr))
            self.assertAlmostEqual(s["avg_r"], sum(rr) / n)
            self.assertEqual(s["wins"], sum(p > 0 for p in pn))
            self.assertEqual(s["losses"], sum(p < 0 for p in pn))
            self.assertAlmostEqual(s["win_rate"], sum(p > 0 for p in pn) / n)
            gl = -sum(p for p in pn if p < 0)
            if gl > 0:
                self.assertAlmostEqual(s["profit_factor"], sum(p for p in pn if p > 0) / gl)
                self.assertAlmostEqual(s["profit_factor_r"], sum(r for r in rr if r > 0) / -sum(r for r in rr if r < 0))
            else:
                self.assertIsNone(s["profit_factor"])
            # drawdown by brute force over every peak/trough pair
            eq = [10000.0]
            for p in pn:
                eq.append(eq[-1] + p)
            dd = max(max(eq[i] - eq[j] for j in range(i, len(eq))) for i in range(len(eq)))
            self.assertAlmostEqual(s["max_dd_usd"], dd)
            cum = [0.0]
            for r in rr:
                cum.append(cum[-1] + r)
            ddr = max(max(cum[i] - cum[j] for j in range(i, len(cum))) for i in range(len(cum)))
            self.assertAlmostEqual(s["max_dd_r"], ddr)
            self.assertAlmostEqual(s["end_balance"], eq[-1])
            self.assertAlmostEqual(s["return_pct"], (eq[-1] / 10000 - 1) * 100)
            best = cur = 0
            for p in pn:
                cur = cur + 1 if p > 0 else 0
                best = max(best, cur)
            self.assertEqual(s["max_win_streak"], best)


class ScanFigures(unittest.TestCase):
    def test_atr_ema_resample_by_hand(self):
        bars = [Bar(0, 10, 12, 9, 11), Bar(1800, 11, 13, 10, 12), Bar(2 * 1800, 12, 12, 8, 9)]
        # TRs: 3 ; max(3,|13-11|,|10-11|)=3 ; max(4,|12-12|,|8-12|)=4  -> seed mean(3,3,4)=10/3
        a = btc_scan.atr_series(bars, 3)
        self.assertEqual(a[:2], [None, None])
        self.assertAlmostEqual(a[2], 10 / 3)
        a2 = btc_scan.atr_series(bars + [Bar(3 * 1800, 9, 10, 9, 10)], 3)   # TR=max(1,|10-9|,|9-9|)=1
        self.assertAlmostEqual(a2[3], (10 / 3 * 2 + 1) / 3)                 # Wilder
        self.assertEqual(btc_scan.ema_series([1, 2, 3], 3)[1:], [1.5, 2.25])   # k=0.5
        hourly = btc_scan.resample([Bar(0, 1, 5, 0, 2), Bar(1800, 2, 6, 1, 3), Bar(3600, 3, 4, 2, 3)], 3600)
        self.assertEqual(len(hourly), 1)                                    # the lone third candle is an incomplete hour
        h = hourly[0]
        self.assertEqual((h.t, h.o, h.h, h.l, h.c), (0, 1, 6, 0, 3))

    def test_swing_break_and_window_changes_by_hand(self):
        px = [(5, 4), (6, 5), (9, 7), (7, 5), (6, 4), (8, 6), (12, 9)]      # (high, low); close = low+... below
        bars = [Bar(i * 1800, l, h, l, h - 0.5) for i, (h, l) in enumerate(px)]
        r = btc_scan.structure_read(bars, k=2)
        self.assertEqual(r["swing_highs"], [(2, 9)])                        # the 9 stands above two candles each side
        self.assertEqual(r["last_break"]["dir"], "up")                      # the last candle closes at 11.5 > 9
        self.assertEqual(r["last_break"]["idx"], 6)
        self.assertEqual(r["last_break"]["bars_ago"], 0)
        # window % change: 1H = 2 candles back, measured close to close
        b30 = [Bar(i * 1800, 100, 101, 99, 100.0 + i) for i in range(400)]
        d = [Bar(i * 86400, 100, 101, 99, 100) for i in range(30)]
        f = btc_scan.fundamental_read(b30, btc_scan.resample(b30, 3600), d)
        self.assertAlmostEqual(f["changes"]["1H"]["pct"], (499 / 497 - 1) * 100)
        self.assertAlmostEqual(f["changes"]["1D"]["pct"], (499 / 451 - 1) * 100)


if __name__ == "__main__":
    unittest.main()
