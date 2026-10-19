"""
market_reconstruct.py — "what happened between 8:00 and 8:30?" and "what did price do after my entry?"
Pure: ledger + stdlib + voice. It is HANDED records and candles; it reads no files and has no clock.

TWO KINDS OF EVIDENCE, NEVER BLENDED
  recorded at the time   the bot's own state snapshots, market events, your ledger entries (trades, notes, alerts). Each
                         was written when it happened (`recorded_at` is shown when it was written noticeably later).
  rebuilt later          price movement worked out NOW from the saved candle archive. True to the candles, but not
                         something the bot necessarily knew at the time, and said so every time.

WHAT IT WILL NOT DO
  * interpret the move (no "bullish", no "sweep"): it reports prices, times and what was recorded.
  * infer an outcome: a trade's recorded result is the user's /outcome; price touching a level is reported as a
    price fact, and when one candle touched both stop and target the order inside it is declared unknowable.
  * fill a gap: missing candles, no snapshots, or a window the archive doesn't cover are each said plainly.
  * go past the window: nothing after `end` is included.
"""
import re
from datetime import timedelta

import ledger
import timeref

BAR = timedelta(minutes=5)
PIP = 0.0001            # GBPUSD; the reply says pips assume a four-decimal pair
CHANGE_FIELDS = ("htf_bias", "market_phase", "thesis_state", "mil_status")
FOLLOW_CAP = timedelta(hours=24)


def _t(v):
    try:
        return ledger.to_utc(v)
    except Exception:
        return None


def _candles(raw, start, end):
    """Usable candles with open time in [start, end), sorted. Candles that can't be read are counted, not dropped silently."""
    ok, bad = [], 0
    for c in raw or []:
        t = _t(c.get("datetime")) if isinstance(c, dict) else None
        vals = [c.get(k) for k in ("open", "high", "low", "close")] if isinstance(c, dict) else []
        if t is None or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals) or c["high"] < c["low"]:
            bad += 1
            continue
        if start <= t < end:
            ok.append({"t": t, "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"]})
    ok.sort(key=lambda x: x["t"])
    return ok, bad


def _archive_span(raw):
    ts = [x for x in (_t(c.get("datetime")) for c in raw or [] if isinstance(c, dict)) if x]
    return (min(ts), max(ts)) if ts else (None, None)


def _price_summary(cs):
    first, last = cs[0], cs[-1]
    hi = max(cs, key=lambda c: c["h"])
    lo = min(cs, key=lambda c: c["l"])
    big = max(cs, key=lambda c: c["h"] - c["l"])
    return {"open": first["o"], "close": last["c"], "net": round(last["c"] - first["o"], 5),
            "net_pips": round((last["c"] - first["o"]) / PIP, 1),
            "high": hi["h"], "high_at": hi["t"], "low": lo["l"], "low_at": lo["t"],
            "range_pips": round((hi["h"] - lo["l"]) / PIP, 1),
            "biggest_candle_pips": round((big["h"] - big["l"]) / PIP, 1), "biggest_candle_at": big["t"],
            "first_candle": first["t"], "last_candle": last["t"]}


def _gaps(cs, start, end):
    """Stretches inside [start, end) with no candle: before the first, between two that are more than one bar apart, after the last."""
    if not cs:
        return [(start, end)]
    out = []
    if cs[0]["t"] - start >= BAR:
        out.append((start, cs[0]["t"]))
    for a, b in zip(cs, cs[1:]):
        if b["t"] - a["t"] > BAR:
            out.append((a["t"] + BAR, b["t"]))
    if end - (cs[-1]["t"] + BAR) >= BAR:
        out.append((cs[-1]["t"] + BAR, end))
    return out


def _snap_changes(history, start, end):
    """What the bot's recorded state said, from snapshots written at the time: the state it started the window with, and each
    change inside it. Only snapshots whose event time AND recorded time are both <= end count (nothing recorded later leaks in)."""
    snaps = [r for r in ledger.view_as_of(history or [], end) if r["event_type"] == "STATE_SNAPSHOT" and r["superseded_by"] is None]
    snaps.sort(key=lambda r: (_t(r["ts"]), _t(r["recorded_at"])))
    before = [r for r in snaps if _t(r["ts"]) < start]
    inside = [r for r in snaps if start <= _t(r["ts"]) <= end]
    out = {"start_state": None, "changes": [], "n_in_window": len(inside)}
    prev = before[-1]["payload"] if before else None
    if prev is not None:
        out["start_state"] = {"as_of": _t(before[-1]["ts"]), **{k: prev.get(k) for k in CHANGE_FIELDS}}
    for r in inside:
        p = r["payload"]
        if prev is not None:
            for k in CHANGE_FIELDS:
                if p.get(k) != prev.get(k):
                    out["changes"].append({"at": _t(r["ts"]), "field": k, "from": prev.get(k), "to": p.get(k)})
        prev = p
    return out


def reconstruct_window(start, end, candles, events=None, history=None, ledger_records=None, signals=None, now=None):
    """-> dict. `end` is never exceeded. Everything outside the window is ignored."""
    if end <= start:
        return {"status": "EMPTY_WINDOW", "start": start, "end": end}
    if now is not None and start > now:
        return {"status": "FUTURE", "start": start, "end": end}
    cs, bad = _candles(candles, start, end)
    a_lo, a_hi = _archive_span(candles)
    rep = {"status": "OK", "start": start, "end": end, "candle_count": len(cs), "unreadable_candles": bad,
           "archive_from": a_lo, "archive_to": a_hi, "expected_bars": int((end - start) / BAR)}
    rep["now"] = now
    rep["price"] = _price_summary(cs) if cs else None
    rep["candle_gaps"] = _gaps(cs, start, end) if cs else []
    rep["engine"] = _snap_changes(history, start, end)
    ev = []
    for e in events or []:
        t = _t(e.get("recorded_at")) if isinstance(e, dict) else None
        if t is not None and start <= t <= end:
            ev.append({"at": t, "headline": e.get("headline"), "code": e.get("code"), "category": e.get("category")})
    rep["market_events"] = sorted(ev, key=lambda x: x["at"])
    sg = []
    for s in signals or []:
        t = _t(s.get("at"))
        if t is not None and start <= t <= end:
            sg.append({**s, "at": t})
    rep["signals"] = sorted(sg, key=lambda x: x["at"])
    mine = []
    for r in ledger_records or []:
        if r.get("event_type") not in ("TRADE_LOGGED", "KNOWLEDGE_STATED", "REASONING_LOGGED", "OUTCOME_RECORDED",
                                      "THREAD_RESOLVED", "SIGNAL_RESPONDED"):
            continue
        t, rec = _t(r.get("ts")), _t(r.get("recorded_at"))
        if t is None or rec is None or not (start <= t <= end) or rec > (now or rec):
            continue
        mine.append({"at": t, "recorded_at": rec, "type": r["event_type"], "payload": r.get("payload") or {}})
    rep["yours"] = sorted(mine, key=lambda x: x["at"])
    return rep


def follow_through(trade, anchor, candles, outcome_at=None, now=None, cap=FOLLOW_CAP):
    """What the saved candles show AFTER a trade's entry, up to the recorded close (or `cap`, or now). Price facts only.
    `trade` is the TRADE_LOGGED payload (direction, entry, sl, tp)."""
    end = anchor + cap
    if outcome_at is not None:
        end = min(end, outcome_at)
    if now is not None:
        end = min(end, now)
    if end <= anchor:
        return {"status": "NOTHING_TO_FOLLOW", "from": anchor, "to": end}
    cs, bad = _candles(candles, anchor, end)
    out = {"status": "OK", "from": anchor, "to": end, "bounded_by": ("the recorded close" if outcome_at is not None and end == outcome_at
                                                                       else "now" if now is not None and end == now else "24 hours"),
           "candle_count": len(cs), "unreadable_candles": bad}
    if not cs:
        out["status"] = "NO_CANDLES"
        return out
    sell = trade.get("direction") == "SELL"
    sl, tp, entry = trade.get("sl"), trade.get("tp"), trade.get("entry")
    out["first_candle"] = cs[0]["t"]
    out["entry"], out["stop"], out["target"] = entry, sl, tp
    hit = None
    for c in cs:
        stop_hit = c["h"] >= sl if sell else c["l"] <= sl
        tgt_hit = c["l"] <= tp if sell else c["h"] >= tp
        if stop_hit or tgt_hit:
            hit = {"at": c["t"], "stop": stop_hit, "target": tgt_hit}
            break
    out["first_touch"] = hit
    bc_ = min(cs, key=lambda c: c["l"]) if sell else max(cs, key=lambda c: c["h"])
    wc_ = max(cs, key=lambda c: c["h"]) if sell else min(cs, key=lambda c: c["l"])
    best = bc_["l"] if sell else bc_["h"]
    worst = wc_["h"] if sell else wc_["l"]
    out["best_price"], out["worst_price"], out["best_at"], out["worst_at"] = best, worst, bc_["t"], wc_["t"]
    out["best_pips"] = round(((entry - best) if sell else (best - entry)) / PIP, 1)
    out["worst_pips"] = round(((worst - entry) if sell else (entry - worst)) / PIP, 1)
    gaps = _gaps(cs, anchor, end)
    out["candle_gaps"] = gaps
    out["last_candle"] = cs[-1]["t"]
    return out


# ----------------------------------------------------------------------
# Plain-language rendering. Every number and time below comes from the report; the words around them hold no facts.
# ----------------------------------------------------------------------
def _p(x):
    return f"{x:.5f}"


def _span(a, b):
    return f"{timeref.hm(a)}-{timeref.hm(b)}"


def _day(dt):
    return timeref.lagos(dt).strftime("%a %d %b")


_REPEAT_RANGE = re.compile(r"\s*\((\d\.\d+)-(\d\.\d+)\)\s*$")
LONG_WINDOW = timedelta(hours=3)


def _clean_headline(h):
    """A logged headline, tidied for reading: no trailing full stops, and a bracketed price range that only repeats the range already
    written in the headline is dropped. The words and numbers that remain are exactly the logged ones."""
    h = (h or "").strip().rstrip(" .")
    m = _REPEAT_RANGE.search(h)
    if m and (f"{m.group(1)}\u2013{m.group(2)}" in h or f"{m.group(1)}-{m.group(2)}" in h[:m.start()]):
        h = h[:m.start()]
    return h.rstrip(" .")


def _group_events(events):
    """Same logged headline more than once -> one entry with every time. Order of first appearance is kept."""
    groups = {}
    for ev in events:
        key = _clean_headline(ev.get("headline") or ev.get("code") or "")
        groups.setdefault(key, []).append(ev["at"])
    return [(k, v) for k, v in groups.items()]


def _times(ts):
    names = [timeref.hm(t) for t in ts]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _gap_text(rep):
    """Missing-candle wording that separates the newest, probably-still-forming stretch from genuine holes in the middle."""
    now, end = rep.get("now"), rep["end"]
    lines, tail = [], None
    for a, b in rep["candle_gaps"]:
        if now is not None and b >= end and end >= now - 2 * BAR:
            tail = (a, b)
        else:
            lines.append((a, b))
    out = []
    if tail:
        out.append(f"  The newest stretch ({_span(*tail)}) isn't in my saved candles yet. That's normal: the latest candle may still be forming or not saved yet, so it isn't a hole.")
    if lines:
        out.append("  Missing candles: " + ", ".join(_span(a, b) for a, b in lines[:4]) +
                   (f" (and {len(lines) - 4} more)" if len(lines) > 4 else "") +
                   ". I can't tell from my records whether that was a market closure or something I failed to save, and I won't guess what price did in those.")
    if lines or tail:
        out.append("  The high, low and range come only from the candles I do have.")
    return out


def _overview(rep):
    """A few plain sentences up front for a long stretch. Only report facts: nothing here explains WHY anything happened."""
    out = []
    pr = rep["price"]
    if pr:
        d = "up" if pr["net"] > 0 else "down" if pr["net"] < 0 else "flat"
        out.append("Price " + ("ended about where it started" if d == "flat" else f"went {d} {abs(pr['net_pips'])} pips net") +
                   f", with a range of {pr['range_pips']} pips: the high was {timeref.hm(pr['high_at'])} and the low {timeref.hm(pr['low_at'])}.")
    eng = rep["engine"]
    ch = eng["changes"]
    if ch:
        bits = [f"{c['field'].replace('_', ' ')} changed at {timeref.hm(c['at'])}" for c in ch[:3]]
        out.append("In my own records: " + "; ".join(bits) + (f" (and {len(ch) - 3} more)" if len(ch) > 3 else "") + ".")
    groups = _group_events(rep["market_events"])
    if groups:
        rep_n = sum(1 for _k, v in groups if len(v) > 1)
        out.append(f"I logged {len(rep['market_events'])} market event(s)" + (f", {rep_n} of them reported more than once" if rep_n else "") + ".")
    if rep["yours"]:
        out.append(f"You left {len(rep['yours'])} record(s) in this stretch.")
    return out


def render_window(rep, assumptions=()):
    if rep["status"] == "FUTURE":
        return "That stretch hasn't happened yet, so there's nothing to go back over."
    if rep["status"] == "EMPTY_WINDOW":
        return "I couldn't work out a stretch of time from that. Try something like 'between 8 and 8:30'."
    s, e = rep["start"], rep["end"]
    out = [f"{_day(s)}, {_span(s, e)} Lagos time."]
    if assumptions:
        out.append("(" + "; ".join(a[0].upper() + a[1:] for a in assumptions) + ".)")
    pr = rep["price"]
    long_ = (e - s) >= LONG_WINDOW
    if long_:
        ov = _overview(rep)
        if ov:
            out.append("")
            out.append("The short version:")
            out += ["  " + x for x in ov]
    out.append("")
    out.append("Price, rebuilt now from my saved 5-minute candles (not something I necessarily knew at the time):")
    if pr:
        direction = "up" if pr["net"] > 0 else "down" if pr["net"] < 0 else "flat"
        move = "didn't move net" if direction == "flat" else f"{direction} {abs(pr['net_pips'])} pips net"
        out.append(f"  It opened at {_p(pr['open'])} and closed at {_p(pr['close'])}, {move}. The high was {_p(pr['high'])} at "
                   f"{timeref.hm(pr['high_at'])} and the low was {_p(pr['low'])} at {timeref.hm(pr['low_at'])}, a range of "
                   f"{pr['range_pips']} pips. The biggest single candle was {pr['biggest_candle_pips']} pips, at {timeref.hm(pr['biggest_candle_at'])}.")
        out.append(f"  I have {rep['candle_count']} candle(s) for it (about {rep['expected_bars']} would be complete).")
        out += _gap_text(rep)
    elif rep["archive_from"] is None:
        out.append("  I don't have a candle archive to look in yet, so I can't tell you what price did.")
    else:
        out.append(f"  I have no saved candles for that stretch (my archive covers {_day(rep['archive_from'])} {timeref.hm(rep['archive_from'])} "
                   f"to {_day(rep['archive_to'])} {timeref.hm(rep['archive_to'])}), so I can't tell you what price did.")
    if rep["unreadable_candles"]:
        out.append(f"  {rep['unreadable_candles']} saved candle(s) were unreadable and were left out.")
    eng = rep["engine"]
    out.append("")
    out.append("What I'd recorded at the time:")
    lines = []
    if eng["start_state"]:
        st = eng["start_state"]
        lines.append(f"My last saved reading before that stretch ({timeref.hm(st['as_of'])}) had the 1H bias as {st['htf_bias'] or 'not recorded'}, "
                     f"the phase as {st['market_phase'] or 'not recorded'}.")
    for c in eng["changes"][:6]:
        lines.append(f"At {timeref.hm(c['at'])} my recorded {c['field'].replace('_', ' ')} went from {c['from'] or 'not recorded'} to {c['to'] or 'not recorded'}.")
    if len(eng["changes"]) > 6:
        lines.append(f"(and {len(eng['changes']) - 6} more changes not shown)")
    groups = _group_events(rep["market_events"])
    for text, times in groups[:6]:
        if len(times) == 1:
            lines.append(f"At {timeref.hm(times[0])} I logged a market event: {text}.")
        else:
            lines.append(f"At {_times(times)} I logged the same market event {len(times)} times: {text}. I can't tell from the records whether that is "
                         "one development reported again or separate ones.")
    if len(groups) > 6:
        lines.append(f"(and {len(groups) - 6} more kinds of market event not shown)")
    for sg in rep["signals"][:3]:
        lines.append(f"At {timeref.hm(sg['at'])} a {str(sg.get('direction', '')).lower()} signal fired" + (f" at {_p(sg['entry'])}" if isinstance(sg.get("entry"), (int, float)) else "") + ".")
    if not lines:
        lines = ["Nothing. I have no saved state readings or events for that stretch, so I can't say what the bot was seeing then."]
    elif not eng["changes"] and eng["n_in_window"]:
        lines.append("My recorded readings didn't change during it.")
    out += ["  " + l for l in lines]
    yours = rep["yours"]
    out.append("")
    out.append("What you did or wrote then:")
    if yours:
        for y in yours[:8]:
            late = f" (you told me at {timeref.hm(y['recorded_at'])})" if (y["recorded_at"] - y["at"]).total_seconds() > 600 else ""
            pl = y["payload"]
            if y["type"] == "KNOWLEDGE_STATED":
                out.append(f"  {timeref.hm(y['at'])} you wrote: \"{pl.get('text')}\"{late}")
            elif y["type"] == "REASONING_LOGGED":
                out.append(f"  {timeref.hm(y['at'])} your reasoning on {pl.get('case_id')}: \"{pl.get('text')}\"{late}")
            elif y["type"] == "TRADE_LOGGED":
                out.append(f"  {timeref.hm(y['at'])} you logged {pl.get('case_id')}: {str(pl.get('direction')).lower()} at {_p(pl['entry'])}{late}")
            elif y["type"] == "OUTCOME_RECORDED":
                out.append(f"  {timeref.hm(y['at'])} you recorded {pl.get('case_id')} as {str(pl.get('result')).lower()}{late}")
            elif y["type"] == "SIGNAL_RESPONDED":
                out.append(f"  {timeref.hm(y['at'])} you answered a signal: {str(pl.get('response')).lower()}{late}")
            else:
                out.append(f"  {timeref.hm(y['at'])} one of your alerts resolved{late}")
    elif rep["market_events"] or rep["engine"]["changes"] or rep["engine"]["start_state"] or rep["price"]:
        out.append("  I couldn't find any notes or trades from you in the records I searched. So I can walk you through the market side, "
                   "but I can't reconstruct what you were thinking then; you may well have been watching without logging anything.")
    else:
        out.append("  Nothing recorded from you in that stretch.")
    out.append("")
    nxt = []
    if rep["engine"]["changes"]:
        nxt.append(f"'what happened around {timeref.hm(rep['engine']['changes'][0]['at'])}' to zoom in on when my recorded reading changed")
    if long_ and rep["price"]:
        nxt.append(f"'what happened between {timeref.hm(rep['price']['high_at'])} and {timeref.hm(min(rep['end'], rep['price']['high_at'] + timedelta(hours=1)))}' for the high")
    out.append("That's all I have for it. Say 'and after that?' and I'll carry on from there, or tell me another stretch."
               + (" You could also ask " + " or ".join(nxt) + "." if nxt else ""))
    return "\n".join(out)


def render_follow(ft, recorded_outcome=None):
    if ft["status"] == "NOTHING_TO_FOLLOW":
        return "There's no time after your entry to follow yet."
    if ft["status"] == "NO_CANDLES":
        return ("I have no saved candles after your entry, so I can't say what price did afterwards. "
                "That doesn't mean nothing happened; it means I can't see it.")
    side = "against you" if False else ""
    out = [f"After your entry, from the saved candles (rebuilt now, not something I necessarily knew then), following it until {ft['bounded_by']}:"]
    out.append(f"  The best price for your trade was {_p(ft['best_price'])} ({ft['best_pips']} pips in your favour) at {timeref.hm(ft['best_at'])}; "
               f"the worst was {_p(ft['worst_price'])} ({ft['worst_pips']} pips against you) at {timeref.hm(ft['worst_at'])}.")
    h = ft["first_touch"]
    if h is None:
        out.append(f"  Price didn't reach your stop {_p(ft['stop'])} or your target {_p(ft['target'])} in the candles I have.")
    elif h["stop"] and h["target"]:
        out.append(f"  At {timeref.hm(h['at'])} one candle reached both your stop {_p(ft['stop'])} and your target {_p(ft['target'])}; "
                   "candles don't show which came first inside them, so I can't tell you.")
    else:
        which = "stop" if h["stop"] else "target"
        out.append(f"  Price first reached your {which} {_p(ft[which])} in the candle at {timeref.hm(h['at'])}.")
    if ft["candle_gaps"]:
        out.append("  Some candles are missing (" + ", ".join(_span(a, b) for a, b in ft["candle_gaps"][:3]) + "), so treat that as 'what I can see'.")
    out.append(f"  Your recorded outcome stays what you told me ({recorded_outcome or 'nothing recorded yet'}). I don't turn price into a result.")
    return "\n".join(out)
