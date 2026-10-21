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


def _conditions(cs, bar):
    """Facts about how the candles behaved, for one timeframe. Counts and sizes only: nothing here names a pattern or a cause."""
    if not cs:
        return None
    sizes = [(c["h"] - c["l"]) / PIP for c in cs]
    up = sum(1 for c in cs if c["c"] > c["o"])
    down = sum(1 for c in cs if c["c"] < c["o"])
    hi = max(cs, key=lambda c: c["h"]); lo = min(cs, key=lambda c: c["l"])
    big = max(cs, key=lambda c: c["h"] - c["l"])
    out = {"n": len(cs), "up": up, "down": down, "flat": len(cs) - up - down,
           "open": cs[0]["o"], "close": cs[-1]["c"], "net_pips": round((cs[-1]["c"] - cs[0]["o"]) / PIP, 1),
           "range_pips": round((hi["h"] - lo["l"]) / PIP, 1), "avg_candle_pips": round(sum(sizes) / len(sizes), 1),
           "biggest_pips": round(max(sizes), 1), "biggest_at": big["t"], "high": hi["h"], "high_at": hi["t"], "low": lo["l"], "low_at": lo["t"],
           "partial_last": cs[-1]["t"] + bar,
           "candles": [{"t": c["t"], "o": c["o"], "c": c["c"], "pips": round((c["h"] - c["l"]) / PIP, 1),
                        "net_pips": round((c["c"] - c["o"]) / PIP, 1)} for c in cs]}
    by_hour = {}
    for c, z in zip(cs, sizes):
        by_hour.setdefault(c["t"].replace(minute=0, second=0, microsecond=0), []).append(z)
    if len(by_hour) >= 3:
        avg = {h: sum(v) / len(v) for h, v in by_hour.items() if len(v) >= max(1, int(0.5 * 60 / (bar.total_seconds() / 60)))}
        if len(avg) >= 3:
            b, q = max(avg, key=avg.get), min(avg, key=avg.get)
            out["busiest_hour"], out["busiest_avg"] = b, round(avg[b], 1)
            out["quietest_hour"], out["quietest_avg"] = q, round(avg[q], 1)
    return out


def _structure_changes(digests, thesis, start, end):
    """What the engine's own per-scan records said about structure, by timeframe. It only knows what was written at each scan (about every
    5 minutes), so a flip that came and went between two scans is not here, and nothing is inferred about direction it did not record."""
    def rows(raw):
        out = []
        for r in raw or []:
            t = _t(r.get("scanned_at")) if isinstance(r, dict) else None
            if t is not None and t <= end:
                out.append((t, r))
        return sorted(out, key=lambda x: x[0])

    def track(rs, getters):
        res = {"start": {}, "changes": [], "scans": 0}
        prev = None
        for t, r in rs:
            cur = {k: g(r) for k, g in getters.items()}
            if t < start:
                res["start"] = cur
            elif prev is not None or res["start"]:
                base = prev if prev is not None else res["start"]
                for k in getters:
                    if cur[k] != base.get(k) and cur[k] is not None:
                        res["changes"].append({"at": t, "what": k, "from": base.get(k), "to": cur[k], "scan_id": r.get("scan_id")})
            if t >= start:
                res["scans"] += 1
            prev = cur
        return res

    st = lambda k: (lambda r: (r.get("structure") or {}).get(k))
    m5 = lambda k: (lambda r: (r.get("mtf_5m") or {}).get(k))
    return {"15m": track(rows(digests), {"choch_detected": st("choch_detected"), "bos_direction": st("bos_direction")}),
            "5m": track(rows(thesis), {"m5_was_choch": m5("m5_was_choch"), "m5_direction": m5("m5_direction"),
                                      "m5_relationship_to_htf": m5("m5_relationship_to_htf")})}


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


def reconstruct_window(start, end, candles, events=None, history=None, ledger_records=None, signals=None, now=None, candles_1h=None, digests=None, thesis=None):
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
    HOUR = timedelta(hours=1)
    c1, _bad1 = _candles(candles_1h, start - HOUR + BAR, end)           # an hourly candle that opened just before `start` still covers its first minutes
    c1 = [c for c in c1 if c["t"] + HOUR > start]
    rep["structure"] = _structure_changes(digests, thesis, start, end)
    rep["cond_5m"] = _conditions(cs, BAR)
    rep["cond_1h"] = _conditions(c1, HOUR)
    rep["cond_1h_has_archive"] = bool(candles_1h)
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


def _condition_lines(rep):
    """'Market conditions' for the 5-minute and the 1-hour. Same two-kinds-of-evidence rule: rebuilt later from saved candles."""
    c5, c1 = rep.get("cond_5m"), rep.get("cond_1h")
    if not c5 and not c1:
        return []
    out = ["", "Market conditions (rebuilt now from my saved candles, with no interpretation):"]
    if c5:
        out.append(f"  5-minute: {c5['n']} candles, {c5['up']} closed up and {c5['down']} closed down"
                   + (f" ({c5['flat']} unchanged)" if c5["flat"] else "") +
                   f". A typical candle ran {c5['avg_candle_pips']} pips; the biggest was {c5['biggest_pips']} pips at {timeref.hm(c5['biggest_at'])}.")
        if c5.get("busiest_hour") and c5["busiest_hour"] != c5["quietest_hour"] and c5["busiest_avg"] != c5["quietest_avg"]:
            out.append(f"    Busiest hour: {timeref.hm(c5['busiest_hour'])} (candles averaged {c5['busiest_avg']} pips). "
                       f"Quietest: {timeref.hm(c5['quietest_hour'])} ({c5['quietest_avg']} pips).")
    else:
        out.append("  5-minute: no saved candles for this stretch.")
    if c1:
        out.append(f"  1-hour: {c1['n']} hourly candle(s), {c1['up']} closed up and {c1['down']} closed down; they opened at {_p(c1['open'])} and the last "
                   f"closed at {_p(c1['close'])} ({'+' if c1['net_pips'] > 0 else ''}{c1['net_pips']} pips). Range {c1['range_pips']} pips: "
                   f"high {_p(c1['high'])} at {timeref.hm(c1['high_at'])}, low {_p(c1['low'])} at {timeref.hm(c1['low_at'])}.")
        if c1["n"] <= 10:
            out.append("    By hour: " + "; ".join(f"{timeref.hm(c['t'])} {'up' if c['net_pips'] > 0 else 'down' if c['net_pips'] < 0 else 'flat'} "
                                                   f"{abs(c['net_pips'])} (range {c['pips']})" for c in c1["candles"]) + " (pips).")
        if rep.get("now") is not None and c1["partial_last"] > min(rep["end"], rep["now"]):
            out.append("    The last hourly candle was still forming at the end of this stretch, so its figures are partial.")
    elif rep.get("cond_1h_has_archive"):
        out.append("  1-hour: none of my saved hourly candles fall in this stretch.")
    else:
        out.append("  1-hour: I don't have a saved hourly candle archive to read from yet.")
    if c5 and c1:
        a, b = ("up" if c5["net_pips"] > 0 else "down" if c5["net_pips"] < 0 else "flat"), ("up" if c1["net_pips"] > 0 else "down" if c1["net_pips"] < 0 else "flat")
        out.append("  Both timeframes show price " + ("moving " + a if a == b and a != "flat" else "roughly flat") + " over it." if a == b
                   else f"  The two disagree on direction: the 5-minute candles show price {a}, the hourly ones {b}.")
    return out


_WORD = {"choch_detected": "a change of character", "m5_was_choch": "a change of character"}


def _structure_lines(rep):
    """What the engine recorded for BOS / CHoCH. Honest about which timeframes it records and how often it looks."""
    sc = rep.get("structure")
    if not sc:
        return []
    out = ["", "Structure, as the engine recorded it (it saves one reading per scan, about every 5 minutes; a flip that came and went between two scans isn't in it):"]
    def tell(c):
        w = c["what"]
        if w in ("choch_detected", "m5_was_choch"):
            return f"{timeref.hm(c['at'])} a change of character was flagged" if c["to"] else f"{timeref.hm(c['at'])} the change-of-character flag cleared"
        if w == "bos_direction":
            return f"{timeref.hm(c['at'])} the break-of-structure direction went from {str(c['from']).lower()} to {str(c['to']).lower()}"
        if w == "m5_direction":
            return f"{timeref.hm(c['at'])} the 5-minute direction went from {str(c['from']).lower()} to {str(c['to']).lower()}"
        return f"{timeref.hm(c['at'])} the 5-minute read against the 1-hour bias went from {str(c['from']).lower()} to {str(c['to']).lower()}"
    for tf, label in (("5m", "5-minute"), ("15m", "15-minute")):
        d = sc[tf]
        if not d["scans"]:
            out.append(f"  {label}: no saved scan readings in this stretch, so I can't say.")
        elif not d["changes"]:
            out.append(f"  {label}: no change of character or break-of-structure change recorded across {d['scans']} scan(s).")
        else:
            out.append(f"  {label}: " + "; ".join(tell(c) for c in d["changes"][:6]) + (f" (and {len(d['changes']) - 6} more)" if len(d["changes"]) > 6 else "") + ".")
    eng = rep["engine"]
    bias = [c for c in eng["changes"] if c["field"] == "htf_bias"]
    if bias:
        out.append("  1-hour: " + "; ".join(f"{timeref.hm(c['at'])} bias went from {c['from'] or 'not recorded'} to {c['to'] or 'not recorded'}" for c in bias[:4]) + ".")
    elif eng["start_state"] or eng["n_in_window"]:
        out.append("  1-hour: my recorded bias didn't change. The engine doesn't record a separate 1-hour change of character or break of structure.")
    else:
        out.append("  1-hour: no saved readings in this stretch. The engine doesn't record a separate 1-hour change of character or break of structure.")
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
    out += _condition_lines(rep)
    out += _structure_lines(rep)
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
