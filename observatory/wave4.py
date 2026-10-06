"""
observatory/wave4.py
====================
OBSERVATORY — Wave 4: CRT (sweep-and-reclaim) and outside-bar claims. ISOLATED from the live bot.

Higher timeframe = 1h. Trigger / measurement timeframes: 5m and 15m (two separate configurations,
1h->5m and 1h->15m). 4H is not used.

FROZEN CONTRACT v1 (standard definitions; frozen 2026-10-06 before any Wave 4 result was seen)
-----------------------------------------------------------------------------------------------
  C1, C2        two consecutive, gap-free 1h candles. C1 defines the range [low1, high1].
  Sweep         C2 trades beyond ONE side of C1 and not the other:
                  sweep-high: high2 > high1 and low2 >= low1       (trade direction: SHORT)
                  sweep-low : low2  < low1  and high2 <= high1     (trade direction: LONG)
                C2 that breaks both sides is excluded.
  RECLAIM       C2 closes back inside C1's range (sweep-high: close2 < high1; sweep-low: close2 > low1).
                This is the CRT signal. Known at C2's close.
  ACCEPTANCE    C2 closes beyond the swept side (the control for P3).
  Trade         enter at the open of the first trigger-timeframe bar after C2 closes.
                stop = the sweep extreme (high2 or low2); target = the far side of C1 (low1 or high1).
                R-geometry varies by event: stop distance d_s and target distance d_t (RR = d_t / d_s).
                Eligible only if both distances >= 0.5 x ATR14(1h) at C2's close.
  Outcome       first touch of target vs stop on trigger-timeframe candles, horizon 6 hours
                (72 x 5m / 24 x 15m); ambiguous 15m bars settled with 5m where possible.
  Null          a driftless market gives P(target first) = d_s / (d_s + d_t) = 1 / (1 + RR). So each event
                is scored as  excess = 1{target first} - 1/(1 + RR)  (expected 0 with no edge), whatever its RR.
                Mean R (+RR on a win, -1 on a loss) is shown beside it; gross, and net of 1 pip.
  Episodes      per (group, side), anchored, K = 6 hours (= the horizon, so windows never overlap).
  P2 (primary)  mean excess of RECLAIM events, per configuration (2 tests). Practical threshold 5 pp.
  P3 (primary)  mean excess of RECLAIM minus ACCEPTANCE, stratified by side (2 tests). The acceptance group is small by construction: an acceptance close
                near the sweep extreme leaves a stop distance below the 0.5 ATR minimum, so only acceptance closes with a long wick
                qualify. P3 is therefore weak evidence either way.
  O1 (primary)  outside bar ALONE (no sweep): a trigger-timeframe bar with high > previous high and low <
                previous low; trade = continuation of its colour, next-open entry, +-1R = ATR14, 24 bars;
                versus ordinary bars matched on direction x session x ATR tercile x bar-size bin (2 tests).
  FDR           Benjamini-Hochberg over the 6 primary tests, q = 0.10.
  Descriptive  by side, by RR bucket (<1, 1-1.5, 1.5-2.5, >=2.5: the ">= 1.5R" question), by C1-range
                tercile (P1: how often a sweep happens), net of spread.
  Split         the frozen absolute holdout date; development events are purged; holdout SEALED
                (this module cannot open it).
  Deferred      P4 (outside bar AFTER a sweep) until P2/P3 and O1 are known; P5 (order-block location) until an
                objective order-block definition is frozen; P6 (combinations).

Run:  python3 -m observatory.wave4                (real data)
      python3 -m observatory.wave4 --selftest     (hand-built geometry, lookahead, null checks)
"""
import argparse
import hashlib
import json
import math
import os
import sys

import numpy as np
import pandas as pd

from . import clean as rc
from . import measure as rm
from . import gates as rg
from . import wave1 as w1

HORIZON_HOURS = 6
MIN_DIST_ATR = 0.5
K_EP_HOURS = 6
MIN_EFFECT = w1.MIN_EFFECT
FDR_Q = w1.FDR_Q
SPREAD_PIPS = w1.SPREAD_PIPS
LTFS = ("5m", "15m")
RR_BUCKETS = ((0.0, 1.0), (1.0, 1.5), (1.5, 2.5), (2.5, 1e9))
O1_RATIO_BINS = (0.8, 1.3)
CONTRACT = {
    "wave": 4, "version": "standard-definition CRT v1", "instrument": "GBPUSD", "htf": "1h", "trigger_timeframes": list(LTFS),
    "horizon_hours": HORIZON_HOURS, "min_distance_atr1h": MIN_DIST_ATR, "episode_hours": K_EP_HOURS,
    "excess": "1{target first} - 1/(1+RR)", "o1_ratio_bins": list(O1_RATIO_BINS), "min_effect": MIN_EFFECT, "fdr_q": FDR_Q,
    "n_primary_tests": 6, "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# CRT signal detection on 1h candles (uses C1 and C2 only: known at C2's close)
# ---------------------------------------------------------------------------
def crt_signals(h1):
    """
    h1: rm.Series of 1h candles. Returns a list of dicts, one per sweep (C2 index j):
      j, side ('high'|'low'), d (+1 long / -1 short), group ('reclaim'|'accept'), stop_level, target_level, atr1h, close_time
    """
    s = h1
    out = []
    for j in range(1, s.n):
        if s.gap[j] > 0 or s.closure[j] or not np.isfinite(s.atr[j]):
            continue
        h1_, l1_, h2, l2, c2 = s.h[j - 1], s.l[j - 1], s.h[j], s.l[j], s.c[j]
        if h2 > h1_ and l2 >= l1_:
            side, d, stop, target = "high", -1, h2, l1_
            group = "reclaim" if c2 < h1_ else "accept"
        elif l2 < l1_ and h2 <= h1_:
            side, d, stop, target = "low", +1, l2, h1_
            group = "reclaim" if c2 > l1_ else "accept"
        else:
            continue
        out.append({"j": j, "side": side, "d": d, "group": group, "stop_level": float(stop), "target_level": float(target),
                    "atr1h": float(s.atr[j]), "close_time": s.index[j] + s.step, "c1_range": float(h1_ - l1_)})
    return out


def trade_geometry(sig, entry_open, spread=0.0, min_dist=None):
    """Returns (stop_dist, target_dist, rr) or None if not eligible."""
    d = sig["d"]
    entry = entry_open + d * spread
    stop_dist = (entry - sig["stop_level"]) * d
    target_dist = (sig["target_level"] - entry) * d
    md = MIN_DIST_ATR if min_dist is None else min_dist
    if stop_dist < md * sig["atr1h"] or target_dist < md * sig["atr1h"]:
        return None
    return stop_dist, target_dist, target_dist / stop_dist


def score_signal(sig, s_ltf, s_lo, spread=0.0, worst=False, min_dist=None):
    """Returns dict(status, rr, excess, R) or {'status': 'EXCLUDED', 'reason': ...}."""
    step = s_ltf.step
    last_bar_time = sig["close_time"] - step
    e = s_ltf.index.get_indexer([last_bar_time])[0]
    if e < 0 or e + 1 >= s_ltf.n or s_ltf.index[e + 1] != sig["close_time"]:
        return {"status": "EXCLUDED", "reason": "no_trigger_bar"}
    geo = trade_geometry(sig, float(s_ltf.o[e + 1]), spread, min_dist)
    if geo is None:
        return {"status": "EXCLUDED", "reason": "geometry"}
    stop_dist, target_dist, rr = geo
    horizon = int(HORIZON_HOURS * 60 / s_ltf.step_minutes)
    res = rm.first_touch(s_ltf, int(e), sig["d"], r_unit=stop_dist, target_r=rr, stop_r=1.0, horizon=horizon, spread=spread)
    status = res["status"]
    if status == rm.AMBIGUOUS and s_lo is not None:
        status, _ = rm.resolve_ambiguous(s_ltf, s_lo, res, sig["d"])
    if status == rm.AMBIGUOUS and worst:
        status = rm.STOP_FIRST
    if status == rm.DATA_GAP:
        return {"status": "EXCLUDED", "reason": "data_gap"}
    if status == rm.AMBIGUOUS:
        return {"status": "EXCLUDED", "reason": "ambiguous"}
    if status == rm.NEITHER:
        return {"status": "NEITHER", "rr": rr, "excess": None, "R": 0.0}
    win = status == rm.TARGET_FIRST
    return {"status": "RESOLVED", "win": win, "rr": rr, "excess": (1.0 if win else 0.0) - 1.0 / (1.0 + rr), "R": rr if win else -1.0}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _mean_test(vals):
    v = np.asarray(vals, float)
    n = len(v)
    if n < 10:
        return {"n": n, "mean": None, "se": None, "ci": None, "z": None, "p": None}
    m, sd = float(v.mean()), float(v.std(ddof=1))
    se = sd / math.sqrt(n)
    z = m / se if se > 0 else 0.0
    return {"n": n, "mean": m, "se": se, "ci": (m - 1.96 * se, m + 1.96 * se), "z": z, "p": w1.norm_p(z)}


def _strat_mean_diff(a_by, b_by):
    """a_by, b_by: dict stratum -> list of excess values. Weighted by the reclaim count."""
    num = var = 0.0
    wsum = 0
    for st, av in a_by.items():
        bv = b_by.get(st, [])
        if len(av) < 10 or len(bv) < 10:
            continue
        na, nb = len(av), len(bv)
        num += na * (np.mean(av) - np.mean(bv))
        var += na * na * (np.var(av, ddof=1) / na + np.var(bv, ddof=1) / nb)
        wsum += na
    if wsum == 0:
        return {"diff": None, "se": None, "ci": None, "z": None, "p": None, "n": 0}
    diff, se = num / wsum, math.sqrt(var) / wsum
    z = diff / se if se > 0 else 0.0
    return {"diff": diff, "se": se, "ci": (diff - 1.96 * se, diff + 1.96 * se), "z": z, "p": w1.norm_p(z), "n": wsum}


def _collapse(sigs):
    """Anchored K_EP_HOURS episodes per (group, side)."""
    if not sigs:
        return []
    idx = np.array([x["j"] for x in sigs])
    keys = np.array([hash((x["group"], x["side"])) % (10 ** 9) for x in sigs])
    _, kept = rm.collapse_episodes(idx, keys, K_EP_HOURS)
    return [x for x, k in zip(sigs, kept) if k]


def _session(t):
    return int(np.digitize(t.hour, w1.SESSION_EDGES))


# ---------------------------------------------------------------------------
# CRT experiments
# ---------------------------------------------------------------------------
def run_crt(h1, s_ltf, s_lo, signals, ltf, dev_end_time, dev=True):
    sel = [x for x in signals if (x["close_time"] < dev_end_time if dev else x["close_time"] >= rc.HOLDOUT_START_UTC)]
    eps = _collapse(sel)
    out = {"ltf": ltf, "signals_total": len(sel), "episodes": len(eps)}
    scored = {"reclaim": [], "accept": []}
    excl = {}
    for sg in eps:
        r = score_signal(sg, s_ltf, s_lo)
        if r["status"] == "EXCLUDED":
            excl[r["reason"]] = excl.get(r["reason"], 0) + 1
            continue
        r["sig"] = sg
        scored[sg["group"]].append(r)
    out["excluded"] = excl
    res = {g: [r for r in scored[g] if r["status"] == "RESOLVED"] for g in scored}
    out["counts"] = {g: {"episodes": len(scored[g]), "resolved": len(res[g]), "neither": sum(1 for r in scored[g] if r["status"] == "NEITHER")} for g in scored}
    # P2
    rec = res["reclaim"]
    p2 = _mean_test([r["excess"] for r in rec])
    p2["mean_R_gross"] = float(np.mean([r["R"] for r in rec])) if rec else None
    p2["win_rate"] = float(np.mean([r["win"] for r in rec])) if rec else None
    p2["mean_RR"] = float(np.mean([r["rr"] for r in rec])) if rec else None
    p2["null_win_rate"] = float(np.mean([1.0 / (1.0 + r["rr"]) for r in rec])) if rec else None
    out["P2_reclaim"] = p2
    acc = res["accept"]
    out["acceptance"] = _mean_test([r["excess"] for r in acc])
    # P3
    def by(rs):
        d = {}
        for r in rs:
            d.setdefault(r["sig"]["side"], []).append(r["excess"])
        return d
    out["P3_reclaim_minus_accept"] = _strat_mean_diff(by(rec), by(acc))
    # descriptive: side, RR bucket, net of spread, worst case ambiguity
    out["by_side"] = {side: _mean_test([r["excess"] for r in rec if r["sig"]["side"] == side]) for side in ("high", "low")}
    buckets = []
    for lo, hi in RR_BUCKETS:
        b = [r for r in rec if lo <= r["rr"] < hi]
        buckets.append({"rr_from": lo, "rr_to": None if hi > 1e8 else hi, "n": len(b),
                        "win_rate": float(np.mean([r["win"] for r in b])) if b else None,
                        "mean_R_gross": float(np.mean([r["R"] for r in b])) if b else None,
                        "mean_excess": float(np.mean([r["excess"] for r in b])) if b else None})
    out["rr_buckets_reclaim"] = buckets
    sp = SPREAD_PIPS * 1e-4
    net = [score_signal(sg["sig"], s_ltf, s_lo, spread=sp) for sg in rec]
    net = [r for r in net if r["status"] == "RESOLVED"]
    out["net_of_spread"] = {"spread_pips": SPREAD_PIPS, "n": len(net), "mean_R_net": float(np.mean([r["R"] for r in net])) if net else None}
    worst = [score_signal(sg["sig"], s_ltf, s_lo, worst=True) for sg in rec]
    worst = [r for r in worst if r["status"] == "RESOLVED"]
    out["worst_case_ambiguity_P2_mean"] = float(np.mean([r["excess"] for r in worst])) if worst else None
    sens = {}
    for md in (0.25, 0.5, 0.8):
        rs = [score_signal(sg, s_ltf, s_lo, min_dist=md) for sg in eps if sg["group"] == "reclaim"]
        rs = [r for r in rs if r["status"] == "RESOLVED"]
        t = _mean_test([r["excess"] for r in rs])
        sens[str(md)] = {"n": t["n"], "mean_excess": t["mean"], "ci": t["ci"]}
    out["min_distance_sensitivity_P2"] = sens
    out["n_min_resolved"] = min(len(rec), len(acc))
    out["sample_label"] = w1.sample_label(out["n_min_resolved"])
    return out


def p1_descriptive(h1, dev_end_time):
    """How often does the next 1h candle sweep a side and reclaim, by C1 range size tercile? (P1, descriptive)"""
    s = h1
    rows = []
    sig_by_j = {x["j"]: x for x in crt_signals(h1)}
    for j in range(1, s.n):
        if s.index[j] + s.step >= dev_end_time or not np.isfinite(s.atr[j - 1]) or s.gap[j] > 0 or s.closure[j]:
            continue
        rows.append((s.h[j - 1] - s.l[j - 1]) / s.atr[j - 1] if s.atr[j - 1] > 0 else np.nan)
        rows[-1] = (rows[-1], j in sig_by_j and sig_by_j[j]["group"] == "reclaim", j in sig_by_j)
    arr = np.array([(r[0], r[1], r[2]) for r in rows if np.isfinite(r[0])], dtype=float)
    if len(arr) < 60:
        return None
    edges = np.quantile(arr[:, 0], [1 / 3, 2 / 3])
    grp = np.digitize(arr[:, 0], edges)
    return [{"c1_range_tercile": g + 1, "n": int((grp == g).sum()), "range_x_ATR": (round(float(arr[grp == g, 0].min()), 2), round(float(arr[grp == g, 0].max()), 2)),
             "p_sweep_any": round(float(arr[grp == g, 2].mean()), 4), "p_sweep_and_reclaim": round(float(arr[grp == g, 1].mean()), 4)} for g in range(3)]


# ---------------------------------------------------------------------------
# O1: outside bar alone
# ---------------------------------------------------------------------------
def run_o1(P, dev=True):
    s = P.s
    n = P.n
    outside = np.zeros(n, bool)
    contig = np.zeros(n, bool)
    contig[1:] = (s.gap[1:] == 0) & (~s.closure[1:])
    outside[1:] = (s.h[1:] > s.h[:-1]) & (s.l[1:] < s.l[:-1]) & contig[1:]
    rbin = np.where(P.ratio < O1_RATIO_BINS[0], 0, np.where(P.ratio < O1_RATIO_BINS[1], 1, 2))
    ev_idx, ct_idx = P.episodes(outside, dev), P.episodes(~outside & contig, dev)

    def strat(i):
        return (int(P.dirn[i]), int(P.session[i]), int(P.atrb[i]), int(rbin[i]))
    es, ec = w1.tally(P, ev_idx)
    cs, cc = w1.tally(P, ct_idx)
    es2 = {}
    for i in ev_idx:
        st, _ = P.outcome(i, 0.0)
        if st in (rm.TARGET_FIRST, rm.STOP_FIRST):
            es2.setdefault(strat(i), [0, 0])[0 if st == rm.TARGET_FIRST else 1] += 1
    cs2 = {}
    for i in ct_idx:
        st, _ = P.outcome(i, 0.0)
        if st in (rm.TARGET_FIRST, rm.STOP_FIRST):
            cs2.setdefault(strat(i), [0, 0])[0 if st == rm.TARGET_FIRST else 1] += 1
    sd = w1.stratified_diff({k: tuple(v) for k, v in es2.items()}, {k: tuple(v) for k, v in cs2.items()})
    te, ne = sum(v[0] for v in es2.values()), sum(sum(v) for v in es2.values())
    tc, nc = sum(v[0] for v in cs2.values()), sum(sum(v) for v in cs2.values())
    out = {"tf": P.tf, "episodes_outside": int(len(ev_idx)), "episodes_control": int(len(ct_idx)), "event_counts": ec,
           "p_outside": (te / ne) if ne else None, "p_outside_ci": w1.wilson(te, ne), "n_outside": ne,
           "p_control": (tc / nc) if nc else None, "n_control": nc, **sd}
    if sd["diff"] is not None:
        out["diff_ci"] = (sd["diff"] - 1.96 * sd["se"], sd["diff"] + 1.96 * sd["se"])
    out["n_min_resolved"] = min(ne, nc)
    out["sample_label"] = w1.sample_label(out["n_min_resolved"])
    out["by_direction"] = w1._by_direction(P, ev_idx, ct_idx)
    return out


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def run_wave4(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    clean = {tf: (preloaded[tf] if preloaded else rc.clean_timeframe(tf, raw_dir, source)[0]) for tf in ("5m", "15m", "1h")}
    s1h, s5, s15 = rm.Series(clean["1h"], 60), rm.Series(clean["5m"], 5), rm.Series(clean["15m"], 15)
    P = {"5m": w1.Prepared("5m", clean["5m"]), "15m": w1.Prepared("15m", clean["15m"], s_lo=s5)}
    rep15 = rc.clean_timeframe("15m", raw_dir, source)[2] if not preloaded else {
        "first": None, "last": None, "raw_lines_parsed": 0, "unique_timestamps": 0, "duplicate_lines": 0, "conflicting_duplicates": 0,
        "dropped_market_closed": 0, "dropped_off_grid": 0, "dropped_invalid_ohlc": 0, "kept_bars": len(clean["15m"]),
        "unexplained_gaps": 0, "missing_open_bars": 0}
    a6 = rg.gate_a6({"15m": clean["15m"]}, {"15m": rep15}, {"15m": s15})
    status = rg.gate_a7({"15m": clean["15m"]}, {"15m": s15}, a6)["details"]["status"]
    dev_end = rc.HOLDOUT_START_UTC - pd.Timedelta(hours=HORIZON_HOURS + 1)
    signals = crt_signals(s1h)
    crt = {"5m": run_crt(s1h, s5, None, signals, "5m", dev_end), "15m": run_crt(s1h, s15, s5, signals, "15m", dev_end)}
    o1 = {tf: run_o1(P[tf]) for tf in LTFS}
    tests = []
    for tf in LTFS:
        if crt[tf]["P2_reclaim"]["p"] is not None:
            tests.append((("P2", tf), crt[tf]["P2_reclaim"]["p"]))
        if crt[tf]["P3_reclaim_minus_accept"]["p"] is not None:
            tests.append((("P3", tf), crt[tf]["P3_reclaim_minus_accept"]["p"]))
        if o1[tf].get("p") is not None:
            tests.append((("O1", tf), o1[tf]["p"]))
    reject, qv = w1.bh_fdr([p for _, p in tests])
    fdr = {t: (rj, q) for (t, _), rj, q in zip(tests, reject, qv)}
    for tf in LTFS:
        c = crt[tf]
        for name, key, val in (("P2", "P2_reclaim", "mean"), ("P3", "P3_reclaim_minus_accept", "diff")):
            r = c[key]
            rj, q = fdr.get((name, tf), (False, None))
            r["fdr_reject"], r["q_value"] = rj, q
            r["verdict"] = "DATA-LIMITED" if r[val] is None else w1.verdict(r[val], r["ci"][0], r["ci"][1], rj, c["n_min_resolved"], status, MIN_EFFECT)
        o = o1[tf]
        rj, q = fdr.get(("O1", tf), (False, None))
        o["fdr_reject"], o["q_value"] = rj, q
        o["verdict"] = "DATA-LIMITED" if o.get("diff") is None else w1.verdict(o["diff"], o["diff_ci"][0], o["diff_ci"][1], rj, o["n_min_resolved"], status, MIN_EFFECT)
    return {"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False,
            "n_primary_tests": len(tests), "crt": crt, "O1": o1, "P1_descriptive": p1_descriptive(s1h, dev_end)}


def _pp(x):
    return "n/a" if x is None else f"{100 * x:+.1f}pp"


def render_markdown(r):
    L = ["# Observatory — Wave 4 report (CRT sweep-and-reclaim + outside bar)\n",
         f"Contract `{r['contract']['version']}` hash `{r['contract_hash']}` · dataset status **{r['dataset_status']}** · holdout opened: **False** · primary tests: {r['n_primary_tests']}\n",
         "**Standard-definition CRT on 1h candles, measured on 5m and on 15m. Development data only. Nothing here is a trading rule.**\n",
         "Each trade enters after C2 closes, stops at the sweep extreme and targets the far side of C1, so its RR varies. "
         "Excess = 1{target first} − 1/(1+RR): zero means no edge whatever the RR. Practical threshold 5 pp.\n"]
    for tf in LTFS:
        c = r["crt"][tf]
        L.append(f"## CRT · 1h → {tf}\n")
        L.append(f"- episodes {c['episodes']} (signals {c['signals_total']}) · reclaim resolved {c['counts']['reclaim']['resolved']} · acceptance resolved {c['counts']['accept']['resolved']} · excluded {c['excluded']} · sample: {c['sample_label']}")
        p2 = c["P2_reclaim"]
        if p2["mean"] is None:
            L.append("- P2: DATA-LIMITED")
        else:
            L.append(f"- **P2 reclaim: mean excess {_pp(p2['mean'])}** (CI {_pp(p2['ci'][0])} to {_pp(p2['ci'][1])}) · win rate {p2['win_rate']:.3f} vs {p2['null_win_rate']:.3f} expected with no edge · mean RR {p2['mean_RR']:.2f} · mean R gross {p2['mean_R_gross']:+.3f} · p={p2['p']:.4f} · q={p2['q_value']:.4f} · **{p2['verdict']}**")
        a = c["acceptance"]
        if a["mean"] is not None:
            L.append(f"- acceptance (closes beyond the sweep): mean excess {_pp(a['mean'])} (CI {_pp(a['ci'][0])} to {_pp(a['ci'][1])}, n={a['n']})")
        p3 = c["P3_reclaim_minus_accept"]
        if p3["diff"] is not None:
            L.append(f"- **P3 reclaim − acceptance: {_pp(p3['diff'])}** (CI {_pp(p3['ci'][0])} to {_pp(p3['ci'][1])}) · p={p3['p']:.4f} · q={p3['q_value']:.4f} · **{p3['verdict']}**")
        for side, v in c["by_side"].items():
            if v["mean"] is not None:
                L.append(f"- sweep-{side} only: mean excess {_pp(v['mean'])} (n={v['n']})")
        L.append("- by RR bucket (the \"≥ 1.5R\" question), reclaim trades:")
        for b in c["rr_buckets_reclaim"]:
            if b["n"]:
                L.append(f"  - RR {b['rr_from']}–{b['rr_to'] or '∞'}: n={b['n']} · win {b['win_rate']:.3f} · mean R {b['mean_R_gross']:+.3f} · excess {_pp(b['mean_excess'])}")
        sens = c.get("min_distance_sensitivity_P2", {})
        if sens:
            L.append("- sensitivity of P2 to the minimum-distance rule (descriptive): " + " · ".join(
                f"{k_}×ATR: n={v['n']}, excess {_pp(v['mean_excess'])}" for k_, v in sens.items()))
        L.append(f"- net of {SPREAD_PIPS} pip spread: mean R {c['net_of_spread']['mean_R_net']:+.3f} (n={c['net_of_spread']['n']}) · worst-case ambiguity P2 mean {_pp(c['worst_case_ambiguity_P2_mean'])}\n")
    L.append("## O1 — outside bar alone (no sweep)\n")
    for tf in LTFS:
        o = r["O1"][tf]
        L.append(f"### {tf}")
        if o.get("diff") is None:
            L.append("- DATA-LIMITED\n")
            continue
        L.append(f"- episodes outside {o['episodes_outside']} · control {o['episodes_control']} · p(outside) {o['p_outside']:.3f} {o['p_outside_ci']} · p(control) {o['p_control']:.3f}")
        L.append(f"- **continuation vs matched ordinary bars: {_pp(o['diff'])}** (CI {_pp(o['diff_ci'][0])} to {_pp(o['diff_ci'][1])}) · p={o['p']:.4f} · q={o['q_value']:.4f} · **{o['verdict']}**")
        for nm, b in o["by_direction"].items():
            if b["diff"] is not None:
                L.append(f"- {nm}: n={b['episodes']}, diff {_pp(b['diff'])} (CI {_pp(b['diff_ci'][0])} to {_pp(b['diff_ci'][1])})")
        L.append("")
    if r.get("P1_descriptive"):
        L.append("## P1 (descriptive) — how often does the next 1h candle sweep and reclaim, by C1 range size?\n")
        for t in r["P1_descriptive"]:
            L.append(f"- tercile {t['c1_range_tercile']} (C1 range {t['range_x_ATR'][0]}–{t['range_x_ATR'][1]}×ATR, n={t['n']}): any sweep {t['p_sweep_any']:.1%} · sweep and reclaim {t['p_sweep_and_reclaim']:.1%}")
    L.append("\n## Not run in this wave\n- P4 (outside bar after a sweep), P5 (order-block location), P6 (combinations), trigger-TF zones as targets.\n- Holdout: sealed.\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _mk(bars, step, start="2026-03-10 00:00"):
    t0 = pd.Timestamp(start, tz="UTC")
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"],
                      index=pd.DatetimeIndex([t0 + pd.Timedelta(minutes=step * i) for i in range(len(bars))]))
    df["gap_before_missing"], df["closure_before"] = 0, False
    ser = rm.Series(df, step)
    ser.gap, ser.closure = ser.gap.copy(), ser.closure.copy()
    return ser


def selftest(null_series=14):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    base = [(100, 101, 99, 100)] * 20                                  # warm-up so ATR exists
    def one(c1, c2):
        s = _mk(base + [c1, c2], 60)
        return [x for x in crt_signals(s) if x["j"] == s.n - 1]
    c1 = (105, 110, 100, 105)                                            # range [100, 110]
    r = one(c1, (105, 111, 101, 108))
    chk("sweep-high reclaim", (r[0]["side"], r[0]["group"], r[0]["d"]), ("high", "reclaim", -1))
    chk("short geometry stop/target", (r[0]["stop_level"], r[0]["target_level"]), (111.0, 100.0))
    r = one(c1, (105, 111, 101, 112))
    chk("sweep-high acceptance", (r[0]["side"], r[0]["group"]), ("high", "accept"))
    r = one(c1, (105, 108, 99, 102))
    chk("sweep-low reclaim -> long", (r[0]["side"], r[0]["group"], r[0]["d"], r[0]["stop_level"], r[0]["target_level"]), ("low", "reclaim", 1, 99.0, 110.0))
    r = one(c1, (105, 108, 99, 98))
    chk("sweep-low acceptance", r[0]["group"], "accept")
    chk("both sides swept -> excluded", len(one(c1, (105, 111, 99, 105))), 0)
    chk("no sweep -> none", len(one(c1, (105, 109, 101, 106))), 0)
    chk("touching the high is not a sweep", len(one(c1, (105, 110, 101, 106))), 0)
    s_gap = _mk(base + [c1, (105, 111, 101, 108)], 60)
    s_gap.gap[s_gap.n - 1] = 2
    chk("gap before C2 blocks the signal", len([x for x in crt_signals(s_gap) if x["j"] == s_gap.n - 1]), 0)
    # geometry and null
    sig = {"d": -1, "stop_level": 111.0, "target_level": 100.0, "atr1h": 2.0}
    g = trade_geometry(sig, 108.0)
    chk("geometry distances", (g[0], g[1], round(g[2], 4)), (3.0, 8.0, 2.6667))
    chk("min-distance filter", trade_geometry(sig, 110.5), None)                  # stop distance 0.5 < 0.5 * ATR(2.0) = 1.0
    chk("spread worsens a short entry", round(trade_geometry(sig, 108.0, 0.5)[0], 4), 3.5 - 0.0 if False else 3.5 - 0.0 if False else round(111.0 - 107.5, 4))
    # scoring on a hand-built 5m path: short from 108, stop 111, target 100
    def run5(path, sig_=sig):
        ltf = [(100, 100.1, 99.9, 100)] * 30
        # last bar of C2 is the one before close_time
        t_close = pd.Timestamp("2026-03-10 00:00", tz="UTC") + pd.Timedelta(minutes=5 * 31)
        bars = ltf + [(108, 108.2, 107.8, 108)] + path + [(108, 108.2, 107.8, 108)] * 100
        s_ = _mk(bars, 5)
        sg = dict(sig_, close_time=t_close)
        return score_signal(sg, s_, None)
    r = run5([(108, 108.5, 99.5, 100.5)])                 # entry = open of this bar = 108; low 99.5 <= 100 -> target first
    chk("target first scored", (r["status"], r["win"], round(r["rr"], 3)), ("RESOLVED", True, 2.667))
    chk("excess for a win", round(r["excess"], 4), round(1 - 1 / (1 + 8 / 3), 4))
    r = run5([(108, 111.5, 107.0, 111.0)])                # high 111.5 >= 111 -> stop first
    chk("stop first scored", (r["status"], r["win"], round(r["excess"], 4)), ("RESOLVED", False, round(-1 / (1 + 8 / 3), 4)))
    r = run5([(108, 111.5, 99.5, 100.0)])
    chk("same bar both -> excluded as ambiguous", (r["status"], r.get("reason")), ("EXCLUDED", "ambiguous"))
    r = run5([(108, 108.5, 107.5, 108)] * 80)
    chk("neither within the horizon", r["status"], "NEITHER")
    # outside-bar definition inside run_o1 is exercised in the null test below
    # no lookahead: signals up to a time are unchanged when later data is removed
    rng = np.random.default_rng(21)
    from . import gates as gg
    d5 = gg._random_walk_series(8000, rng, step=5).df
    h1 = d5[["open", "high", "low", "close"]].resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    h1["gap_before_missing"], h1["closure_before"] = 0, False
    s_full = rm.Series(h1, 60)
    sig_full = {(x["j"], x["group"]) for x in crt_signals(s_full)}
    cut = 400
    s_cut = rm.Series(h1.iloc[:cut + 1], 60)
    sig_cut = {(x["j"], x["group"]) for x in crt_signals(s_cut)}
    chk("signals unchanged when the future is removed", {x for x in sig_full if x[0] <= cut} == sig_cut, True)
    # null: on random walks the CRT and outside-bar measures should not look informative
    fp = {"P2": 0, "P3": 0, "O1": 0}
    used = 0
    m2, m3, m1 = [], [], []
    for _ in range(null_series):
        ser = gg._random_walk_series(45000, rng, step=5)
        d5_ = ser.df
        h1_ = d5_[["open", "high", "low", "close"]].resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        h1_["gap_before_missing"], h1_["closure_before"] = 0, False
        s1, s5_ = rm.Series(h1_, 60), rm.Series(d5_, 5)
        end = d5_.index[-1] + pd.Timedelta(days=1)
        c_ = run_crt(s1, s5_, None, crt_signals(s1), "5m", end)
        Pp = w1.Prepared("5m", d5_)
        saved = rc.HOLDOUT_START_UTC
        o_ = run_o1(Pp)
        if c_["P2_reclaim"]["p"] is None or c_["P3_reclaim_minus_accept"]["p"] is None or o_.get("p") is None:
            continue
        used += 1
        m2.append(c_["P2_reclaim"]["mean"])
        m3.append(c_["P3_reclaim_minus_accept"]["diff"])
        m1.append(o_["diff"])
        fp["P2"] += c_["P2_reclaim"]["p"] < 0.05
        fp["P3"] += c_["P3_reclaim_minus_accept"]["p"] < 0.05
        fp["O1"] += o_["p"] < 0.05
    chk("null series usable", used >= 12, True)
    for k_ in fp:
        chk(f"null {k_} false-positive rate <= 20%", used > 0 and fp[k_] / used <= 0.20, True)
    chk("null means near zero", all(abs(float(np.mean(v))) < 0.03 for v in (m2, m3, m1) if v), True)
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": used, "false_positive_rates": {k_: round(v / used, 3) for k_, v in fp.items()} if used else None,
                     "mean_P2": round(float(np.mean(m2)), 4) if m2 else None, "mean_P3": round(float(np.mean(m3)), 4) if m3 else None,
                     "mean_O1": round(float(np.mean(m1)), 4) if m1 else None}}


def main():
    ap = argparse.ArgumentParser(description="Observatory Wave 4 (CRT + outside bar). Read-only on raw data.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    res = run_wave4(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "wave4_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "wave4_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
