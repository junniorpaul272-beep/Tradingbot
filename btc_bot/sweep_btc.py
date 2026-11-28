"""
sweep_btc.py: a ONE-OFF side-by-side test of different breakout / exit windows on the stored candles.

Question it answers: the strategy enters on a 20-day high and exits on a 10-day low (20/10). What would 15/10, 15/5 or
10/5 have done on the same candles with everything else unchanged (ATR length 20, 2 ATR stop, same day-trading rules)?

Safety: this is separate from the live bot. It only READS history_30m.csv and accounts.json, never touches the live
state, trades, ledger or alerts, and writes only into data/sweep/. Nothing here changes how the live bot trades.

Fairness: all variants use the same candles and the same 20-day ATR, so they all start trading at the same moment.
Every variant is shown with the same numbers, plus a cost-aware result: shorter windows give tighter exits, so a fixed
trading cost is a bigger part of each 1R. The cost is an ASSUMPTION (--cost-pct, default 0.10% round trip).

Honesty: picking the best of several variants on one year of data is how people fool themselves. The output says so.

Usage:  python btc_bot/sweep_btc.py [--pairs 20:10,15:10,15:5,10:5] [--cost-pct 0.10] [--days 365] [--send]
"""
import argparse
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import btc_bank  # noqa: E402
import btc_history  # noqa: E402
import btc_stats  # noqa: E402
from btc_engine import Config, run_both  # noqa: E402

UTC = timezone.utc
DEFAULT_PAIRS = "20:10,15:10,15:5,10:5"


def parse_pairs(text):
    """'20:10,15:5' -> [(20, 10), (15, 5)]. The first pair is the baseline everything is compared with."""
    pairs = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n, m = (int(x) for x in part.split(":"))
        except ValueError:
            raise ValueError(f"'{part}' is not a pair like 15:10 (entry days : exit days)") from None
        if not (2 <= n <= 60 and 2 <= m <= 60):
            raise ValueError(f"{part}: days must be between 2 and 60")
        if (n, m) in pairs:
            raise ValueError(f"{part} is listed twice")
        pairs.append((n, m))
    if len(pairs) < 2:
        raise ValueError("give at least two pairs (the first is the baseline)")
    return pairs


def net_r(seg, cost_pct):
    """R of one trade after a round-trip cost of cost_pct % of the entry price (None if it has no risk)."""
    r = btc_stats.trade_r(seg)
    if r is None:
        return None
    return r - (cost_pct / 100.0 * seg["entry"]) / seg["risk"]


def run_variant(bars, n, m, accounts, cost_pct):
    cfg = Config(n=n, m=m)
    cfg.validate()
    _, _, events, _ = run_both(bars, cfg, keep_last=0)
    segs = btc_stats.segments_from_events(events)
    gross = btc_stats.compute_stats(segs)
    # the same trades with the cost taken off, as R-only trades (risk 1, pnl = net R)
    nets = [{"pnl": net_r(s, cost_pct), "risk": 1.0, "open_t": s["open_t"], "close_t": s["close_t"]}
            for s in segs if net_r(s, cost_pct) is not None]
    net = btc_stats.compute_stats(nets)
    by_side = {k: btc_stats.compute_stats(v) for k, v in btc_stats.split_by(segs, "side").items()}
    risk_pct = [s["risk"] / s["entry"] * 100 for s in segs if s["risk"]]
    acct = []
    if accounts:
        bank = btc_bank.Bank(accounts, path=None)
        bank.ensure_accounts()
        for ev in events:
            bank.commit(bank.plan(ev)[1])
        for row in bank.summary():
            st = btc_stats.compute_stats(bank.closed_trades(row["name"]), row["start"])
            acct.append((row["name"], row["start"], st))
    return {"n": n, "m": m, "segs": segs, "gross": gross, "net": net, "by_side": by_side,
            "median_risk_pct": sorted(risk_pct)[len(risk_pct) // 2] if risk_pct else None, "accounts": acct}


def _f(x, spec, none="n/a"):
    return none if x is None else format(x, spec)


def _r(x, d=2):
    return "n/a" if x is None else f"{x:+.{d}f}R"


def variant_block(v, base, cost_pct):
    g, nt = v["gross"], v["net"]
    tag = f"{v['n']}/{v['m']}" + ("  (current logic)" if v is base else "")
    if g["n"] == 0:
        return f"{tag}\nNo closed trades."
    sides = {k: s["n"] for k, s in v["by_side"].items()}
    lines = [
        tag,
        f"Trades {g['n']} ({sides.get('LONG', 0)} long, {sides.get('SHORT', 0)} short) · won {g['win_rate']:.0%} · "
        f"typical hold {_f(g['avg_hold_h'], '.0f')}h",
        f"Idealised: total {_r(g['total_r'], 1)} · average {_r(g['avg_r'])} · profit factor "
        f"{_f(g['profit_factor_r'], '.2f')} · worst drawdown {_f(g['max_dd_r'], '.1f')}R",
        f"After {cost_pct:g}% round-trip cost: total {_r(nt['total_r'], 1)} · average {_r(nt['avg_r'])} · profit factor "
        f"{_f(nt['profit_factor_r'], '.2f')} · worst drawdown {_f(nt['max_dd_r'], '.1f')}R",
        f"Typical exit distance {_f(v['median_risk_pct'], '.1f')}% of price (the tighter, the more a cost weighs)",
    ]
    long_s, short_s = v["by_side"].get("LONG"), v["by_side"].get("SHORT")
    if long_s and short_s and long_s["n"] and short_s["n"]:
        lines.append(f"Long {_r(long_s['total_r'], 1)} · Short {_r(short_s['total_r'], 1)} (idealised)")
    if v["accounts"]:
        lines.append("Demo accounts (no costs): " + " · ".join(
            (f"{name} {st['return_pct']:+.1f}% ({st['n']} trades)" if st["n"] else f"{name} no trades")
            for name, _, st in v["accounts"]))
    return "\n".join(lines)


def comparison_text(results, stamp, cost_pct):
    base = results[0]
    head = (f"BTC window test (a side test, the live bot is unchanged)\n{stamp}\n"
            f"Entry days / exit days. Everything else as the live bot: 20-day ATR, 2 ATR stop, day-trading rules.")
    blocks = [variant_block(v, base, cost_pct) for v in results]
    rank = sorted((v for v in results if v["net"]["n"]), key=lambda v: -(v["net"]["total_r"] or 0))
    order = " > ".join(f"{v['n']}/{v['m']} ({_r(v['net']['total_r'], 1)})" for v in rank)
    foot = ("Ranked by total R after cost: " + order + "\n\n"
            "How to read this: all variants trade the same candles over the same period. R is one unit of risk, so "
            "totals compare fairly across windows. The cost line is an assumption, not your broker's real spread. "
            "One year is one market regime; choosing the best of several variants on the same year tends to look "
            "better than it will trade live, so treat a winner as a candidate to forward-test on the demo, not as a "
            "result. A window that wins only by trading more often is the first one a real cost will hurt.")
    return "\n\n".join([head] + blocks + [foot])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Side-by-side test of entry/exit windows")
    ap.add_argument("--pairs", default=DEFAULT_PAIRS, help="entry:exit day pairs; the first is the baseline")
    ap.add_argument("--cost-pct", type=float, default=0.10, help="assumed round-trip cost, %% of price")
    ap.add_argument("--days", type=float, default=0, help="use only the last DAYS days of the file (0 = all)")
    ap.add_argument("--csv", default=os.path.join(HERE, "data", "history_30m.csv"))
    ap.add_argument("--accounts", default=os.path.join(HERE, "accounts.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "data", "sweep"))
    ap.add_argument("--send", action="store_true", help="also send the result to Telegram")
    args = ap.parse_args(argv)

    try:
        pairs = parse_pairs(args.pairs)
    except ValueError as e:
        sys.exit(f"--pairs: {e}")
    if not 0 <= args.cost_pct <= 5:
        sys.exit("--cost-pct must be between 0 and 5 (percent of price, round trip).")
    if not os.path.exists(args.csv):
        sys.exit(f"No candle file at {args.csv}. Run the bot's history mode first.")
    bars = btc_history.load_csv(args.csv)
    if args.days:
        bars = [b for b in bars if b.t >= bars[-1].t - args.days * 86400]
    if len(bars) < 48 * 30:
        sys.exit(f"Only {len(bars)} candles: need at least about 30 days to test anything.")
    accounts, problem = btc_bank.load_accounts(args.accounts)
    if problem:
        print(f"Warning: {problem}", file=sys.stderr)

    results = [run_variant(bars, n, m, accounts, args.cost_pct) for n, m in pairs]
    first, last = datetime.fromtimestamp(bars[0].t, UTC), datetime.fromtimestamp(bars[-1].t, UTC)
    stamp = f"{(bars[-1].t - bars[0].t) / 86400:.0f} days of candles, {first:%Y-%m-%d} to {last:%Y-%m-%d} UTC"
    text = comparison_text(results, stamp, args.cost_pct)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "summary.txt"), "w") as f:
        f.write(text + "\n")
    for v in results:
        btc_stats.write_closed_csv(os.path.join(args.out, f"trades_{v['n']}_{v['m']}.csv"), v["segs"])
    print(text)

    if args.send:
        import turtle_btc  # only needed for sending
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not (token and chat):
            sys.exit("--send needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        if not turtle_btc.send_telegram(token, chat, text):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
