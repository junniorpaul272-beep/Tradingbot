"""overlap_check.py - development-period MEASUREMENT CONSTRUCTION check. RESEARCH, read-only. No regime labels, no thresholds, no outcomes.
Question answered: are the 10/20/40-trading-day measurements built from consistent, correctly bounded underlying days? It does NOT say the measurements
are useful. Rows after the period end are dropped at load time, so nothing beyond the development period is examined, except the point-in-time
check, which compares against a build of the full file and reports only whether numbers are identical.
Usage: python3 overlap_check.py <1h file> <start YYYY-MM-DD> <end YYYY-MM-DD> [out.json]"""
import sys, json, hashlib, random, collections, datetime as dt
from . import awareness as aw

def load_period(path, start, end):
    rows = [r for r in aw.read_jsonl(path) if start <= str(r["datetime"])[:10] <= end]
    return rows

def build(rows):
    candles, rep = aw.clean_hourly(rows); days, blocked = aw.to_days(candles, set(rep["conflicted_days"]))
    return candles, rep, days, blocked, [aw.observation(days, i, blocked=blocked) for i in range(len(days))]

def weekdays_between(a, b):
    a, b = dt.date.fromisoformat(a), dt.date.fromisoformat(b); n = 0
    while a <= b:
        n += a.weekday() < 5; a += dt.timedelta(days=1)
    return n

def spearman(x, y):
    def rank(v):
        o = sorted(range(len(v)), key=lambda i: v[i]); r = [0.0] * len(v); i = 0
        while i < len(o):
            j = i
            while j + 1 < len(o) and v[o[j + 1]] == v[o[i]]: j += 1
            for k in range(i, j + 1): r[o[k]] = (i + j) / 2
            i = j + 1
        return r
    rx, ry = rank(x), rank(y); n = len(x); mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry)); den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** .5
    return round(num / den, 3) if den else None

def check(rows, windows=aw.WINDOWS, pit_step=1):
    candles, rep, days, blocked, led = build(rows); res = {}
    res["dataset"] = {"rows_in_period": len(rows), "candles_kept": rep["kept"], "trading_days": len(days), "first": days[0]["date"] if days else None, "last": days[-1]["date"] if days else None,
                      "weekend_dropped": rep["weekend_dropped"], "duplicates": rep["duplicates"], "conflicting_duplicates": rep["conflicting_duplicates"],
                      "blocked_days": len(blocked), "blocked_by_reason": dict(collections.Counter(v.split(" (")[0] for v in blocked.values())),
                      "blocked_by_weekday": dict(collections.Counter(["Mon", "Tue", "Wed", "Thu", "Fri"][dt.date.fromisoformat(d).weekday()] for d in blocked)),
                      "blocked_by_year": dict(collections.Counter(d[:4] for d in blocked))}
    # 1. coverage
    cov = {}
    for w in windows:
        c = collections.Counter()
        for o in led:
            m = o["measurements"][str(w)]
            c["insufficient_history" if m is None else "unavailable_blocked_day" if "unavailable" in m else "valid"] += 1
        cov[str(w)] = dict(c, end_dates=len(led))
    res["coverage"] = cov
    # 2. shared observations / 3. missing-data effects
    tri = collections.Counter(); badspan = 0; shared_mismatch = 0
    for i, o in enumerate(led):
        ms = {w: o["measurements"][str(w)] for w in windows}
        state = tuple("V" if (m and "unavailable" not in m) else "U" if m else "H" for m in ms.values())
        tri["/".join(f"{w}:{s}" for w, s in zip(windows, state))] += 1
        for w, m in ms.items():
            if m and "unavailable" not in m:
                span = [d["date"] for d in days[i - w + 1: i + 1]]
                if len(span) != w or weekdays_between(span[0], span[-1]) != w: badspan += 1          # hidden skipped weekday inside a "valid" window
                if m["to"] != days[i]["date"] or m["from"] != span[0]: shared_mismatch += 1
    res["comparability"] = {"end_date_states (V=valid U=unavailable H=not enough history)": dict(tri),
                            "windows_with_hidden_missing_weekday": badspan, "windows_whose_dates_do_not_match_their_end_date": shared_mismatch,
                            "end_dates_where_all_windows_valid": sum(1 for o in led if all(o["measurements"][str(w)] and "unavailable" not in o["measurements"][str(w)] for w in windows)),
                            "end_dates_where_windows_disagree_on_availability": sum(1 for o in led if len({("unavailable" in (o["measurements"][str(w)] or {})) for w in windows if o["measurements"][str(w)]}) > 1)}
    # 4. overlap between windows
    ov = {}
    for w in windows:
        valid = [i for i, o in enumerate(led) if o["measurements"][str(w)] and "unavailable" not in o["measurements"][str(w)]]
        greedy, last = 0, -10 ** 9
        for i in valid:
            if i - last >= w: greedy += 1; last = i
        ov[str(w)] = {"valid_windows": len(valid), "share_of_days_reused_by_next_end_date": round((w - 1) / w, 3),
                      "max_non_overlapping_windows": greedy, "effective_independent_fraction": round(greedy / len(valid), 3) if valid else None}
    res["overlap_between_windows"] = ov
    # measurement-vs-measurement similarity (descriptive; serial dependence means these are not independent samples)
    names = ["net_pips", "net_in_mean_daily_ranges", "path_efficiency", "range_width_pips", "range_position", "up_day_share"]; corr = {}
    for w in windows:
        vals = {n: [] for n in names}
        for o in led:
            m = o["measurements"][str(w)]
            if m and "unavailable" not in m and all(m[n] is not None for n in names):
                for n in names: vals[n].append(m[n])
        corr[str(w)] = {"n": len(vals[names[0]]), "spearman": {f"{a} ~ {b}": spearman(vals[a], vals[b]) for ia, a in enumerate(names) for b in names[ia + 1:] if len(vals[a]) > 5}}
    res["measure_similarity_spearman_descriptive_only"] = corr
    # 5. point in time: observation as of day D rebuilt from data ending at D equals the one from the full period
    by_day = collections.defaultdict(list)
    for r in rows: by_day[str(r["datetime"])[:10]].append(r)
    full = {o["as_of"]: o for o in led}; mism = 0; tested = 0
    for i in range(0, len(led), pit_step):
        d = led[i]["as_of"]; cut = [r for dd in by_day if dd <= d for r in by_day[dd]]
        cl = build(cut); last = cl[4][-1] if cl[4] else None
        tested += 1
        if last is None or last["as_of"] != d or last["measurements"] != full[d]["measurements"]: mism += 1
    res["point_in_time"] = {"end_dates_tested": tested, "end_dates_that_changed_when_later_data_was_removed": mism}
    # 6. reproducibility
    sh = list(rows); random.Random(11).shuffle(sh); again = build(sh)[4]
    dump = lambda l: hashlib.sha256(json.dumps(l, sort_keys=True).encode()).hexdigest()[:16]
    res["reproducibility"] = {"build_hash": dump(led), "rebuild_same_order_hash": dump(build(rows)[4]), "rebuild_shuffled_input_hash": dump(again), "identical": dump(led) == dump(build(rows)[4]) == dump(again)}
    return res

def friday_floor_sensitivity(rows):
    out = {}
    for floor in (19, 20, 21, 22):
        old = aw.FRIDAY_MIN_LAST_HOUR; aw.FRIDAY_MIN_LAST_HOUR = floor
        try:
            _, rep, days, blocked, led = build(rows)
            fri = sum(1 for d in blocked if dt.date.fromisoformat(d).weekday() == 4)
            ok40 = sum(1 for o in led if o["measurements"]["40"] and "unavailable" not in o["measurements"]["40"])
            out[str(floor)] = {"trading_days": len(days), "blocked_fridays": fri, "valid_40d_windows": ok40}
        finally:
            aw.FRIDAY_MIN_LAST_HOUR = old
    return out

if __name__ == "__main__":
    path, start, end = sys.argv[1:4]; rows = load_period(path, start, end)
    res = {"input": {"file": path.split("/")[-1], "file_sha256": hashlib.sha256(open(path, "rb").read()).hexdigest()[:16], "period": [start, end],
                     "definition_version": aw.DEFINITION_VERSION, "friday_floor_hour": aw.FRIDAY_MIN_LAST_HOUR, "expected_bars_mon_thu": aw.EXPECTED_BARS}}
    res.update(check(rows)); res["friday_floor_sensitivity"] = friday_floor_sensitivity(rows)
    print(json.dumps(res, indent=1, default=str))
    if len(sys.argv) > 4: json.dump(res, open(sys.argv[4], "w"), indent=1, default=str)
