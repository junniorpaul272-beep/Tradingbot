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
from datetime import datetime, timedelta, timezone

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
    scan_ts = sorted(t for t in (_t(r.get("scanned_at")) for r in (digests or []) if isinstance(r, dict)) if t is not None)
    before_start = [t for t in scan_ts if t <= start]
    after_end = [t for t in scan_ts if t >= end]
    inwin = ([before_start[-1]] if before_start else []) + [t for t in scan_ts if start < t < end] + ([after_end[0]] if after_end else [])
    rep["scan_gaps"] = [(a, b) for a, b in zip(inwin, inwin[1:]) if b - a > SCAN_MAX_GAP and b > start and a < end]
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


def _structure_lines(rep, limit=6):
    """What the engine recorded for BOS / CHoCH. Honest about which timeframes it records and how often it looks."""
    sc = rep.get("structure")
    if not sc:
        return []
    out = ["", "Structure, as the engine recorded it (it saves one reading per scan, about every 5 minutes; a flip that came and went between two scans isn't in it):"]
    def tell(c):
        w = c["what"]
        if w in ("choch_detected", "m5_was_choch"):
            return f"{timeref.hm(c['at'])} a change of character was flagged" if c["to"] else f"{timeref.hm(c['at'])} the change-of-character flag cleared"
        fr = "no recorded leg" if c["from"] in (None, "") else str(c["from"]).lower()
        if w == "bos_direction":
            return f"{timeref.hm(c['at'])} the direction of the latest 15-minute break went from {fr} to {str(c['to']).lower()}"
        if w == "m5_direction":
            return f"{timeref.hm(c['at'])} the 5-minute direction went from {fr} to {str(c['to']).lower()}"
        return f"{timeref.hm(c['at'])} the 5-minute read against the 1-hour bias went from {fr} to {str(c['to']).lower()}"
    for tf, label in (("5m", "5-minute"), ("15m", "15-minute")):
        d = sc[tf]
        if not d["scans"]:
            out.append(f"  {label}: no saved scan readings in this stretch, so I can't say.")
        elif not d["changes"]:
            out.append(f"  {label}: no change of character or change in the direction of the latest break recorded across {d['scans']} scan(s).")
        else:
            out.append(f"  {label}: " + "; ".join(tell(c) for c in d["changes"][:limit]) + (f" (and {len(d['changes']) - limit} more; say 'details' for all of them)" if len(d["changes"]) > limit else "") + ".")
    eng = rep["engine"]
    bias = [c for c in eng["changes"] if c["field"] == "htf_bias"]
    if bias:
        out.append("  1-hour: " + "; ".join(f"{timeref.hm(c['at'])} bias went from {c['from'] or 'not recorded'} to {c['to'] or 'not recorded'}" for c in bias[:4]) + ".")
    elif eng["start_state"] or eng["n_in_window"]:
        out.append("  1-hour: my recorded bias didn't change. The engine doesn't record a separate 1-hour change of character or break of structure.")
    else:
        out.append("  1-hour: no saved readings in this stretch. The engine doesn't record a separate 1-hour change of character or break of structure.")
    return out


def render_window_brief(rep, assumptions=()):
    """The short chronological answer: what price did, the few structure events, what the engine had recorded going in, and every material evidence
    gap. The full timeline is one word away ('details'). Never shorter by hiding uncertainty."""
    if rep["status"] in ("FUTURE", "EMPTY_WINDOW"):
        return render_window(rep, assumptions)
    s, e = rep["start"], rep["end"]
    out = [f"{_day(s)}, {_span(s, e)} Lagos time."]
    if assumptions:
        out.append("(" + "; ".join(a[0].upper() + a[1:] for a in assumptions) + ".)")
    pr = rep["price"]
    out.append("")
    if pr:
        direction = "up" if pr["net"] > 0 else "down" if pr["net"] < 0 else "flat"
        move = "didn't move net" if direction == "flat" else f"{direction} {abs(pr['net_pips'])} pips net"
        out.append(f"Price (rebuilt now from my saved candles, not necessarily what I knew then) went from {_p(pr['open'])} to {_p(pr['close'])}, {move}. "
                   f"The high was {_p(pr['high'])} at {timeref.hm(pr['high_at'])} and the low {_p(pr['low'])} at {timeref.hm(pr['low_at'])}.")
    elif rep["archive_from"] is None:
        out.append("I don't have a candle archive to look in yet, so I can't tell you what price did.")
    else:
        out.append(f"I have no saved candles for that stretch (my archive covers {_day(rep['archive_from'])} {timeref.hm(rep['archive_from'])} "
                   f"to {_day(rep['archive_to'])} {timeref.hm(rep['archive_to'])}), so I can't tell you what price did.")
    out += _condition_lines(rep)                                   # Nelly asked for 5-minute and 1-hour conditions: kept in the short version
    out += _structure_lines(rep, limit=3)
    eng = rep["engine"]
    st = eng.get("start_state")
    if st:
        out.append("")
        out.append(f"Going in, my last saved reading ({timeref.hm(st['as_of'])}) had the 1-hour bias as {st['htf_bias'] or 'not recorded'} and the phase as "
                   f"{st['market_phase'] or 'not recorded'}.")
    gaps = []
    if pr:
        gl = _gap_text(rep)
        gaps += [x.strip() for x in gl[:2]]
    for a, b in (rep.get("scan_gaps") or [])[:3]:
        gaps.append(f"No saved scans between {timeref.hm(a)} and {timeref.hm(b)} Lagos time, so anything that came and went in there isn't in my records.")
    if not st and not eng["changes"] and not eng["n_in_window"]:
        gaps.append("I have no saved state readings for that stretch, so I can't say what the bot was seeing then.")
    if gaps:
        out.append("")
        out.append("Gaps in my records:")
        out += ["  " + g for g in gaps]
    out.append("")
    out.append("That's the short version. Say 'details' for the full timeline, including your own notes and every recorded change.")
    return "\n".join(out)


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


# ---------------------------------------------------------------------------------------------------------------------------------
# "when was the last change of character?" — a LOOKUP in the engine's own saved records, not a market reading.
#   15-minute  structure_digest_log: the choch_detected flag; bos_direction
#   5-minute   thesis_context_log: mtf_5m.m5_was_choch
#   1-hour     state history: the 1-hour leg flipping direction (the engine's own definition of a CHoCH on the leg: "new dominant leg IS a
#              direction flip")
#   zones      zone_log: each fair value gap / order block the first scan it was seen
# Times are when a scan FIRST SAW it (about every 5 minutes); a flag has no direction recorded except where the leg itself carries one.
# ---------------------------------------------------------------------------------------------------------------------------------
def _flag_runs(rows, getter, upto):
    """rows: [(time, record)] sorted. -> (last run that started as False->True) as {"at","scan_id","still","cleared_at"} or None, and the span."""
    last, prev, run = None, None, None
    for t, r in rows:
        if t > upto:
            break
        cur = getter(r)
        if cur is True and prev is False:
            run = {"at": t, "scan_id": r.get("scan_id"), "still": True, "cleared_at": None}
            last = run
        elif cur is False and prev is True and run is not None:
            run["still"], run["cleared_at"] = False, t
        if cur is not None:
            prev = cur
    first_true = next((t for t, r in rows if getter(r) is True), None)
    return last, first_true


def _all_runs(rows, getter, upto):
    """Every run that started as False->True, oldest first: [{"at","scan_id","still","cleared_at"}]."""
    runs, prev, cur_run = [], None, None
    for t, r in rows:
        if t > upto:
            break
        cur = getter(r)
        if cur is True and prev is False:
            cur_run = {"at": t, "scan_id": r.get("scan_id"), "still": True, "cleared_at": None}
            runs.append(cur_run)
        elif cur is False and prev is True and cur_run is not None:
            cur_run["still"], cur_run["cleared_at"] = False, t
        if cur is not None:
            prev = cur
    return runs


def _choch_runs(rows, flag_get, dir_get, upto):
    """The engine's CHoCH flag describes the CURRENT leg, not a one-off pulse: it stays true while the leg stands, and a second flip in the other
    direction leaves it true. So a new change of character is a scan where the flag is true and EITHER it was false before OR the leg direction
    is not the one the previous scan saw. Each run also carries how long the scanner had been silent just before it (a gap means the event may
    be older than the scan that first saw it). -> [{"at","scan_id","direction","still","cleared_at","gap_min"}], oldest first."""
    runs, prev_flag, prev_dir, prev_t, cur = [], None, None, None, None
    for t, r in rows:
        if t > upto:
            break
        f, d = flag_get(r), dir_get(r)
        if f is True and (prev_flag is False or (prev_flag is True and d in ("BULLISH", "BEARISH") and prev_dir in ("BULLISH", "BEARISH") and d != prev_dir)):
            cur = {"at": t, "scan_id": r.get("scan_id"), "direction": d, "still": True, "cleared_at": None,
                   "gap_min": int((t - prev_t).total_seconds() // 60) if prev_t else None, "after_scan": prev_t}
            runs.append(cur)
        elif f is False and prev_flag is True and cur is not None and cur["still"]:
            cur["still"], cur["cleared_at"] = False, t
        if f is not None:
            prev_flag = f
        if d in ("BULLISH", "BEARISH"):
            prev_dir = d
        prev_t = t
    return runs


def dedupe_zones(zones):
    """The engine's zone log says each zone is logged once, but 16 of 128 order blocks in the real log appear 2-4 times with identical prices (they
    drop out of the active list and come back). 'First seen' is the EARLIEST row for an identity key; later copies are the same zone, not new ones."""
    best = {}
    for z in zones or []:
        if not isinstance(z, dict):
            continue
        k = z.get("identity_key") or (z.get("zone_type"), z.get("direction"), z.get("formation_time"), z.get("high"), z.get("low"))
        t = _t(z.get("scanned_at"))
        if t is None:
            continue
        if k not in best or t < best[k][0]:
            best[k] = (t, z)
    return [z for _, z in sorted(best.values(), key=lambda x: x[0])]


def last_structure_events(digests, thesis, zones, history, now):
    """-> dict of the most recent structure events on record, by source. Pure; reads only what it is handed, nothing after `now`."""
    def rows(raw, key="scanned_at"):
        out = []
        for r in raw or []:
            t = _t(r.get(key)) if isinstance(r, dict) else None
            if t is not None and t <= now:
                out.append((t, r))
        return sorted(out, key=lambda x: x[0])

    dg, th = rows(digests), rows(thesis)
    res = {"now": now, "from_15m": dg[0][0] if dg else None, "to_15m": dg[-1][0] if dg else None,
           "from_5m": th[0][0] if th else None, "to_5m": th[-1][0] if th else None}
    st = lambda k: (lambda r: (r.get("structure") or {}).get(k))
    flag15 = lambda r: (r.get("structure") or {}).get("choch_detected") if isinstance((r.get("structure") or {}).get("choch_detected"), bool) else None
    flag5 = lambda r: (r.get("mtf_5m") or {}).get("m5_was_choch") if isinstance((r.get("mtf_5m") or {}).get("m5_was_choch"), bool) else None
    res["runs_choch_15m"] = _choch_runs(dg, flag15, lambda r: (r.get("structure") or {}).get("bos_direction"), now)
    res["runs_choch_5m"] = _choch_runs(th, flag5, lambda r: (r.get("mtf_5m") or {}).get("m5_direction"), now)
    res["choch_15m"] = res["runs_choch_15m"][-1] if res["runs_choch_15m"] else None
    res["choch_5m"] = res["runs_choch_5m"][-1] if res["runs_choch_5m"] else None
    for side in ("high", "low"):
        g = (lambda s: (lambda r: (r.get("liquidity") or {}).get(f"swing_{s}_swept") if isinstance((r.get("liquidity") or {}).get(f"swing_{s}_swept"), bool) else None))(side)
        runs = _all_runs(dg, g, now)
        for run in runs:
            row = next((r for t, r in dg if t == run["at"]), {})
            run["pips"] = (row.get("liquidity") or {}).get(f"swing_{side}_sweep_distance_pips")
        res["runs_sweep_" + side] = runs
        res["sweep_recorded_" + side] = any(g(r) is not None for _, r in dg)
    last_bos, prev = None, None
    for t, r in dg:
        d = st("bos_direction")(r)
        if d in ("BULLISH", "BEARISH"):
            if prev is not None and d != prev:
                last_bos = {"at": t, "from": prev, "to": d, "scan_id": r.get("scan_id")}
            prev = d
    res["bos_15m"], res["bos_now"] = last_bos, prev
    bos_changes, p2 = [], None
    for t, r in dg:
        d = st("bos_direction")(r)
        if d in ("BULLISH", "BEARISH"):
            if p2 is not None and d != p2:
                bos_changes.append({"at": t, "from": p2, "to": d})
            p2 = d
    res["bos_changes"] = bos_changes
    snaps = sorted(((_t(r["ts"]), r["payload"]) for r in ledger.view_as_of(history or [], now)
                    if r.get("event_type") == "STATE_SNAPSHOT" and r.get("superseded_by") is None and _t(r.get("ts")) is not None), key=lambda x: x[0])
    flip, prev_leg = None, None
    for t, p in snaps:
        leg = p.get("macro_leg") if isinstance(p, dict) else None
        d = leg.get("direction") if isinstance(leg, dict) else None
        if d in ("BULLISH", "BEARISH"):
            if prev_leg is not None and d != prev_leg:
                flip = {"at": t, "from": prev_leg, "to": d, "origin_time": _t(leg.get("origin_time"))}
            prev_leg = d
    res["leg_flip_1h"], res["leg_now_1h"] = flip, prev_leg
    res["from_1h"], res["to_1h"] = (snaps[0][0], snaps[-1][0]) if snaps else (None, None)
    zrows = rows(dedupe_zones(zones))
    for key, zt in (("fvg", "FVG"), ("ob", "ORDER_BLOCK")):
        res["zones_all_" + key] = [{"at": t, "direction": z.get("direction"), "high": z.get("high"), "low": z.get("low")}
                                   for t, z in zrows if z.get("zone_type") == zt]
        hit = None
        for t, z in zrows:
            if z.get("zone_type") == zt:
                hit = {"at": t, "key": z.get("identity_key"), "direction": z.get("direction"), "high": z.get("high"), "low": z.get("low"),
                       "formation_time": _t(z.get("formation_time")), "scan_id": z.get("scan_id")}
        res["zone_" + key] = hit
    res["from_zone"] = zrows[0][0] if zrows else None
    return res


def _when(dt, now):
    """'today 14:35' / 'yesterday 09:10' / 'Mon 20 Oct 14:35', in Lagos time, from a UTC datetime."""
    a, b = timeref.lagos(dt), timeref.lagos(now)
    days = (b.date() - a.date()).days
    if days == 0:
        return f"today at {timeref.hm(dt)}"
    if days == 1:
        return f"yesterday at {timeref.hm(dt)}"
    return f"{a:%A %d %b} at {timeref.hm(dt)}"


def render_last_events(res, wants):
    """Plain words for the last structure event(s) asked about. Says what each source can and cannot tell, and where its records begin."""
    now = res["now"]
    out = []
    if "choch" in wants:
        lines = []
        c15, c5, lf = res["choch_15m"], res["choch_5m"], res["leg_flip_1h"]
        def one(label, c, since):
            if c:
                d = f" into a {c['direction'].lower()} leg" if c.get("direction") in ("BULLISH", "BEARISH") else ""
                state = "and it is still showing now" if c["still"] else f"and the flag cleared at {timeref.hm(c['cleared_at'])}"
                gap = (f" (my scans skipped {c['gap_min']} minutes just before it, so it could have happened any time after {timeref.hm(c['after_scan'])})"
                       if isinstance(c.get("gap_min"), int) and c["gap_min"] > 7 and c.get("after_scan") else "")
                if c["still"] and res.get("asof_anchor"):
                    state = f"and it was still showing when the next one was first seen at {timeref.hm(res['asof_anchor'])}"
                return f"{label}: first seen by my scan {_when(c['at'], now)}{d}, {state}{gap}."
            if since:
                return f"{label}: no new change of character seen in my saved scans since {_when(since, now)}."
            return None
        l15 = one("15-minute", c15, res["from_15m"]) or "15-minute: I have no saved scans to look in."
        lines.append(l15)
        l5 = one("5-minute", c5, res["from_5m"])
        if l5:
            lines.append(l5)
        if lf:
            lines.append(f"1-hour: the engine's 1-hour leg changed direction, from {lf['from'].lower()} to {lf['to'].lower()}, first seen {_when(lf['at'], now)}. "
                         "That is the closest thing I have to a 1-hour change of character, but it is only a change of leg direction, not the engine's own "
                         "change-of-character flag, so treat it as a rough match.")
        elif res["from_1h"]:
            lines.append(f"1-hour: the engine's 1-hour leg hasn't changed direction in my records since {_when(res['from_1h'], now)}.")
        body = ["The last change of character I have on record:"] + ["  " + x for x in lines]
        if c15 or c5 or lf:
            body.append("  'First seen' is the time of the scan that noticed it, which is usually a minute or two after the candle that caused it closed; "
                        "the exact candle isn't saved.")
        out += body
    if "bos" in wants:
        b = res["bos_15m"]
        if b:
            out.append(f"The direction of the engine's latest 15-minute break (a break of structure or a change of character) last changed {_when(b['at'], now)}, "
                       f"from {b['from'].lower()} to {b['to'].lower()}." + (f" It reads {res['bos_now'].lower()} now." if res.get("bos_now") else "")
                       + " I can only see changes of direction: a break that carried on the same way isn't saved as its own event, so there may have been later ones.")
        elif res["from_15m"]:
            out.append(f"The direction of the engine's latest 15-minute break hasn't changed in my saved scans since {_when(res['from_15m'], now)}"
                       + (f"; it reads {res['bos_now'].lower()}." if res.get("bos_now") else "."))
        else:
            out.append("I have no saved 15-minute scans to look in for a break of structure.")
    if "sweep" in wants:
        lines = []
        for side, word in (("high", "swing high"), ("low", "swing low")):
            runs = res.get("runs_sweep_" + side) or []
            if runs:
                r = runs[-1]
                state = "and it is still showing now" if r["still"] else f"and it cleared at {timeref.hm(r['cleared_at'])}"
                far = f" by {_p1(r['pips'])} pips" if isinstance(r.get("pips"), (int, float)) else ""
                lines.append(f"The engine's sweep check against the {word} first came up true {_when(r['at'], now)}{far}, {state}.")
            elif res.get("sweep_recorded_" + side):
                lines.append(f"No sweep of the {word} in my saved scans since {_when(res['from_15m'], now)}.")
            elif res["from_15m"]:
                lines.append(f"My saved scans from {_when(res['from_15m'], now)} don't include a reading of whether the {word} was swept, so I can't say.")
        if not lines:
            lines.append("I have no saved scans to look in for a sweep.")
        out += ["The last sweep I have on record:"] + ["  " + x for x in lines]
        out.append("  That is the engine's own sweep-and-reclaim check (price poked past the level and closed back, judged in the direction of the bias), run "
                   "on whichever swing high and low were current at the time. I can't see which level it was if the swing point moved, a check that stays true "
                   "for several scans shows once, and I can't say what price did afterwards.")
    for key, name in (("fvg", "fair value gap"), ("ob", "order block")):
        if key in wants:
            z = res.get("zone_" + key)
            if z:
                form = f" It formed on the 15-minute candle at {timeref.hm(z['formation_time'])} Lagos time." if z.get("formation_time") else ""
                out.append(f"The last {name} the engine logged was {str(z['direction']).lower()}, first seen {_when(z['at'], now)}, covering "
                           f"{_p(z['low'])} to {_p(z['high'])}.{form}")
            elif res.get("from_zone"):
                out.append(f"No {name} logged in my zone records since {_when(res['from_zone'], now)}.")
            else:
                out.append(f"I have no zone records to look in for a {name}.")
    return "\n".join(out)


def event_anchor(res, wants):
    """The time to carry on from for 'and after that': the most recent event of the kind asked about, else None."""
    c = []
    if "choch" in wants:
        pick = next((x for x in (res["choch_15m"], res["choch_5m"], res["leg_flip_1h"]) if x), None)      # the 15-minute one is the main read
        if pick:
            c.append(pick["at"])
    if "bos" in wants and res["bos_15m"]:
        c.append(res["bos_15m"]["at"])
    if "sweep" in wants:
        c += [r[-1]["at"] for r in (res.get("runs_sweep_high"), res.get("runs_sweep_low")) if r]
    for key in ("fvg", "ob"):
        if key in wants and res.get("zone_" + key):
            c.append(res["zone_" + key]["at"])
    return max(c) if c else None


def _p1(x):
    return f"{float(x):.1f}"


def render_compare_prev(res, wants):
    """Compare the latest event of a kind with the one before it: times and the gap between them, only what is recorded. Says so when
    there is no earlier one. Never adds an interpretation."""
    now = res["now"]
    key = (wants or ["choch"])[0]
    series = []
    if key == "choch":
        series = [("15-minute change of character", res.get("runs_choch_15m") or []), ("5-minute change of character", res.get("runs_choch_5m") or [])]
    elif key == "sweep":
        series = [("swing high sweep", res.get("runs_sweep_high") or []), ("swing low sweep", res.get("runs_sweep_low") or [])]
    elif key in ("fvg", "ob"):
        series = [("fair value gap" if key == "fvg" else "order block", res.get("zones_all_" + key) or [])]
    elif key == "bos":
        return "For the break of structure I only keep its current direction and when it changed, so there's no list of separate breaks to compare."
    out = []
    for name, runs in series:
        if len(runs) < 2:
            out.append(f"{name}: " + ("only one on record, so there's nothing earlier to compare it with." if runs else "none on record."))
            continue
        a, b = runs[-2], runs[-1]
        gap = b["at"] - a["at"]
        mins = int(gap.total_seconds() // 60)
        span = f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"
        line = f"{name}: latest {_when(b['at'], now)}; the one before {_when(a['at'], now)}; {span} apart."
        if "high" in b and "low" in b:
            line += (f" Latest covered {_p(b['low'])} to {_p(b['high'])} ({str(b.get('direction')).lower()}); the earlier one {_p(a['low'])} to {_p(a['high'])} "
                     f"({str(a.get('direction')).lower()}).")
        out.append(line)
    out.append("That's what the records show; they don't say whether one mattered more than the other.")
    return "\n".join(out)


SCAN_MAX_GAP = timedelta(minutes=7)      # real scans come every 5.0 min (99% within 6): a longer wait means a scan was missed


def zone_state_at(digests, zones, when, kind, now):
    """Was price inside the engine's fair value gap / order block at `when`? Reads ONLY the saved scan at (or just before) that moment: never today's
    zone or today's price. -> dict(ok, text). Says plainly when there is no scan close enough, when the engine had no zone in view, and that the
    saved flag does not name which zone it meant (it carries no id), listing the candidates logged by then."""
    name = "fair value gap" if kind == "fvg" else "order block"
    rows = []
    for r in digests or []:
        t = _t(r.get("scanned_at"))
        if t is not None and t <= now:
            rows.append((t, r))
    rows.sort(key=lambda x: x[0])
    before = [x for x in rows if x[0] <= when]
    after = [x for x in rows if x[0] > when]
    if not rows:
        return {"ok": False, "text": f"I have no saved scans to look in, so I can't say whether price was inside a {name}."}
    if not before:
        return {"ok": False, "text": f"My saved scans start at {_when(rows[0][0], now)}, after the time you mean, so I can't say."}
    t, r = before[-1]
    gap = when - t
    if gap > SCAN_MAX_GAP:
        nxt = f" and the next one was {_when(after[0][0], now)}" if after else ""
        return {"ok": False, "text": f"I have no scan close enough to {_when(when, now)}: my nearest earlier one was {_when(t, now)}{nxt}. "
                                      f"I won't use a later reading, or today's, to answer for that moment."}
    loc = r.get("location") or {}
    key_present, key_in, key_dist = (("fvg_present", "price_in_fvg", "fvg_distance_pips") if kind == "fvg"
                                     else ("order_block_present", "price_in_order_block", "ob_distance_pips"))
    present, inside, dist = loc.get(key_present), loc.get(key_in), loc.get(key_dist)
    if present is None or inside is None:
        return {"ok": False, "text": f"The scan {_when(t, now)} didn't record a {name} reading, so I can't say."}
    head = f"From the scan {_when(t, now)}"
    if gap >= timedelta(minutes=2):             # say how far the evidence is from the moment asked about: approximate is not exact
        head += f" ({int(gap.total_seconds() // 60)} minutes before the time you asked about; the nearest scan up to then)"
    if not present:
        return {"ok": True, "text": f"{head}: the engine had no {name} in view, so there was nothing for price to be inside."}
    cands = []
    zt = "FVG" if kind == "fvg" else "ORDER_BLOCK"
    for z in dedupe_zones(zones):
        zs = _t(z.get("scanned_at"))
        if z.get("zone_type") == zt and zs is not None and zs <= t and t - zs <= timedelta(hours=6):
            cands.append((zs, z))
    cands.sort(key=lambda x: x[0])
    state = ("price was inside it" if inside else
             (f"price was outside it, about {dist} pips away" if isinstance(dist, (int, float)) else "price was outside it"))
    zone_out = None
    txt = f"{head}: the engine had {'an' if name[0] in 'aeiou' else 'a'} {name} in view and {state}."
    if cands:
        zs, z = cands[-1]
        zone_out = {"key": z.get("identity_key"), "at": zs}
        txt += (f" The most recent one it had logged by then was {str(z.get('direction')).lower()}, first seen {_when(zs, now)}, covering "
                f"{_p(z.get('low'))} to {_p(z.get('high'))}.")
        if len(cands) > 1:
            txt += (f" It had logged {len(cands)} in the six hours before, and the saved reading doesn't say which one it refers to, so that is the latest, "
                    "not a confirmed match.")
        else:
            txt += " The saved reading doesn't name the zone, so that is the only candidate, not a confirmed match."
    else:
        txt += " I can't tell which one it was: none is logged in the six hours before that scan."
    return {"ok": True, "text": txt, "zone": zone_out}


def _lagos_day_start(now):
    a = timeref.lagos(now)
    return a.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(now.tzinfo)


def event_window(now, part=None, window=None):
    """-> (start, end, label) in UTC for a list/first/count question: an explicit window, the morning or afternoon of today (Lagos), or today so far."""
    day0 = _lagos_day_start(now)
    if window is not None:
        return window.start, min(window.end, now), f"between {timeref.hm(window.start)} and {timeref.hm(window.end)} Lagos time"
    if part == "morning":
        return day0, min(day0 + timedelta(hours=12), now), "this morning (Lagos time, before 12:00)"
    if part == "afternoon":
        return day0 + timedelta(hours=12), min(day0 + timedelta(hours=18), now), "this afternoon (Lagos time, 12:00 to 18:00)"
    return day0, now, "today so far (Lagos time)"


def render_event_list(res, wants, mode, start, end, label):
    """First / count / list of the events a question asked about within a stretch. Same labelling and caveats as render_last_events."""
    now = res["now"]
    def inside(t):
        return start <= t <= end
    def plural(n, word):
        return f"{n} {word}" + ("" if n == 1 else "s")
    out = []
    def emit(name, items, fmt, recorded_from, caveat=None):
        sel = [x for x in items if inside(x["at"])]
        if mode == "first":
            sel = sel[:1]
        if not sel:
            if recorded_from is None:
                out.append(f"{name}: I have no saved scans to look in.")
            elif recorded_from > start:
                out.append(f"{name}: none in my saved scans {label}, but my scans only start at {_when(recorded_from, now)}.")
            else:
                out.append(f"{name}: none seen in my saved scans {label}.")
            return
        head = (f"{name}: {plural(len(sel), 'event')} {label}" if mode == "count" else f"{name} {label}")
        out.append(head + ": " + "; ".join(fmt(x) for x in sel) + ".")
        if caveat:
            out.append("  " + caveat)
    d_fmt = lambda x: f"{timeref.hm(x['at'])}" + (f" (into a {x['direction'].lower()} leg)" if x.get("direction") in ("BULLISH", "BEARISH") else "")
    if "choch" in wants:
        emit("15-minute change of character", res.get("runs_choch_15m") or [], d_fmt, res.get("from_15m"),
             "Times are when my scan first saw each one; the flag describes the current leg, so a new one is counted when the leg's direction changes.")
        emit("5-minute change of character", res.get("runs_choch_5m") or [], d_fmt, res.get("from_5m"))
    if "bos" in wants:
        emit("Changes in the direction of the engine's latest 15-minute break", res.get("bos_changes") or [],
             lambda x: f"{timeref.hm(x['at'])} ({x['from'].lower()} to {x['to'].lower()})", res.get("from_15m"),
             "A break that carried on the same way isn't saved as its own event, so this is not a list of every break.")
    if "sweep" in wants:
        for side, word in (("high", "swing high"), ("low", "swing low")):
            if res.get("sweep_recorded_" + side):
                emit(f"The engine's sweep check against the {word}", res.get("runs_sweep_" + side) or [],
                     lambda x: f"{timeref.hm(x['at'])}" + (f" ({x['pips']} pips)" if isinstance(x.get("pips"), (int, float)) else ""), res.get("from_15m"))
            else:
                out.append(f"The engine's sweep check against the {word}: my saved scans don't include a reading, so I can't say.")
        out.append("  That is the engine's own sweep-and-reclaim check, run on whichever swing point was current; a check that stays true for several scans counts once.")
    for key, name in (("fvg", "fair value gap"), ("ob", "order block")):
        if key in wants:
            emit(f"{name[0].upper() + name[1:]}s logged", res.get("zones_all_" + key) or [],
                 lambda x: f"{timeref.hm(x['at'])} ({str(x.get('direction')).lower()}, {_p(x['low'])} to {_p(x['high'])})", res.get("from_zone"))
    return "\n".join(out)


def event_times(res, wants):
    """-> [(label, time)] the most recent recorded event of each kind asked about, oldest first. Used to show the ORDER things were recorded in."""
    out = []
    if "choch" in wants:
        for lab, k in (("the last 15-minute change of character", "choch_15m"), ("the last 5-minute change of character", "choch_5m")):
            if res.get(k):
                out.append((lab + " was first seen", res[k]["at"]))
    if "bos" in wants and res.get("bos_15m"):
        out.append(("the last change in the direction of the 15-minute break was first seen", res["bos_15m"]["at"]))
    if "sweep" in wants:
        for side in ("high", "low"):
            r = (res.get("runs_sweep_" + side) or [])
            if r:
                out.append((f"the engine's sweep check against the swing {side} first came up true", r[-1]["at"]))
    for key, name in (("fvg", "fair value gap"), ("ob", "order block")):
        if key in wants and res.get("zone_" + key):
            out.append((f"the last {name} was first logged", res["zone_" + key]["at"]))
    return sorted(out, key=lambda x: x[1])



# ---------------------------------------------------------------- facts for discussing an idea
IDEA_WINDOW = timedelta(minutes=60)


def idea_facts(res, digests, candles15, now, thesis=None):
    """Descriptive counts from the saved records, for talking about an idea: what happened around each 15-minute change of character the engine
    flagged. Observed only: no averages of outcomes are offered as evidence, and every row says what could not be checked. -> dict."""
    rows = sorted(((_t(r.get("scanned_at")), r) for r in digests or [] if isinstance(r, dict) and _t(r.get("scanned_at")) is not None
                   and _t(r.get("scanned_at")) <= now), key=lambda x: x[0])
    cs, _bad = _candles(candles15, datetime.min.replace(tzinfo=timezone.utc), now)
    out = {"scans": len(rows), "first": rows[0][0] if rows else None, "last": rows[-1][0] if rows else None, "events": [], "candles": len(cs)}
    out["days"] = len({timeref.lagos(t).date() for t, _ in rows})
    gaps = sorted((b[0] - a[0]).total_seconds() / 60 for a, b in zip(rows, rows[1:]))
    out["scan_median_min"] = round(gaps[len(gaps) // 2], 1) if gaps else None
    out["scan_max_gap_min"] = round(gaps[-1], 1) if gaps else None
    th = sorted(((_t(r.get("scanned_at")), r) for r in thesis or [] if isinstance(r, dict) and _t(r.get("scanned_at")) is not None and _t(r.get("scanned_at")) <= now), key=lambda x: x[0])
    side = {"BULLISH": ("swing_low_swept", "BULLISH"), "BEARISH": ("swing_high_swept", "BEARISH")}      # the sweep check is judged in the direction of the bias
    for run in (res or {}).get("runs_choch_15m") or []:
        t, d = run["at"], str(run.get("direction") or "").upper()
        if d not in side:
            continue
        flag, need_bias = side[d]
        near = [(rt, r) for rt, r in rows if t - IDEA_WINDOW <= rt <= t + IDEA_WINDOW]
        before = [r for rt, r in near if rt <= t]
        after = [r for rt, r in near if rt > t]
        checkable = any(((r.get("regime") or {}).get("macro_bias") or "").upper() == need_bias for r in before + after)
        here = next((r for rt, r in rows if rt == t), None) or (before[-1] if before else {})
        loc = (here.get("location") or {}) if isinstance(here, dict) else {}
        fwd = None
        later = [c for c in cs if c["t"] >= t]
        if len(later) >= 4:
            entry = later[0]["o"]; win = later[:4]
            sign = 1 if d == "BULLISH" else -1
            fwd = {"pips_1h": round(sign * (win[-1]["c"] - entry) * 10000, 1),
                   "best": round(sign * ((max(c["h"] for c in win) if sign > 0 else min(c["l"] for c in win)) - entry) * 10000, 1),
                   "worst": round(sign * ((min(c["l"] for c in win) if sign > 0 else max(c["h"] for c in win)) - entry) * 10000, 1)}
        vol = sess = None
        pv = [r for rt, r in th if rt <= t and t - rt <= timedelta(minutes=30)]
        if pv:
            vv = (pv[-1].get("mtf_15m") or {}).get("volatility_state")
            vol = str(vv).lower() if vv else None
            ss = (pv[-1].get("mtf_15m") or {}).get("session")
            sess = str(ss).lower() if ss else None
        scans_b = [rt for rt, _r in near if rt <= t]; scans_a = [rt for rt, _r in near if rt > t]
        pts = sorted(scans_b + scans_a)
        hole = round(max(((b - a).total_seconds() / 60 for a, b in zip(pts, pts[1:])), default=0), 1)
        out["events"].append({"at": t, "dir": d, "checkable": checkable, "vol": vol, "session": sess, "scans_before": len(scans_b), "scans_after": len(scans_a), "max_hole_min": hole,
                              "sweep_before": bool(checkable and any((r.get("liquidity") or {}).get(flag) for r in before)),
                              "sweep_after": bool(checkable and any((r.get("liquidity") or {}).get(flag) for r in after)),
                              "in_fvg": bool(loc.get("price_in_fvg")), "in_ob": bool(loc.get("price_in_order_block")),
                              "gap_min": run.get("gap_min"), "fwd": fwd})
    return out
