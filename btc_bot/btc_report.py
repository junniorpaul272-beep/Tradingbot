"""
btc_report.py: ALL the wording. The engine, the scan and the bank return facts; this module turns them into
sentences and never decides anything. Every line is either a recorded fact or a reading derived by a rule from
recorded facts. Anything the scan could not check is said to be "not checked", never skipped silently.
Plain words and percentages (no pips), clean spacing, one idea per line.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from btc_engine import NY
from btc_scan import FLAT_BAND_ATR

SIDE = {
    "LONG": {"word": "long", "break": "above", "trigger": "high", "band": "low", "moved": "fell"},
    "SHORT": {"word": "short", "break": "below", "trigger": "low", "band": "high", "moved": "rose"},
}


# --------------------------------------------------------------------------- small formatters
def money(x):
    return "n/a" if x is None else f"{x:,.0f}"


def pct(x, nd=1, signed=True):
    if x is None:
        return "n/a"
    return f"{x:+.{nd}f}%" if signed else f"{x:.{nd}f}%"


def lots_text(x):
    return f"{x:g}"


def ago(bars_ago, bar_minutes):
    mins = bars_ago * bar_minutes
    if mins <= 0:
        return "on the latest candle"
    if mins < 90:
        return f"about {mins} minutes ago"
    hours = mins / 60
    if hours < 48:
        return f"about {round(hours)} hours ago"
    return f"about {round(hours / 24)} days ago"


def when(epoch, display_tz):
    a = datetime.fromtimestamp(epoch, NY)
    b = datetime.fromtimestamp(epoch, ZoneInfo(display_tz))
    return f"{a:%a %H:%M} NY / {b:%H:%M} {display_tz.split('/')[-1]}"


def _dist(p):
    """'0.8% above price' for a level's pct_from_price."""
    return f"{abs(p):.1f}% {'above' if p >= 0 else 'below'} price"


# --------------------------------------------------------------------------- scan wording (shared pieces)
def _break_phrase(fr):
    lb = fr["last_break"]
    word = "above" if lb["dir"] == "up" else "below"
    return f"closed {word} {money(lb['level'])} {ago(lb['bars_ago'], fr['bar_minutes'])}"


def _frame_line(name, fr):
    if fr.get("status") != "ok":
        return f"{name}: not checked ({fr.get('reason', 'unavailable')})."
    c, lb = fr["control"], fr["last_break"]
    if c == "buyers":
        return f"{name}: buyers. Price {_break_phrase(fr)} and has stayed above it."
    if c == "sellers":
        return f"{name}: sellers. Price {_break_phrase(fr)} and has stayed below it."
    if c == "contested":
        who, back = ("Buyers", "under") if lb["dir"] == "up" else ("Sellers", "back above")
        word = "above" if lb["dir"] == "up" else "below"
        return (f"{name}: contested. {who} pushed price {word} {money(lb['level'])} "
                f"{ago(lb['bars_ago'], fr['bar_minutes'])}, then it closed {back} it.")
    return f"{name}: nobody clearly in control. No recent swing has been broken on a close."


def _structure_line(fr):
    return {
        "up": "The 1H swings are stepping up: higher highs and higher lows.",
        "down": "The 1H swings are stepping down: lower highs and lower lows.",
        "mixed": "The 1H swings disagree: the highs and lows are not stepping the same way.",
        "unclear": "There are not enough confirmed 1H swings yet to read the structure.",
    }[fr["structure"]]


def _control_sentence(fr):
    c, lb = fr["control"], fr["last_break"]
    if c == "buyers":
        return (f"Buyers are in control: price {_break_phrase(fr)} and has stayed above it. "
                f"Continuation needs 1H closes to hold above {money(lb['level'])}; a close back below it "
                f"would be the first sign of a reversal.")
    if c == "sellers":
        return (f"Sellers are in control: price {_break_phrase(fr)} and has stayed below it. "
                f"Continuation needs 1H closes to stay below {money(lb['level'])}; a close back above it "
                f"would be the first sign of a reversal.")
    if c == "contested":
        who = "buyers" if lb["dir"] == "up" else "sellers"
        word = "above" if lb["dir"] == "up" else "below"
        return (f"Control is contested: {who} broke {word} {money(lb['level'])} "
                f"{ago(lb['bars_ago'], fr['bar_minutes'])}, but price has since closed back through it, "
                f"so the break has not held.")
    return "Nobody has clear control on 1H: price has not closed beyond a recent swing."


def _activity_words(label):
    return {"quiet": "unusually quiet", "normal": "normal", "active": "unusually active"}.get(label)


# --------------------------------------------------------------------------- the two reports
def technical_report(scan, stamp):
    if scan.get("status") != "ok":
        return f"BTC technical read\n{stamp}\n\nNot checked: {scan.get('reason', 'no data')}."
    tech, fund = scan["technical"], scan["fundamental"]
    frames = tech["frames"]
    one_h = frames["1H"]
    out = [f"BTC technical read\n{stamp}"]

    # 1. bigger picture
    if one_h.get("status") == "ok":
        lines = [_structure_line(one_h), _control_sentence(one_h)]
        r2 = (fund.get("ranges") or {}).get("2D") if fund.get("status") == "ok" else None
        if r2:
            lines.append(f"Over the last 2 days price has ranged {money(r2['low'])} to {money(r2['high'])} "
                         f"and sits in the {r2['zone']} part of that range.")
        if one_h["stretched"]:
            lines.append(f"Price is stretched {one_h['stretched']} its 50-hour average "
                         f"(about {abs(one_h['extension_atr']):.1f} ATRs), so it is extended in that direction.")
        out.append("Bigger picture (1H)\n" + "\n".join(lines))
    else:
        out.append(f"Bigger picture (1H)\nNot checked ({one_h.get('reason', 'unavailable')}).")

    # 2. who is in control, per timeframe
    out.append("Who is in control\n" + "\n".join(_frame_line(n, frames[n]) for n in ("5M", "15M", "1H")))

    # 3. what it is watching
    ctl = one_h.get("control") if one_h.get("status") == "ok" else None
    watch = []
    px = {k: {lv["price"] for lv in tech["levels"] if lv["kind"] == k} for k in ("swing_high", "swing_low",
                                                                                "long_trigger", "short_trigger")}
    for lv in tech["levels"]:
        k, price = lv["kind"], money(lv["price"])
        d = _dist(lv["pct_from_price"])
        away = f", {lv['atr_away']:.1f} daily ATRs away" if lv.get("atr_away") is not None else ""
        if k == "swing_high":
            meaning = {"buyers": "A close above it extends the buyers' run.",
                       "sellers": "A close above it would be the first sign sellers are losing control."}.get(
                ctl, "A close above it would hand buyers the lead.")
            also = " It is also the Turtle long trigger (the 20-day high)." if lv["price"] in px["long_trigger"] else ""
            watch.append(f"{price} ({d}): the last 1H swing high nobody has closed above. {meaning}{also}")
        elif k == "swing_low":
            meaning = {"sellers": "A close below it extends the sellers' run.",
                       "buyers": "A close below it would be the first sign buyers are losing control."}.get(
                ctl, "A close below it would hand sellers the lead.")
            also = " It is also the Turtle short trigger (the 20-day low)." if lv["price"] in px["short_trigger"] else ""
            watch.append(f"{price} ({d}): the last 1H swing low nobody has closed below. {meaning}{also}")
        elif k == "long_trigger" and lv["price"] in px["swing_high"]:
            continue
        elif k == "short_trigger" and lv["price"] in px["swing_low"]:
            continue
        elif k == "break_level":
            who, side = ("Buyers", "above") if ctl == "buyers" else ("Sellers", "below")
            watch.append(f"{price} ({d}): where the last 1H break happened. {who} keep control while 1H closes "
                         f"stay {side} it.")
        elif k == "long_trigger":
            watch.append(f"{price} ({d}): the Turtle long trigger, the highest high of the last 20 days{away}.")
        elif k == "short_trigger":
            watch.append(f"{price} ({d}): the Turtle short trigger, the lowest low of the last 20 days{away}.")
        elif k == "active_exit_long":
            watch.append(f"{price} ({d}): the active exit for the long signal that is on.")
        elif k == "active_exit_short":
            watch.append(f"{price} ({d}): the active exit for the short signal that is on.")
    out.append("What it is watching\n" + ("\n\n".join(watch) if watch else
                                          "Nothing yet: there is not enough confirmed structure."))
    return "\n\n".join(out)


def fundamental_report(scan, stamp):
    fund = scan.get("fundamental") if scan.get("status") == "ok" else None
    if not fund or fund.get("status") != "ok":
        reason = (fund or scan).get("reason", "no data")
        return f"BTC fundamental read\n{stamp}\n\nNot checked: {reason}."
    ch, rg, act = fund["changes"], fund["ranges"], fund["activity"]
    out = [f"BTC fundamental read\n{stamp}\n\nPrice: {money(fund['price'])}"]

    zone = {"upper": "upper part", "middle": "middle", "lower": "lower part"}
    lines = [f"{n}: {money(r['low'])} to {money(r['high'])}, {zone[r['zone']]} of the range"
             for n, r in rg.items() if r]
    out.append("Where price sits\n" + ("\n".join(lines) if lines else "Not enough candles yet."))

    moved = " · ".join(f"{n} {pct(ch[n]['pct'])}" for n in ("1H", "1D", "2D", "4D", "2W") if ch.get(n))
    out.append("How far it has moved\n" + (moved or "Not enough candles yet."))

    # reading
    read = []
    med, fast, slow = act["medium"], act["fast"], act["slow"]
    bias = fund["bias_2w"]
    if bias is None:
        read.append("Two-week bias: not enough data to judge.")
    else:
        chg = ch["2W"]["pct"]
        band = FLAT_BAND_ATR * med["pct"]
        if bias == "flat":
            read.append(f"Two-week bias: flat. BTC is {pct(chg)} over two weeks, less than the {band:.1f}% that "
                        f"{FLAT_BAND_ATR:g} ordinary days of movement would be.")
        else:
            read.append(f"Two-week bias: {bias}. BTC is {pct(chg)} over two weeks, more than the {band:.1f}% that "
                        f"{FLAT_BAND_ATR:g} ordinary days of movement would be (it usually moves about "
                        f"{med['pct']:.1f}% a day).")
    push = fund["push_1h"]
    if push is None:
        read.append("Last hour: not enough data to judge.")
    else:
        size = {"quiet": "a quiet hour", "normal": "about a normal hour", "strong": "a strong push for an hour"}[push["size"]]
        rel = {"with": " That goes with the two-week bias.", "against": " That goes against the two-week bias.",
               "neutral": ""}[push["vs_bias"]]
        read.append(f"Last hour: {pct(push['pct'], 2)}, {size}.{rel}")
    flow = fund["flow"]
    if flow == "consistent":
        read.append("Flow: the 1-day, 2-day, 4-day and 2-week windows all point the same way.")
    elif flow == "mixed":
        read.append("Flow: the shorter windows and the longer ones are not pointing the same way.")
    out.append("Reading\n" + "\n".join(read))

    # activity
    alines = []
    if fast and fast["label"]:
        alines.append(f"Hourly: BTC moves about {fast['pct']:.2f}% an hour, {_activity_words(fast['label'])} "
                      f"compared with the last {fast['history_n'] // 24} days.")
    else:
        alines.append("Hourly: not enough history to judge.")
    if med and med["label"]:
        alines.append(f"Daily: about {med['pct']:.1f}% a day, {_activity_words(med['label'])} "
                      f"compared with the last {med['history_n']} days.")
    else:
        alines.append("Daily: not enough history to judge.")
    if slow and slow["label"]:
        alines.append(f"Two-week average daily range: {slow['pct']:.1f}%, {_activity_words(slow['label'])} "
                      f"compared with the last {slow['history_n']} days.")
    else:
        alines.append("Two-week average daily range: not enough history to judge.")
    out.append("Activity\n" + "\n".join(alines))

    sent = fund["sentiment"]
    out.append(f"Sentiment\nNot checked: {sent['reason']}.")
    return "\n\n".join(out)


def context_lines(scan, side):
    """Three short lines for an alert. Information only: it never changes whether a signal fires."""
    if not scan or scan.get("status") != "ok":
        return ["Context: not checked (no scan data)."]
    one_h = scan["technical"]["frames"]["1H"]
    fund = scan["fundamental"]
    lines = []
    if one_h.get("status") == "ok":
        lb = one_h["last_break"]
        if one_h["control"] in ("buyers", "sellers"):
            lines.append(f"1H control: {one_h['control']} (price {_break_phrase(one_h)}).")
        elif one_h["control"] == "contested":
            lines.append("1H control: contested (the last break has not held).")
        else:
            lines.append("1H control: nobody clearly in control.")
    else:
        lines.append(f"1H control: not checked ({one_h.get('reason', 'unavailable')}).")
    if fund.get("status") == "ok":
        bias = fund["bias_2w"]
        if bias is None:
            lines.append("2-week bias: not enough data to judge.")
        else:
            chg = pct(fund["changes"]["2W"]["pct"])
            word = side.lower()
            if bias == "flat":
                tail = "The two-week picture is flat."
            else:
                aligned = (side == "LONG") == (bias == "up")
                tail = f"This {word} goes {'with' if aligned else 'against'} it."
            lines.append(f"2-week bias: {bias} ({chg}). {tail}")
        med = fund["activity"]["medium"]
        words = _activity_words(med["label"]) if med and med["label"] else None
        lines.append(f"Daily activity: {words}." if words else "Daily activity: not enough history to judge.")
    else:
        lines.append(f"Fundamentals: not checked ({fund.get('reason', 'unavailable')}).")
    return ["Context (information only, it does not change the signal)"] + lines


# --------------------------------------------------------------------------- accounts
_SKIP = {
    "BELOW_MIN": "skipped: the smallest lot ({min_lot}) would risk {min_risk_pct:.1f}% of the balance, above your {risk_pct:g}%",
    "MARGIN_TOO_SMALL": "skipped: the margin cap leaves less than the minimum lot",
    "SIDE_OFF": "skipped: this account does not take {side} trades",
    "NO_DISTANCE": "skipped: there is no distance between entry and exit",
    "ALREADY_OPEN": "skipped: a position from earlier is still open",
    "NO_BALANCE": "skipped: the balance is zero or below",
}


def account_lines(results):
    """Lines for the accounts block of an alert (open / skip / close results from the bank)."""
    opens = [r for r in results if r["action"] in ("open", "skip")]
    closes = [r for r in results if r["action"] == "close"]
    out = []
    if opens:
        rows = []
        for r in opens:
            if r["action"] == "open":
                extra = {"MAX_LOT": " (capped at the max lot)", "MARGIN_LIMITED": " (limited by the margin cap)",
                         "MIN_LOT_ROUNDED_UP": " (rounded UP to the minimum lot, so the risk is above your setting)"
                         }.get(r["status"], "")
                rows.append(f"{r['account']}: {lots_text(r['lots'])} lots, risking {r['actual_risk']:,.2f} USD "
                            f"({r['actual_risk_pct']:.2f}% of {money(r['balance'])}){extra}")
            else:
                rows.append(f"{r['account']}: " + _SKIP.get(r["status"], "skipped").format(**r.get("fmt", {})))
        out.append("Position sizes (demo accounts)\n" + "\n".join(rows))
    if closes:
        rows = [f"{r['account']}: {r['pnl']:+,.2f} USD, balance {money(r['balance'])}" for r in closes]
        out.append("Results on the demo accounts (idealised fills, no spread or fees)\n" + "\n".join(rows))
    return out


# --------------------------------------------------------------------------- alerts
def format_event(ev, cfg, display_tz, context=None, accounts=None, notes=None):
    s = SIDE[ev.side]
    word = s["word"]
    if ev.kind == "ENTRY":
        body = (f"BTC Turtle: ENTRY ({word})\n\n"
                f"Price broke {s['break']} the {cfg.n}-day {s['trigger']} ({money(ev.entry_level)}).\n"
                f"Reference price: {money(ev.price)}\n\n"
                f"Stop: {money(ev.stop)}  ({cfg.atr_mult:g} ATR = {money(cfg.atr_mult * ev.atr)})\n"
                f"Trailing exit band ({cfg.m}-day {s['band']}): {money(ev.exit_level)}\n"
                f"Active exit (the tighter of the two): {money(ev.active_stop)}")
    elif ev.kind == "REENTRY":
        body = (f"BTC Turtle: RE-ENTRY ({word} still valid)\n\n"
                f"Reference price (candle open): {money(ev.price)}\n"
                f"Active exit: {money(ev.active_stop)}  "
                f"(stop {money(ev.stop)}, {cfg.m}-day {s['band']} {money(ev.exit_level)})")
    elif ev.kind == "CLOSE":
        why = ("Missed close caught up on the first candle after it." if ev.late
               else f"Scheduled close ({cfg.close_h:02d}:{cfg.close_min:02d} NY).")
        body = (f"BTC Turtle: CLOSE ({word})\n\n{why}\nReference price: {money(ev.price)}\n\n"
                f"Re-entry is considered from {cfg.open_h:02d}:{cfg.open_min:02d} NY "
                f"if the signal is still valid. Flat over the weekend.")
    else:
        if ev.flat:
            body = (f"BTC Turtle: SIGNAL ENDED ({word})\n\n"
                    f"You were already flat by schedule. Price {s['moved']} through the active exit "
                    f"({money(ev.price)}), so no re-entry will follow.")
        else:
            body = (f"BTC Turtle: EXIT ({word})\n\n"
                    f"Price {s['moved']} through the active exit (the tighter of the {cfg.atr_mult:g} ATR stop "
                    f"and the {cfg.m}-day {s['band']}).\nExit reference: {money(ev.price)}")
    parts = [body]
    if context:
        parts.append("\n".join(context))
    parts.extend(accounts or [])
    parts.extend(notes or [])
    if ev.kind in ("ENTRY", "REENTRY"):
        parts.append("Took it on your demo? Send /taken btc, and add your real fill price if it differs.")
    elif ev.kind in ("CLOSE", "EXIT") and not ev.flat:
        parts.append("Closed it on your demo? Send /closed btc, and add your price if it differs.")
    parts.append(f"Confirmed at the {cfg.interval_min}m candle close, {when(ev.close_time, display_tz)}.")
    return "\n\n".join(parts)


def format_status(statuses, cfg, account_summary=None):
    long_s, short_s = statuses["LONG"], statuses["SHORT"]
    if not long_s["ready"]:
        return (f"BTC Turtle: warming up\n\n{long_s['completed_days']} completed trading days loaded; "
                f"need {max(cfg.n, cfg.m, cfg.atr_len)} before signals can fire.")
    parts = ["BTC Turtle: status", f"Last close: {money(long_s['last_close'])}   ATR ({cfg.atr_len}-day): {money(long_s['atr'])}"]
    for st in (long_s, short_s):
        s = SIDE[st["side"]]
        if st["held"]:
            state = "signal on, position held"
        elif st["signal_on"]:
            state = "signal on, flat by schedule (waiting for the re-entry window)"
        else:
            state = "no signal"
        block = [f"{st['side'].title()}: {state}",
                 f"Trigger ({cfg.n}-day {s['trigger']}): {money(st['entry_level'])}",
                 f"Exit band ({cfg.m}-day {s['band']}): {money(st['exit_level'])}"]
        if st["active_stop"] is not None:
            block.append(f"Active exit: {money(st['active_stop'])}  (stop {money(st['stop'])})")
        parts.append("\n".join(block))
    if account_summary:
        parts.append("\n".join(account_summary))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- statistics
LEGEND = ("R = one unit of risk: a trade stopped out at its exit loses 1R. "
          "Profit factor = money won divided by money lost; above 1 means more was won than lost.")
SMALL_SAMPLE = 30
_SKIP_WORDS = {
    "BELOW_MIN": "the minimum lot was too big for the account",
    "MARGIN_TOO_SMALL": "the margin cap left less than the minimum lot",
    "SIDE_OFF": "the account does not take that side",
    "ALREADY_OPEN": "a position was still open",
    "NO_BALANCE": "the balance was zero or below",
    "NO_DISTANCE": "no distance between entry and exit",
}


def usd(x):
    return "n/a" if x is None else (f"{x:,.2f}" if abs(x) < 100 else f"{x:,.0f}")


def _rr(x, nd=2):
    return "n/a" if x is None else f"{x:+.{nd}f}R"


def _pf(s, in_r=False):
    """Profit factor in R (strategy and taken trades) or in USD (accounts); 'n/a' says why when undefined."""
    pf = s["profit_factor_r"] if in_r else s["profit_factor"]
    if s["n"] == 0:
        return "n/a"
    if pf is None:
        return "n/a (no losing trade yet)" if s["wins"] else "n/a"
    return f"{pf:.2f}"


def stats_block(title, s, sides=None):
    """Headline statistics for one set of trades, measured in R."""
    if s["n"] == 0:
        return f"{title}\nNo closed trades yet."
    split = f" ({sides.get('LONG', 0)} long, {sides.get('SHORT', 0)} short)" if sides else ""
    dd = "n/a" if s["max_dd_r"] is None else f"{s['max_dd_r']:.1f}R"
    lines = [title,
             f"Trades: {s['n']}{split} · won {s['win_rate']:.0%}",
             f"Total {_rr(s['total_r'], 1)} · average {_rr(s['avg_r'])} per trade · profit factor {_pf(s, True)}",
             f"Average win {_rr(s['avg_win_r'])} · average loss {_rr(s['avg_loss_r'])} · "
             f"best {_rr(s['best_r'], 1)} · worst {_rr(s['worst_r'], 1)}",
             f"Worst drawdown {dd} · longest losing run {s['max_loss_streak']} · "
             f"longest winning run {s['max_win_streak']}"]
    if s["avg_hold_h"] is not None:
        lines.append(f"Typical hold: {s['avg_hold_h']:.1f} hours")
    if s["n"] < SMALL_SAMPLE:
        lines.append(f"Only {s['n']} trades so far: far too few to judge the strategy from.")
    return "\n".join(lines)


def side_lines(by_side):
    """One compact line per side: {'LONG': stats, 'SHORT': stats}."""
    out = []
    for side in ("LONG", "SHORT"):
        s = by_side.get(side)
        if s and s["n"]:
            out.append(f"{side.title()}: {s['n']} trades · total {_rr(s['total_r'], 1)} · "
                       f"average {_rr(s['avg_r'])} · profit factor {_pf(s, True)}")
    return out


def account_block(summary, s):
    """One account: balance line, then its headline numbers and why signals were skipped."""
    off = " (switched off)" if not summary["enabled"] else ""
    lines = [f"{summary['name']}{off}: {usd(summary['start'])} → {usd(summary['balance'])} USD "
             f"({summary['return_pct']:+.1f}%)"]
    if s["n"]:
        dd = "n/a" if s["max_dd_pct"] is None else f"{s['max_dd_pct']:.1f}%"
        lines.append(f"  {s['n']} trades · won {s['win_rate']:.0%} · profit factor {_pf(s)} · "
                     f"average {_rr(s['avg_r'])} · total {_rr(s['total_r'], 1)} · worst drawdown {dd}")
    else:
        lines.append("  no closed trades yet")
    unplanned = summary["skips"] - summary["skip_reasons"].get("SIDE_OFF", 0)
    if unplanned and unplanned >= 0.2 * (s["n"] + unplanned):
        lines.append(f"  It could only take {s['n']} of {s['n'] + unplanned} signals it was eligible for, so its "
                     f"numbers describe the slice its size allowed, not the strategy.")
    if summary["skips"]:
        why = ", ".join(f"{n}× {_SKIP_WORDS.get(k, k.lower())}" for k, n in sorted(summary["skip_reasons"].items(),
                                                                                  key=lambda kv: -kv[1]))
        lines.append(f"  skipped {summary['skips']} signal(s): {why}")
    if summary["open"]:
        lines.append("  open: " + ", ".join(f"{p['side'].lower()} {p['lots']:g} lots from {money(p['entry'])}"
                                            for p in summary["open"]))
    return "\n".join(lines)


def stats_report(title, stamp, strategy, by_side, accounts, taken=None, taken_waiting=None):
    """The full results message. `accounts` is [(bank summary row, stats)]; `taken` is stats for her own fills."""
    sides = {k: v["n"] for k, v in by_side.items()}
    parts = [f"{title}\n{stamp}",
             stats_block("Strategy: every signal, idealised fills, measured in R", strategy, sides)]
    sl = side_lines(by_side)
    if sl:
        parts.append("\n".join(sl))
    if taken is not None:
        block = stats_block("Your taken trades: your own fills", taken)
        if taken_waiting:
            block += "\n" + "\n".join(taken_waiting)
        parts.append(block)
    if accounts:
        parts.append("Demo accounts: realised balance after position sizing, no spread or fees; "
                     "profit factor in USD, average and total in R\n"
                     + "\n\n".join(account_block(a, s) for a, s in accounts))
    else:
        parts.append("Demo accounts\nNone set up.")
    parts.append(LEGEND)
    return "\n\n".join(parts)


def bank_report(stamp, accounts):
    if not accounts:
        return "BTC bank\nNo accounts set up."
    return (f"BTC bank\n{stamp}\n\n" + "\n\n".join(account_block(a, s) for a, s in accounts) + "\n\n" + LEGEND)


def format_catchup(events, display_tz, limit=6):
    """One message for signals that happened while the bot was not running (recorded, not alerted)."""
    lines = []
    for e in events[:limit]:
        what = {"ENTRY": "entry", "REENTRY": "re-entry", "CLOSE": "close", "EXIT": "exit"}[e.kind]
        lines.append(f"{SIDE[e.side]['word']} {what} at {money(e.price)}, {when(e.close_time, display_tz)}")
    more = f"\nand {len(events) - limit} more" if len(events) > limit else ""
    return (f"BTC Turtle: {len(events)} signal(s) happened while the bot was not running\n\n"
            + "\n".join(lines) + more + "\n\nThey are recorded in the bank and the data files so the record has no "
            "gaps, but they were not alerted because they are old news. Your own journal is unaffected: only trades "
            "you record with /taken btc count as taken.")
