"""
Observatory — STUDY J, STAGE 1: THE SWEEP -> CONFIRMATION -> POI -> TRIGGER LADDER, COUNTS ONLY (GBPUSD, development data only). Read-only. The holdout stays sealed.

WHAT THIS IS
  A trading-video model ("sweep liquidity, confirm with a structure/state change, return to a point of interest, enter on a candle break") is four hypotheses stacked, plus CISD as
  an alternative confirmation. This file DEFINES every event mechanically, builds the full funnel and COUNTS it. THERE IS NO OUTCOME CODE HERE: no barrier, no return, no estimate.
  The counts decide which rungs are testable at all (a funnel thins fast) before any claim is registered.

ORIENTATION. Everything is written for the BEARISH reversal (a swept HIGH predicts DOWN). The bullish setup is the same code on negated prices (high<->-low, close->-close); both are pooled
  and mirrored into the predicted direction. Continuation setups are a different hypothesis and are NOT in this study.

DEFINITIONS (fixed before any outcome exists; every rule uses bars up to the decision bar only)
  Level      the latest CONFIRMED 1h swing high (pivot: strictly higher than the 2 bars either side, gap-free; confirmed at the close of the 2nd bar after it), at most 72h old, not yet pierced.
  SWEEP      first 5m bar whose HIGH exceeds the level, AND a 5m CLOSE back at or below the level within 12 bars (1h) of the pierce, gap-free. Decision bar r = that reclaim close.
             No reclaim within 12 bars = an acceptance (a break, not a sweep): counted, not an event. One event per pierce bar (several levels pierced by one bar: the highest level).
             Extreme bar s = the bar of the highest high between pierce and reclaim; ext = its high; overshoot = (ext - level) / ATR(1h).
             INVALIDATION: any later 5m high above ext before a node fires kills that setup (the sweep failed).
  Delivery series   the maximal run of consecutive UP-closing 5m bars ending at e (e = s if s closed up, else the bar before s if that closed up); at most 48 bars; needs >= 2 bars for CISD.
  CISD       first 5m CLOSE below the OPEN of the first bar of the delivery series, at or after r, within 48 bars (4h) of r, before invalidation.        (change in state of delivery)
  BOS        first 5m CLOSE below the latest confirmed 5m swing low (fractal, 3 bars each side, confirmed at the close of the 3rd bar after it) that was confirmed by the bar s and lies at most
             96 bars before s, at or after r, within 48 bars of r, before invalidation.                                                         (break of structure)
  POI zones (bearish, all known when formed; wick-to-wick ranges)
     OB       the delivery series' last up-closing bar e: [low_e, high_e].
     RB       the upper wick of the sweep bar [max(open_s, close_s), high_s], only if >= 0.25 ATR tall.
     BREAKER  the bar immediately before the delivery series if it closed DOWN (a failed bullish order block): [low_d, high_d]; it FORMS at the first 5m close below low_d after s.
  RETEST     after max(root time, zone formation): the first bar within 96 bars whose high reaches the zone's low while its CLOSE stays at or below the zone's high; the root bar's close must be
             below the zone (otherwise price was already in it: not an event). A close above the zone's high, or a high above ext, kills the zone.
  BOC        after a retest bar: the first of the next 12 bars that CLOSES below the previous bar's low, with no close above the zone's high and no high above ext before it. Decision bar b.
  Chains: root in {CISD, BOS}  x  zone in {OB, RB, BREAKER}  ->  retest -> BOC.  Six chains, counted separately. Nothing is pooled across chains.

FUNNEL FACTS COUNTED PER NODE
  events; usable (a gap-free 24h outcome window from the node's decision bar, inside development data); weekday of the decision bar; recent/older split at the block boundary; distinct
  2-day blocks; detectable effect from the counts (standards S6); volatility-state-matched placebo supply (clock = UTC weekday/hour, +-15% trailing-24h range); the elapsed-time distribution
  from the parent node; the CONTROL SUPPLY a later outcome test would use: parent events that have NOT produced this node by the same elapsed time (so the node is compared with the same
  setup at the same decision age, not with setups that were never going to confirm).

Run:  python3 -m observatory.studyj --counts [--raw-dir ...] [--out ...] [--boundary 2024-10-03]      python3 -m observatory.studyj --selftest
"""
import argparse
import json
import math
import os
import sys

import numpy as np
import pandas as pd

from . import clean as rc
from . import studya as sa
from . import studyb as sb
from . import studyi as si
from . import standards as st
from .studyb import NS_5, NS_H, NS_D, HMAX

CODE_VERSION = "studyj-counts-v1"
K1H, K5 = 2, 3
LEVEL_LIFE_NS = 72 * NS_H
R_RECLAIM = 12
W_CONF = 48
SERIES_MAX, SERIES_MIN = 48, 2
STRUCT_BACK = 96
RB_MIN_ATR = 0.25
RETEST_WIN, BOC_WIN = 96, 12
ROOTS = ("cisd", "bos")
ZONES = ("ob", "rb", "brk")
DEFAULT_BOUNDARY = "2024-10-03"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def run_end(ns):
    """run_end[i] = last index j >= i such that bars i..j are consecutive 5m bars."""
    n = len(ns)
    brk = np.flatnonzero(np.diff(ns) != NS_5)
    k = np.searchsorted(brk, np.arange(n), side="left")
    return np.where(k < len(brk), brk[np.minimum(k, max(len(brk) - 1, 0))] if len(brk) else n - 1, n - 1)


def pivots_high(h, ns, k, step):
    """Indices i with h[i] strictly above the k bars on each side and a gap-free window (ns[i+k]-ns[i-k] == 2k steps)."""
    n = len(h)
    if n < 2 * k + 1:
        return np.array([], int)
    i = np.arange(k, n - k)
    ok = (ns[i + k] - ns[i - k]) == 2 * k * step
    for d in range(1, k + 1):
        ok &= (h[i] > h[i - d]) & (h[i] > h[i + d])
    return i[ok]


def pivots_low(l, ns, k, step):
    return pivots_high(-l, ns, k, step)


def orient(arrs, sign):
    """sign +1 as is; -1 negated with high<->low (a swept LOW becomes a swept HIGH)."""
    o, h, l, c = arrs
    return (o, h, l, c) if sign > 0 else (-o, -l, -h, -c)


# ---------------------------------------------------------------------------
# one sweep: nodes
# ---------------------------------------------------------------------------
def _first(mask):
    w = np.flatnonzero(mask)
    return int(w[0]) if w.size else None


def _retest_and_boc(A, RE, zlow, zhigh, start0, ext):
    o, h, l, c, ns = A["o"], A["h"], A["l"], A["c"], A["ns"]
    n = len(c)
    if start0 >= n - 1 or not (c[start0] < zlow):
        return None, None
    lim = min(start0 + RETEST_WIN, RE[start0], n - 1)
    if lim <= start0:
        return None, None
    seg_h = h[start0 + 1:lim + 1]
    u_rel = _first(seg_h >= zlow)
    if u_rel is None:
        return None, None
    u = start0 + 1 + u_rel
    if c[u] > zhigh or h[u] > ext or np.any(h[start0 + 1:u] > ext):
        return None, None
    lim2 = min(u + BOC_WIN, RE[u], n - 1)
    boc = None
    if lim2 > u:
        idx = np.arange(u + 1, lim2 + 1)
        trig = c[idx] < l[idx - 1]
        dead = (c[idx] > zhigh) | (h[idx] > ext)
        t_rel, d_rel = _first(trig), _first(dead)
        if t_rel is not None and (d_rel is None or t_rel < d_rel):
            boc = int(idx[t_rel])
    return u, boc


def analyze_sweep(A, RE, q, r, H, pl_conf):
    """A: dict of oriented 5m arrays o,h,l,c,ns,atr. q = pierce bar, r = reclaim bar, H = level. pl_conf: sorted array of (pivot index, confirmation index) rows as two arrays.
    Returns a dict of node decision-bar indices (absent = no such event) plus geometry."""
    o, h, l, c, ns, atr = A["o"], A["h"], A["l"], A["c"], A["ns"], A["atr"]
    n = len(c)
    out = {"q": int(q), "r": int(r), "H": float(H), "nodes": {}}
    s = q + int(np.argmax(h[q:r + 1]))
    ext = float(h[s])
    a_r = float(atr[r])
    out["s"], out["ext"] = int(s), ext
    out["over_atr"] = float((ext - H) / a_r) if np.isfinite(a_r) and a_r > 0 else float("nan")
    lim = min(r + W_CONF, RE[r], n - 1)
    inv_rel = _first(h[r:lim + 1] > ext)
    inv = r + inv_rel if inv_rel is not None else lim + 1
    out["nodes"]["sweep"] = int(r)
    # delivery series
    e = s if c[s] > o[s] else s - 1
    f = None
    if e >= 0 and c[e] > o[e] and (ns[s] - ns[e]) == (s - e) * NS_5:
        f = e
        while f - 1 >= 0 and c[f - 1] > o[f - 1] and (ns[f] - ns[f - 1]) == NS_5 and (e - f + 1) < SERIES_MAX:
            f -= 1
    out["series_len"] = int(e - f + 1) if f is not None else 0
    roots = {}
    if f is not None and (e - f + 1) >= SERIES_MIN:
        K = o[f]
        m = _first(c[r:lim + 1] < K)
        if m is not None and r + m < inv:
            roots["cisd"] = r + m
    pj, pc = pl_conf
    if len(pj):
        pos = int(np.searchsorted(pc, s, side="right")) - 1
        if pos >= 0 and pj[pos] >= s - STRUCT_BACK:
            B = l[pj[pos]]
            m = _first(c[r:lim + 1] < B)
            if m is not None and r + m < inv:
                roots["bos"] = r + m
    for k, v in roots.items():
        out["nodes"][k] = int(v)
    if len(roots) == 2:
        out["nodes"]["both"] = int(max(roots.values()))
    # zones
    zones = {}
    if f is not None:
        zones["ob"] = (float(l[e]), float(h[e]), s)
        d = f - 1
        if d >= 0 and c[d] < o[d] and (ns[f] - ns[d]) == NS_5:
            zl, zh = float(l[d]), float(h[d])
            mf = _first(c[s + 1:lim + 1] < zl)
            if mf is not None:
                zones["brk"] = (zl, zh, s + 1 + mf)
    wl = max(o[s], c[s])
    if np.isfinite(a_r) and a_r > 0 and (h[s] - wl) / a_r >= RB_MIN_ATR:
        zones["rb"] = (float(wl), float(h[s]), s)
    for rname, rt in roots.items():
        for zname, (zl, zh, zt) in zones.items():
            u, b = _retest_and_boc(A, RE, zl, zh, max(rt, zt), ext)
            if u is not None:
                out["nodes"][f"{rname}:{zname}:retest"] = int(u)
            if b is not None:
                out["nodes"][f"{rname}:{zname}:boc"] = int(b)
    return out


# ---------------------------------------------------------------------------
# whole series: all sweeps in one orientation
# ---------------------------------------------------------------------------
def detect_sweeps(A5, h1, ns1, atr_unused=None):
    """All sweep events in one orientation. A5: dict o,h,l,c,ns,atr (oriented 5m). h1, ns1: oriented 1h highs and times. Returns (events, counters)."""
    ns, h, c = A5["ns"], A5["h"], A5["c"]
    n = len(ns)
    RE = run_end(ns)
    A5 = dict(A5)
    cnt = {"pivots_1h": 0, "pierced": 0, "reclaimed": 0, "acceptance": 0, "gap_in_reclaim_window": 0, "expired_unpierced": 0, "merged_same_pierce": 0, "atr_missing": 0}
    pv = pivots_high(h1, ns1, K1H, NS_H)
    cnt["pivots_1h"] = int(len(pv))
    pj = pivots_low(A5["l"], ns, K5, NS_5)
    pl = (pj, pj + K5)
    best = {}
    for i in pv:
        H = h1[i]
        conf_t = ns1[i + K1H] + NS_H
        q0 = int(np.searchsorted(ns, conf_t, side="left"))
        q1 = int(np.searchsorted(ns, ns1[i] + LEVEL_LIFE_NS, side="right"))
        if q0 >= n:
            continue
        w = _first(h[q0:q1] > H)
        if w is None:
            cnt["expired_unpierced"] += 1
            continue
        q = q0 + w
        cnt["pierced"] += 1
        hi = min(q + R_RECLAIM, n)
        t = _first(c[q:hi] <= H)
        if t is None:
            cnt["acceptance"] += 1
            continue
        r = q + t
        if ns[r] - ns[q] != t * NS_5:
            cnt["gap_in_reclaim_window"] += 1
            continue
        if not (np.isfinite(A5["atr"][r]) and A5["atr"][r] > 0):
            cnt["atr_missing"] += 1
            continue
        if q in best:
            cnt["merged_same_pierce"] += 1
            if H <= best[q][0]:
                continue
        best[q] = (float(H), r)
    cnt["reclaimed"] = len(best)
    events = [analyze_sweep(A5, RE, q, r, H, pl) for q, (H, r) in sorted(best.items())]
    return events, cnt


# ---------------------------------------------------------------------------
# counting
# ---------------------------------------------------------------------------
NODE_PARENT = {"sweep": None, "cisd": "sweep", "bos": "sweep", "both": "sweep"}
for _r in ROOTS:
    for _z in ZONES:
        NODE_PARENT[f"{_r}:{_z}:retest"] = _r
        NODE_PARENT[f"{_r}:{_z}:boc"] = f"{_r}:{_z}:retest"
NODE_ORDER = ["sweep", "cisd", "bos", "both"] + [f"{r}:{z}:{k}" for r in ROOTS for z in ZONES for k in ("retest", "boc")]


def count_nodes(evs_by_sign, ns5, hold_ns, bnd_ns, plc):
    """evs_by_sign: {sign: [event dicts]}; plc(p) -> placebo candidate dict or None. Returns the funnel table."""
    flat = [(sg, e) for sg, es in evs_by_sign.items() for e in es]
    res = {}
    for node in NODE_ORDER:
        rows = [(sg, e, e["nodes"][node]) for sg, e in flat if node in e["nodes"]]
        usable = [(sg, e, p) for sg, e, p in rows if si.window_status(ns5, p) is None and ns5[p] + (HMAX + 1) * NS_5 <= hold_ns]
        drops = {}
        for sg, e, p in rows:
            w = si.window_status(ns5, p) if ns5[p] + (HMAX + 1) * NS_5 <= hold_ns else "data_end"
            if w is not None:
                drops[w] = drops.get(w, 0) + 1
        t_u = np.array([ns5[p] for _, _, p in usable], np.int64)
        nb = len({int(t // (2 * NS_D)) for t in t_u})
        wd = si.wd_counts(pd.DatetimeIndex(t_u.astype("datetime64[ns]")).dayofweek.to_numpy()) if len(t_u) else [0] * 7
        par = NODE_PARENT[node]
        entry = {"events": len(rows), "usable": len(usable), "dropped_reasons": drops, "weekday_Mon_Sun": wd, "recent": int((t_u >= bnd_ns).sum()), "older": int((t_u < bnd_ns).sum()), "blocks_2day": nb,
                 "feasibility": st.feasibility(len(usable), nb), "recent_vs_older_detectable_diff": st.block_diff_detectable(int((t_u >= bnd_ns).sum()), int((t_u < bnd_ns).sum())),
                 "unique_events": len({(sg, e["q"]) for sg, e, _ in rows}) if node in ("sweep", "cisd", "bos", "both") else len({(sg, e["q"], node) for sg, e, _ in rows})}
        pcs = [plc(int(p)) for _, _, p in usable]
        pcs = [x for x in pcs if x is not None]
        entry["placebo"] = {"with_vol_state": len(pcs), "median_candidates_vol0.15": float(np.median([x["vol0.15"] for x in pcs])) if pcs else None,
                            "share_below_30": float(np.mean([x["vol0.15"] < 30 for x in pcs])) if pcs else None}
        if par:
            pr = {(sg, e["q"]): e["nodes"][par] for sg, e in flat if par in e["nodes"]}
            dts = [(e["nodes"][node] - e["nodes"][par]) * 5 for sg, e in flat if node in e["nodes"] and par in e["nodes"]]
            entry["parent_events"] = len(pr)
            entry["elapsed_min_from_parent"] = {"median": float(np.median(dts)), "p90": float(np.percentile(dts, 90)), "same_bar": int(sum(d == 0 for d in dts))} if dts else {}
            # control supply: parent events with no such node by the median elapsed time (past-only at that age) and a usable window from there
            if dts:
                med = int(round(np.median(dts) / 5))
                ctrl = 0
                for sg, e in flat:
                    if par not in e["nodes"]:
                        continue
                    pp = e["nodes"][par]
                    if node in e["nodes"] and e["nodes"][node] <= pp + med:
                        continue
                    p2 = pp + med
                    if p2 < len(ns5) and ns5[p2] - ns5[pp] == med * NS_5 and si.window_status(ns5, p2) is None and ns5[p2] + (HMAX + 1) * NS_5 <= hold_ns:
                        ctrl += 1
                entry["control_supply_not_yet_at_median_elapsed"] = ctrl
        res[node] = entry
    return res


def run_counts(raw_dir=None, source="observatory", boundary=DEFAULT_BOUNDARY, preloaded=None):
    raw_dir = raw_dir or rc.DEFAULT_RAW_DIR
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    bnd_ns = pd.Timestamp(boundary, tz="UTC").as_unit("ns").value
    ctx = sb.make_ctx(s5, s1h, +1, hold_ns)
    ns5, atr_t, x = ctx["ns5"], ctx["atr_t"], ctx["x"]
    nd = int((ns5 < hold_ns).sum())
    ns1 = sa._ns(s1h.index)
    nd1 = int((ns1 < hold_ns).sum())
    good_q = np.concatenate([p[0] for p in ctx["pools"].values()]) if ctx["pools"] else np.array([], int)
    keys, naive = si.clock_keys(ns5[:nd], "UTC")
    pools = si.build_pools(good_q, keys)
    vs = si.vol_state_clock(ns5[:nd], s5.h[:nd], s5.l[:nd], atr_t[:nd])
    plc = lambda p: si.placebo_counts(p, ns5[:nd], naive, pools, x, vs)
    base = (s5.o[:nd], s5.h[:nd], s5.l[:nd], s5.c[:nd])
    b1 = (s1h.h[:nd1], s1h.l[:nd1])
    evs, cnts = {}, {}
    for sign in (+1, -1):
        o, h, l, c = orient(base, sign)
        h1 = b1[0] if sign > 0 else -b1[1]
        A5 = {"o": o, "h": h, "l": l, "c": c, "ns": ns5[:nd], "atr": atr_t[:nd]}
        evs[sign], cnts[sign] = detect_sweeps(A5, h1, ns1[:nd1])
    res = {"dataset_status": status, "boundary_utc": boundary, "holdout_opened": False, "code_version": CODE_VERSION, "development_first_bar": str(s5.index[0]),
           "level_counters": {"bearish(high sweeps)": cnts[+1], "bullish(low sweeps, mirrored)": cnts[-1]},
           "setup_flags": {}, "funnel": count_nodes(evs, ns5[:nd], hold_ns, bnd_ns, plc)}
    flat = [e for es in evs.values() for e in es]
    ov = [e["over_atr"] for e in flat if np.isfinite(e["over_atr"])]
    res["sweep_geometry"] = {"overshoot_atr": si._quant(ov), "series_len_quantiles": si._quant([e["series_len"] for e in flat]), "no_delivery_series": int(sum(e["series_len"] == 0 for e in flat))}
    res["node_overlap"] = {"cisd_and_bos": int(sum("cisd" in e["nodes"] and "bos" in e["nodes"] for e in flat)), "cisd_only": int(sum("cisd" in e["nodes"] and "bos" not in e["nodes"] for e in flat)),
                           "bos_only": int(sum("bos" in e["nodes"] and "cisd" not in e["nodes"] for e in flat)), "neither": int(sum("cisd" not in e["nodes"] and "bos" not in e["nodes"] for e in flat)),
                           "same_decision_bar": int(sum("cisd" in e["nodes"] and "bos" in e["nodes"] and e["nodes"]["cisd"] == e["nodes"]["bos"] for e in flat))}
    return res


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _f3(x):
    return "n/a" if x is None else f"{x:.3f}"


def render_markdown(r):
    L = ["# Observatory — Study J, stage 1: the sweep → confirmation → POI → trigger funnel, counts only (GBPUSD)\n",
         f"Dataset **{r['dataset_status']}** · first bar {r['development_first_bar']} · block boundary {r['boundary_utc']} · holdout opened: **False** · code version **{r['code_version']}** · **no outcome has been computed in this report**\n",
         "## Level and sweep counters (before any confirmation)\n"]
    for k, v in r["level_counters"].items():
        L.append(f"- {k}: {v}")
    g = r["sweep_geometry"]
    L += [f"\nSweep geometry: overshoot (ATR) {g['overshoot_atr']} · delivery-series length {g['series_len_quantiles']} · sweeps without a delivery series: {g['no_delivery_series']}",
          f"CISD / BOS overlap among sweeps: {r['node_overlap']}\n", "## Funnel\n",
          "| node | events | usable (24h window) | recent / older | 2-day blocks | detectable (opt / cons) | tier | Mon..Fri | placebo median cand. (vol ±15%) | parent events | median elapsed from parent (min) | control supply (not-yet at median age) |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for node in NODE_ORDER:
        v = r["funnel"][node]
        fz = v["feasibility"]
        el = v.get("elapsed_min_from_parent", {})
        L.append(f"| {node} | {v['events']} | {v['usable']} | {v['recent']} / {v['older']} | {v['blocks_2day']} | {_f3(fz.get('detectable_optimistic'))} / {_f3(fz.get('detectable_conservative'))} | {fz['tier']} | {v['weekday_Mon_Sun'][:5]} | "
                 f"{v['placebo']['median_candidates_vol0.15']} | {v.get('parent_events', '-')} | {el.get('median', '-')} | {v.get('control_supply_not_yet_at_median_elapsed', '-')} |")
    L += ["\nDropped at the outcome-window stage (reasons) and recent-vs-older detectable differences are in the json.\n", "## Not done here\n",
          "- Any outcome, estimate or test; the registration of families; continuation setups; FVG/IFVG zones (Study of FVGs already done); variants of any parameter above."]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------
def _bars(rows, t0="2025-03-03 00:00"):
    t0n = pd.Timestamp(t0, tz="UTC").as_unit("ns").value
    a = np.array(rows, float)
    ns = t0n + np.arange(len(a), dtype=np.int64) * NS_5
    return {"o": a[:, 0], "h": a[:, 1], "l": a[:, 2], "c": a[:, 3], "ns": ns, "atr": np.full(len(a), 0.1)}


BASE_ROWS = [
    (10.0, 10.1, 9.8, 9.9),     # 0  d: down-closing bar = failed-OB candidate, zone [9.8, 10.1]
    (9.9, 10.25, 9.9, 10.2),    # 1  series start f (open 9.9 = CISD level)
    (10.2, 10.55, 10.2, 10.5),  # 2
    (10.5, 10.85, 10.5, 10.8),  # 3  e
    (10.8, 11.3, 10.7, 10.75),  # 4  sweep bar s: pierces 11.0, closes back at 10.75 (down-close)
    (10.75, 10.8, 10.2, 10.3),  # 5
    (10.3, 10.35, 9.85, 9.88),  # 6  closes below 9.9 -> CISD
    (9.88, 9.9, 9.6, 9.65),     # 7  closes below swing low 9.8 -> BOS; closes below breaker low -> breaker forms
    (9.65, 9.7, 9.5, 9.55),     # 8
    (9.55, 9.85, 9.55, 9.8),    # 9  retest of the breaker zone from below (high 9.85 >= 9.8, close 9.8 <= 10.1)
    (9.8, 9.82, 9.6, 9.62),     # 10
    (9.62, 9.65, 9.4, 9.45),    # 11 closes below previous low 9.6 -> BOC
    (9.45, 9.5, 9.3, 9.35),
]
SWING_LOW = (np.array([0]), np.array([0 + K5]))     # swing-low pivot at bar 0 (low 9.8), confirmed at bar 3


def selftest():
    checks = []

    def chk(name, got, want):
        checks.append({"check": name, "ok": bool(got == want), "got": got, "want": want})

    A = _bars(BASE_ROWS)
    RE = run_end(A["ns"])
    ev = analyze_sweep(A, RE, 4, 4, 11.0, SWING_LOW)
    nd = ev["nodes"]
    chk("sweep decision bar = the reclaim close", nd["sweep"], 4)
    chk("CISD fires on the first close below the open of the delivery series", nd.get("cisd"), 6)
    chk("BOS fires on the first close below the confirmed swing low", nd.get("bos"), 7)
    chk("breaker chain: retest bar", nd.get("cisd:brk:retest"), 9)
    chk("breaker chain: BOC bar", nd.get("cisd:brk:boc"), 11)
    chk("same chain from the BOS root", (nd.get("bos:brk:retest"), nd.get("bos:brk:boc")), (9, 11))
    chk("OB zone never revisited -> no OB retest", "cisd:ob:retest" in nd, False)
    chk("RB zone never revisited -> no RB retest", "cisd:rb:retest" in nd, False)
    chk("delivery series length", ev["series_len"], 3)
    chk("overshoot in ATR", round(ev["over_atr"], 3), 3.0)
    # invalidation: a new high above the sweep extreme before CISD kills the setup
    rows = list(BASE_ROWS)
    rows[5] = (10.75, 11.5, 10.2, 10.3)
    B = _bars(rows)
    nd2 = analyze_sweep(B, run_end(B["ns"]), 4, 4, 11.0, SWING_LOW)["nodes"]
    chk("a high above the extreme before the break kills CISD and BOS", ("cisd" in nd2, "bos" in nd2), (False, False))
    # a close above the zone high kills the BOC
    rows = list(BASE_ROWS)
    rows[10] = (9.8, 10.5, 9.6, 10.3)          # closes above the zone high 10.1 right after the retest
    C = _bars(rows)
    nd3 = analyze_sweep(C, run_end(C["ns"]), 4, 4, 11.0, SWING_LOW)["nodes"]
    chk("a close above the zone high after the retest kills the BOC (retest remains)", (nd3.get("cisd:brk:retest"), "cisd:brk:boc" in nd3), (9, False))
    # a delivery series shorter than 2 bars gives no CISD
    rows = list(BASE_ROWS)
    rows[2] = (10.5, 10.55, 10.2, 10.3)     # bar 2 closes down: the series is bars 3 only
    D = _bars(rows)
    evd = analyze_sweep(D, run_end(D["ns"]), 4, 4, 11.0, SWING_LOW)
    chk("series of length 1 -> no CISD", (evd["series_len"], "cisd" in evd["nodes"]), (1, False))
    # a gap inside the confirmation window ends it
    E = _bars(BASE_ROWS)
    E["ns"] = E["ns"].copy()
    E["ns"][6:] += 7 * NS_H
    nd4 = analyze_sweep(E, run_end(E["ns"]), 4, 4, 11.0, SWING_LOW)["nodes"]
    chk("a gap before the break: no CISD/BOS", ("cisd" in nd4, "bos" in nd4), (False, False))
    # pivots
    h1 = np.array([1, 2, 5, 3, 2, 1, 4, 6, 4, 1.0])
    ns1 = np.arange(len(h1), dtype=np.int64) * NS_H
    chk("1h pivot highs (strict, 2 bars each side)", list(pivots_high(h1, ns1, 2, NS_H)), [2, 7])
    ns1g = ns1.copy()
    ns1g[8:] += 3 * NS_H
    chk("a gap inside the pivot window disqualifies that pivot only", list(pivots_high(h1, ns1g, 2, NS_H)), [2])
    # whole-series detection: bearish level swept and reclaimed
    rng = np.random.default_rng(5)
    n = 900
    c = 100 + np.cumsum(rng.normal(0, 0.05, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.03, n))
    l = np.minimum(o, c) - np.abs(rng.normal(0, 0.03, n))
    ns = pd.Timestamp("2025-03-03", tz="UTC").as_unit("ns").value + np.arange(n, dtype=np.int64) * NS_5
    atr = np.full(n, 0.2)
    ns1 = ns[::12][:n // 12]
    h1 = np.array([h[i * 12:(i + 1) * 12].max() for i in range(len(ns1))])
    A5 = {"o": o, "h": h, "l": l, "c": c, "ns": ns, "atr": atr}
    e_all, _ = detect_sweeps(A5, h1, ns1)
    # mirror symmetry: negated prices with the orientation flag give the mirror image
    nA = dict(zip(("o", "h", "l", "c"), orient((o, h, l, c), -1)))
    nA.update(ns=ns, atr=atr)
    nh1 = -np.array([l[i * 12:(i + 1) * 12].min() for i in range(len(ns1))])
    e_neg, _ = detect_sweeps(nA, nh1, ns1)
    chk("detect twice gives identical events (determinism)", [x["nodes"] for x in detect_sweeps(A5, h1, ns1)[0]], [x["nodes"] for x in e_all])
    chk("the random-walk test has events to compare (not vacuous)", (len(e_all) >= 5, len([x for x in e_all if "cisd" in x["nodes"]]) >= 1), (True, True))
    # mirror: the bullish image of the hand-built bearish sequence, run through orient(-1), reproduces the same nodes
    mir = np.array(BASE_ROWS, float)
    mo, mh, ml, mc = -mir[:, 0], -mir[:, 2], -mir[:, 1], -mir[:, 3]        # a genuine bullish series (high/low swapped under negation)
    Mx = dict(zip(("o", "h", "l", "c"), orient((mo, mh, ml, mc), -1)))
    Mx.update(ns=_bars(BASE_ROWS)["ns"], atr=np.full(len(mo), 0.1))
    ndm = analyze_sweep(Mx, run_end(Mx["ns"]), 4, 4, 11.0, SWING_LOW)["nodes"]
    chk("mirror symmetry: the bullish image gives the same nodes after orientation", ndm, nd)
    # past-only: events whose nodes all lie before a cut are the same with or without the future
    cut = 600
    A5c = {k: (v[:cut] if hasattr(v, "__len__") else v) for k, v in A5.items()}
    h1c, ns1c = h1[:cut // 12 - 1], ns1[:cut // 12 - 1]
    e_cut, _ = detect_sweeps(A5c, h1c, ns1c)
    chk("past-only comparison is not vacuous", len(safe_ref := [x for x in e_all if x["q"] < cut - 400]) >= 1, True)
    safe = lambda es: sorted((x["q"], tuple(sorted(x["nodes"].items()))) for x in es if max(x["nodes"].values()) < cut - 2 * W_CONF - RETEST_WIN - BOC_WIN)
    chk("past-only: events fully formed before the cut are unchanged when the future is removed", safe(e_cut), safe([x for x in e_all if x["q"] < cut]))
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
        with open(os.path.join(a.out, "studyj_counts.json"), "w") as f:
            json.dump(r, f, indent=2, default=str)
        md = render_markdown(r)
        with open(os.path.join(a.out, "studyj_counts.md"), "w") as f:
            f.write(md)
        print(md)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
