"""
Observatory — STUDY L, STAGE 1: THE HEIKIN ASHI PULLBACK-EXHAUSTION PATTERN, COUNTS ONLY (GBPUSD, development data only). Read-only. The holdout stays sealed.

WHERE THIS COMES FROM. A remembered trading strategy: Heikin Ashi candles on the 1-minute chart, trade only in the direction the price is on of an EMA (50 or 100, forgotten), wait for TWO candles against that direction with no wick on the side facing
the trend, then a doji that is "not too small" and "bigger than at least one of the two", enter immediately for 1:1. Several words are undefined in the memory, so this file freezes each one explicitly (a RECONSTRUCTION, not the creator's exact rule).
THIS FILE DOES THE COUNTS ONLY: no outcome code. It answers: how many setups exist under each reading, at which timeframe, with how much power, and is the "no wick" condition an artifact of thin 1-minute bars?

DEFINITIONS (long side; the short side is the same code on the negated series: prices negated, highs and lows swapped)
  Heikin Ashi (HA): HA_close = (O+H+L+C)/4 ; HA_open = (previous HA_open + previous HA_close)/2 (first bar: (O+C)/2) ; HA_high = max(H, HA_open, HA_close) ; HA_low = min(L, HA_open, HA_close). Past-only by construction (the open decays 0.5 per bar).
  Trend filter: the REAL close is above the EMA(N) of REAL closes (EMA computed with span N, adjust=False), N in {50, 100}. Two readings: 'close' = at the decision bar only; 'all3' = at each of the three setup bars (the memory says the price "oscillates" around the EMA).
  Two pullback candles: each of the two bars before the doji is BEARISH (HA_close < HA_open) with NO UPPER WICK: HA_high - HA_open <= tol x (HA_high - HA_low), tol = 0.05 (primary) or 0 (strict). (For the short side: bullish with no lower wick.)
  Doji (the decision bar t): body/range of the HA candle between 0.10 and 0.40 ("not so small") AND body >= the smaller of the two previous bodies ("bigger than at least one of the two"); any colour.
  Decision = the CLOSE of the doji bar; entry at the next bar's open (an "immediate" entry inside the forming doji would use its final body: look-ahead, so it is a different strategy and is not tested).
  Spacing: events of one side closer than 12 bars of the timeframe are dropped (one setup, not a run of overlapping ones). The first 5 x N bars of each series are warm-up and carry no events.
  Outcome windows (for the later stage): H = 8 hours on every timeframe (1m 480 bars, 5m 96, 15m 32), gap-free, inside development data. (v1 used 96 bars everywhere: 24 h on 15m, which removed every Friday.)
TIMEFRAMES: 1m (recent block only: the fetched shards start 2024-10-03), 5m, 15m (2020 onward). The 1-minute feed is thin (about 40% flat bars, audit 2026-10-24), which can manufacture 'no wick' candles: this file measures that.

Run:  python3 -m observatory.studyl --counts [--raw-dir ...] [--out ...] [--boundary 2024-10-03]      python3 -m observatory.studyl --selftest
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

from . import clean as rc
from . import data1m as d1
from . import standards as st
from . import studyi as si

CODE_VERSION = "studyl-counts-v2"
TFS = {"1m": 1, "5m": 5, "15m": 15}
EMAS = (50, 100)
TOLS = {"tol5": 0.05, "tol0": 0.0}
TRENDS = ("close", "all3")
DOJI_LO, DOJI_HI = 0.10, 0.40
SPACING = 12
H_BARS = 96                                  # default only (selftests); the studies use H_OF
H_OF = {"1m": 480, "5m": 96, "15m": 32}      # the SAME 8-hour horizon on every timeframe (v1 used 96 bars = 24 h on 15m, which erased every Friday)
WARM = 5
DEFAULT_BOUNDARY = "2024-10-03"
NS_MIN = 60 * 10 ** 9
NS_D = 24 * 3600 * 10 ** 9


def ns_of(idx):
    """Nanoseconds since epoch regardless of the index's storage unit (the cleaner returns microseconds)."""
    return pd.DatetimeIndex(idx).as_unit("ns").asi8.astype(np.int64)


def ha(o, h, l, c):
    """Heikin Ashi arrays (ho, hh, hl, hc)."""
    n = len(o)
    hc = (o + h + l + c) / 4.0
    ho = np.empty(n)
    ho[0] = (o[0] + c[0]) / 2.0
    for i in range(1, n):
        ho[i] = (ho[i - 1] + hc[i - 1]) * 0.5
    return ho, np.maximum(h, np.maximum(ho, hc)), np.minimum(l, np.minimum(ho, hc)), hc


def setup_flags(ho, hh, hl, hc, tol, lo=DOJI_LO, hi=DOJI_HI):
    """Per bar: bear_nowick[i] (bearish HA with no upper wick), doji[i] (needs the two previous bodies). Returns (bear, doji_ok)."""
    rng = hh - hl
    body = np.abs(hc - ho)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = np.where(rng > 0, body / rng, np.nan)
    bear = (hc < ho) & (rng > 0) & ((hh - ho) <= tol * rng + 1e-12)
    n = len(ho)
    prev_min = np.full(n, np.nan)
    if n > 2:
        prev_min[2:] = np.minimum(body[1:-1], body[:-2])
    doji = (ratio >= lo) & (ratio <= hi) & (body >= prev_min)
    return bear, doji


def long_events(o, h, l, c, ns, step_ns, N, tol, trend):
    """Decision-bar positions of long setups in the oriented series (before spacing and window checks)."""
    ho, hh, hl, hc = ha(o, h, l, c)
    bear, doji = setup_flags(ho, hh, hl, hc, tol)
    ema = pd.Series(c).ewm(span=N, adjust=False).mean().to_numpy()
    up = c > ema
    n = len(c)
    t = np.arange(n)
    ok = np.zeros(n, bool)
    if n > 2:
        ok[2:] = bear[1:-1] & bear[:-2] & doji[2:] & up[2:]
        if trend == "all3":
            ok[2:] &= up[1:-1] & up[:-2]
        contiguous = np.zeros(n, bool)
        contiguous[2:] = (ns[2:] - ns[:-2]) == 2 * step_ns
        ok &= contiguous
    ok &= t >= WARM * N
    return np.flatnonzero(ok)


def space(pos, step_bars=SPACING):
    keep, last = [], -10 ** 12
    for p in pos:
        if p - last >= step_bars:
            keep.append(int(p))
            last = p
    return np.array(keep, int)


def usable(pos, ns, step_ns, hold_ns, H=H_BARS):
    out = []
    for p in pos:
        e = p + H
        if e < len(ns) and ns[e] - ns[p] == H * step_ns and ns[e] + step_ns <= hold_ns:
            out.append(int(p))
    return np.array(out, int)


def count_cell(arrs, ns, step_ns, hold_ns, bnd_ns, N, tol_name, trend, H=H_BARS):
    res = {"sides": {}}
    allt = []
    for sign in (+1, -1):
        o, h, l, c = arrs[sign]
        raw = long_events(o, h, l, c, ns, step_ns, N, TOLS[tol_name], trend)
        sp = space(raw)
        us = usable(sp, ns, step_ns, hold_ns, H)
        res["sides"]["long" if sign > 0 else "short"] = {"raw_setups": int(len(raw)), "after_spacing": int(len(sp)), "usable": int(len(us))}
        allt += [int(ns[p]) for p in us]
    t = np.array(allt, np.int64)
    res["usable"] = int(len(t))
    res["recent"], res["older"] = int((t >= bnd_ns).sum()), int((t < bnd_ns).sum())
    res["blocks_2day"] = int(len({int(x // (2 * NS_D)) for x in t}))
    res["weekday_Mon_Sun"] = si.wd_counts(pd.DatetimeIndex(t.astype("datetime64[ns]")).dayofweek.to_numpy()) if len(t) else [0] * 7
    res["feasibility"] = st.feasibility(res["usable"], res["blocks_2day"])
    return res


def artifact_stats(arrs):
    """Unconditional shares (long orientation + short orientation pooled): how often a HA bar is 'strong' with no wick on the trend-facing side, and how often the real bar is flat."""
    out = {}
    tot = bear_all = 0
    for sign in (+1, -1):
        o, h, l, c = arrs[sign]
        ho, hh, hl, hc = ha(o, h, l, c)
        for name, tol in TOLS.items():
            b, _ = setup_flags(ho, hh, hl, hc, tol)
            out.setdefault(name, 0)
            out[name] += int(b.sum())
        tot += len(o)
    o, h, l, c = arrs[+1]
    return {"strong_nowick_share": {k: v / tot for k, v in out.items()}, "real_flat_bar_share": float((h == l).mean()), "bars": int(len(o))}


def load_tf(tf, raw_dir, hold_ns):
    if tf == "1m":
        try:
            clean, _, rep = d1.clean_1m(raw_dir)
        except (TypeError, ValueError, FileNotFoundError):
            return pd.DataFrame(), {}
        if clean is None or clean.empty:
            return pd.DataFrame(), rep
    else:
        clean, _, rep = rc.clean_timeframe(tf, raw_dir)
    clean = clean[clean.index < pd.Timestamp(hold_ns, tz="UTC")]
    return clean, rep


def run_counts(raw_dir=None, boundary=DEFAULT_BOUNDARY, tfs=None):
    raw_dir = raw_dir or rc.DEFAULT_RAW_DIR
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    bnd_ns = pd.Timestamp(boundary, tz="UTC").as_unit("ns").value
    res = {"boundary_utc": boundary, "holdout_opened": False, "code_version": CODE_VERSION, "timeframes": {}}
    for tf in (tfs or TFS):
        clean, rep = load_tf(tf, raw_dir, hold_ns)
        if clean.empty:
            res["timeframes"][tf] = {"empty": True}
            continue
        step_ns = TFS[tf] * NS_MIN
        ns = ns_of(clean.index)
        o, h, l, c = (clean[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        arrs = {+1: (o, h, l, c), -1: (-o, -l, -h, -c)}
        cells = {}
        for N in EMAS:
            for tol in TOLS:
                for tr in TRENDS:
                    cells[f"ema{N}|{tol}|{tr}"] = count_cell(arrs, ns, step_ns, hold_ns, bnd_ns, N, tol, tr, H_OF[tf])
        zc = None
        if tf == "15m":
            c1h, _ = load_tf("1h", raw_dir, hold_ns)
            zc = zone_cells(clean, c1h, hold_ns, bnd_ns, H_OF["15m"])
        res["timeframes"][tf] = {"zone_cells": zc, "bars": int(len(clean)), "first": str(clean.index[0]), "last": str(clean.index[-1]), "cleaning": {k: rep.get(k) for k in ("kept_bars", "dropped_market_closed", "unexplained_gaps", "missing_open_bars")},
                                 "artifact": artifact_stats(arrs), "cells": cells}
    return res


# ---------------------------------------------------------------------------
# L.b / L.c : 1H support-resistance zones and the 15M doji at the zone (counts only)
# ---------------------------------------------------------------------------
ZONE_W_ATR = 0.25          # zone half-width in ATR(14) of the 1H series, taken at confirmation time
ZONE_EXPIRY_BARS = 120     # a level stops being eligible after 120 1H bars (5 days)
SWING_KS = (3, 5)          # a swing needs k lower highs (higher lows) on each side; confirmed k bars after it
DOJI_MAX = 0.25            # doji: |close - open| / (high - low) <= 0.25
H1_NS = 60 * NS_MIN


def atr14(h, l, c):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()


def swing_levels(ns1, h1, l1, c1, k):
    """Confirmed 1H swing levels. Returns list of dicts: kind ('res'/'sup'), lo, hi (zone), t0 (eligible from, ns), t1 (ineligible from, ns).
    A swing at bar j is confirmed when bar j+k has CLOSED, i.e. at ns[j+k] + 1h. Zone = level +/- ZONE_W_ATR x ATR14 known at the confirmation bar.
    A level ends at expiry or when a later 1H bar CLOSES beyond the zone (above its top for a resistance, below its bottom for a support); that ends it from the close of that bar.
    Requires the 2k+1 bars to be contiguous hourly bars (no gap)."""
    n = len(ns1)
    atr = atr14(h1, l1, c1)
    out = []
    for j in range(k, n - k):
        if ns1[j + k] - ns1[j - k] != 2 * k * H1_NS:
            continue
        a = atr[j + k]
        if not np.isfinite(a) or a <= 0:
            continue
        t0 = int(ns1[j + k] + H1_NS)
        for kind, lvl, ok in (("res", h1[j], h1[j] > h1[j - k:j].max() and h1[j] > h1[j + 1:j + k + 1].max()),
                              ("sup", l1[j], l1[j] < l1[j - k:j].min() and l1[j] < l1[j + 1:j + k + 1].min())):
            if not ok:
                continue
            lo, hi = lvl - ZONE_W_ATR * a, lvl + ZONE_W_ATR * a
            t1 = t0 + ZONE_EXPIRY_BARS * H1_NS
            for m in range(j + k + 1, min(n, j + k + 1 + ZONE_EXPIRY_BARS)):
                if (kind == "res" and c1[m] > hi) or (kind == "sup" and c1[m] < lo):
                    t1 = min(t1, int(ns1[m] + H1_NS))
                    break
            out.append({"kind": kind, "lo": float(lo), "hi": float(hi), "t0": t0, "t1": int(t1)})
    return out


def zone_touch_flags(ns, h, l, c, levels):
    """Per 15M bar: touch_res / touch_sup = the bar ENTERS an eligible zone from the outside: its range reaches the zone and the PREVIOUS close was on the near side
    (below a resistance zone's bottom / above a support zone's top). Eligibility uses the bar's open time (>= confirmation, < end)."""
    n = len(ns)
    tr = np.zeros(n, bool)
    ts = np.zeros(n, bool)
    pc = np.r_[np.nan, c[:-1]]
    for z in levels:
        a = int(np.searchsorted(ns, z["t0"], "left"))
        b = int(np.searchsorted(ns, z["t1"], "left"))
        if b <= a:
            continue
        sl = slice(a, b)
        if z["kind"] == "res":
            tr[sl] |= (h[sl] >= z["lo"]) & (pc[sl] < z["lo"])
        else:
            ts[sl] |= (l[sl] <= z["hi"]) & (pc[sl] > z["hi"])
    tr[0] = ts[0] = False
    return tr, ts


def doji_flags(o, h, l, c):
    rng = h - l
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(rng > 0, np.abs(c - o) / rng, np.nan)
    return r <= DOJI_MAX


def tally(pos_by_side, ns, step_ns, hold_ns, bnd_ns, extra=None, H=H_BARS):
    allt, sides = [], {}
    for side, pos in pos_by_side.items():
        sp = space(np.asarray(pos, int))
        us = usable(sp, ns, step_ns, hold_ns, H)
        sides[side] = {"raw_setups": int(len(pos)), "after_spacing": int(len(sp)), "usable": int(len(us))}
        allt += [int(ns[p]) for p in us]
    t = np.array(allt, np.int64)
    res = {"sides": sides, "usable": int(len(t))}
    res["recent"], res["older"] = int((t >= bnd_ns).sum()), int((t < bnd_ns).sum())
    res["blocks_2day"] = int(len({int(x // (2 * NS_D)) for x in t}))
    res["weekday_Mon_Sun"] = si.wd_counts(pd.DatetimeIndex(t.astype("datetime64[ns]")).dayofweek.to_numpy()) if len(t) else [0] * 7
    res["feasibility"] = st.feasibility(res["usable"], res["blocks_2day"])
    if extra:
        res.update(extra)
    return res


def zone_cells(clean15, clean1h, hold_ns, bnd_ns, H=32):
    """Counts for L.b (does the zone change the original HA pattern?) and L.c (the 15M doji at the zone, with matched touch-without-doji control)."""
    ns = ns_of(clean15.index)
    o, h, l, c = (clean15[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    ns1 = ns_of(clean1h.index)
    h1, l1, c1 = (clean1h[k].to_numpy(float) for k in ("high", "low", "close"))
    step = 15 * NS_MIN
    cells = {}
    ho, hh, hl, hc = ha(o, h, l, c)
    for k in SWING_KS:
        lv = swing_levels(ns1, h1, l1, c1, k)
        tr, ts = zone_touch_flags(ns, h, l, c, lv)
        # touch at a support = LONG candidate; touch at a resistance = SHORT candidate
        dj_real = doji_flags(o, h, l, c)
        rng = hh - hl
        with np.errstate(invalid="ignore", divide="ignore"):
            dj_ha = np.where(rng > 0, np.abs(hc - ho) / rng, np.nan) <= DOJI_MAX
        bull = c > o
        for N in (None, 50, 100):
            ema = pd.Series(c).ewm(span=N, adjust=False).mean().to_numpy() if N else None
            warm = np.arange(len(c)) >= (WARM * N if N else 0)
            # trend-aligned = long only if close > EMA, short only if close < EMA (note: a support touch above the EMA is a with-trend dip; below is counter-trend)
            lg_ok = warm & ((c > ema) if N else True)
            sh_ok = warm & ((c < ema) if N else True)
            tag = f"k{k}|ema{N if N else 'none'}"
            cells[f"touch_control|{tag}"] = tally({"long": np.flatnonzero(ts & lg_ok), "short": np.flatnonzero(tr & sh_ok)}, ns, step, hold_ns, bnd_ns, H=H)
            for dn, dj in (("realdoji", dj_real), ("hadoji", dj_ha)):
                lgp, shp = np.flatnonzero(ts & dj & lg_ok), np.flatnonzero(tr & dj & sh_ok)
                cells[f"{dn}|{tag}"] = tally({"long": lgp, "short": shp}, ns, step, hold_ns, bnd_ns, H=H,
                                             extra={"doji_colour_bull_share": float(np.mean(np.r_[bull[lgp], bull[shp]])) if len(lgp) + len(shp) else None})
                nd = ~dj
                cells[f"touch_nodoji|{dn}|{tag}"] = tally({"long": np.flatnonzero(ts & nd & lg_ok), "short": np.flatnonzero(tr & nd & sh_ok)}, ns, step, hold_ns, bnd_ns, H=H)
        # L.b : the HA pattern (primary reading: tol 5%, trend 'close') with a zone touched by any of its three setup bars (support for long, resistance for short)
        for N in EMAS:
            lpos = long_events(o, h, l, c, ns, step, N, TOLS["tol5"], "close")
            spos = long_events(-o, -l, -h, -c, ns, step, N, TOLS["tol5"], "close")
            near_s = ts | np.r_[False, ts[:-1]] | np.r_[False, False, ts[:-2]]
            near_r = tr | np.r_[False, tr[:-1]] | np.r_[False, False, tr[:-2]]
            lz = lpos[near_s[lpos]] if len(lpos) else lpos
            sz = spos[near_r[spos]] if len(spos) else spos
            cells[f"ha_pattern_at_zone|k{k}|ema{N}"] = tally({"long": lz, "short": sz}, ns, step, hold_ns, bnd_ns, H=H,
                                                              extra={"ha_pattern_all_setups": int(len(lpos) + len(spos))})
        cells[f"_levels|k{k}"] = {"resistance": sum(z["kind"] == "res" for z in lv), "support": sum(z["kind"] == "sup" for z in lv),
                                  "touch_res_bars": int(tr.sum()), "touch_sup_bars": int(ts.sum())}
    return cells


def _f(x, nd=3):
    return "n/a" if x is None else f"{x:.{nd}f}"


def render_markdown(r):
    L = ["# Observatory — Study L, stage 1: the Heikin Ashi pullback-exhaustion pattern, counts only (GBPUSD)\n",
         f"Block boundary {r['boundary_utc']} · holdout opened: **False** · code version **{r['code_version']}** · **no outcome has been computed in this report**\n",
         "Reconstruction of a remembered strategy: HA candles, real-close EMA(N) trend filter, two pullback candles with no wick on the trend-facing side, then a doji (body/range 0.10-0.40 and body >= the smaller of the two previous bodies); decision = the doji's close, entry at the next open; spacing 12 bars; H = 8 hours on every timeframe (1m 480 bars, 5m 96, 15m 32), gap-free.\n"]
    for tf, v in r["timeframes"].items():
        if v.get("empty"):
            L.append(f"## {tf}: no data\n")
            continue
        a = v["artifact"]
        L += [f"## {tf} · {v['bars']} bars · {v['first']} … {v['last']}\n", f"cleaning: {v['cleaning']}",
              f"Artifact check — share of ALL HA bars that are strong with no trend-facing wick: tol 5% {_f(a['strong_nowick_share']['tol5'], 4)}, strict {_f(a['strong_nowick_share']['tol0'], 4)} · share of real bars that are flat (high = low): {_f(a['real_flat_bar_share'], 4)}\n",
              "| reading (EMA / wick tolerance / trend test) | long raw → spaced → usable | short raw → spaced → usable | usable total | recent / older | 2-day blocks | detectable (opt / cons) | tier |", "|---|---|---|---|---|---|---|---|"]
        for k, c in v["cells"].items():
            lg, sh, fz = c["sides"]["long"], c["sides"]["short"], c["feasibility"]
            L.append(f"| {k} | {lg['raw_setups']} → {lg['after_spacing']} → {lg['usable']} | {sh['raw_setups']} → {sh['after_spacing']} → {sh['usable']} | {c['usable']} | {c['recent']} / {c['older']} | {c['blocks_2day']} | {_f(fz.get('detectable_optimistic'))} / {_f(fz.get('detectable_conservative'))} | {fz['tier']} |")
        if v.get("zone_cells"):
            L += ["\n### L.b / L.c — 1H swing zones (width 0.25 ATR(1h), 5-day expiry, ends on a 1H close through it) and the 15M doji at the zone\n",
                  "| cell | long raw → spaced → usable | short raw → spaced → usable | usable | recent / older | 2-day blocks | detectable (opt / cons) | tier | extra |", "|---|---|---|---|---|---|---|---|---|"]
            for k, c in v["zone_cells"].items():
                if k.startswith("_levels"):
                    L.append(f"| {k} | levels/touches: {c} | | | | | | | |")
                    continue
                lg, sh, fz = c["sides"]["long"], c["sides"]["short"], c["feasibility"]
                ex = {x: c[x] for x in ("doji_colour_bull_share", "ha_pattern_all_setups") if x in c}
                L.append(f"| {k} | {lg['raw_setups']} → {lg['after_spacing']} → {lg['usable']} | {sh['raw_setups']} → {sh['after_spacing']} → {sh['usable']} | {c['usable']} | {c['recent']} / {c['older']} | {c['blocks_2day']} | {_f(fz.get('detectable_optimistic'))} / {_f(fz.get('detectable_conservative'))} | {fz['tier']} | {ex} |")
            L.append("")
        k0 = next(iter(v["cells"]))
        L.append(f"\nWeekday coverage Mon..Sun (first reading): {v['cells'][k0]['weekday_Mon_Sun']}\n")
    L += ["## Not done here\n", "- Any outcome, estimate, test, stop/target or cost; the placebo; the choice of the primary reading (made only after these counts are reviewed); the 1:1 exit rule; a higher-timeframe bias filter."]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------
def selftest():
    checks = []

    def chk(name, got, want):
        checks.append({"check": name, "ok": bool(got == want), "got": got, "want": want})

    # HA formula by hand
    o = np.array([10.0, 10.2, 10.1]); h = np.array([10.5, 10.6, 10.4]); l = np.array([9.9, 10.0, 9.8]); c = np.array([10.2, 10.1, 9.9])
    ho, hh, hl, hc = ha(o, h, l, c)
    chk("HA close = (O+H+L+C)/4", [round(x, 6) for x in hc], [10.15, 10.225, 10.05])
    chk("HA open: first (O+C)/2, then (prev HA open + prev HA close)/2", [round(x, 6) for x in ho], [10.1, 10.125, 10.175])
    chk("HA high/low include the HA open and close", (round(hh[1], 6), round(hl[2], 6)), (10.6, 9.8))
    # crafted HA arrays for the pattern
    def arrs(rows):
        a = np.array(rows, float)
        return a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    # bars: (ho, hh, hl, hc); two bearish flat-top candles then a doji with body ratio 0.25 and body >= min(prev bodies)
    base = [(10.0, 10.0, 9.0, 9.2), (9.4, 9.4, 8.5, 8.7), (8.8, 9.2, 8.4, 8.7)]
    bear, doji = setup_flags(*arrs(base), 0.05)
    chk("two bearish no-upper-wick candles are recognised", (bool(bear[0]), bool(bear[1])), (True, True))
    chk("the doji (ratio 0.125, body 0.1 < smaller previous body 0.3) fails the size rule", bool(doji[2]), False)
    base2 = [(10.0, 10.0, 9.0, 9.7), (9.8, 9.8, 9.0, 9.5), (9.3, 9.7, 8.9, 9.1)]      # bodies 0.3, 0.3, doji body 0.2 (ratio 0.25) < 0.3 -> fails; make doji body 0.3
    base2[2] = (9.3, 9.9, 8.7, 9.0)                                                 # body 0.3 over range 1.2 = 0.25, >= 0.3 min -> passes
    bear2, doji2 = setup_flags(*arrs(base2), 0.05)
    chk("a doji with ratio 0.25 and body >= the smaller previous body passes", bool(doji2[2]), True)
    base3 = [(10.0, 10.2, 9.0, 9.7), (9.8, 9.8, 9.0, 9.5), (9.3, 9.9, 8.7, 9.0)]
    chk("an upper wick of 0.2 on a range of 1.2 (> 5%) is NOT 'no wick'", bool(setup_flags(*arrs(base3), 0.05)[0][0]), False)
    chk("the same wick-free-within-tolerance candle passes at 5% (wick 0.04 / range 1.0)", bool(setup_flags(*arrs([(10.0, 10.04, 9.0, 9.7)]), 0.05)[0][0]), True)
    chk("strict tolerance 0 rejects that candle", bool(setup_flags(*arrs([(10.0, 10.04, 9.0, 9.7)]), 0.0)[0][0]), False)
    chk("a bullish candle is never a bearish no-wick candle", bool(setup_flags(*arrs([(9.0, 10.0, 9.0, 9.7)]), 0.05)[0][0]), False)
    chk("a zero-range bar is never a setup bar (no divide by zero)", bool(setup_flags(*arrs([(9.0, 9.0, 9.0, 9.0)]), 0.05)[0][0]), False)
    # full pipeline on a hand-made series: up trend (above EMA), two red strong candles, then a doji
    n = 400
    c = 100 + np.arange(n) * 0.02
    o_ = np.r_[c[0], c[:-1]]
    h_ = np.maximum(o_, c) + 0.005
    l_ = np.minimum(o_, c) - 0.005
    ns = np.arange(n, dtype=np.int64) * 5 * NS_MIN
    # force a pullback at bars 300, 301 (strong red, flat top) and a doji at 302
    for i, (op, cl) in {300: (108.0, 107.2), 301: (107.4, 106.6)}.items():
        o_[i], c[i] = op, cl
        h_[i], l_[i] = op, cl - 0.01
    o_[302], c[302], h_[302], l_[302] = 106.7, 106.6, 107.2, 106.1
    pos = long_events(o_, h_, l_, c, ns, 5 * NS_MIN, 50, 0.05, "close")
    # (the real close at 300..302 must stay above the EMA for the trend test: the series is a steady up trend with a short dip)
    chk("the full pipeline returns only decision bars at or after warm-up", bool((pos >= WARM * 50).all()), True)
    # mirror symmetry: negating prices and swapping high/low moves long events to short events at the same bars
    neg = long_events(-o_, -l_, -h_, -c, ns, 5 * NS_MIN, 50, 0.05, "close")
    # a negated up-trend is a down-trend: no LONG events in it (price below EMA), so the mirror of the long events must be found by running the short orientation on the original
    chk("mirror: long events of a pure up-trend are absent in its negation", len(neg), 0)
    # past-only: truncating the future leaves earlier decisions unchanged
    cut = 330
    pos_cut = long_events(o_[:cut], h_[:cut], l_[:cut], c[:cut], ns[:cut], 5 * NS_MIN, 50, 0.05, "close")
    chk("past-only: events before the cut are identical without the future", list(pos_cut), [int(p) for p in pos if p < cut])
    # contiguity: a gap inside the three setup bars removes the event
    if len(pos):
        p0 = int(pos[0])
        ns2 = ns.copy()
        ns2[p0 - 1:] += 7 * NS_MIN
        pos_gap = long_events(o_, h_, l_, c, ns2, 5 * NS_MIN, 50, 0.05, "close")
        chk("a gap between the setup bars removes the event", p0 in set(int(x) for x in pos_gap), False)
    # spacing
    chk("spacing keeps one event per 12 bars", list(space(np.array([20, 25, 31, 32, 50, 61]))), [20, 32, 50])
    # usable window
    ns3 = np.arange(300, dtype=np.int64) * NS_MIN
    chk("usable requires a gap-free H-bar window inside development data", list(usable(np.array([10, 150, 250]), ns3, NS_MIN, hold_ns=10 ** 30)), [10, 150])
    ns4 = ns3.copy()
    ns4[60:] += NS_MIN
    chk("a gap inside the window drops the event", list(usable(np.array([10, 150]), ns4, NS_MIN, hold_ns=10 ** 30)), [150])
    chk("holdout cut: a window ending at the holdout is dropped", list(usable(np.array([10]), ns3, NS_MIN, hold_ns=int(ns3[106]))), [])
    ix = pd.date_range("2025-01-01", periods=3, freq="15min", tz="UTC").as_unit("us")
    chk("ns_of returns nanoseconds even for a microsecond index (15-minute step = 9e11 ns)", int(np.diff(ns_of(ix))[0]), 15 * NS_MIN)
    # --- zones ---
    m = 40
    ns1 = np.arange(m, dtype=np.int64) * H1_NS
    h1 = np.full(m, 100.0); l1 = np.full(m, 99.0); c1 = np.full(m, 99.5)
    h1[10] = 101.0                                        # a clear swing high at bar 10
    h1 = h1 + np.linspace(0, 0.0001, m)                   # remove ties
    lv = swing_levels(ns1, h1, l1 + np.linspace(0, 0.0001, m), c1, 3)
    zs = [z for z in lv if z["kind"] == "res" and z["lo"] < 101.0 < z["hi"]]
    chk("a swing high at bar 10 (k=3) becomes eligible exactly at the close of bar 13", [z["t0"] for z in zs], [int(ns1[13] + H1_NS)])
    chk("no swing is confirmed at a bar without k bars after it", all(z["t0"] <= int(ns1[m - 1] + H1_NS) for z in lv), True)
    # touch needs approach from outside
    ns = np.arange(30, dtype=np.int64) * 15 * NS_MIN
    z = [{"kind": "res", "lo": 101.0, "hi": 101.5, "t0": 0, "t1": 10 ** 18}]
    hh_ = np.full(30, 100.0); ll_ = np.full(30, 99.5); cc_ = np.full(30, 99.8)
    hh_[10] = 101.2; hh_[11] = 101.3; cc_[10] = 101.1; cc_[11] = 101.0   # bar 10 enters from outside; bar 11's previous close is already inside
    tr_, ts_ = zone_touch_flags(ns, hh_, ll_, cc_, z)
    chk("a bar entering a resistance zone from below is a touch; the next bar (already inside) is not", (bool(tr_[10]), bool(tr_[11]), bool(ts_.any())), (True, False, False))
    z2 = [{"kind": "res", "lo": 101.0, "hi": 101.5, "t0": int(ns[12]), "t1": 10 ** 18}]
    chk("a zone is not eligible before its confirmation time", bool(zone_touch_flags(ns, hh_, ll_, cc_, z2)[0][10]), False)
    chk("doji test: body/range 0.2 yes, 0.3 no", (bool(doji_flags(np.array([10.0]), np.array([11.0]), np.array([9.0]), np.array([10.4]))[0]), bool(doji_flags(np.array([10.0]), np.array([11.0]), np.array([9.0]), np.array([10.6]))[0])), (True, False))
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
        with open(os.path.join(a.out, "studyl_counts.json"), "w") as f:
            json.dump(r, f, indent=2, default=str)
        md = render_markdown(r)
        with open(os.path.join(a.out, "studyl_counts.md"), "w") as f:
            f.write(md)
        print(md)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
