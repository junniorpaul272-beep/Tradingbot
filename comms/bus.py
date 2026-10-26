"""
observatory/data1m.py
=====================
OBSERVATORY — 1-minute candles: loader, cleaner and DATA AUDIT (counts only, no trading concept, no outcome). ISOLATED. Read-only on the raw shards. The holdout stays sealed.

WHAT IT DOES
  * Reads the monthly shards observatory_data/raw/GBPUSD_1m_YYYY-MM.jsonl written by observatory/fetch.py (--tfs 1m) and cleans them with EXACTLY the same policy as the other timeframes
    (observatory.clean.clean_candles with a 1-minute step: de-duplicate, off-grid, impossible candles, the FX closed window, gaps vs closures).
  * Drops every bar at or after the frozen holdout start (the fetcher never requests them; this is the second lock).
  * AUDIT (feasibility first, S6): per-month coverage and the loss ledger; gaps; whether the 1-minute bars aggregate to the 5-minute file of the SAME feed (a mismatch would make any
    cross-timeframe study invalid); the size of a 1-minute bar against the flat 1-pip cost, by UTC hour; the break-even win rate of a 1:1 trade for several stop sizes and costs.

Run:  python3 -m observatory.data1m --selftest      python3 -m observatory.data1m --audit [--raw-dir observatory_data/raw] [--out observatory_run]
"""
import argparse
import json
import math
import os
import re
import sys
import tempfile

import numpy as np
import pandas as pd

from . import clean as rc
from . import measure as rm

PIP = 1e-4
SHARD_RE = re.compile(r"^GBPUSD_1m_(\d{4}-\d{2})\.jsonl$")
STOPS_PIPS = (2, 3, 5, 8, 10)
COSTS_PIPS = (0.5, 1.0, 1.5)


def shard_files(raw_dir):
    names = sorted(f for f in os.listdir(raw_dir) if SHARD_RE.match(f)) if os.path.isdir(raw_dir) else []
    return [os.path.join(raw_dir, f) for f in names]


def load_raw_1m(raw_dir):
    """Concatenate all monthly shards (read-only). raw_line is made unique across shards (running offset). Returns (DataFrame, malformed_count, shard names)."""
    frames, bad, off, names = [], 0, 0, []
    for p in shard_files(raw_dir):
        df, b = rc.load_raw(p)
        bad += b
        if len(df):
            df = df.copy()
            df["raw_line"] = df["raw_line"] + off
            off = int(df["raw_line"].max())
            frames.append(df)
        names.append(os.path.basename(p))
    raw = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["t", "open", "high", "low", "close", "raw_line"])
    return raw, bad, names


def clean_1m(raw_dir=rc.DEFAULT_RAW_DIR):
    """Returns (clean, gaps, report) for 1m, holdout removed."""
    raw, bad, names = load_raw_1m(raw_dir)
    clean, gaps, report = rc.clean_candles(raw, 1, malformed=bad)
    n0 = len(clean)
    clean = clean[clean.index < rc.HOLDOUT_START_UTC]
    gaps = gaps[gaps["gap_start"] < rc.HOLDOUT_START_UTC] if len(gaps) else gaps
    report.update(holdout_excluded_bars=int(n0 - len(clean)), kept_bars_development=int(len(clean)), shards=names, timeframe="1m", step_minutes=1)
    if len(clean):
        report["first"], report["last"] = clean.index[0].isoformat(), clean.index[-1].isoformat()
    return clean, gaps, report


def series_1m(raw_dir=rc.DEFAULT_RAW_DIR):
    clean, _, report = clean_1m(raw_dir)
    return rm.Series(clean, 1), report


def aggregate_5m(c1):
    """Complete 5-minute groups of 1m bars (5 of 5 present) -> o/h/l/c, indexed by the 5m bar open."""
    if c1.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    g = c1.groupby(c1.index.floor("5min"))
    cnt = g["close"].size()
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last()})
    return out[cnt == 5]


def compare_with_5m(c1, c5, tol_pips=0.15):
    """Agreement of the aggregated 1m bars with the 5m file of the same feed, bar by bar on the common timestamps."""
    a = aggregate_5m(c1)
    common = a.index.intersection(c5.index)
    if len(common) == 0:
        return {"compared": 0}
    x, y = a.loc[common], c5.loc[common]
    out = {"compared": int(len(common)), "tolerance_pips": tol_pips}
    for col in ("open", "high", "low", "close"):
        d = (x[col] - y[col]).abs() / PIP
        out[col] = {"share_within_tolerance": float((d <= tol_pips).mean()), "median_abs_diff_pips": float(d.median()), "p99_abs_diff_pips": float(np.percentile(d, 99)), "max_abs_diff_pips": float(d.max())}
    allok = ((x["open"] - y["open"]).abs() / PIP <= tol_pips) & ((x["high"] - y["high"]).abs() / PIP <= tol_pips) & ((x["low"] - y["low"]).abs() / PIP <= tol_pips) & ((x["close"] - y["close"]).abs() / PIP <= tol_pips)
    out["share_all_four_within_tolerance"] = float(allok.mean())
    return out


def _q(a, qs=(10, 25, 50, 75, 90, 99)):
    a = np.asarray(a, float)
    a = a[np.isfinite(a)]
    return {f"p{q}": float(np.percentile(a, q)) for q in qs} if len(a) else {}


def audit(raw_dir=rc.DEFAULT_RAW_DIR):
    clean, gaps, report = clean_1m(raw_dir)
    res = {"code": "data1m-audit-v1", "holdout_opened": False, "report": report}
    if clean.empty:
        res["empty"] = True
        return res
    idx = clean.index
    months = idx.strftime("%Y-%m")
    per_month = {}
    for m in sorted(set(months)):
        mm = months == m
        first, last = idx[mm][0], idx[mm][-1]
        # expected open minutes between the first and last bar of the month (closure-aware)
        slots = pd.date_range(first, last, freq="1min")
        expected = int((~rc.is_market_closed(slots, holidays=True)).sum())
        per_month[m] = {"bars": int(mm.sum()), "expected_open_minutes_in_span": expected, "coverage": float(mm.sum() / expected) if expected else None}
    res["per_month"] = per_month
    res["weekday_Mon_Sun_bars"] = [int((idx.dayofweek == d).sum()) for d in range(7)]
    lens = gaps["missing_open_bars"].to_numpy() if len(gaps) else np.array([], int)
    res["gaps"] = {"unexplained_gaps": int(len(gaps)), "missing_open_minutes": int(lens.sum()) if len(lens) else 0, "longest_gap_minutes": int(lens.max()) if len(lens) else 0,
                   "gap_length_quantiles": _q(lens) if len(lens) else {}, "gaps_over_5_minutes": int((lens > 5).sum()), "gaps_over_60_minutes": int((lens > 60).sum()),
                   "closure_gaps": report.get("closure_gaps")}
    rng = ((clean["high"] - clean["low"]) / PIP).to_numpy()
    flat = (clean["high"] == clean["low"]).to_numpy()
    atr14 = rm.atr(clean).to_numpy() / PIP
    res["bar_size_pips"] = {"range_quantiles": _q(rng), "share_flat_bars": float(flat.mean()), "share_range_below_1_pip": float((rng < 1.0).mean()), "share_range_below_2_pips": float((rng < 2.0).mean()), "atr14_quantiles": _q(atr14)}
    hours = {}
    hr = idx.hour.to_numpy()
    for h in range(24):
        m = hr == h
        if m.sum() == 0:
            continue
        hours[h] = {"bars": int(m.sum()), "median_range_pips": float(np.median(rng[m])), "median_atr14_pips": float(np.nanmedian(atr14[m])), "share_flat": float(flat[m].mean()), "share_below_1_pip": float((rng[m] < 1.0).mean())}
    res["by_utc_hour"] = hours
    res["breakeven_win_rate_1to1"] = {f"stop_{s}": {f"cost_{c}": 0.5 + c / (2.0 * s) for c in COSTS_PIPS} for s in STOPS_PIPS}
    try:
        c5, _, rep5 = rc.clean_timeframe("5m", raw_dir)
        c5 = c5[c5.index < rc.HOLDOUT_START_UTC]
        res["vs_5m_file"] = compare_with_5m(clean, c5)
    except Exception as ex:
        res["vs_5m_file"] = {"error": repr(ex)}
    return res


def render_markdown(r):
    L = ["# Observatory — 1-minute data audit (GBPUSD, counts only, no outcome)\n"]
    if r.get("empty"):
        return "\n".join(L + ["No 1m shards found (or no bars before the holdout)."])
    rep = r["report"]
    L += [f"Shards: {len(rep['shards'])} ({rep['shards'][0]} … {rep['shards'][-1]}) · first bar {rep['first']} · last bar {rep['last']} · holdout opened: **False** (bars at/after the holdout start excluded: {rep['holdout_excluded_bars']})\n",
          "## Loss ledger\n",
          f"raw lines {rep['raw_lines_parsed']} · malformed {rep['malformed_lines']} · unique timestamps {rep['unique_timestamps']} · duplicate lines {rep['duplicate_lines']} (conflicting {rep['conflicting_duplicates']}) · off-grid {rep['dropped_off_grid']} · "
          f"impossible {rep['dropped_invalid_ohlc']} · FX closed window {rep['dropped_market_closed']} · **kept {rep['kept_bars_development']}**\n",
          "## Coverage by month (kept bars / open minutes between the month's first and last bar)\n", "| month | bars | expected | coverage |", "|---|---|---|---|"]
    for m, v in r["per_month"].items():
        L.append(f"| {m} | {v['bars']} | {v['expected_open_minutes_in_span']} | {v['coverage']:.4f} |")
    g = r["gaps"]
    L += [f"\nWeekday coverage Mon..Sun (bars): {r['weekday_Mon_Sun_bars']}",
          f"\n## Gaps\n\nunexplained gaps {g['unexplained_gaps']} · missing open minutes {g['missing_open_minutes']} · longest {g['longest_gap_minutes']} min · gaps > 5 min: {g['gaps_over_5_minutes']} · gaps > 60 min: {g['gaps_over_60_minutes']} · closure gaps (weekends/holidays, not data loss): {g['closure_gaps']}",
          f"gap length quantiles (minutes): {g['gap_length_quantiles']}"]
    b = r["bar_size_pips"]
    L += ["\n## How big is a 1-minute bar against the cost? (pips)\n", f"range quantiles: {b['range_quantiles']} · flat bars (high = low): {b['share_flat_bars']:.3f} · range < 1 pip: {b['share_range_below_1_pip']:.3f} · range < 2 pips: {b['share_range_below_2_pips']:.3f}",
          f"ATR(14) of 1m bars, pips: {b['atr14_quantiles']}", "\n| UTC hour | bars | median range | median ATR(14) | flat | range < 1 pip |", "|---|---|---|---|---|---|"]
    for h, v in r["by_utc_hour"].items():
        L.append(f"| {h:02d} | {v['bars']} | {v['median_range_pips']:.2f} | {v['median_atr14_pips']:.2f} | {v['share_flat']:.3f} | {v['share_below_1_pip']:.3f} |")
    L += ["\n## Break-even win rate of a 1:1 trade (= 0.5 + cost / (2 × stop)); the edge a rule needs over a coin flip just to pay the cost\n", "| stop (pips) | " + " | ".join(f"cost {c}" for c in COSTS_PIPS) + " |", "|---|" + "---|" * len(COSTS_PIPS)]
    for s in STOPS_PIPS:
        L.append(f"| {s} | " + " | ".join(f"{r['breakeven_win_rate_1to1'][f'stop_{s}'][f'cost_{c}']:.3f}" for c in COSTS_PIPS) + " |")
    v = r["vs_5m_file"]
    L += ["\n## Do the 1m bars aggregate to the 5m file of the same feed?\n"]
    if "error" in v:
        L.append(f"5m file could not be compared: {v['error']}")
    elif not v.get("compared"):
        L.append("No common 5-minute bars to compare.")
    else:
        L.append(f"compared {v['compared']} complete 5-minute bars · all four of O/H/L/C within {v['tolerance_pips']} pip: **{v['share_all_four_within_tolerance']:.4f}**")
        for col in ("open", "high", "low", "close"):
            c = v[col]
            L.append(f"- {col}: within tolerance {c['share_within_tolerance']:.4f} · median |diff| {c['median_abs_diff_pips']:.3f} pips · p99 {c['p99_abs_diff_pips']:.3f} · max {c['max_abs_diff_pips']:.2f}")
    L += ["\n## Not done here\n", "- Any trading concept, signal, outcome or estimate. This is the data-quality and cost-feasibility gate (S6) before a 1-minute study is pre-registered."]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------
def _write_shards(d, minutes, seed=1, drop=None, dup_first=False):
    """Write a synthetic 1m random walk (including the weekend quotes the provider emits) as monthly shards, plus the matching 5m file. Returns the full 1m frame."""
    rng = np.random.default_rng(seed)
    c = 1.30 + np.cumsum(rng.normal(0, 0.00005, len(minutes)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.00002, len(minutes)))
    l = np.minimum(o, c) - np.abs(rng.normal(0, 0.00002, len(minutes)))
    df = pd.DataFrame({"open": o.round(5), "high": h.round(5), "low": l.round(5), "close": c.round(5)}, index=minutes)
    df["high"] = df[["open", "high", "low", "close"]].max(axis=1)
    df["low"] = df[["open", "high", "low", "close"]].min(axis=1)
    keep = df if drop is None else df.drop(drop)
    for m, part in keep.groupby(keep.index.strftime("%Y-%m")):
        with open(os.path.join(d, f"GBPUSD_1m_{m}.jsonl"), "w") as f:
            for i, (t, r) in enumerate(part.iterrows()):
                line = json.dumps({"datetime": t.strftime("%Y-%m-%d %H:%M:%S"), "open": f"{r.open:.5f}", "high": f"{r.high:.5f}", "low": f"{r.low:.5f}", "close": f"{r.close:.5f}"})
                f.write(line + "\n")
                if dup_first and i == 0:
                    f.write(line + "\n")
    a = aggregate_5m(df)
    with open(os.path.join(d, "GBPUSD_5m.jsonl"), "w") as f:
        for t, r in a.iterrows():
            f.write(json.dumps({"datetime": t.strftime("%Y-%m-%d %H:%M:%S"), "open": f"{r.open:.5f}", "high": f"{r.high:.5f}", "low": f"{r.low:.5f}", "close": f"{r.close:.5f}"}) + "\n")
    return df


def selftest():
    checks = []

    def chk(name, got, want):
        checks.append({"check": name, "ok": bool(got == want), "got": got, "want": want})

    d = tempfile.mkdtemp()
    minutes = pd.date_range("2026-02-26 00:00", "2026-03-04 23:59", freq="1min", tz="UTC")           # Thu .. Wed, crosses the Feb/Mar shard boundary and a weekend
    hole = pd.date_range("2026-03-03 10:00", periods=3, freq="1min", tz="UTC")
    full = _write_shards(d, minutes, drop=hole, dup_first=True)
    clean, gaps, rep = clean_1m(d)
    exp_open = ~rc.is_market_closed(minutes)
    chk("two monthly shards read", rep["shards"], ["GBPUSD_1m_2026-02.jsonl", "GBPUSD_1m_2026-03.jsonl"])
    chk("raw_line is unique across shards", bool(clean["raw_line"].is_unique), True)
    chk("kept = open minutes minus the 3 removed", len(clean), int(exp_open.sum()) - 3)
    chk("weekend quotes dropped as the closed window", rep["dropped_market_closed"], int((~exp_open).sum()))
    chk("one duplicate line per shard counted (2 shards)", rep["duplicate_lines"], 2)
    chk("one unexplained gap of 3 open minutes", (len(gaps), int(gaps["missing_open_bars"].iloc[0])), (1, 3))
    chk("no bar inside the closed window survives", bool(rc.is_market_closed(clean.index).sum() == 0), True)
    chk("weekend is a closure gap, not a data gap", rep["closure_gaps"] >= 1, True)
    # holdout lock
    d2 = tempfile.mkdtemp()
    hm = pd.date_range("2026-06-17 23:50", "2026-06-18 00:09", freq="1min", tz="UTC")
    _write_shards(d2, hm)
    c2, _, r2 = clean_1m(d2)
    chk("no bar at or after the holdout start is kept", bool((c2.index < rc.HOLDOUT_START_UTC).all()), True)
    chk("the excluded holdout bars (00:00-00:09) are counted", r2["holdout_excluded_bars"], 10)
    chk("last kept bar is 23:59 on 2026-06-17", str(c2.index[-1]), "2026-06-17 23:59:00+00:00")
    # agreement with the 5m file
    a = audit(d)
    chk("synthetic 1m aggregates to its own 5m file exactly", a["vs_5m_file"]["share_all_four_within_tolerance"], 1.0)
    p5 = os.path.join(d, "GBPUSD_5m.jsonl")
    rows = [json.loads(x) for x in open(p5)]
    rows[500]["close"] = f"{float(rows[500]['close']) + 0.0010:.5f}"
    rows[500]["high"] = f"{max(float(rows[500]['high']), float(rows[500]['close'])):.5f}"
    with open(p5, "w") as f:
        for r_ in rows:
            f.write(json.dumps(r_) + "\n")
    a2 = audit(d)
    chk("a corrupted 5m bar is detected", a2["vs_5m_file"]["share_all_four_within_tolerance"] < 1.0, True)
    chk("audit has hours and break-even table", (len(a["by_utc_hour"]) == 24, abs(a["breakeven_win_rate_1to1"]["stop_3"]["cost_1.0"] - (0.5 + 1.0 / 6)) < 1e-12), (True, True))
    chk("break-even: stop 5 pips, cost 1 pip = 0.60", round(a["breakeven_win_rate_1to1"]["stop_5"]["cost_1.0"], 6), 0.6)
    chk("markdown renders", "Break-even win rate" in render_markdown(a), True)
    # aggregation only uses complete 5-minute groups
    part = full.iloc[:7]
    chk("aggregation: only complete groups of 5", len(aggregate_5m(part.assign(open=part["open"]))) <= 2, True)
    bad = [c_ for c_ in checks if not c_["ok"]]
    return {"checks": len(checks), "failures": bad, "passed": not bad}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--out", default=".")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2, default=str))
        sys.exit(0 if r["passed"] else 1)
    if a.audit:
        r = audit(a.raw_dir)
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "data1m_audit.json"), "w") as f:
            json.dump(r, f, indent=2, default=str)
        md = render_markdown(r)
        with open(os.path.join(a.out, "data1m_audit.md"), "w") as f:
            f.write(md)
        print(md)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
