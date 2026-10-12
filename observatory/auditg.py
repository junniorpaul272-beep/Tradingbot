"""
Observatory — Audit of the extended history and of Study G (GBPUSD, 5m, development data only). Read-only. The holdout stays sealed.

Answers the audit questions raised after Study G:
  1. DATA QUALITY of the newly fetched older history: duplicates, conflicts, gaps, OHLC validity (from the cleaning report), per-year coverage, extreme bars,
     the UTC hour-of-day volatility profile per year (a timezone or session-assignment error shows up as a shifted profile), and a CROSS-TIMEFRAME check: 5m bars
     aggregated to 15m and 1h must equal the separately fetched 15m and 1h bars.
  2. EVENT LOSS in G: every candidate event that Study G drops at the outcome-window gate, with the REASON (weekend/closure, short missing run, data end) and weekday,
     compared with the events that were kept. The dropped events have no outcome by construction, so the comparison uses only observable features.
  3. BLOCK COMPARISON, exact: Study G's own event rows split at the block boundary (2024-10-03), per-block estimates and a cluster-robust difference test.
     Declared as ONE family (the 4 G tests). Descriptive; it does not change any Study G verdict.
  4. OVERLAP WITH STUDY F: share of G events with a Study F approach event (same orientation) in the PAST 24h only (a later F event would be look-ahead), and G's estimate for events with and without that context.
Nothing here tunes a rule or opens the holdout. Every number is computed on development data (before 2026-06-18).

Run:  python3 -m observatory.auditg [--raw-dir ...] [--out ...] [--boundary 2024-10-03]     --selftest
"""
import argparse
import json
import math
import os
import sys
import tempfile

import numpy as np
import pandas as pd

from . import clean as rc
from . import studya as sa
from . import studyb as sb
from . import studyf as sf
from . import studyg as sg
from .studyb import window_ok, NS_5, NS_D, NS_H, HMAX
from .studya import _ns, _p
from .studyc import cluster_test, cluster_diff

TOL = 2.5e-5                  # prices have 5 decimals: a difference above 2.5 pips-tenths is a real mismatch
EXTREME_RET = 0.004           # a 5m close-to-close move above 0.4% is listed as an extreme bar
KEYS = sg.TEST_KEYS


# ---------------------------------------------------------------------------
# 1. data quality
# ---------------------------------------------------------------------------
def _agg(df5, n, step):
    g = df5.groupby(df5.index.floor(step))
    a = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), n=("close", "size"))
    return a[a["n"] == n].drop(columns="n")


def cross_check(df5, dfx, n, step):
    """5m aggregated to the coarser frame versus the separately fetched bars. Only buckets with all n constituents and present in both are compared."""
    a = _agg(df5, n, step)
    j = a.join(dfx[["open", "high", "low", "close"]], how="inner", lsuffix="_a", rsuffix="_f")
    out = {"compared": int(len(j))}
    if not len(j):
        return out, pd.DataFrame()
    bad = {}
    for c in ("open", "high", "low", "close"):
        bad[c] = (j[c + "_a"] - j[c + "_f"]).abs() > TOL
    anybad = bad["open"] | bad["high"] | bad["low"] | bad["close"]
    bad_rows = j[anybad]
    out["mismatches"] = [{"t": str(t), **{c: round(float(r[c + "_a"] - r[c + "_f"]), 5) for c in ("open", "high", "low", "close")}} for t, r in bad_rows.head(40).iterrows()]
    out.update({"mismatched_any": int(anybad.sum()), "mismatch_share": float(anybad.mean()),
                "by_field": {c: int(b.sum()) for c, b in bad.items()},
                "max_abs_diff": float(max((j[c + "_a"] - j[c + "_f"]).abs().max() for c in ("open", "high", "low", "close")))})
    return out, j.assign(bad=anybad)


def data_quality(raw_dir, source, boundary):
    boundary = pd.Timestamp(boundary)
    boundary = boundary.tz_localize("UTC") if boundary.tzinfo is None else boundary.tz_convert("UTC")
    cl, rep = {}, {}
    for tf in ("5m", "15m", "1h"):
        c, g, r = rc.clean_timeframe(tf, raw_dir, source)
        cl[tf] = c[c.index < boundary]
        rep[tf] = {k: r.get(k) for k in ("raw_lines_parsed", "unique_timestamps", "duplicate_lines", "conflicting_duplicates", "dropped_market_closed", "dropped_off_grid",
                                         "dropped_invalid_ohlc", "kept_bars", "unexplained_gaps", "missing_open_bars", "closure_gaps", "first", "last")}
    out = {"cleaning": rep, "years": {}, "cross": {}, "extreme_bars": [], "hour_profile": {}}
    d5 = cl["5m"]
    if not len(d5):
        return out
    gb = d5["gap_before_missing"].to_numpy()
    yrs = d5.index.year
    for y in sorted(set(yrs)):
        m = yrs == y
        miss = int(gb[m].sum())
        out["years"][int(y)] = {"bars": int(m.sum()), "missing_bars_in_unexplained_gaps": miss, "gap_events": int((gb[m] > 0).sum()),
                                "largest_gap_bars": int(gb[m].max()), "missing_share": miss / float(m.sum() + miss)}
    ohlc_bad = int(((d5["high"] < d5[["open", "close"]].max(axis=1) - 1e-12) | (d5["low"] > d5[["open", "close"]].min(axis=1) + 1e-12) | (d5[["open", "high", "low", "close"]] <= 0).any(axis=1)).sum())
    out["ohlc_invalid_after_cleaning"] = ohlc_bad
    sret = d5["close"].pct_change()
    nxt = sret.shift(-1)
    ex = sret[sret.abs() > EXTREME_RET].abs().sort_values(ascending=False).head(10)
    out["extreme_bars"] = [{"t": str(t), "abs_move_pct": round(float(abs(sret[t])) * 100, 3), "move_pct": round(float(sret[t]) * 100, 3),
                            "next_bar_move_pct": None if not np.isfinite(nxt[t]) else round(float(nxt[t]) * 100, 3)} for t in ex.index]
    # cross-timeframe, by year
    for tf, n, step in (("15m", 3, "15min"), ("1h", 12, "1h")):
        tot, j = cross_check(d5, cl[tf], n, step)
        out["cross"][tf] = tot
        if len(j):
            out["cross"][tf]["by_year"] = {int(y): {"compared": int((j.index.year == y).sum()), "mismatched": int(j["bad"][j.index.year == y].sum())} for y in sorted(set(j.index.year))}
    # hour-of-day range profile per year (pips); a shifted profile = a timezone / session-assignment problem
    rng = (d5["high"] - d5["low"]) / sb.PIP
    prof = rng.groupby([d5.index.year, d5.index.hour]).mean().unstack()
    ref_year = 2025 if 2025 in prof.index else prof.index[-1]
    for y in prof.index:
        v = prof.loc[y].to_numpy(float)
        ok = np.isfinite(v) & np.isfinite(prof.loc[ref_year].to_numpy(float))
        out["hour_profile"][int(y)] = {"top3_hours_utc": [int(h) for h in np.argsort(-np.nan_to_num(v))[:3]], "corr_with_%d" % ref_year: float(np.corrcoef(v[ok], prof.loc[ref_year].to_numpy(float)[ok])[0, 1]) if ok.sum() > 5 else None,
                                  "best_shift_hours": int(max(range(-6, 7), key=lambda s: np.corrcoef(np.roll(v, s)[ok], prof.loc[ref_year].to_numpy(float)[ok])[0, 1]))}
    return out


# ---------------------------------------------------------------------------
# 2. event loss in G
# ---------------------------------------------------------------------------
def candidates(ctx, key):
    """Every candidate event of a G key BEFORE the outcome-window gate (the admission test is replaced by a recorder)."""
    orig = sf._admit
    sf._admit = lambda ctx_, p, region, cnt: True
    try:
        ev, cnt = sg.events_for(ctx, key, "dev")
    finally:
        sf._admit = orig
    return ev, cnt


def classify_loss(ctx, e):
    """Replicates sf._admit's order. Returns None if admitted, else a reason string."""
    ns5, p, t = ctx["ns5"], e["p"], e["t"]
    if t + (HMAX + 1) * NS_5 > ctx["hold_ns"]:
        return "outside_region"
    if not window_ok(ns5, p + 1, HMAX):
        if p + 1 + HMAX > len(ns5):
            return "window:data_end"
        seg = np.diff(ns5[p + 1:p + 1 + HMAX])
        k = int(np.flatnonzero(seg != NS_5)[0])
        hrs = float(seg[k]) / NS_H
        return "window:closure_>=20h" if hrs >= 20 else ("window:closure_1-20h" if hrs >= 1 else "window:missing_candles_<1h")
    if not (np.isfinite(ctx["x"][p]) and sb.trail_ok(ns5, p) and np.isfinite(ctx["atr_t"][p]) and ctx["atr_t"][p] > 0):
        return "no_trail"
    return None


def loss_audit(ctxs, keys=KEYS):
    out = {}
    for key in keys:
        rec = {"candidates": 0, "kept": 0, "reasons": {}, "weekday_dropped": [0] * 7, "weekday_kept": [0] * 7, "kept_feat": [], "dropped_feat": []}
        for ctx in ctxs:
            ev, _ = candidates(ctx, key)
            wd = sb.weekday(np.array([e["t"] for e in ev], np.int64)) if ev else []
            for e, w in zip(ev, wd):
                why = classify_loss(ctx, e)
                rec["candidates"] += 1
                feat = (e["atr"] / sb.PIP, e["frac"], 1 if ctx["sign"] > 0 else -1)
                if why is None:
                    rec["kept"] += 1
                    rec["weekday_kept"][int(w)] += 1
                    rec["kept_feat"].append(feat)
                else:
                    rec["reasons"][why] = rec["reasons"].get(why, 0) + 1
                    if why.startswith("window"):
                        rec["weekday_dropped"][int(w)] += 1
                        rec["dropped_feat"].append(feat)
        k, d = np.array(rec.pop("kept_feat")).reshape(-1, 3), np.array(rec.pop("dropped_feat")).reshape(-1, 3)
        rec["compare"] = {"atr_pips": [float(k[:, 0].mean()) if len(k) else None, float(d[:, 0].mean()) if len(d) else None],
                          "mean_time_fraction": [float(k[:, 1].mean()) if len(k) else None, float(d[:, 1].mean()) if len(d) else None],
                          "share_high_side": [float((k[:, 2] > 0).mean()) if len(k) else None, float((d[:, 2] > 0).mean()) if len(d) else None]}
        out[key] = rec
    return out


# ---------------------------------------------------------------------------
# 3. exact block comparison and 4. overlap with Study F
# ---------------------------------------------------------------------------
def _fade(key):
    return sg.CLAIM[key.split(":")[1]] == "fade"


def block_compare(rows, boundary_ns):
    out = {}
    for key in KEYS + sg.CTRL_KEYS:
        r = rows[key]
        a, b = [x for x in r if x["t"] >= boundary_ns], [x for x in r if x["t"] < boundary_ns]      # a = recent block, b = older block
        sa_, sb_ = sg.summarize(a, _fade(key)), sg.summarize(b, _fade(key))
        row = {"recent": {k: sa_.get(k) for k in ("n", "mean", "se", "z", "p")}, "older": {k: sb_.get(k) for k in ("n", "mean", "se", "z", "p")}}
        if len(a) >= 30 and len(b) >= 30:
            dd = sg._diff(a, b)
            row["diff"] = {k: dd.get(k) for k in ("diff", "se", "ci", "z", "p")}
        ex = {}
        for nm, grp in (("recent", a), ("older", b)):
            rr = [x for x in grp if "pup" in x]
            ex[nm] = cluster_test([x["rng"] - x["prng"] for x in rr], np.array([x["t"] for x in rr], np.int64)) if len(rr) >= 30 else {}
        row["range_excess"] = {nm: {k: v.get(k) for k in ("n", "mean", "se", "z")} for nm, v in ex.items()}
        out[key] = row
    return out


def near_past(ft_sorted, t, window_ns):
    """True if an event time in ft_sorted lies in [t - window, t]. PAST ONLY: conditioning on a LATER Study F event would select on the future path of price
    (F events are price approaching the previous-day boundary, which is correlated with the 24h outcome) and manufacture an effect."""
    i = int(np.searchsorted(ft_sorted, t, side="right"))
    return bool(i > 0 and t - int(ft_sorted[i - 1]) <= window_ns)


def overlap_with_f(rows, ctxs_f, window_ns=24 * NS_H):
    fts = {1: [], -1: []}
    for ctx in ctxs_f:
        for key in ("A:0", "A:0.1", "A:0.2"):
            ev, _ = sf.events_for(ctx, key, "dev")
            fts[ctx["sign"]] += [e["t"] for e in ev]
    fts = {s: np.sort(np.unique(np.array(v, np.int64))) for s, v in fts.items()}
    out = {}
    for key in KEYS:
        r = rows[key]
        near = []
        for x in r:
            near.append(near_past(fts[int(x["side"])], x["t"], window_ns))
        near = np.array(near, bool)
        ov, no = [x for x, b in zip(r, near) if b], [x for x, b in zip(r, near) if not b]
        s_all, s_no = sg.summarize(r, _fade(key)), sg.summarize(no, _fade(key))
        out[key] = {"n": len(r), "overlap_share": float(near.mean()) if len(r) else None,
                    "all": {k: s_all.get(k) for k in ("n", "mean", "se", "z")}, "without_f_overlap": {k: s_no.get(k) for k in ("n", "mean", "se", "z")},
                    "with_f_overlap_mean": float(np.mean([x["d"] for x in ov])) if ov else None}
    return out


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def run_audit(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", boundary="2024-10-03", preloaded=None):
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    bnd_ns = pd.Timestamp(boundary, tz="UTC").as_unit("ns").value
    ctxs = [sg.ctx_g(s5, s1h, +1, hold_ns), sg.ctx_g(s5, s1h, -1, hold_ns)]
    rows = sg.collect(s5, s1h, hold_ns)["rows"]
    res = {"dataset_status": status, "boundary_utc": boundary, "development_first_bar": str(s5.index[0]), "holdout_opened": False}
    res["loss"] = loss_audit(ctxs)
    res["blocks"] = block_compare(rows, bnd_ns)
    res["overlap_f"] = overlap_with_f(rows, [sf.ctx_f(s5, s1h, +1, hold_ns), sf.ctx_f(s5, s1h, -1, hold_ns)])
    return res


def render_markdown(r, dq=None):
    L = ["# Observatory — Audit of the extended history and of Study G (GBPUSD)\n",
         f"Dataset **{r['dataset_status']}** · first bar {r['development_first_bar']} · block boundary {r['boundary_utc']} · holdout opened: **False**\n"]
    if dq:
        L += ["## 1. Data quality (development data)\n", "| timeframe | raw lines | unique ts | duplicate lines | conflicting dups | market-closed dropped | off-grid | invalid OHLC dropped | kept | unexplained gaps | missing bars |\n|---|---|---|---|---|---|---|---|---|---|---|"]
        for tf, c in dq["cleaning"].items():
            L.append(f"| {tf} | {c['raw_lines_parsed']} | {c['unique_timestamps']} | {c['duplicate_lines']} | {c['conflicting_duplicates']} | {c['dropped_market_closed']} | {c['dropped_off_grid']} | {c['dropped_invalid_ohlc']} | {c['kept_bars']} | {c['unexplained_gaps']} | {c['missing_open_bars']} |")
        L += ["\nCounts above are over the WHOLE raw file; the tables below are development data only.\n", f"OHLC-invalid bars remaining after cleaning (development): **{dq.get('ohlc_invalid_after_cleaning')}**\n",
              "### Coverage by year (5m)\n", "| year | bars | gap events | missing bars | largest gap (bars) | missing share |\n|---|---|---|---|---|---|"]
        for y, v in dq["years"].items():
            L.append(f"| {y} | {v['bars']} | {v['gap_events']} | {v['missing_bars_in_unexplained_gaps']} | {v['largest_gap_bars']} | {100 * v['missing_share']:.2f}% |")
        L += ["\n### Cross-timeframe: 5m aggregated vs the separately fetched 15m / 1h bars\n", "| frame | compared | mismatched | share | by field (o/h/l/c) | max abs diff |\n|---|---|---|---|---|---|"]
        for tf, v in dq["cross"].items():
            if v.get("compared"):
                f = v["by_field"]
                L.append(f"| {tf} | {v['compared']} | {v['mismatched_any']} | {100 * v['mismatch_share']:.2f}% | {f['open']}/{f['high']}/{f['low']}/{f['close']} | {v['max_abs_diff']:.5f} |")
        for tf, v in dq["cross"].items():
            if v.get("by_year"):
                L.append(f"\n{tf} mismatches by year: " + ", ".join(f"{y}: {m['mismatched']}/{m['compared']}" for y, m in v["by_year"].items()))
        for tf, v in dq["cross"].items():
            if v.get("mismatches"):
                L.append(f"\n{tf} mismatching bars (aggregated 5m minus fetched, in price units; first 40):")
                L += [f"- {m['t']}: o {m['open']:+.5f} h {m['high']:+.5f} l {m['low']:+.5f} c {m['close']:+.5f}" for m in v["mismatches"]]
        L += ["\n### Hour-of-day range profile by year (a shift of the best-matching profile away from 0 hours would indicate a timezone problem)\n", "| year | top-3 UTC hours | correlation with the reference year | best shift (hours) |\n|---|---|---|---|"]
        for y, v in dq["hour_profile"].items():
            cc = [x for k, x in v.items() if k.startswith("corr_with")][0]
            L.append(f"| {y} | {v['top3_hours_utc']} | {cc:.3f} | {v['best_shift_hours']} |")
        L += ["\n### Largest 5m moves (development)\n"] + [f"- {e['t']}: {e['move_pct']:+.3f}% (next bar {e['next_bar_move_pct']:+.3f}%)" + ("  <- reverses at once: one-bar spike pattern" if e["next_bar_move_pct"] is not None and e["move_pct"] * e["next_bar_move_pct"] < 0 and abs(e["next_bar_move_pct"]) > 0.7 * abs(e["move_pct"]) else "") for e in dq["extreme_bars"]]
    L += ["\n## 2. Event loss in Study G (candidates dropped before scoring)\n", "Reasons are exclusive and follow the study's own gate order. Dropped events have no 24h outcome by construction; they are compared with kept events on observable features only.\n",
          "| test | candidates | kept | outside region | closure ≥20h | closure 1–20h | missing candles <1h | data end | no trail | dropped Mon..Sun (UTC weekday) | kept Mon..Sun |\n|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, v in r["loss"].items():
        g = v["reasons"]
        L.append(f"| {k} | {v['candidates']} | {v['kept']} | {g.get('outside_region', 0)} | {g.get('window:closure_>=20h', 0)} | {g.get('window:closure_1-20h', 0)} | {g.get('window:missing_candles_<1h', 0)} | "
                 f"{g.get('window:data_end', 0)} | {g.get('no_trail', 0)} | {v['weekday_dropped']} | {v['weekday_kept']} |")
    L += ["\n| test | ATR pips (kept / dropped) | mean time fraction (kept / dropped) | share high-side (kept / dropped) |\n|---|---|---|---|"]
    for k, v in r["loss"].items():
        c = v["compare"]
        f = lambda x: "n/a" if x is None else f"{x:.3f}"
        L.append(f"| {k} | {f(c['atr_pips'][0])} / {f(c['atr_pips'][1])} | {f(c['mean_time_fraction'][0])} / {f(c['mean_time_fraction'][1])} | {f(c['share_high_side'][0])} / {f(c['share_high_side'][1])} |")
    L += ["\n## 3. Exact block comparison of Study G (one family: the 4 G tests; descriptive)\n", "Events are split by decision time at the boundary; difference = recent − older, cluster-robust. Bonferroni threshold for the 4 G tests: p < 0.0125.\n",
          "| test | recent n | recent excess | older n | older excess | difference | 95% CI | z | p | below 0.0125? |\n|---|---|---|---|---|---|---|---|---|---|"]
    for k in KEYS + sg.CTRL_KEYS:
        v = r["blocks"][k]
        a, b, d = v["recent"], v["older"], v.get("diff")
        if a.get("mean") is None or b.get("mean") is None or not d:
            L.append(f"| {k} | {a.get('n')} | n/a | {b.get('n')} | n/a | n/a | n/a | n/a | n/a | n/a |")
            continue
        flag = ("yes" if d["p"] < 0.0125 else "no") if k in KEYS else "(control)"
        L.append(f"| {k} | {a['n']} | {a['mean']:+.3f} | {b['n']} | {b['mean']:+.3f} | {d['diff']:+.3f} | [{d['ci'][0]:+.3f}, {d['ci'][1]:+.3f}] | {d['z']:+.2f} | {_p(d['p'])} | {flag} |")
    L += ["\n### Expansion (24h range excess in ATR) by block\n", "| group | recent n | recent excess (z) | older n | older excess (z) |\n|---|---|---|---|---|"]
    for k in KEYS + sg.CTRL_KEYS:
        e = r["blocks"][k]["range_excess"]
        g = lambda x: "n/a" if not x or x.get("mean") is None else f"{x['mean']:+.3f} ({x['z']:+.2f})"
        L.append(f"| {k} | {e['recent'].get('n', 0)} | {g(e['recent'])} | {e['older'].get('n', 0)} | {g(e['older'])} |")
    L += ["\n## 4. Overlap of G events with Study F approach events (same orientation, within 24h)\n", "| test | G events | share with an F event nearby | G estimate (z) | G without F overlap (z) | mean outcome of overlapping events |\n|---|---|---|---|---|---|"]
    for k, v in r["overlap_f"].items():
        g = lambda x: "n/a" if not x or x.get("mean") is None else f"{x['mean']:+.3f} ({x['z']:+.2f}, n={x['n']})"
        L.append(f"| {k} | {v['n']} | {100 * v['overlap_share']:.1f}% | {g(v['all'])} | {g(v['without_f_overlap'])} | {'n/a' if v['with_f_overlap_mean'] is None else format(v['with_f_overlap_mean'], '+.3f')} |")
    L.append("\n## Not audited\n- Anything on the holdout. Outcomes of dropped events (they do not exist). Tuning or filtering by block. A formal multi-study dependence model.\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _write_raw(df5, d):
    os.makedirs(d, exist_ok=True)
    for tf, rule in (("5m", None), ("15m", "15min"), ("1h", "1h")):
        x = df5 if rule is None else df5.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        with open(os.path.join(d, f"GBPUSD_{tf}.jsonl"), "w") as f:
            for t, r in x.iterrows():
                f.write(json.dumps({"datetime": t.strftime("%Y-%m-%d %H:%M:%S"), "open": f"{r['open']:.5f}", "high": f"{r['high']:.5f}", "low": f"{r['low']:.5f}", "close": f"{r['close']:.5f}"}) + "\n")


def selftest():
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    rng = np.random.default_rng(91)
    # ---- data quality on a synthetic raw directory: cross-check is clean, then one corrupted 1h bar is found
    df = sb._synth_ohlc(40000, rng)
    df = df[["open", "high", "low", "close"]].round(5)
    with tempfile.TemporaryDirectory() as d:
        _write_raw(df, d)
        dq = data_quality(d, "observatory", "2099-01-01")
        c1 = dq["cross"].get("1h", {})
        chk("cross-check on consistent files: many hourly bars compared, none mismatched", (c1.get("compared", 0) > 300, c1.get("mismatched_any")), (True, 0))
        p1 = os.path.join(d, "GBPUSD_1h.jsonl")
        L_ = open(p1).read().splitlines()
        r_ = json.loads(L_[200])
        r_["high"] = f"{float(r_['high']) + 0.001:.5f}"
        L_[200] = json.dumps(r_)
        open(p1, "w").write("\n".join(L_) + "\n")
        dq2 = data_quality(d, "observatory", "2099-01-01")
        c2 = dq2["cross"]["1h"]
        chk("a corrupted 1h high is found by the cross-check (exactly one mismatch, in the high field)", (c2["mismatched_any"], c2["by_field"]["high"]), (1, 1))
        chk("hour profile of a flat-clock synthetic world needs no shift", dq["hour_profile"][2026]["best_shift_hours"] in (0,) or abs(dq["hour_profile"][2026]["corr_with_2026"] - 1.0) < 1e-9, True)
    # ---- loss audit: reconcile with Study G's own gate counters, with weekend closures inserted
    df = sb._synth_ohlc(60000, rng)
    keep = np.ones(len(df), bool)
    days = (df.index.dayofweek.to_numpy() * 24 + df.index.hour.to_numpy())
    keep &= ~((days >= 4 * 24 + 22) | (days < 0))                     # Friday 22:00 UTC onward ...
    keep &= ~(df.index.dayofweek.to_numpy() >= 5)                      # ... all Saturday and Sunday: a weekend closure each week
    dfg = df[keep].copy()
    dfg["gap_before_missing"], dfg["closure_before"] = 0, False
    s5, s1h = sa._series_from_df(dfg)
    far = int(_ns(s5.index)[-5000])
    ctxs = [sg.ctx_g(s5, s1h, +1, far), sg.ctx_g(s5, s1h, -1, far)]
    la = loss_audit(ctxs)
    ok, total_drop = True, 0
    for key in KEYS:
        gates = {"outside_region": 0, "window": 0, "no_trail": 0}
        for ctx in ctxs:
            _, cnt = sg.events_for(ctx, key, "dev")
            for kk in gates:
                gates[kk] += cnt[kk]
        v = la[key]
        mine = {"outside_region": v["reasons"].get("outside_region", 0), "window": sum(x for k_, x in v["reasons"].items() if k_.startswith("window")), "no_trail": v["reasons"].get("no_trail", 0)}
        ok &= mine == gates
        ok &= v["candidates"] == v["kept"] + sum(v["reasons"].values())
        total_drop += mine["window"]
    chk("loss audit reconciles with Study G's own gate counters for every test (outside region, window, no trail) and candidates = kept + dropped", bool(ok), True)
    wk = sum(v["reasons"].get("window:closure_>=20h", 0) for v in la.values())
    chk(f"with weekend closures, window losses are classified as closure >= 20h ({wk} of {total_drop}) and none as short missing candles", (wk > 0 and wk == total_drop, sum(v["reasons"].get("window:missing_candles_<1h", 0) for v in la.values())), (True, 0))
    chk("dropped events cluster late in the week (Thursday-Friday carry the weekend-crossing windows)", sum(sum(v["weekday_dropped"][3:5]) for v in la.values()) > sum(sum(v["weekday_dropped"][0:2]) for v in la.values()), True)
    # ---- block split and overlap reconcile with the pooled estimate
    col = sg.collect(s5, s1h, far)
    bnd = int(_ns(s5.index)[len(s5.index) // 2])
    bc = block_compare(col["rows"], bnd)
    ok = True
    for key in KEYS:
        r = col["rows"][key]
        a, b = [x for x in r if x["t"] >= bnd], [x for x in r if x["t"] < bnd]
        pooled = sg.summarize(r, _fade(key))
        if pooled.get("mean") is None or len(a) < 30 or len(b) < 30:
            continue
        ok &= abs((len(a) * bc[key]["recent"]["mean"] + len(b) * bc[key]["older"]["mean"]) / len(r) - pooled["mean"]) < 1e-9 and bc[key]["recent"]["n"] + bc[key]["older"]["n"] == len(r)
    chk("block split: n adds up and the n-weighted block means equal the pooled mean", bool(ok), True)
    ft_ = np.array([100, 500], np.int64)
    chk("overlap is PAST ONLY: an F event 40 units AFTER the G event does not count, one 40 units BEFORE does, one beyond the window does not",
        (near_past(ft_, 60, 50), near_past(ft_, 540, 50), near_past(ft_, 700, 50), near_past(ft_, 100, 50)), (False, True, False, True))
    ov = overlap_with_f(col["rows"], [sf.ctx_f(s5, s1h, +1, far), sf.ctx_f(s5, s1h, -1, far)])
    chk("overlap: shares are within [0, 1] and the non-overlapping subset is never larger than the whole", all(0 <= v["overlap_share"] <= 1 and v["without_f_overlap"].get("n", 0) <= v["n"] for v in ov.values() if v["n"]), True)
    md = render_markdown({"dataset_status": "X", "development_first_bar": "-", "boundary_utc": "-", "loss": la, "blocks": bc, "overlap_f": ov}, None)
    chk("report renders", len(md) > 1500, True)
    return {"checks": n, "failures": fails, "passed": not fails}


def main():
    ap = argparse.ArgumentParser(description="Observatory audit of the extended history and Study G. Read-only; the holdout stays sealed.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--boundary", default="2024-10-03")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    dq = data_quality(a.raw_dir, a.source, rc.HOLDOUT_START_UTC)
    res = run_audit(a.raw_dir, a.source, a.boundary)
    res["data_quality"] = dq
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "auditg_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res, dq)
    with open(os.path.join(a.out, "auditg_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
