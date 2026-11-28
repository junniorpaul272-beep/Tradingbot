"""
btc_stats.py: performance statistics. Pure functions: no files, no network, no wording (btc_report words them).

Two views, because they answer different questions:
  * STRATEGY view (account-free). Every signal is paired entry -> exit and measured in R, where 1R is the distance
    from the entry to the active exit at the moment of entry (a trade that is stopped out on the nose is -1R).
    R does not depend on account size, lot rounding or skipped trades, so it is the clean measure of the strategy.
  * ACCOUNT view. Each demo account's closed positions in USD, after position sizing.

Honesty rules: a number that cannot be computed is None (reported as "n/a"), never 0. For example the profit factor
of a run with no losing trade is None, not infinity and not 0. R is defined only for trades that recorded a risk.

Trade dicts need: pnl, risk (both in the same unit, USD or "per 1 BTC"), open_t and close_t (epoch seconds).
"""
import csv
from datetime import datetime, timezone

_OPEN = ("ENTRY", "REENTRY")
_LEAVE = ("CLOSE", "EXIT")


def _g(x, *keys):
    """Read the first present key/attribute from a dict or an object."""
    for k in keys:
        if isinstance(x, dict):
            if k in x:
                return x[k]
        elif hasattr(x, k):
            return getattr(x, k)
    return None


def segments_from_events(events):
    """Pair each ENTRY/REENTRY with the next CLOSE/EXIT of the same side (a 'flat' EXIT never counts: nothing was
    held). `events` are engine Event objects or trades.jsonl records, in time order. Risk and pnl are per 1 BTC."""
    open_by_side, out = {}, []
    for e in events:
        kind, side = _g(e, "kind"), _g(e, "side")
        if kind in _OPEN:
            open_by_side[side] = e
        elif kind in _LEAVE and not _g(e, "flat"):
            o = open_by_side.pop(side, None)
            if o is None:
                continue
            entry, exit_, stop = _g(o, "price"), _g(e, "price"), _g(o, "active_stop")
            d = 1 if side == "LONG" else -1
            out.append({"side": side, "open_t": _g(o, "t", "bar_time"), "close_t": _g(e, "t", "bar_time"),
                        "entry": entry, "exit": exit_, "stop": stop,
                        "risk": abs(entry - stop) if stop is not None else None,
                        "pnl": d * (exit_ - entry), "close_kind": kind, "event_id": _g(o, "event_id")})
    return out


def trade_r(t):
    """R multiple of one trade, or None when it has no usable risk."""
    risk = t.get("risk")
    return t["pnl"] / risk if risk and risk > 0 else None


def _streak(values, keep):
    best = cur = 0
    for v in values:
        cur = cur + 1 if keep(v) else 0
        best = max(best, cur)
    return best


def compute_stats(trades, start_balance=None):
    """All headline numbers for a list of closed trades (oldest first). Undefined numbers are None."""
    n = len(trades)
    keys = ("wins", "losses", "flat", "win_rate", "net_pnl", "gross_profit", "gross_loss", "profit_factor",
            "profit_factor_r", "total_r", "avg_r", "avg_win_r", "avg_loss_r", "best_r", "worst_r", "r_known", "avg_win", "avg_loss",
            "payoff", "max_dd_r", "max_dd_usd", "max_dd_pct", "max_win_streak", "max_loss_streak", "avg_hold_h",
            "end_balance", "return_pct")
    s = {"n": n, "start_balance": start_balance, **{k: None for k in keys}}
    if n == 0:
        return s
    ordered = sorted(trades, key=lambda t: t["close_t"])
    pnls = [t["pnl"] for t in ordered]
    rs = [trade_r(t) for t in ordered]
    known = [r for r in rs if r is not None]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    s.update(wins=len(wins), losses=len(losses), flat=n - len(wins) - len(losses), win_rate=len(wins) / n,
             net_pnl=sum(pnls), gross_profit=sum(wins), gross_loss=sum(losses))
    s["profit_factor"] = s["gross_profit"] / abs(s["gross_loss"]) if losses else None
    s["avg_win"] = sum(wins) / len(wins) if wins else None
    s["avg_loss"] = sum(losses) / len(losses) if losses else None
    s["payoff"] = s["avg_win"] / abs(s["avg_loss"]) if wins and losses else None
    if known:
        win_r = [r for r in known if r > 0]
        loss_r = [r for r in known if r < 0]
        s.update(r_known=len(known), total_r=sum(known), avg_r=sum(known) / len(known),
                 avg_win_r=sum(win_r) / len(win_r) if win_r else None,
                 avg_loss_r=sum(loss_r) / len(loss_r) if loss_r else None,
                 best_r=max(known), worst_r=min(known))
        neg_r = sum(r for r in known if r < 0)
        s["profit_factor_r"] = sum(win_r) / abs(neg_r) if neg_r < 0 else None
        cum = peak = dd = 0.0
        for r in known:
            cum += r
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
        s["max_dd_r"] = dd
    s["max_win_streak"] = _streak(pnls, lambda p: p > 0)
    s["max_loss_streak"] = _streak(pnls, lambda p: p < 0)
    holds = [(t["close_t"] - t["open_t"]) / 3600 for t in ordered if t.get("open_t") is not None]
    s["avg_hold_h"] = sum(holds) / len(holds) if holds else None
    if start_balance:
        bal = peak = start_balance
        dd_usd = dd_pct = 0.0
        for p in pnls:
            bal += p
            peak = max(peak, bal)
            dd_usd = max(dd_usd, peak - bal)
            dd_pct = max(dd_pct, (peak - bal) / peak * 100 if peak > 0 else 0.0)
        s.update(end_balance=bal, return_pct=(bal / start_balance - 1) * 100, max_dd_usd=dd_usd, max_dd_pct=dd_pct)
    return s


def split_by(trades, key):
    """{value: [trades]} preserving order, e.g. split_by(trades, "side")."""
    out = {}
    for t in trades:
        out.setdefault(t.get(key), []).append(t)
    return out


def _iso(t):
    return "" if t is None else datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")


def write_closed_csv(path, trades):
    """One flat row per closed trade, for a spreadsheet. Times are UTC."""
    cols = ["account", "side", "open_utc", "close_utc", "lots", "entry", "exit", "stop", "risk", "pnl", "r",
            "balance_after", "close_kind"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for t in sorted(trades, key=lambda x: x["close_t"]):
            r = trade_r(t)
            w.writerow([t.get("account", ""), t["side"], _iso(t.get("open_t")), _iso(t["close_t"]),
                        t.get("lots", ""), t["entry"], t["exit"], t.get("stop", ""),
                        "" if t.get("risk") is None else round(t["risk"], 4), round(t["pnl"], 4),
                        "" if r is None else round(r, 4), t.get("balance_after", ""), t.get("close_kind", "")])
