"""audit_research_dataset.py - READ-ONLY audit of the Research Lab candle files (observatory_data/raw) against the live logs.
Changes nothing. Usage: python3 audit_research_dataset.py <repo_root> [out.json]
Checks per timeframe: coverage, timestamp format, duplicates/conflicts, malformed, weekend rows, weekly session open/close pattern,
gaps inside the session, zero-range filler candles, overlap with the live log, and cross-timeframe consistency (15m/5m rebuilt into 1h)."""
import sys, json, math, collections, datetime as dt
from . import awareness as aw

STEP = {"1h": 60, "15m": 15, "5m": 5}
HOLDOUT = "2026-06-18"        # the Research Lab's sealed holdout start (observatory/fetch.py HOLDOUT_START_UTC)

def load(path):
    rows = {}; bad = 0; dup = 0; conf = 0; n = 0; naive = 0; sorted_ok = True; prev = None
    for line in open(path):
        if not line.strip(): continue
        n += 1
        try:
            r = json.loads(line); raw = str(r["datetime"]); naive += (raw[-1] != "Z" and "+" not in raw[10:])
            t = aw.canon_ts(raw); v = tuple(float(r[k]) for k in ("open", "high", "low", "close"))
            o, h, l, c = v
            if not (all(math.isfinite(x) and x > 0 for x in v) and h >= max(o, c, l) and l <= min(o, c, h)): raise ValueError
        except Exception:
            bad += 1; continue
        if prev and t < prev: sorted_ok = False
        prev = t
        if t in rows:
            dup += 1; conf += rows[t] != v
        rows[t] = v
    return rows, dict(rows=n, malformed=bad, duplicate_rows=dup, conflicting_duplicates=conf, naive_timestamps=naive, in_file_order=sorted_ok)

def T(s): return dt.datetime.fromisoformat(s)

def audit(rows, step):
    ts = sorted(rows); out = {"first": ts[0], "last": ts[-1], "candles": len(ts)}
    wk = collections.Counter(T(t).weekday() for t in ts); out["by_weekday"] = {["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][k]: v for k, v in sorted(wk.items())}
    # weekly session: first and last candle of each ISO week
    weeks = collections.defaultdict(list)
    for t in ts:
        y, w, _ = T(t).isocalendar(); weeks[(y, w)].append(t)
    # market week runs Sun evening -> Fri evening, so key on (date+2h) shifted
    weeks = collections.defaultdict(list)
    for t in ts:
        y, w, _ = (T(t) + dt.timedelta(hours=2)).isocalendar(); weeks[(y, w)].append(t)
    op = collections.Counter(); cl = collections.Counter()
    for k, v in weeks.items():
        op[f"{T(v[0]).strftime('%a %H:%M')}"] += 1; cl[f"{T(v[-1]).strftime('%a %H:%M')}"] += 1
    out["weekly_open_top"] = op.most_common(4); out["weekly_close_top"] = cl.most_common(4)
    # gaps between consecutive candles
    gaps = collections.Counter(); big = []
    for a, b in zip(ts, ts[1:]):
        d = (T(b) - T(a)).total_seconds() / 60
        if d > step:
            kind = "weekend close" if (d >= 24 * 60 and T(a).weekday() in (4, 5, 6)) else "in-session gap"
            gaps[kind] += 1
            if kind == "in-session gap": big.append((a, b, int(d)))
    out["gaps"] = dict(gaps)
    ins = collections.Counter(("%d-%d steps" % (min(g[2] // step - 1, 99), min(g[2] // step - 1, 99))) for g in big)
    lens = collections.Counter()
    for _, _, d in big:
        m = d // step - 1                                  # missing candles
        lens["1" if m == 1 else "2-3" if m <= 3 else "4-12" if m <= 12 else ">12"] += 1
    out["in_session_gap_sizes_missing_candles"] = dict(lens)
    out["largest_in_session_gaps"] = sorted(big, key=lambda g: -g[2])[:8]
    out["in_session_gaps_per_year"] = dict(collections.Counter(g[0][:4] for g in big))
    out["zero_range_candles"] = sum(1 for t in ts if rows[t][1] == rows[t][2]) 
    out["zero_range_share_by_weekday"] = {["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][k]: round(sum(1 for t in ts if T(t).weekday() == k and rows[t][1] == rows[t][2]) / max(1, wk[k]), 4) for k in range(7)}
    out["holdout_split"] = {"before_holdout": sum(1 for t in ts if t[:10] < HOLDOUT), "from_holdout": sum(1 for t in ts if t[:10] >= HOLDOUT)}
    return out

def overlap(a, b, tol=0.00001):
    com = [t for t in a if t in b]; diff = [(t, a[t], b[t]) for t in com if max(abs(x - y) for x, y in zip(a[t], b[t])) > tol]
    return {"common": len(com), "differ": len(diff), "examples": diff[:5]}

def rebuild(rows, step):
    """aggregate finer candles into 1h; only hours with all sub-candles"""
    n = 60 // step; by = collections.defaultdict(list)
    for t in sorted(rows): by[t[:13]].append(t)
    out = {}
    for h, v in by.items():
        if len(v) == n:
            out[h + ":00:00+00:00"] = (rows[v[0]][0], max(rows[x][1] for x in v), min(rows[x][2] for x in v), rows[v[-1]][3])
    return out

if __name__ == "__main__":
    R = sys.argv[1].rstrip("/") + "/"; res = {}; data = {}
    for tf in ("1h", "15m", "5m"):
        rows, q = load(R + f"observatory_data/raw/GBPUSD_{tf}.jsonl"); data[tf] = rows
        res[tf] = {"file_quality": q, **audit(rows, STEP[tf])}
    for tf, f in (("1h", "ohlc_1h_log.jsonl"), ("15m", "ohlc_15m_log.jsonl")):
        live, q = load(R + f); res[tf]["live_log"] = {"file_quality": q, "overlap_vs_research": overlap(live, data[tf], 0.000015)}
    for tf in ("15m", "5m"):
        rb = rebuild(data[tf], STEP[tf]); res[tf]["rebuilt_into_1h_vs_1h_file"] = overlap(rb, data["1h"], 0.000015)
    print(json.dumps(res, indent=1, default=str))
    if len(sys.argv) > 2: json.dump(res, open(sys.argv[2], "w"), indent=1, default=str)
