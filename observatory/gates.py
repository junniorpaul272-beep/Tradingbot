"""
observatory/gates.py
====================
OBSERVATORY — measurement gates A1-A7. ISOLATED from the live bot.

Run (from the repo root):
  python3 -m observatory.gates                         (real data: observatory_data/raw)
  python3 -m observatory.gates --source live-logs --raw-dir .   (scanner's partial logs, dev check only)
  python3 -m observatory.gates --selftest-only         (A1-A4 on synthetic data, no data needed)

Gates (A1-A4 are PASS/FAIL; A5-A7 are measurements):
  A1  first-touch classifier is correct on hand-built paths
  A2  the pipeline does not "find edges" in random data (false-positive rate)
  A3  no lookahead (as-of harness catches a leaky detector; closed-candle context)
  A4  episode collapsing is correct
  A5  same-bar target/stop frequency per timeframe (+ lower-TF resolution)
  A6  data inventory and coverage (counts, gaps, blind spots, episodes, holdout size)
  A7  research-readiness: DEVELOPMENT_ONLY / EXPLORATORY_READY / PRIMARY_READY

No trading concepts (no FVG/BOS/CHoCH/IFVG/premium-discount). The only "event"
used is a price-only displacement (range >= k x ATR) so A6/A7 have something to
count. Imports: stdlib + pandas + numpy + observatory.clean + observatory.measure.
Raw logs are only read.

PROPOSED thresholds (not frozen — see catalog Section 6):
"""
import argparse
import json
import math
import os

import numpy as np
import pandas as pd

from . import clean as rc
from . import measure as rm

# ---- proposed constants -----------------------------------------------------
K_EPISODE = 12            # episode gap, event-TF bars
HORIZON = 24              # outcome horizon, event-TF bars
DISPLACEMENT_K = (1.0, 1.5, 2.0)
DEV_FRACTION = 0.70       # (legacy, unused: the holdout is the frozen absolute date rc.HOLDOUT_START_UTC)
SAMPLE_PRIMARY = 100      # count label only, NOT evidence quality
A2_SERIES = 200
A2_FP_LIMIT = 0.10        # collapsed false-positive rate must be <= this
EXPLORATORY_MONTHS = 3
PRIMARY_MONTHS = 12
EXPLORATORY_REGIME_WEEKS = 4
PRIMARY_REGIME_WEEKS = 8
SESSIONS_UTC = {"ASIA": (0, 7), "LONDON": (7, 13), "NY": (13, 21), "LATE": (21, 24)}


def _gate(name, passed, details):
    return {"gate": name, "passed": passed, "details": details}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _mk_series(bars, step=5, gaps=None, closures=None):
    """bars = [(o,h,l,c), ...]. gaps/closures = {bar_index: value}."""
    t0 = pd.Timestamp("2026-09-08 00:00", tz="UTC")
    idx = [t0 + pd.Timedelta(minutes=step * i) for i in range(len(bars))]
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex(idx))
    df["gap_before_missing"] = 0
    df["closure_before"] = False
    for k, v in (gaps or {}).items():
        df.iloc[k, df.columns.get_loc("gap_before_missing")] = v
    for k in (closures or {}):
        df.iloc[k, df.columns.get_loc("closure_before")] = True
    return rm.Series(df, step)


def _flat(n, p=100.0):
    return [(p, p + 0.1, p - 0.1, p)] * n


def _ztest_two_prop(t1, n1, t2, n2):
    if n1 < 5 or n2 < 5:
        return 0.0
    p1, p2, p = t1 / n1, t2 / n2, (t1 + t2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return 0.0 if se == 0 else (p1 - p2) / se


# ---------------------------------------------------------------------------
# A1  first-touch correctness
# ---------------------------------------------------------------------------
def gate_a1():
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got}, want {want}")

    # long, event bar 0, entry = open of bar 1 = 100, R=1 -> target 101, stop 99
    def long_case(rest, **kw):
        s = _mk_series(_flat(1) + rest + _flat(30), **kw.pop("series_kw", {}))
        return rm.first_touch(s, 0, 1, r_unit=1.0, horizon=kw.pop("horizon", 24), **kw)

    T, S, A, N, G = rm.TARGET_FIRST, rm.STOP_FIRST, rm.AMBIGUOUS, rm.NEITHER, rm.DATA_GAP
    # +1.5R then -1R  => target first (path order matters)
    chk("long +1.5R then -1R", long_case([(100, 101.5, 99.5, 101), (101, 101.2, 98.5, 99)])["status"], T)
    # -1R then +1.5R  => stop first
    chk("long -1R then +1.5R", long_case([(100, 100.5, 98.5, 99), (99, 101.5, 99, 101)])["status"], S)
    # both inside one bar => ambiguous
    chk("long same bar", long_case([(100, 101.2, 98.8, 100)])["status"], A)
    # neither within horizon
    chk("long neither", long_case([(100, 100.5, 99.5, 100)] * 24, horizon=24)["status"], N)
    # exact touch counts
    chk("long exact target touch", long_case([(100, 101.0, 99.5, 100.5)])["status"], T)
    chk("long exact stop touch", long_case([(100, 100.5, 99.0, 99.5)])["status"], S)
    # horizon boundary: bar e+24 counts, e+25 does not
    quiet = [(100, 100.5, 99.5, 100)] * 23
    chk("target at last horizon bar", long_case(quiet + [(100, 101.5, 99.5, 101)], horizon=24)["status"], T)
    chk("target one bar past horizon", long_case(quiet + [(100, 100.5, 99.5, 100), (100, 101.5, 99.5, 101)], horizon=24)["status"], N)
    # short mirror cases
    def short_case(rest):
        s = _mk_series(_flat(1) + rest + _flat(30))
        return rm.first_touch(s, 0, -1, r_unit=1.0, horizon=24)
    chk("short +1.5R then -1R", short_case([(100, 100.5, 98.5, 99), (99, 101.5, 98.8, 101)])["status"], T)
    chk("short -1R then +1.5R", short_case([(100, 101.5, 99.5, 101), (101, 101, 98.5, 99)])["status"], S)
    chk("short same bar", short_case([(100, 101.2, 98.8, 100)])["status"], A)
    # data gaps
    chk("gap inside window, undecided", long_case([(100, 100.5, 99.5, 100)] * 5, series_kw={"gaps": {4: 3}})["status"], G)
    chk("gap between event and entry bar", long_case([(100, 100.5, 99.5, 100)] * 5, series_kw={"gaps": {1: 2}})["status"], G)
    s = _mk_series(_flat(10))
    chk("end of data", rm.first_touch(s, 0, 1, r_unit=1.0, horizon=24)["status"], G)
    chk("end-of-data reason", rm.first_touch(s, 0, 1, r_unit=1.0, horizon=24)["reason"], "end_of_data")
    # weekend closure is NOT a gap, but is flagged
    r = long_case([(100, 100.5, 99.5, 100)] * 5, series_kw={"closures": {4}})
    chk("closure not a gap", r["status"], N)
    chk("closure flagged", r["crosses_closure"], True)
    # gap AFTER the window must not censor
    chk("gap after outcome decided does not censor", long_case([(100, 101.5, 99.5, 101)], series_kw={"gaps": {20: 5}})["status"], T)
    chk("gap before outcome censors", long_case([(100, 100.5, 99.5, 100)] * 5 + [(100, 101.5, 99.5, 101)], series_kw={"gaps": {4: 5}})["status"], G)
    chk("gap beyond horizon ignored", long_case([(100, 100.5, 99.5, 100)] * 5, series_kw={"gaps": {30: 5}})["status"], N)
    # spread: long pays +0.2 => entry 100.2, target 101.2; bar high 101.1 must NOT hit
    chk("spread shifts target", long_case([(100, 101.1, 99.5, 100.5)], spread=0.2)["status"], N)
    # r_unit missing
    chk("no r_unit", rm.first_touch(_mk_series(_flat(40)), 0, 1)["status"], G)  # ATR(14) not yet defined at bar 0
    # lower-timeframe resolution of an ambiguous 15M bar using 5M bars
    t0 = pd.Timestamp("2026-09-08 00:00", tz="UTC")
    hi_df = pd.DataFrame([(100, 100.1, 99.9, 100), (100, 101.2, 98.8, 100)] + _flat(30),
                         columns=["open", "high", "low", "close"],
                         index=pd.DatetimeIndex([t0 + pd.Timedelta(minutes=15 * i) for i in range(32)]))
    hi_df["gap_before_missing"], hi_df["closure_before"] = 0, False
    s_hi = rm.Series(hi_df, 15)
    res = rm.first_touch(s_hi, 0, 1, r_unit=1.0, horizon=24)
    chk("15M ambiguous before drill-down", res["status"], A)

    def lo_series(bars15_start_min, bars):
        idx = pd.DatetimeIndex([t0 + pd.Timedelta(minutes=bars15_start_min + 5 * i) for i in range(len(bars))])
        d = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=idx)
        d["gap_before_missing"], d["closure_before"] = 0, False
        return rm.Series(d, 5)
    # the ambiguous 15M bar is bar 1 => 00:15..00:30 => three 5M bars
    ok_t = lo_series(15, [(100, 100.4, 99.6, 100.2), (100.2, 101.2, 100.0, 101), (101, 101, 98.8, 99)])
    chk("drill-down target first", rm.resolve_ambiguous(s_hi, ok_t, res, 1)[0], T)
    ok_s = lo_series(15, [(100, 100.4, 98.8, 99), (99, 101.2, 99, 101), (101, 101, 100, 100)])
    chk("drill-down stop first", rm.resolve_ambiguous(s_hi, ok_s, res, 1)[0], S)
    same = lo_series(15, [(100, 101.2, 98.8, 100), (100, 100.3, 99.7, 100), (100, 100.3, 99.7, 100)])
    chk("drill-down same lower bar stays ambiguous", rm.resolve_ambiguous(s_hi, same, res, 1)[0], A)
    short_cov = lo_series(15, [(100, 100.3, 99.7, 100), (100, 100.3, 99.7, 100)])
    chk("drill-down incomplete coverage stays ambiguous", rm.resolve_ambiguous(s_hi, short_cov, res, 1)[1], "lower_tf_incomplete")
    return _gate("A1 first-touch correctness", not fails, {"checks": n, "failures": fails})


# ---------------------------------------------------------------------------
# A2  pipeline on random data
# ---------------------------------------------------------------------------
def _random_walk_series(n, rng, step=5):
    sub = rng.normal(0, 0.0002, size=(n, 4))
    o = np.empty(n)
    c = np.empty(n)
    h = np.empty(n)
    l = np.empty(n)
    prev = 1.30
    for i in range(n):
        path = prev + np.cumsum(sub[i])
        o[i], c[i] = prev, path[-1]
        h[i], l[i] = max(prev, path.max()), min(prev, path.min())
        prev = c[i]
    t0 = pd.Timestamp("2026-01-05 00:00", tz="UTC")
    df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c},
                      index=pd.DatetimeIndex([t0 + pd.Timedelta(minutes=step * i) for i in range(n)]))
    df["gap_before_missing"], df["closure_before"] = 0, False
    return rm.Series(df, step)


def gate_a2(series=A2_SERIES, n=2500, seed=20261003):
    """
    Toy 'event' with NO information by construction: a bar whose range >= 1.5 x ATR,
    traded in its own direction. Control = ordinary bars (range < 1 x ATR) traded the same way.
    On random walks the true difference is 0, so a two-sided 5% test should fire ~5% of the time.
    Two arms: naive (every event counts) and collapsed (episode rule). Reports both.
    """
    rng = np.random.default_rng(seed)
    hits = {"naive": 0, "collapsed": 0}
    used = 0
    for _ in range(series):
        s = _random_walk_series(n, rng)
        atr_prior = np.roll(s.atr, 1)
        rng_bar = s.h - s.l
        valid = np.isfinite(atr_prior)
        valid[:15] = False
        valid[-(HORIZON + 2):] = False
        ratio = np.where(valid, rng_bar / np.where(atr_prior > 0, atr_prior, np.nan), np.nan)
        ev = np.where(ratio >= 1.5)[0]
        ctl_pool = np.where(ratio < 1.0)[0]
        if len(ev) < 20 or len(ctl_pool) < 50:
            continue
        ctl = np.sort(rng.choice(ctl_pool, size=min(400, len(ctl_pool)), replace=False))
        used += 1

        def outcomes(ix):
            out = []
            for e in ix:
                d = 1 if s.c[e] >= s.o[e] else -1
                r = rm.first_touch(s, int(e), d, horizon=HORIZON)
                out.append((int(e), d, r["status"]))
            return out
        o_ev, o_ct = outcomes(ev), outcomes(ctl)

        def stat(a, b):
            ta = sum(1 for _, _, st in a if st == rm.TARGET_FIRST)
            na = ta + sum(1 for _, _, st in a if st == rm.STOP_FIRST)
            tb = sum(1 for _, _, st in b if st == rm.TARGET_FIRST)
            nb = tb + sum(1 for _, _, st in b if st == rm.STOP_FIRST)
            return abs(_ztest_two_prop(ta, na, tb, nb)) > 1.96
        if stat(o_ev, o_ct):
            hits["naive"] += 1

        def collapsed(o):
            if not o:
                return o
            idx = np.array([x[0] for x in o])
            keys = np.array([x[1] for x in o])
            _, kept = rm.collapse_episodes(idx, keys, K_EPISODE)
            return [x for x, k in zip(o, kept) if k]
        if stat(collapsed(o_ev), collapsed(o_ct)):
            hits["collapsed"] += 1
    rates = {k: (v / used if used else None) for k, v in hits.items()}
    passed = used >= 50 and rates["collapsed"] is not None and rates["collapsed"] <= A2_FP_LIMIT
    return _gate("A2 pipeline on random walks", passed, {
        "series_used": used, "nominal_rate": 0.05, "limit": A2_FP_LIMIT,
        "false_positive_rate_naive_counting": rates["naive"],
        "false_positive_rate_episode_collapsed": rates["collapsed"],
        "note": "K_EPISODE=%d vs HORIZON=%d: if collapsed rate is above nominal, overlapping outcome windows "
                "inside one episode are still correlated; consider K >= HORIZON." % (K_EPISODE, HORIZON),
    })


# ---------------------------------------------------------------------------
# A3  no lookahead
# ---------------------------------------------------------------------------
def _clean_detector(d):
    """Break above prior-20-bar high, known at that bar's close."""
    hh = d["high"].shift(1).rolling(20).max()
    pos = np.where((d["close"] > hh).to_numpy())[0]
    return {(int(i), 1) for i in pos}


def _leaky_detector(d):
    """Cheats: 'event' if the NEXT bar closes higher. Must be caught."""
    nxt = d["close"].shift(-1)
    pos = np.where((nxt > d["close"]).to_numpy())[0]
    return {(int(i), 1) for i in pos}


def gate_a3(df=None):
    if df is None or len(df) < 400:
        s = _random_walk_series(600, np.random.default_rng(7), step=15)
        df = s.df
    df = df.iloc[-600:]
    points = list(np.linspace(60, len(df) - 2, 40).astype(int))
    clean_bad = rm.check_no_lookahead(_clean_detector, df, points)
    leaky_bad = rm.check_no_lookahead(_leaky_detector, df, points)
    # higher-timeframe context must use only bars closed at event time
    ctx_fails = []
    h_idx = pd.DatetimeIndex([pd.Timestamp("2026-09-08 00:00", tz="UTC") + pd.Timedelta(hours=i) for i in range(48)])
    hstep = pd.Timedelta(hours=1)
    cases = [("2026-09-08 10:15", "2026-09-08 09:00"),   # 5M event closing 10:15 -> 09:00 bar (closed 10:00)
             ("2026-09-08 10:00", "2026-09-08 09:00"),   # closes exactly at 10:00 -> 09:00 bar usable, 10:00 not
             ("2026-09-08 09:59", "2026-09-08 08:00"),   # 09:00 bar not closed yet
             ("2026-09-08 00:30", None)]                 # nothing closed yet
    for et, want in cases:
        pos = rm.last_closed_bar_pos(h_idx, hstep, pd.Timestamp(et, tz="UTC"))
        got = None if pos < 0 else str(h_idx[pos])[:16]
        if got != (want and want[:16].replace("T", " ")):
            ctx_fails.append({"event_time": et, "got": got, "want": want})
    passed = (not clean_bad) and len(leaky_bad) > 0 and not ctx_fails
    return _gate("A3 no lookahead", passed, {
        "samples": len(points), "clean_detector_mismatches": len(clean_bad),
        "leaky_detector_caught": len(leaky_bad) > 0, "context_closed_bar_failures": ctx_fails})


# ---------------------------------------------------------------------------
# A4  episodes
# ---------------------------------------------------------------------------
def gate_a4():
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got}, want {want}")
    ids, kept = rm.collapse_episodes([0, 5, 12, 13, 30, 31, 60], None, 12)
    chk("hand-built episode count", int(kept.sum()), 4)
    chk("hand-built membership", list(ids), [0, 0, 0, 1, 2, 2, 3])
    _, kept = rm.collapse_episodes([0, 10, 20], None, 12)
    chk("anchored not chained", int(kept.sum()), 2)
    _, kept = rm.collapse_episodes([0, 3], [1, -1], 12)
    chk("different keys never merge", int(kept.sum()), 2)
    _, kept = rm.collapse_episodes([], None, 12)
    chk("empty", int(kept.sum()), 0)
    rng = np.random.default_rng(3)
    for trial in range(200):
        m = int(rng.integers(1, 60))
        idx = np.sort(rng.integers(0, 400, size=m))
        keys = rng.choice([1, -1], size=m)
        ids, kept = rm.collapse_episodes(idx, keys, 12)
        chk("kept <= events", bool(kept.sum() <= m), True)
        for key in (1, -1):
            starts = idx[(keys == key) & kept]
            if len(starts) > 1:
                chk("episode starts > K apart", bool((np.diff(starts) > 12).all()), True)
            members = idx[(keys == key)]
            mem_ids = ids[(keys == key)]
            for e_id in np.unique(mem_ids):
                grp = members[mem_ids == e_id]
                if grp.max() - grp.min() > 12:
                    chk("episode span <= K", False, True)
        _, kept2 = rm.collapse_episodes(idx[kept], keys[kept], 12)
        chk("idempotent", int(kept2.sum()), int(kept.sum()))
    return _gate("A4 episode collapsing", not fails, {"checks": n, "failures": fails[:10]})


# ---------------------------------------------------------------------------
# A5  same-bar frequency
# ---------------------------------------------------------------------------
def gate_a5(series_by_tf):
    out = {}
    lo = series_by_tf.get("5m")
    for tf, s in series_by_tf.items():
        counts = {rm.TARGET_FIRST: 0, rm.STOP_FIRST: 0, rm.NEITHER: 0, rm.AMBIGUOUS: 0, rm.DATA_GAP: 0}
        resolved = unresolved = 0
        for e in range(15, s.n - 1):
            for d in (1, -1):
                r = rm.first_touch(s, e, d, horizon=HORIZON)
                counts[r["status"]] += 1
                if r["status"] == rm.AMBIGUOUS and lo is not None and tf != "5m":
                    st, _ = rm.resolve_ambiguous(s, lo, r, d)
                    if st == rm.AMBIGUOUS:
                        unresolved += 1
                    else:
                        resolved += 1
        usable = sum(v for k, v in counts.items() if k != rm.DATA_GAP)
        out[tf] = {"entries_tested": int(sum(counts.values())), "counts": counts,
                   "ambiguous_share_of_usable": round(counts[rm.AMBIGUOUS] / usable, 4) if usable else None,
                   "drilldown_resolved": resolved, "drilldown_unresolved": unresolved,
                   "note": "toy entries: every bar, both directions, 1R/1R, horizon %d" % HORIZON}
    return _gate("A5 same-bar ambiguity (measurement)", None, out)


# ---------------------------------------------------------------------------
# A6  inventory
# ---------------------------------------------------------------------------
def _displacement_events(s, k):
    prior = np.roll(s.atr, 1)
    prior[0] = np.nan
    ratio = (s.h - s.l) / np.where(prior > 0, prior, np.nan)
    idx = np.where(np.isfinite(ratio) & (ratio >= k))[0]
    d = np.where(s.c[idx] >= s.o[idx], 1, -1)
    return idx, d


def _session_coverage(clean, step_minutes):
    if clean.empty:
        return {}
    step = pd.Timedelta(minutes=step_minutes)
    grid = pd.date_range(clean.index[0], clean.index[-1], freq=step)
    grid = grid[~rc.is_market_closed(grid)]
    out = {}
    for name, (a, b) in SESSIONS_UTC.items():
        exp = int(((grid.hour >= a) & (grid.hour < b)).sum())
        got = int(((clean.index.hour >= a) & (clean.index.hour < b)).sum())
        out[name] = {"expected": exp, "present": got, "coverage": round(got / exp, 3) if exp else None}
    return out


def gate_a6(clean_by_tf, reports, series_by_tf):
    out = {}
    for tf, clean in clean_by_tf.items():
        s = series_by_tf[tf]
        rep = reports[tf]
        entry = {
            "first": rep["first"], "last": rep["last"], "raw_lines": rep["raw_lines_parsed"],
            "unique_timestamps": rep["unique_timestamps"], "duplicate_lines": rep["duplicate_lines"],
            "conflicting_duplicates": rep["conflicting_duplicates"],
            "dropped_market_closed": rep["dropped_market_closed"], "dropped_off_grid": rep["dropped_off_grid"],
            "dropped_invalid_ohlc": rep["dropped_invalid_ohlc"], "kept_bars": rep["kept_bars"],
            "unexplained_gaps": rep["unexplained_gaps"], "missing_open_bars": rep["missing_open_bars"],
        }
        expected = rep["kept_bars"] + rep["missing_open_bars"]
        entry["open_market_coverage"] = round(rep["kept_bars"] / expected, 4) if expected else None
        entry["session_coverage"] = _session_coverage(clean, s.step_minutes)
        blind = [n for n, v in entry["session_coverage"].items() if v["coverage"] is not None and v["coverage"] < 0.6]
        entry["session_blind_spots(<60%)"] = blind
        gaps_after = [(clean.index[i - 1].isoformat(), int(s.gap[i])) for i in np.where(s.gap > 0)[0]]
        entry["largest_gaps(start, missing_bars)"] = sorted(gaps_after, key=lambda x: -x[1])[:5]
        # longest contiguous stretch with no unexplained gap
        brk = np.where(s.gap > 0)[0]
        edges = np.concatenate([[0], brk, [s.n]])
        entry["longest_gap_free_run_bars"] = int(np.diff(edges).max()) if s.n else 0
        # DATA_GAP exposure of toy long entries
        reasons = {}
        for e in range(15, s.n - 1):
            r = rm.first_touch(s, e, 1, horizon=HORIZON)
            if r["status"] == rm.DATA_GAP:
                reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        total = max(1, s.n - 16)
        entry["toy_entries_DATA_GAP_share"] = round(sum(reasons.values()) / total, 4)
        entry["toy_entries_DATA_GAP_reasons"] = reasons
        # price-only displacement episodes, dev/holdout (split at the frozen absolute holdout date)
        cut = int(s.index.searchsorted(rc.HOLDOUT_START_UTC, side="left"))   # same frozen boundary as the experiments
        eps = {}
        for k in DISPLACEMENT_K:
            idx, d = _displacement_events(s, k)
            _, kept = rm.collapse_episodes(idx, d, K_EPISODE)
            ke = idx[kept]
            eps[str(k)] = {"episodes_total": int(kept.sum()), "development": int((ke < cut).sum()),
                           "holdout": int((ke >= cut).sum())}
        entry["displacement_episodes_by_k(ATR)"] = eps
        out[tf] = entry
    return _gate("A6 data inventory (measurement)", None, out)


# ---------------------------------------------------------------------------
# A7  readiness
# ---------------------------------------------------------------------------
def gate_a7(clean_by_tf, series_by_tf, a6):
    c = clean_by_tf.get("15m")
    if c is None or c.empty:
        return _gate("A7 research readiness", None, {"status": "DEVELOPMENT_ONLY", "reason": "no 15M data"})
    months = (c.index[-1] - c.index[0]).days / 30.44
    wk = c.resample("W-SUN").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    wk["net"] = wk["close"] - wk["open"]
    wk["rng"] = wk["high"] - wk["low"]
    label = np.where(wk["net"] >= 0.4 * wk["rng"], "UP", np.where(wk["net"] <= -0.4 * wk["rng"], "DOWN", "RANGE"))
    wk["regime"] = label
    regime_weeks = wk["regime"].value_counts().to_dict()
    for r in ("UP", "DOWN", "RANGE"):
        regime_weeks.setdefault(r, 0)
    vol_terciles = None
    if len(wk) >= 3:
        q = wk["rng"].quantile([1 / 3, 2 / 3]).to_numpy()
        vol = np.where(wk["rng"] <= q[0], "LOW", np.where(wk["rng"] <= q[1], "MID", "HIGH"))
        vol_terciles = pd.Series(vol).value_counts().to_dict()
    disp = a6["details"]["15m"]["displacement_episodes_by_k(ATR)"]["1.5"]
    dev_ok = disp["development"] >= SAMPLE_PRIMARY
    hold_ok = disp["holdout"] >= SAMPLE_PRIMARY
    sess = a6["details"]["15m"]["session_coverage"]
    sess_ok = all(v["coverage"] is not None and v["coverage"] >= 0.6 for v in sess.values())
    regimes_primary = all(regime_weeks[r] >= PRIMARY_REGIME_WEEKS for r in ("UP", "DOWN", "RANGE"))
    regimes_expl = sum(1 for r in ("UP", "DOWN", "RANGE") if regime_weeks[r] >= EXPLORATORY_REGIME_WEEKS) >= 2
    reasons = []
    if months < PRIMARY_MONTHS:
        reasons.append(f"months {months:.1f} < {PRIMARY_MONTHS}")
    if not regimes_primary:
        reasons.append(f"regime weeks {regime_weeks} (need each of UP/DOWN/RANGE >= {PRIMARY_REGIME_WEEKS})")
    if not (dev_ok and hold_ok):
        reasons.append(f"15M displacement(1.5) episodes dev={disp['development']} holdout={disp['holdout']} (need both >= {SAMPLE_PRIMARY})")
    if not sess_ok:
        reasons.append("a session has <60% coverage")
    if not reasons:
        status = "PRIMARY_READY"
    elif months >= EXPLORATORY_MONTHS and regimes_expl and dev_ok:
        status = "EXPLORATORY_READY"
    else:
        status = "DEVELOPMENT_ONLY"
    return _gate("A7 research readiness", None, {
        "status": status, "calendar_months": round(months, 2), "weeks": int(len(wk)),
        "regime_weeks": regime_weeks, "volatility_week_terciles": vol_terciles,
        "session_coverage_15m": sess, "why_not_PRIMARY_READY": reasons,
        "note": "N >= %d is a COUNT label only; it is not evidence quality." % SAMPLE_PRIMARY})


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
def run_all(raw_dir=rc.DEFAULT_RAW_DIR, out_dir=rc.DEFAULT_CLEAN_DIR, selftest_only=False, source="observatory"):
    gates = []
    print("A1 ...", flush=True)
    gates.append(gate_a1())
    print("A4 ...", flush=True)
    gates.append(gate_a4())
    clean_by_tf, reports, series_by_tf = {}, {}, {}
    if not selftest_only:
        for tf in rc.TIMEFRAMES:
            c, g, r = rc.clean_timeframe(tf, raw_dir, source)
            rc.write_clean(tf, c, g, r, out_dir, raw_dir, source)
            clean_by_tf[tf], reports[tf] = c, r
            series_by_tf[tf] = rm.Series(c, rc.STEP_MINUTES[tf])
    print("A3 ...", flush=True)
    gates.append(gate_a3(clean_by_tf.get("15m")))
    print("A2 (random-walk pipeline test, ~1 min) ...", flush=True)
    gates.append(gate_a2())
    if not selftest_only and series_by_tf:
        print("A5 ...", flush=True)
        gates.append(gate_a5(series_by_tf))
        print("A6 ...", flush=True)
        a6 = gate_a6(clean_by_tf, reports, series_by_tf)
        gates.append(a6)
        print("A7 ...", flush=True)
        gates.append(gate_a7(clean_by_tf, series_by_tf, a6))
    gates.sort(key=lambda g: g["gate"])
    report = {"gates": gates}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "gates_report.json"), "w") as f:
        json.dump(report, f, indent=2, default=str)
    return report


def print_report(report):
    print("\n==== RESEARCH GATES ====")
    for g in report["gates"]:
        flag = {True: "PASS", False: "FAIL", None: "INFO"}[g["passed"]]
        print(f"[{flag}] {g['gate']}")
        if g["gate"].startswith(("A1", "A3", "A4")) or g["passed"] is False:
            print("   ", json.dumps(g["details"], default=str)[:600])
    a7 = next((g for g in report["gates"] if g["gate"].startswith("A7")), None)
    if a7:
        print("\nDATASET STATUS:", a7["details"]["status"])
        for r in a7["details"].get("why_not_PRIMARY_READY", []):
            print("   -", r)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default=rc.DEFAULT_CLEAN_DIR)
    ap.add_argument("--selftest-only", action="store_true")
    a = ap.parse_args()
    print_report(run_all(a.raw_dir, a.out, a.selftest_only, a.source))
