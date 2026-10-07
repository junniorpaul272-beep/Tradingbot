"""
observatory/wave5.py
====================
OBSERVATORY — Wave 5: extension from the higher-timeframe average. ISOLATED from the live bot.

ONE question: does how far price is stretched from the 1h average predict whether the next move continues,
for ordinary bars, for displacements, and for displacements after allowing for their size?

WHY THIS QUESTION
-----------------
Two earlier observations point here: (a) B2: larger 15m displacements continue less; (b) Wave 3 control arm:
ordinary bars moving AWAY from the higher-timeframe EMA side continued about 3 pp less than bars moving toward
it. Both are "the more stretched, the less reliable continuation". NOTE: (b) was seen on development data, so
this wave is EXPLORATORY: whatever it finds is a hypothesis, to be frozen and tested once on the holdout.

FROZEN CONTRACT (hash printed in every report; frozen 2026-10-06 before any Wave 5 result was seen)
-------------------------------------------------------------------------------------------------
  Extension      X = (close of the event bar - EMA50(1h)) / ATR14(1h), where EMA50 and ATR14 are read from the
                 latest 1h bar that is fully CLOSED at the event's confirmation time (closed bars only).
                 AWAY-extension A = d * X, d = the bar's direction (+1 bullish / -1 bearish). A > 0: the bar
                 moves in the direction of the side of the average that price is already on (stretched away);
                 A < 0: the bar moves toward the average.
  Bins (fixed)   5 bins on A with FIXED edges (-1.9, -0.5, 0.6, 2.0) in 1h-ATR units. They were taken from the
                 outcome-blind distribution of A in ordinary bars (about quintiles) and never re-estimated.
  Populations    ORDINARY bars: range/ATR14(prior) < 1.0.   DISPLACEMENTS: range/ATR14(prior) >= 1.0.
                 Trade = continuation of the bar's direction, next-open entry, +-1R = ATR14 at the event bar,
                 24 bars, first touch (same as Wave 1). Episodes K = 12 anchored per direction.
  Configs        event timeframe 5m and 15m (context = 1h average). Each separately.
  Primary tests  (6, Benjamini-Hochberg q = 0.10, two-sided):
                 All three use a CLUSTER-ROBUST linear-trend test (clusters = calendar days, CR1 sandwich) because the
                 extension is persistent in time: neighbouring events share a bin AND overlapping price paths, and the
                 plain Cochran-Armitage test was found to over-state significance on random data (false positives about
                 20% for displacements). Even so the cluster-robust z still over-rejects on random data (sd about
                 1.3), so every reported z is divided by max(1, sd of that z over 40 random-walk series) = CALIBRATED z.
                 The raw robust z and the naive z are printed beside it.
                 E-ord  ordinary bars: trend of P(+1R first) over the 5 bins (bin score 1..5)
                 E-disp displacements: same trend
                 E-add  displacements, trend of P over the 5 bins AFTER stratifying by displacement size
                        (terciles with fixed edges 1.22 / 1.59 x ATR): the slope is estimated within size strata,
                        so extension must add beyond size
                 Practical threshold 5 pp between the lowest and highest bin.
  Secondary      (descriptive) mean signed forward move over the horizon in ATR units per bin (uses the whole
                 path, so it has less noise than a +-1R first-touch); bullish and bearish separately;
                 first half vs second half of development.
  Split          the frozen absolute holdout date; development events purged; holdout SEALED (no way to open
                 it here). Any positive result becomes a frozen hypothesis for one future holdout test.
  Deferred       range location, session, FVG interaction, prior displacement history: each its own later question.

Run:  python3 -m observatory.wave5                (real data)
      python3 -m observatory.wave5 --selftest
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

A_EDGES = (-1.9, -0.5, 0.6, 2.0)
SIZE_EDGES = (1.22, 1.59)
EMA_SPAN = 50
TFS = ("5m", "15m")
MIN_EFFECT = w1.MIN_EFFECT
FDR_Q = w1.FDR_Q
CONTRACT = {
    "wave": 5, "instrument": "GBPUSD", "event_timeframes": list(TFS), "context": "1h EMA50 / ATR14, closed bars only",
    "A_edges": list(A_EDGES), "size_edges": list(SIZE_EDGES), "K_episode": w1.K_EPISODE, "horizon_bars": w1.HORIZON,
    "tests": ["E-ord", "E-disp", "E-add"], "n_primary_tests": 6, "min_effect": MIN_EFFECT, "fdr_q": FDR_Q, "calibration": "z / max(1, sd of z on 40 random-walk series)",
    "holdout_start_utc": str(rc.HOLDOUT_START_UTC), "purge_bars": w1.HORIZON + 1,
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# extension feature (closed higher-timeframe bars only)
# ---------------------------------------------------------------------------
def extension_X(htf_df, htf_step_min, ev_index, ev_step_min, ev_close):
    """X = (event close - EMA50(1h at last closed bar)) / ATR14(1h at last closed bar); NaN when unknown."""
    ema = htf_df["close"].ewm(span=EMA_SPAN, adjust=False).mean().to_numpy()
    atr = rm.atr(htf_df).to_numpy()
    htf_close_time = htf_df.index + pd.Timedelta(minutes=htf_step_min)
    ev_time = ev_index + pd.Timedelta(minutes=ev_step_min)
    pos = htf_close_time.searchsorted(ev_time, side="right") - 1
    ok = pos >= EMA_SPAN
    pc = np.clip(pos, 0, None)
    ok &= np.isfinite(atr[pc]) & (atr[pc] > 0)
    X = np.full(len(ev_index), np.nan)
    X[ok] = (np.asarray(ev_close)[ok] - ema[pc[ok]]) / atr[pc[ok]]
    return X


def bin_of(a):
    return int(np.digitize(a, A_EDGES))


def ca_components(groups):
    """Cochran-Armitage (T, V) for ordered groups [(target_first, stop_first), ...]; z = T / sqrt(V)."""
    n = [t + s for t, s in groups]
    x = [t for t, s in groups]
    N, X = sum(n), sum(x)
    if N == 0 or X in (0, N):
        return 0.0, 0.0
    pbar = X / N
    sc = list(range(1, len(groups) + 1))
    T = sum(si * (xi - ni * pbar) for si, xi, ni in zip(sc, x, n))
    V = pbar * (1 - pbar) * (sum(ni * si * si for ni, si in zip(n, sc)) - sum(ni * si for ni, si in zip(n, sc)) ** 2 / N)
    return T, max(V, 0.0)


def combined_trend(strata_groups):
    """CMH-type trend: sum of T over strata / sqrt(sum of V). Returns (z, p)."""
    T = V = 0.0
    for g in strata_groups:
        t, v = ca_components(g)
        T += t
        V += v
    if V <= 0:
        return 0.0, 1.0
    z = T / math.sqrt(V)
    return z, w1.norm_p(z)


# ---------------------------------------------------------------------------
# experiment
# ---------------------------------------------------------------------------
class Ext:
    def __init__(self, P, htf_df):
        self.P = P
        s = P.s
        self.X = extension_X(htf_df, 60, s.index, s.step_minutes, s.c)
        self.A = P.dirn * self.X
        self.ok = np.isfinite(self.A) & P.valid
        # signed forward move over the horizon in ATR units (secondary): uses close[e+H] relative to the next open
        n, H = P.n, w1.HORIZON
        self.fwd = np.full(n, np.nan)
        e = np.arange(n - H - 1)
        gapfree = np.ones(len(e), bool)
        cs_gap = np.concatenate([[0], np.cumsum((s.gap > 0).astype(int))])
        gapfree = (cs_gap[e + H + 1] - cs_gap[e + 1]) == 0
        move = (s.c[e + H] - s.o[e + 1]) * P.dirn[e] / s.atr[e]
        self.fwd[e] = np.where(gapfree & np.isfinite(move), move, np.nan)

    def events(self, mask, dev=True):
        idx = self.P.episodes(mask & self.ok, dev)
        return idx

    def bins_counts(self, idx):
        groups = [[0, 0] for _ in range(5)]
        fwd = [[] for _ in range(5)]
        for i in idx:
            st, _ = self.P.outcome(i, 0.0)
            b = bin_of(self.A[i])
            if st == rm.TARGET_FIRST:
                groups[b][0] += 1
            elif st == rm.STOP_FIRST:
                groups[b][1] += 1
            if np.isfinite(self.fwd[i]):
                fwd[b].append(self.fwd[i])
        return [tuple(g) for g in groups], fwd

    def resolved_arrays(self, idx):
        """bin score (1..5), outcome (1 = target first), calendar day, for events resolved as target/stop."""
        sc, y, day, keep = [], [], [], []
        for i in idx:
            st, _ = self.P.outcome(i, 0.0)
            if st not in (rm.TARGET_FIRST, rm.STOP_FIRST):
                continue
            sc.append(bin_of(self.A[i]) + 1)
            y.append(1.0 if st == rm.TARGET_FIRST else 0.0)
            day.append(int(self.P.s.index[i].normalize().value // 86_400_000_000_000))
            keep.append(i)
        return np.array(sc, float), np.array(y, float), np.array(day), np.array(keep, int)


def _table(groups, fwd):
    rows = []
    for b, (t, s_) in enumerate(groups):
        lo = -np.inf if b == 0 else A_EDGES[b - 1]
        hi = np.inf if b == 4 else A_EDGES[b]
        n = t + s_
        rows.append({"bin": b + 1, "A_from": None if lo == -np.inf else lo, "A_to": None if hi == np.inf else hi, "n_resolved": n,
                     "p": round(t / n, 4) if n else None, "ci": w1.wilson(t, n),
                     "mean_fwd_ATR": round(float(np.mean(fwd[b])), 4) if fwd[b] else None, "n_fwd": len(fwd[b])})
    return rows



def null_sd(tf, n_series=40, seed=5150):
    """
    Spread (standard deviation) of each trend z-statistic on pure random-walk data. A perfectly calibrated
    test has sd = 1. The displacement trend tests were found to have sd of about 1.3 (false positives about
    20% instead of 5%) even with cluster-robust variances, because the extension is persistent in time and
    every event shares its price path with its neighbours. The reported z is therefore divided by this
    simulated sd whenever it exceeds 1 ("calibrated z").
    """
    from . import gates as g
    rng = np.random.default_rng(seed)
    step = rc.STEP_MINUTES[tf]
    bars = 45000 if tf == "5m" else 15000
    zs = {"E_ord": [], "E_disp": [], "E_add": []}
    for _ in range(n_series):
        d = g._random_walk_series(bars, rng, step=step).df
        P_ = w1.Prepared(tf, d)
        E_ = Ext(P_, _aggregate(d, 60))
        r = run_tf(E_, calibrate=False)
        for k in zs:
            zs[k].append(r[k]["trend_z"])
    return {k: float(np.std(v)) for k, v in zs.items()}


def _aggregate(df, minutes):
    d = df[["open", "high", "low", "close"]].resample(f"{minutes}min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    d["gap_before_missing"], d["closure_before"] = 0, False
    return d


def run_tf(E, dev=True, calibrate=False, cal=None):
    P = E.P
    out = {"tf": P.tf}
    # E-ord
    ord_idx = E.events(P.ratio < 1.0, dev)
    g, f = E.bins_counts(ord_idx)
    zz, pp, sl = w1.cluster_slope_test(*E.resolved_arrays(ord_idx)[:3])
    zc, pc = combined_trend([g])
    tab = _table(g, f)
    out["E_ord"] = {"episodes": int(len(ord_idx)), "table": tab, "trend_z": round(zz, 3), "trend_p": pp, "slope_per_bin": sl,
                    "naive_trend_z": round(zc, 3), "naive_trend_p": pc,
                    "top_minus_bottom": (tab[4]["p"] - tab[0]["p"]) if tab[4]["p"] is not None and tab[0]["p"] is not None else None,
                    "n_min_resolved": min(t["n_resolved"] for t in tab)}
    # E-disp
    d_idx = E.events(P.ratio >= 1.0, dev)
    g2, f2 = E.bins_counts(d_idx)
    zz2, pp2, sl2 = w1.cluster_slope_test(*E.resolved_arrays(d_idx)[:3])
    zc2, pc2 = combined_trend([g2])
    tab2 = _table(g2, f2)
    out["E_disp"] = {"episodes": int(len(d_idx)), "table": tab2, "trend_z": round(zz2, 3), "trend_p": pp2, "slope_per_bin": sl2,
                     "naive_trend_z": round(zc2, 3), "naive_trend_p": pc2,
                     "top_minus_bottom": (tab2[4]["p"] - tab2[0]["p"]) if tab2[4]["p"] is not None and tab2[0]["p"] is not None else None,
                     "n_min_resolved": min(t["n_resolved"] for t in tab2)}
    # E-add: stratify displacements by size tercile
    sb = np.digitize(P.ratio[d_idx], SIZE_EDGES)
    strata = []
    per = []
    for k in range(3):
        gk, _ = E.bins_counts(d_idx[sb == k])
        strata.append(gk)
        per.append({"size_tercile": k + 1, "episodes": int((sb == k).sum()), "p_by_bin": [round(t / (t + s_), 3) if t + s_ else None for t, s_ in gk]})
    sc_, y_, day_, keep_ = E.resolved_arrays(d_idx)
    z3, p3, sl3 = w1.cluster_slope_test(sc_, y_, day_, stratum=np.digitize(P.ratio[keep_], SIZE_EDGES))
    zc3, pc3 = combined_trend(strata)
    out["E_add"] = {"trend_z": round(z3, 3), "trend_p": p3, "slope_per_bin": sl3, "naive_trend_z": round(zc3, 3), "naive_trend_p": pc3, "per_size_tercile": per}
    # descriptive: direction split and stability for E-ord and E-disp
    desc = {}
    for name, idx in (("ordinary", ord_idx), ("displacement", d_idx)):
        dd = {}
        for sign, nm in ((1, "bullish"), (-1, "bearish")):
            gs, fs = E.bins_counts(idx[P.dirn[idx] == sign])
            tt = _table(gs, fs)
            dd[nm] = {"p_by_bin": [t["p"] for t in tt], "top_minus_bottom": (tt[4]["p"] - tt[0]["p"]) if tt[4]["p"] is not None and tt[0]["p"] is not None else None}
        mid = P.cut // 2
        for nm, sel in (("first_half", idx < mid), ("second_half", idx >= mid)):
            gs, fs = E.bins_counts(idx[sel])
            tt = _table(gs, fs)
            dd[nm] = {"p_by_bin": [t["p"] for t in tt], "top_minus_bottom": (tt[4]["p"] - tt[0]["p"]) if tt[4]["p"] is not None and tt[0]["p"] is not None else None,
                      "trend_p": combined_trend([gs])[1]}
        desc[name] = dd
    out["descriptive"] = desc
    return out


def run_wave5(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    clean = {tf: (preloaded[tf] if preloaded else rc.clean_timeframe(tf, raw_dir, source)[0]) for tf in ("5m", "15m", "1h")}
    s5 = rm.Series(clean["5m"], 5)
    P = {"5m": w1.Prepared("5m", clean["5m"]), "15m": w1.Prepared("15m", clean["15m"], s_lo=s5)}
    rep15 = rc.clean_timeframe("15m", raw_dir, source)[2] if not preloaded else {
        "first": None, "last": None, "raw_lines_parsed": 0, "unique_timestamps": 0, "duplicate_lines": 0, "conflicting_duplicates": 0,
        "dropped_market_closed": 0, "dropped_off_grid": 0, "dropped_invalid_ohlc": 0, "kept_bars": len(clean["15m"]),
        "unexplained_gaps": 0, "missing_open_bars": 0}
    a6 = rg.gate_a6({"15m": clean["15m"]}, {"15m": rep15}, {"15m": P["15m"].s})
    status = rg.gate_a7({"15m": clean["15m"]}, {"15m": P["15m"].s}, a6)["details"]["status"]
    cal = {tf: null_sd(tf) for tf in TFS}
    res = {tf: run_tf(Ext(P[tf], clean["1h"])) for tf in TFS}
    for tf in TFS:
        for key in ("E_ord", "E_disp", "E_add"):
            r = res[tf][key]
            f_ = max(1.0, cal[tf][key])
            r["null_sd"] = cal[tf][key]
            r["raw_robust_z"], r["raw_robust_p"] = r["trend_z"], r["trend_p"]
            r["trend_z"] = round(r["trend_z"] / f_, 3)
            r["trend_p"] = w1.norm_p(r["trend_z"])
    tests = []
    for tf in TFS:
        for key in ("E_ord", "E_disp", "E_add"):
            tests.append(((key, tf), res[tf][key]["trend_p"]))
    reject, qv = w1.bh_fdr([p for _, p in tests])
    fdr = {t: (rj, q) for (t, _), rj, q in zip(tests, reject, qv)}
    for tf in TFS:
        for key in ("E_ord", "E_disp", "E_add"):
            r = res[tf][key]
            r["fdr_reject"], r["q_value"] = fdr[(key, tf)]
            tb = r.get("top_minus_bottom")
            sig = r["q_value"] < FDR_Q
            if key == "E_add":
                r["verdict"] = "EXPLORATORY_SIGNAL (FDR-surviving)" if sig else "NOT_SIGNIFICANT"
            elif r.get("n_min_resolved", 0) < 30:
                r["verdict"] = "DATA-LIMITED"
            elif sig and tb is not None and abs(tb) >= MIN_EFFECT:
                r["verdict"] = "EXPLORATORY_SIGNAL (FDR-surviving, >= 5 pp)"
            elif sig:
                r["verdict"] = "SMALL_EFFECT (FDR-surviving)"
            else:
                r["verdict"] = "NOT_SIGNIFICANT"
    return {"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False,
            "n_primary_tests": len(tests), **res}


# ---------------------------------------------------------------------------
# PRE-REGISTERED HYPOTHESIS H-EXT-5M (frozen 2026-10-06). Holdout NOT opened.
# ---------------------------------------------------------------------------
H_EXT_5M = {
    "id": "H-EXT-5M", "frozen_utc": "2026-10-06", "timeframe": "5m",
    "claim": "among displacements, continuation probability FALLS as the bar stretches further away from the 1h EMA50",
    "population": "episode-first displacements (K=12 anchored per direction), range/ATR14(prior) >= 1.0, extension known (closed 1h bars)",
    "bins_A": [None, -1.9, -0.5, 0.6, 2.0, None],
    "statistic": "linear trend of P(+1R first) over the 5 fixed bins (E-disp); cluster-robust z divided by a frozen inflation factor of 1.15",
    "direction": "negative, one-sided, alpha = 0.05",
    "outcome": "as Wave 1: next-open entry, +-1R = ATR14 at the event bar, 24 bars, first touch",
    "secondary_descriptive": ["E-add (after allowing for size)", "bullish and bearish separately", "15m (not expected: no effect on development)"],
    "dev_observed_first_12_months": {"p_by_bin": [0.5507, 0.5021, 0.4852, 0.4886, 0.4773], "top_minus_bottom": -0.073, "calibrated_z": -3.17, "q": 0.0049},
    "open_rule": "open the holdout ONCE, only when expected power >= 0.80 at the dev-observed effect; INCONCLUSIVE counts as NOT CONFIRMED",
}


def ext_readiness(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", sims=2000, preloaded=None):
    """Counts HOLDOUT displacement events per frozen bin and estimates power. No outcome is read."""
    clean = {tf: (preloaded[tf] if preloaded else rc.clean_timeframe(tf, raw_dir, source)[0]) for tf in ("5m", "1h")}
    P = w1.Prepared("5m", clean["5m"])
    E = Ext(P, clean["1h"])
    idx = E.events(P.ratio >= 1.0, dev=False)
    bins = np.array([bin_of(E.A[i]) for i in idx], int)
    counts = np.bincount(bins, minlength=5).tolist() if len(idx) else [0] * 5
    first = P.s.index[P.hold_start] if P.hold_start < P.n else None
    months = (P.s.index[-1] - first).days / 30.44 if first is not None else 0.0
    per_month = len(idx) / months if months > 0 else None
    rng = np.random.default_rng(3)
    obs = H_EXT_5M["dev_observed_first_12_months"]["p_by_bin"]

    def power(n_total, p_lo, p_hi):
        ps = np.linspace(p_lo, p_hi, 5)
        per = max(1, n_total // 5)
        hit = 0
        for _ in range(sims):
            gs = []
            for p in ps:
                t = int(rng.binomial(per, p))
                gs.append((t, per - t))
            z, _ = w1.trend_test(gs)
            hit += 1 if z < -1.645 * 1.15 else 0
        return hit / sims
    full, half = (obs[0], obs[4]), ((obs[0] + 0.5) / 2, (obs[4] + 0.5) / 2)
    need = None
    for nt in range(600, 8001, 200):
        if power(nt, *full) >= 0.80:
            need = nt
            break
    n = len(idx)
    return {"hypothesis": H_EXT_5M["id"], "holdout_from": str(first)[:10] if first is not None else None, "holdout_months": round(months, 2),
            "holdout_episodes_total": n, "per_bin": counts, "episodes_per_month": None if per_month is None else round(per_month, 1),
            "power_now_full_effect": round(power(n, *full), 2), "power_now_half_effect": round(power(n, *half), 2),
            "episodes_needed_for_80pct_power_full_effect": need,
            "additional_months_to_wait": None if (need is None or not per_month) else round(max(0.0, need / per_month - months), 1),
            "ready_to_open": bool(need is not None and n >= need)}


def _pp(x):
    return "n/a" if x is None else f"{100 * x:+.1f}pp"


def render_markdown(r):
    L = ["# Observatory — Wave 5 report (extension from the 1h average)\n",
         f"Contract hash `{r['contract_hash']}` · dataset status **{r['dataset_status']}** · holdout opened: **False** · primary tests: {r['n_primary_tests']}\n",
         "**EXPLORATORY: the direction of this effect was first seen on development data (Wave 3 control arm, B2). A finding here is a hypothesis for one future holdout test, not a result. Nothing is a trading rule.**\n",
         "A = how far the bar's direction stretches away from the 1h EMA50, in 1h-ATR units (A > 0: moving away from the average, A < 0: toward it). "
         "Bins are fixed: A < −1.9 | −1.9…−0.5 | −0.5…0.6 | 0.6…2.0 | ≥ 2.0. p = P(+1R before −1R) continuing the bar; "
         "fwd = mean signed forward move over 24 bars in ATR units (descriptive).\n"]
    names = {"E_ord": "E-ord · ordinary bars", "E_disp": "E-disp · displacements (≥ 1.0 ATR)", "E_add": "E-add · displacements, trend after allowing for size"}
    for tf in TFS:
        t = r[tf]
        L.append(f"## {tf}\n")
        for key in ("E_ord", "E_disp"):
            e = t[key]
            L.append(f"### {names[key]} (episodes {e['episodes']})")
            for row in e["table"]:
                lo = "−∞" if row["A_from"] is None else row["A_from"]
                hi = "+∞" if row["A_to"] is None else row["A_to"]
                L.append(f"- bin {row['bin']} (A {lo} … {hi}): p={row['p']} {row['ci']} n={row['n_resolved']} · fwd {row['mean_fwd_ATR']}")
            L.append(f"- calibrated trend z={e['trend_z']} p={e['trend_p']:.4f} q={e['q_value']:.4f} (raw robust z={e['raw_robust_z']}, naive z={e['naive_trend_z']}, null sd {e['null_sd']:.2f}) · top−bottom {_pp(e['top_minus_bottom'])} · **{e['verdict']}**\n")
        a = t["E_add"]
        L.append(f"### {names['E_add']}")
        for pt in a["per_size_tercile"]:
            L.append(f"- size tercile {pt['size_tercile']} (n={pt['episodes']}): p by bin {pt['p_by_bin']}")
        L.append(f"- calibrated trend z={a['trend_z']} p={a['trend_p']:.4f} q={a['q_value']:.4f} (raw robust z={a['raw_robust_z']}, naive z={a['naive_trend_z']}, null sd {a['null_sd']:.2f}) · **{a['verdict']}**\n")
        L.append("### stability (descriptive)")
        for pop, dd in t["descriptive"].items():
            for nm in ("bullish", "bearish", "first_half", "second_half"):
                v = dd[nm]
                L.append(f"- {pop} · {nm}: p by bin {v['p_by_bin']} · top−bottom {_pp(v['top_minus_bottom'])}" + (f" · trend p={v['trend_p']:.3f}" if "trend_p" in v else ""))
        L.append("")
    L.append("## Not run in this wave\n- Range location, session, FVG interaction, prior displacement history (each its own question).\n- Holdout: sealed.\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def selftest(null_series=20):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    from . import gates as g
    rng = np.random.default_rng(31)
    d5 = g._random_walk_series(9000, rng, step=5).df
    d60 = _aggregate(d5, 60)
    X = extension_X(d60, 60, d5.index, 5, d5["close"].to_numpy())
    chk("unknown before warm-up", bool(np.isnan(X[:300]).all()), True)
    # sign convention: extension above the EMA is positive; A = d * X
    ema = d60["close"].ewm(span=EMA_SPAN, adjust=False).mean().to_numpy()
    atr = rm.atr(d60).to_numpy()
    i = 5000
    et = d5.index[i] + pd.Timedelta(minutes=5)
    pos = (d60.index + pd.Timedelta(minutes=60)).searchsorted(et, side="right") - 1
    want = (d5["close"].iloc[i] - ema[pos]) / atr[pos]
    chk("X formula on a sample bar", bool(np.isclose(X[i], want)), True)
    # no lookahead: removing or altering future 1h data leaves earlier X unchanged
    cut_t = d5.index[6000]
    trunc = d60[d60.index + pd.Timedelta(minutes=60) <= cut_t + pd.Timedelta(minutes=5)]
    Xp = extension_X(trunc, 60, d5.index[:6001], 5, d5["close"].to_numpy()[:6001])
    chk("X unchanged when the future is removed", bool(np.allclose(X[:6001], Xp, equal_nan=True)), True)
    alt = d60.copy()
    alt.loc[alt.index > cut_t, "close"] *= 1.5
    Xa = extension_X(alt, 60, d5.index[:6001], 5, d5["close"].to_numpy()[:6001])
    chk("X unchanged when the future is altered", bool(np.allclose(X[:6001], Xa, equal_nan=True)), True)
    # bins
    chk("bin edges", [bin_of(v) for v in (-3, -1.9, -1.0, -0.5, 0.0, 0.6, 1.0, 2.0, 5)], [0, 1, 1, 2, 2, 3, 3, 4, 4])
    # CA components combine to the plain trend test for one stratum
    grp = [(20, 80), (30, 70), (40, 60), (50, 50), (60, 40)]
    z1, _ = w1.trend_test(grp)
    T, V = ca_components(grp)
    chk("CA components equal trend_test z", bool(np.isclose(T / math.sqrt(V), z1)), True)
    # two identical strata double the information, not the effect size
    z2, _ = combined_trend([grp, grp])
    chk("combined trend grows like sqrt(2)", bool(np.isclose(z2, z1 * math.sqrt(2))), True)
    # cluster-robust slope test: a true trend is detected, none is not, and clustering widens the variance
    rr = np.random.default_rng(2)
    sc = rr.integers(1, 6, 4000).astype(float)
    cl = rr.integers(0, 400, 4000)
    yv = (rr.random(4000) < 0.5 + 0.03 * (sc - 3)).astype(float)
    zt, _, bt = w1.cluster_slope_test(sc, yv, cl)
    chk("cluster test detects a real trend (+3 pp per bin)", bool(zt > 4 and abs(bt - 0.03) < 0.01), True)
    yn = (rr.random(4000) < 0.5).astype(float)
    chk("cluster test finds nothing in noise", abs(w1.cluster_slope_test(sc, yn, cl)[0]) < 3.0, True)
    # null: no extension effect on random walks
    cal = null_sd("5m", n_series=40, seed=77)
    fp, used = 0, 0
    zs = []
    for _ in range(null_series):
        ser = g._random_walk_series(45000, rng, step=5)
        d5_ = ser.df
        P_ = w1.Prepared("5m", d5_)
        E_ = Ext(P_, _aggregate(d5_, 60))
        r = run_tf(E_)
        used += 1
        for key in ("E_ord", "E_disp", "E_add"):
            zc = r[key]["trend_z"] / max(1.0, cal[key])
            zs.append(zc)
            fp += 1 if abs(zc) > 1.96 else 0
    chk("calibrated null false-positive rate <= 15% (nominal 5%; mild over-rejection is disclosed)", used > 0 and fp / (3 * used) <= 0.15, True)
    chk("null mean z near zero", abs(float(np.mean(zs))) < 0.5, True)
    # note: the sd of the raw displacement z varied between 1.0 and 1.3 across seeds (heavy-tailed), which is why it is
    # re-estimated at every run instead of being hard-coded.
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": used, "calibrated_false_positive_rate": round(fp / (3 * used), 3) if used else None, "mean_z": round(float(np.mean(zs)), 3) if zs else None, "null_sd": {k: round(v, 2) for k, v in cal.items()}}}


def main():
    ap = argparse.ArgumentParser(description="Observatory Wave 5 (extension). Read-only on raw data.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--readiness", action="store_true", help="count holdout events for the frozen H-EXT-5M hypothesis; no outcomes are read")
    a = ap.parse_args()
    if a.readiness:
        print(json.dumps({"spec": H_EXT_5M, "readiness": ext_readiness(a.raw_dir, a.source)}, indent=2, default=str))
        return
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    res = run_wave5(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "wave5_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "wave5_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
