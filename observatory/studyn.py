"""
Observatory — STUDY N, STAGE 1: THE EMA PULLBACK AS A REACTION LEVEL, COUNTS ONLY (GBPUSD, development data only). Read-only. The holdout stays sealed.

QUESTION (for the later outcome stage): when price pulls back to an EMA inside a trend, does it resume the trend more often than at matched times without the EMA interaction? That is "the EMA as a REACTION LEVEL".
It is kept separate from "the EMA as a TREND FILTER" (Study L covers EMA 50 / 100 as a filter), so the trend here is defined WITHOUT any tested EMA.

FROZEN DEFINITIONS (long side; the short side is the same code on the negated series)
  Trend (independent of every EMA): on the last COMPLETED 1H bar j at decision time, over the previous 24 hourly bars (gap-free): net = close_j - close_(j-24) > 0 and efficiency ER = |net| / sum(|1h close changes|) >= thr, thr in {0.20, 0.35}. (A completed 1H bar is one whose close time <= the decision time.)
  EMA(N): span N on the real closes of the entry timeframe, adjust=False; N in {9, 20, 50, 100, 150, 200}. Warm-up: the first 5 x N bars carry no events.
  Pullback touch at bar t: the trend is up, the PREVIOUS bar closed above its EMA (price approached from the trend side) and bar t's LOW reaches its EMA (low <= EMA_t); bars t-1 and t are contiguous.
    rule 'touch'  : that is all (decision at the close of bar t).
    rule 'reject' : additionally bar t CLOSES back above its EMA (a rejection candle).
  Decision at the close of bar t, entry at the next bar's open. Same-side events closer than 12 bars are dropped. Usable window H = 8 hours (15m 32 bars, 5m 96 bars), gap-free, inside development data.
  Control pool (for the later matched placebo): bars in the same trend state, price above the EMA on the previous AND this bar (no touch). Its size is counted.
TIMEFRAMES: 15m primary, 5m sensitivity (1m is excluded: thin feed, no older block, cost larger than the stop).
THIS FILE DOES THE COUNTS ONLY. No outcome code.

Run:  python3 -m observatory.studyn --counts [--raw-dir ...] [--out ...] [--boundary 2024-10-03]      python3 -m observatory.studyn --selftest
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

from . import clean as rc
from . import standards as st
from . import studyl as sl

CODE_VERSION = "studyn-counts-v1"
TFS = {"15m": 15, "5m": 5}
EMAS = (9, 20, 50, 100, 150, 200)
RULES = ("touch", "reject")
ER_THR = (0.20, 0.35)
LOOK = 24
WARM = 5
DEFAULT_BOUNDARY = "2024-10-03"
NS_MIN, NS_D, H1_NS = sl.NS_MIN, sl.NS_D, sl.H1_NS


def er_1h(ns1, c1, L=LOOK):
    """Per 1H bar j: (net move over L hours, efficiency ratio); NaN unless the L+1 bars are contiguous hourly bars."""
    n = len(c1)
    net = np.full(n, np.nan)
    er = np.full(n, np.nan)
    if n <= L:
        return net, er
    cs = np.r_[0.0, np.cumsum(np.abs(np.diff(c1)))]
    ok = (ns1[L:] - ns1[:-L]) == L * H1_NS
    nt = c1[L:] - c1[:-L]
    path = cs[L:] - cs[:-L]
    with np.errstate(invalid="ignore", divide="ignore"):
        e = np.where(path > 0, np.abs(nt) / path, np.nan)
    net[L:] = np.where(ok, nt, np.nan)
    er[L:] = np.where(ok, e, np.nan)
    return net, er


def trend_up(ns, step_ns, ns1, net, er, thr, sign):
    """Per entry-timeframe bar: True if the last 1H bar completed by the bar's close shows an up-trend in the oriented series."""
    j = np.searchsorted(ns1 + H1_NS, ns + step_ns, side="right") - 1
    jj = np.clip(j, 0, len(ns1) - 1)
    nt = sign * net[jj]
    return (j >= 0) & np.isfinite(nt) & (nt > 0) & (er[jj] >= thr)


def touch_flags(o, h, l, c, ns, step_ns, N, up):
    """Returns (touch, reject, pool) boolean arrays in the oriented series."""
    ema = pd.Series(c).ewm(span=N, adjust=False).mean().to_numpy()
    n = len(c)
    prev_above = np.zeros(n, bool)
    prev_above[1:] = c[:-1] > ema[:-1]
    contig = np.zeros(n, bool)
    contig[1:] = (ns[1:] - ns[:-1]) == step_ns
    base = up & prev_above & contig & (np.arange(n) >= WARM * N)
    touch = base & (l <= ema)
    reject = touch & (c > ema)
    pool = base & (l > ema)
    return touch, reject, pool


def cell_positions(arrs, ns, step_ns, ns1, net, er, N, thr):
    out = {"touch": {}, "reject": {}}
    pool = 0
    for sign, side in ((+1, "long"), (-1, "short")):
        o, h, l, c = arrs[sign]
        up = trend_up(ns, step_ns, ns1, net, er, thr, sign)
        t, r, p = touch_flags(o, h, l, c, ns, step_ns, N, up)
        out["touch"][side], out["reject"][side] = np.flatnonzero(t), np.flatnonzero(r)
        pool += int(p.sum())
    return out, pool


def run_counts(raw_dir=None, boundary=DEFAULT_BOUNDARY):
    raw_dir = raw_dir or rc.DEFAULT_RAW_DIR
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    bnd_ns = pd.Timestamp(boundary, tz="UTC").as_unit("ns").value
    c1h, _ = sl.load_tf("1h", raw_dir, hold_ns)
    ns1 = sl.ns_of(c1h.index)
    net, er = er_1h(ns1, c1h["close"].to_numpy(float))
    res = {"boundary_utc": boundary, "holdout_opened": False, "code_version": CODE_VERSION, "timeframes": {}}
    for tf, mins in TFS.items():
        clean, rep = sl.load_tf(tf, raw_dir, hold_ns)
        step = mins * NS_MIN
        ns = sl.ns_of(clean.index)
        o, h, l, c = (clean[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        arrs = {+1: (o, h, l, c), -1: (-o, -l, -h, -c)}
        H = sl.H_OF[tf]
        cells = {}
        for thr in ER_THR:
            share = float(np.mean([trend_up(ns, step, ns1, net, er, thr, s).mean() for s in (+1, -1)]) * 2)
            for N in EMAS:
                pos, pool = cell_positions(arrs, ns, step, ns1, net, er, N, thr)
                for rule in RULES:
                    cell = sl.tally(pos[rule], ns, step, hold_ns, bnd_ns, H=H, extra={"control_pool_bars": pool, "trend_share_of_bars": share})
                    cells[f"ema{N}|{rule}|er{thr}"] = cell
        res["timeframes"][tf] = {"bars": int(len(clean)), "first": str(clean.index[0]), "last": str(clean.index[-1]), "horizon_bars": H, "cells": cells}
    return res


def _f(x, nd=3):
    return "n/a" if x is None else f"{x:.{nd}f}"


def render_markdown(r):
    L = ["# Observatory — Study N, stage 1: the EMA pullback as a reaction level, counts only (GBPUSD)\n",
         f"Block boundary {r['boundary_utc']} · holdout opened: **False** · code version **{r['code_version']}** · **no outcome has been computed in this report**\n",
         "Trend = last completed 1H bar, 24h net move and efficiency ratio (no EMA involved). Event = a bar whose low reaches the EMA after the previous close was above it ('touch'; 'reject' also closes back above). Decision at the bar's close, entry next open, spacing 12 bars, H = 8 h.\n"]
    for tf, v in r["timeframes"].items():
        L += [f"## {tf} · {v['bars']} bars · {v['first']} … {v['last']} · H = {v['horizon_bars']} bars\n",
              "| cell (EMA / rule / ER threshold) | long raw → spaced → usable | short raw → spaced → usable | usable | recent / older | 2-day blocks | detectable (opt / cons) | tier | control pool bars | weekday Mon..Sun |", "|---|---|---|---|---|---|---|---|---|---|"]
        for k, c in v["cells"].items():
            lg, sh, fz = c["sides"]["long"], c["sides"]["short"], c["feasibility"]
            L.append(f"| {k} | {lg['raw_setups']} → {lg['after_spacing']} → {lg['usable']} | {sh['raw_setups']} → {sh['after_spacing']} → {sh['usable']} | {c['usable']} | {c['recent']} / {c['older']} | {c['blocks_2day']} | {_f(fz.get('detectable_optimistic'))} / {_f(fz.get('detectable_conservative'))} | {fz['tier']} | {c['control_pool_bars']} | {c['weekday_Mon_Sun']} |")
        L.append("")
    L += ["## Not done here\n", "- Any outcome, estimate, test, stop/target or cost; the placebo; the choice of primary readings (made after these counts); entry rules beyond the two defined; trend-strength or pullback-depth interaction tests."]
    return "\n".join(L)


def selftest():
    checks = []

    def chk(name, got, want):
        checks.append({"check": name, "ok": bool(got == want), "got": got, "want": want})

    # efficiency ratio: a straight line has ER 1; a zig-zag has a low ER
    ns1 = np.arange(60, dtype=np.int64) * H1_NS
    net, er = er_1h(ns1, np.arange(60, dtype=float))
    chk("ER of a straight line is 1 and only defined after 24 bars", (bool(np.isnan(er[23])), round(float(er[30]), 6), round(float(net[30]), 6)), (True, 1.0, 24.0))
    zz = np.tile([0.0, 1.0], 30)
    chk("ER of a zig-zag is ~0", bool(er_1h(ns1, zz)[1][40] < 0.05), True)
    ns_gap = ns1.copy()
    ns_gap[30:] += H1_NS
    chk("a gap inside the 24 h window gives no trend", bool(np.isnan(er_1h(ns_gap, np.arange(60, dtype=float))[1][40])), True)
    # alignment: a 15m bar uses only 1H bars that closed by its own close
    ns = np.array([0, 15, 30, 45, 60, 75], dtype=np.int64) * NS_MIN + 40 * H1_NS
    netx = np.arange(60, dtype=float)
    erx = np.ones(60)
    up = trend_up(ns, 15 * NS_MIN, ns1, netx + 1, erx, 0.2, +1)
    j = np.searchsorted(ns1 + H1_NS, ns + 15 * NS_MIN, side="right") - 1
    chk("the 1H bar used closes at or before the decision time (bar :45 uses the hour that ends at :00)", [int(x) for x in j], [39, 39, 39, 40, 40, 40])
    chk("an up-trend in the series is not an up-trend in its negation", bool(trend_up(ns, 15 * NS_MIN, ns1, netx + 1, erx, 0.2, -1).any()), False)
    # touch rules
    n = 60
    c = 100 + np.arange(n) * 0.1
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + 0.02
    l = np.minimum(o, c) - 0.02
    ns15 = np.arange(n, dtype=np.int64) * 15 * NS_MIN
    N = 5
    ema = pd.Series(c).ewm(span=N, adjust=False).mean().to_numpy()
    l[40] = ema[40] - 0.05            # bar 40 dips to the EMA, closes above it (previous close above)
    c[40] = ema[40] + 0.3
    h[40] = c[40] + 0.02
    ema = pd.Series(c).ewm(span=N, adjust=False).mean().to_numpy()
    l[40] = min(l[40], ema[40] - 0.05)
    l[50] = c[50] - 0.5               # bar 50 dips far below its EMA but we then close below it too
    c[50] = ema[50] - 0.2
    upall = np.ones(n, bool)
    t, r, p = touch_flags(o, h, l, c, ns15, 15 * NS_MIN, N, upall)
    chk("bar 40 is a touch and a rejection (closes back above the EMA)", (bool(t[40]), bool(r[40])), (True, True))
    chk("a touch that closes below the EMA is not a rejection", (bool(t[50]), bool(r[50])), (True, False))
    chk("pool bars never touch", bool((t & p).any()), False)
    chk("no event inside the warm-up (5 x N bars)", bool(t[:WARM * N].any()), False)
    # past-only
    t2, _, _ = touch_flags(o[:45], h[:45], l[:45], c[:45], ns15[:45], 15 * NS_MIN, N, upall[:45])
    chk("past-only: events before the cut are the same without the future", list(np.flatnonzero(t2)), [int(x) for x in np.flatnonzero(t) if x < 45])
    # gap
    ns_g = ns15.copy()
    ns_g[40:] += 15 * NS_MIN
    t3, _, _ = touch_flags(o, h, l, c, ns_g, 15 * NS_MIN, N, upall)
    chk("a gap before the bar removes the event (previous bar not contiguous)", bool(t3[40]), False)
    # mirror: negated series of a down-trend dip gives the same event
    tn, _, _ = touch_flags(-o, -l, -h, -c, ns15, 15 * NS_MIN, N, upall)
    chk("a long-oriented touch is not a touch in the negated (down) series", bool(tn[40]), False)
    bad = [x for x in checks if not x["ok"]]
    return {"checks": len(checks), "failures": bad, "passed": not bad}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--counts", action="store_true")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--out", default=".")
    ap.add_argument("--boundary", default=DEFAULT_BOUNDARY)
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2, default=str))
        sys.exit(0 if r["passed"] else 1)
    if a.counts:
        r = run_counts(a.raw_dir, boundary=a.boundary)
        os.makedirs(a.out, exist_ok=True)
        json.dump(r, open(os.path.join(a.out, "studyn_counts.json"), "w"), indent=2, default=str)
        md = render_markdown(r)
        open(os.path.join(a.out, "studyn_counts.md"), "w").write(md)
        print(md)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
