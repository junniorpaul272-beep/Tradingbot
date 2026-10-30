"""awareness.py - market-awareness measurements for GBPUSD from HOURLY candles. RESEARCH, read-only.

Not connected to the engine, BUS, signals or alerts. Pure functions: candles in, measurements out. Nothing here touches the network or a file
except the two small helpers at the bottom. See SPEC_MARKET_AWARENESS.md for every definition; DEFINITION_VERSION changes when one does.

POINT-IN-TIME RULE: an observation 'as of' trading day D uses only candles of days <= D. Recomputing it later, with more candles on disk,
must give the identical result (test_awareness.py checks this)."""
import json
import math

DEFINITION_VERSION = "awareness-v0.1-provisional"
PIP = 0.0001
EXPECTED_BARS = 24             # Mon-Thu: hourly candles in a complete day on this feed (observed; see SPEC section 3)
FRIDAY_MIN_LAST_HOUR = 19      # Friday: contiguous from 00:00 and last candle at or after 19:00 UTC. The audit of 6.7 years shows the weekly close sits at 19-23 UTC depending on year (provisional)
WINDOWS = (10, 20, 40)         # trading days (provisional; longer windows are added only as history accumulates)


def canon_ts(raw):
    """Canonical UTC timestamp 'YYYY-MM-DDTHH:MM:SS+00:00'. A timestamp with no offset is taken as UTC (the feed's stated convention);
    any other offset is converted to UTC. Raises on anything unparseable."""
    import datetime as dt
    t = dt.datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def clean_hourly(rows):
    """rows: iterable of dicts with datetime (ISO string), open, high, low, close.
    Returns (candles, report). Timestamps are converted to canonical UTC BEFORE weekday filtering, duplicate detection, sorting.
    candles: sorted list of (iso, o, h, l, c), Mon-Fri only. Identical duplicates collapse. CONFLICTING duplicates (same instant, different prices)
    are NOT resolved by picking one: that timestamp is removed from the candles, both versions are kept in report['conflicts'], and its day is
    treated as unusable (see to_days). Malformed rows are dropped and counted. Input order does not matter."""
    rep = {"rows": 0, "malformed": 0, "duplicates": 0, "conflicting_duplicates": 0, "weekend_dropped": 0, "kept": 0,
           "offset_converted": 0, "conflicts": []}
    versions, n = {}, {}
    for r in rows:
        rep["rows"] += 1
        try:
            raw = str(r["datetime"]).strip()
            t = canon_ts(raw); o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
            ok = all(math.isfinite(v) and v > 0 for v in (o, h, l, c)) and h >= max(o, c, l) and l <= min(o, c, h)
            wd = _weekday(t)
        except Exception:
            ok = False
        if not ok:
            rep["malformed"] += 1; continue
        if not raw.endswith(("Z", "+00:00")):
            rep["offset_converted"] += 1
        if wd >= 5:
            rep["weekend_dropped"] += 1; continue
        versions.setdefault(t, set()).add((o, h, l, c))
        n[t] = n.get(t, 0) + 1
    rep["duplicates"] = sum(v - 1 for v in n.values())
    out = []
    for t in sorted(versions):
        if len(versions[t]) > 1:
            rep["conflicting_duplicates"] += 1
            rep["conflicts"].append({"timestamp": t, "versions": sorted(versions[t])})
        else:
            (o, h, l, c), = versions[t]; out.append((t, o, h, l, c))
    rep["kept"] = len(out)
    rep["conflicted_days"] = sorted({x["timestamp"][:10] for x in rep["conflicts"]})
    return out, rep


def _weekday(iso):
    import datetime as dt
    return dt.datetime.fromisoformat(iso).weekday()


def to_days(candles, conflicted_days=()):
    """Group canonical-UTC hourly candles into trading days (UTC calendar date). Returns (days, blocked).
    A day is COMPLETE only if its hours run without a hole from 00:00 and, Mon-Thu, to 23:00; on Friday to a last candle at or after FRIDAY_MIN_LAST_HOUR
    (the real weekly close, which the 6.7-year audit shows moves between 19:00 and 23:00 UTC by year). Only complete days are used. Every other weekday between the first and last complete day is listed in
    blocked with its reason: 'missing', 'incomplete (n bars)' or 'conflicting candle data'. Nothing is filled. Days before the first / after the
    last complete day are not reported as gaps (coverage start/end are reported instead)."""
    import datetime as dt
    by = {}
    for t, o, h, l, c in candles:
        by.setdefault(t[:10], []).append((t, o, h, l, c))
    days, short = [], {}
    for d in sorted(by):
        b = by[d]
        if d in conflicted_days:
            continue
        hrs = sorted({int(x[0][11:13]) for x in b}); last = hrs[-1]
        friday = dt.date.fromisoformat(d).weekday() == 4
        full = hrs == list(range(last + 1)) and (last >= FRIDAY_MIN_LAST_HOUR if friday else last == EXPECTED_BARS - 1)
        if not full:
            short[d] = len(b); continue
        days.append({"date": d, "open": b[0][1], "high": max(x[2] for x in b), "low": min(x[3] for x in b), "close": b[-1][4], "bars": len(b)})
    blocked = {}
    if days:
        cur = dt.date.fromisoformat(days[0]["date"]); end = dt.date.fromisoformat(days[-1]["date"]); have = {x["date"] for x in days}
        while cur <= end:
            k = cur.isoformat()
            if cur.weekday() < 5 and k not in have:
                blocked[k] = "conflicting candle data" if k in conflicted_days else (f"incomplete ({short[k]} bars)" if k in short else "missing")
            cur += dt.timedelta(days=1)
    return days, blocked


def measure(days, end_idx, window, blocked=None):
    """Measurements over days[end_idx-window+1 .. end_idx]. None if there is not enough history (never invented).
    If a blocked weekday (missing / incomplete / conflicting) lies inside the window's dates, returns {'window_days', 'unavailable': reason}."""
    if end_idx - window + 1 < 0 or window < 2:
        return None
    x = days[end_idx - window + 1: end_idx + 1]
    bad = sorted(d for d in (blocked or {}) if x[0]["date"] < d < x[-1]["date"])
    if bad:
        return {"window_days": window, "unavailable": "; ".join(f"{d}: {blocked[d]}" for d in bad)}
    closes = [d["close"] for d in x]
    net = closes[-1] - closes[0]
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    hi, lo = max(d["high"] for d in x), min(d["low"] for d in x)
    mean_range = sum(d["high"] - d["low"] for d in x) / len(x)
    steps = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    return {
        "window_days": window,
        "from": x[0]["date"], "to": x[-1]["date"],
        "net_pips": round(net / PIP, 1),                                   # last close minus first close of the window
        "net_in_mean_daily_ranges": round(net / mean_range, 2) if mean_range > 0 else None,
        "path_efficiency": round(abs(net) / path, 3) if path > 0 else None,   # |net| / sum of |daily close changes|: 1 = straight line, 0 = no progress
        "range_width_pips": round((hi - lo) / PIP, 1),                      # highest high minus lowest low in the window
        "range_position": round((closes[-1] - lo) / (hi - lo), 3) if hi > lo else None,   # 0 = last close at window low, 1 = at window high
        "up_day_share": round(sum(1 for s in steps if s > 0) / len(steps), 3),
    }


def observation(days, end_idx, windows=WINDOWS, blocked=None, version=None):
    """One point-in-time record: everything known on trading day days[end_idx], using nothing later."""
    return {
        "definition_version": version or DEFINITION_VERSION,
        "as_of": days[end_idx]["date"],
        "days_available": end_idx + 1,
        "measurements": {str(w): measure(days, end_idx, w, blocked) for w in windows},     # None where history is too short
        "classification": "NOT_DEFINED_YET",                                       # labels wait for validated definitions (spec section 5)
        "limitations": ["thresholds and windows are provisional", "history is short", "GBPUSD only: not separate GBP and USD strength"],
    }


def enrich(candles, ema_spans=(20, 50, 100, 200)):
    """Per-candle view like a chart legend: open/high/low/close plus change, and EMAs of the close. Point-in-time: every value uses only that
    candle and earlier ones (an EMA is causal), so later candles never alter it. Input: cleaned candles from clean_hourly (sorted).
      change_pips        close minus the PREVIOUS candle's close (None for the first candle)   - the figure a chart legend shows
      change_pct         that change as a percentage of the previous close
      body_pips          close minus this candle's own open
      ema_<n>            exponential average of closes, smoothing 2/(n+1), seeded with the first close; ema_<n>_warm is False for the first 3*n
                         candles (not yet settled; never used as if it were)
    Gaps are not bridged: if the previous candle is more than 1 hour earlier (weekend, holiday, missing), change is still taken against the
    previous AVAILABLE candle and gap_hours says how far back that was."""
    import datetime as dt
    out = []; ema = {n: None for n in ema_spans}; prev = None
    for i, (t, o, h, l, c) in enumerate(candles):
        row = {"datetime": t, "open": o, "high": h, "low": l, "close": c, "body_pips": round((c - o) / PIP, 1)}
        if prev is None:
            row.update(change_pips=None, change_pct=None, gap_hours=None)
        else:
            row.update(change_pips=round((c - prev[4]) / PIP, 1), change_pct=round((c / prev[4] - 1) * 100, 4),
                       gap_hours=round((dt.datetime.fromisoformat(t) - dt.datetime.fromisoformat(prev[0])).total_seconds() / 3600, 2))
        for n in ema_spans:
            k = 2.0 / (n + 1); ema[n] = c if ema[n] is None else ema[n] + k * (c - ema[n])
            row[f"ema_{n}"] = round(ema[n], 6); row[f"ema_{n}_warm"] = i >= 3 * n
        out.append(row); prev = (t, o, h, l, c)
    return out


def build_ledger(rows):
    """Every trading day's observation, oldest first, plus the data-quality report. Deterministic."""
    candles, rep = clean_hourly(rows)
    days, blocked = to_days(candles, set(rep["conflicted_days"]))
    rep = dict(rep, trading_days=len(days), blocked_days=blocked, gaps=sorted(blocked),
               first=days[0]["date"] if days else None, last=days[-1]["date"] if days else None,
               coverage_note="blocked_days only covers weekdays between the first and last complete day")
    return [observation(days, i, blocked=blocked) for i in range(len(days))], rep


def read_jsonl(path):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


if __name__ == "__main__":
    import sys
    led, rep = build_ledger(read_jsonl(sys.argv[1]))
    print("data report:", json.dumps(rep))
    print("latest observation:", json.dumps(led[-1], indent=1))
