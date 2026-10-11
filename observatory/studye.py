"""
observatory/studye.py
=====================
OBSERVATORY — STUDY E: price progress, path cost and time (the "journey" question). ISOLATED from the live bot.

THE QUESTION
  At a fixed observation time (no event selection, no swing confirmation), does the PATH price has taken so far carry information about what it does next,
  beyond what recent movement, volatility and the clock already tell us?  (The water-carrying analogy is retired as a mechanism: nothing here assumes price
  "works", "conserves effort" or "has a destination". OHLC data cannot show hidden order pressure and this study does not claim to.)

DESIGN (all fixed before any outcome is read)
  Observation   every full-hour close (the last 5m bar of each UTC hour), both sides folded together by ORIENTATION: the side is the sign of the net move over the
                lookback; the structure below is measured in that direction (down = exact mirror, prices negated).
  Lookback      L = 48 5m bars (4h) is PRIMARY (the horizon of Study A's one positive finding). L = 12 (1h) and L = 144 (12h) are reported as descriptive sensitivity only.
  Features      measured on the closes of the lookback window, in ATR(1h) units, using only bars up to the observation:
                  eff    efficiency  |net| / total path
                  retr   retracement given back: (highest oriented close in the window - current close) / ATR
                  stall  time since the last extreme: (bars since the highest oriented close) / L           (0 = at the extreme now, 1 = it was L bars ago)
                  acc    pace: progress in the last quarter of the window minus an even quarter of the total progress, in ATR (acceleration / deceleration)
  Controls      progress |net|/ATR, log(path/ATR) (volatility), log of the short/long path-rate ratio (is volatility rising or falling), side, hour of day
                (two harmonics), weekday. Efficiency is a ratio of controls, so the linear controls cannot absorb it.
  Outcomes      horizon H = 72 5m bars (6h) from the next 5m bar:
                  DIRECTION  continuation = +1 ATR in the direction of the lookback move before -1 ATR; both in one bar or neither = unresolved (excluded here, counted)
                  EXPANSION  log(forward range / ATR), forward range = highest high - lowest low over the horizon (size of the move, no direction)
  Grid          one observation per hour. Outcome windows overlap, so inference is clustered by 2-day block and ALL results below are out of sample.
  Test          'does information X add anything?' = out-of-sample improvement of a model with controls + X over the controls alone. 8 contiguous time folds,
                a 24h purge around each test fold (training never sees observations whose windows touch the test fold). Statistic = mean per-observation loss
                reduction (log-loss for DIRECTION via ridge logistic regression, squared error / variance for EXPANSION via ridge OLS), SE clustered by 2-day block,
                ONE-SIDED p (improvement). Under the null the gain is slightly NEGATIVE (the larger model overfits), which makes the test conservative.
  Effect size   realised lift: outcome of the top decile minus the bottom decile of the model's predicted shift (out of sample), cluster SE. In probability points for
                DIRECTION (x 2 ATR = pips) and in log-range units for EXPANSION.

FAMILIES (BH-FDR q = 0.10 inside each)
  family 1 (primary)    E.1 the whole feature set adds DIRECTIONAL information;  E.2 the whole feature set adds EXPANSION information   (2 tests)
  family 2 (secondary)  each feature alone x 2 outcomes: eff, retr, stall, acc  (8 tests)  — 'stall' is the friend's breakout-setup claim, 'eff' the follow-up of Study A's 4h finding
  Reported, not tested: three-way outcome shares by stall tercile (continues / reverses / stays unresolved); sensitivity at L = 12 and 144; standardised coefficients.
  Practical thresholds (pre-declared): DIRECTION gain 0.0008 log-loss (about a 2 probability-point shift), EXPANSION gain 0.005 of the variance.

PRE-DECLARED READING (the friend's three-way table)
  E.1 survives, E.2 not  -> conditional DIRECTIONAL information beyond controls.
  E.2 survives, E.1 not  -> EXPANSION information only: the path predicts how much price moves, not which way.
  neither survives       -> no demonstrated incremental information from the path features at this lookback.
  Surviving is a development result: a hypothesis for one holdout test, never a trading rule. Reaching a barrier is not a profitable trade.
The holdout (>= HOLDOUT_START_UTC) is sealed: no outcome is computed for it; --readiness counts holdout observations and computes power from development estimates.
--power runs a simulation with the real sampling structure (hourly grid, overlapping windows, clustering, volatility clustering) and reports what size of effect is detectable.

Run:  python3 -m observatory.studye [--raw-dir ...] [--out ...]   --selftest   --counts   --readiness   --power
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
from . import wave1 as w1
from . import studya as sa
from . import studyb as sb
from . import studyc as sc
from .studyb import barrier, atr_at, weekday, NS_5, NS_D, NS_H, PIP, FDR_Q
from .studya import _ns, _phi, _a, _ci, _p
from .studyc import cluster_test, cluster_diff

L_PRIMARY, L_SENS = 48, (12, 144)
H_BARS = 72
GRID = 12
K_FOLDS = 8
PURGE_NS = 24 * NS_H
RIDGE = 1.0
MIN_GAIN = {"dir": 0.0008, "exp": 0.005}
BARRIER_ATR = 1.0
FEATS = ("eff", "retr", "stall", "acc")
CTRLS = ("prog", "logpath", "logrvr", "side", "hs1", "hc1", "hs2", "hc2", "wd0", "wd1", "wd2", "wd3")
OUTCOMES = ("dir", "exp")
CONTRACT = {
    "study": "E", "module": "studye", "version": "v1", "instrument": "GBPUSD", "grid": "every full-hour close", "lookback_5m_bars": {"primary": L_PRIMARY, "sensitivity": list(L_SENS)},
    "horizon_5m_bars": H_BARS, "barrier_atr": BARRIER_ATR, "features": list(FEATS), "controls": list(CTRLS), "folds": K_FOLDS, "purge_hours": 24, "ridge": RIDGE,
    "min_gain": MIN_GAIN, "inference": "one-sided clustered (2-day block) mean out-of-sample loss reduction", "fdr_q": FDR_Q,
    "family1": ["E.1 direction, all features", "E.2 expansion, all features"], "family2": ["each feature alone x direction/expansion (8)"],
    "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# features (pure functions of a window of closes; nothing after the window)
# ---------------------------------------------------------------------------
def path_features(W, a):
    """W: (n, L+1) closes of the lookback window ending at the observation; a: (n,) ATR at the observation.
    Orientation = sign of the net move; rows with zero net move get sg = 0 (excluded by the caller). Returns dict of arrays."""
    W = np.asarray(W, float)
    n, Lp = W.shape
    L = Lp - 1
    net = W[:, -1] - W[:, 0]
    sg = np.sign(net)
    X = sg[:, None] * W
    dX = np.diff(X, axis=1)
    pathraw = np.abs(dX).sum(1)
    q = max(L // 4, 1)
    prog = (X[:, -1] - X[:, 0]) / a
    pos = L - np.argmax(X[:, ::-1], axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        eff = np.where(pathraw > 0, np.abs(net) / pathraw, np.nan)
        rvr = (np.abs(dX[:, -q:]).sum(1) / q) / (pathraw / L)
    return {"sg": sg, "prog": prog, "path": pathraw / a, "eff": eff, "retr": (X.max(1) - X[:, -1]) / a, "stall": (L - pos) / L,
            "acc": (X[:, -1] - X[:, -1 - q]) / a - prog / 4, "rvr": rvr}


def build_obs(s5, s1h, L, H, hold_ns):
    """All hourly observations with features; outcomes ONLY for development observations (window ends before the holdout boundary)."""
    ns5 = _ns(s5.index)
    c, h, l = np.asarray(s5.c, float), np.asarray(s5.h, float), np.asarray(s5.l, float)
    n = len(ns5)
    ns1 = _ns(s1h.index)
    p = np.flatnonzero((ns5 + NS_5) % NS_H == 0)
    p = p[(p >= L) & (p + 1 + H <= n)]
    ok = (ns5[p] - ns5[p - L] == L * NS_5) & (ns5[p + H] - ns5[p + 1] == (H - 1) * NS_5)
    p = p[ok]
    t = ns5[p] + NS_5
    a = atr_at(t, ns1, s1h.atr)
    good = np.isfinite(a) & (a > 0)
    p, t, a = p[good], t[good], a[good]
    W = c[p[:, None] + np.arange(-L, 1)[None, :]]
    F = path_features(W, a)
    keep = (F["sg"] != 0) & np.isfinite(F["eff"]) & np.isfinite(F["rvr"]) & (F["rvr"] > 0) & (F["path"] > 0)
    p, t, a = p[keep], t[keep], a[keep]
    F = {k: v[keep] for k, v in F.items()}
    hr = (t // NS_H) % 24
    wd = weekday(t)
    O = {"p": p, "t": t, "a": a, "sg": F["sg"], "prog": F["prog"], "logpath": np.log(F["path"]), "logrvr": np.log(F["rvr"]), "side": (F["sg"] > 0).astype(float),
         "eff": F["eff"], "retr": F["retr"], "stall": F["stall"], "acc": F["acc"],
         "hs1": np.sin(2 * np.pi * hr / 24), "hc1": np.cos(2 * np.pi * hr / 24), "hs2": np.sin(4 * np.pi * hr / 24), "hc2": np.cos(4 * np.pi * hr / 24)}
    for d in range(4):
        O[f"wd{d}"] = (wd == d).astype(float)
    dev = t + (H + 1) * NS_5 <= hold_ns
    O["dev"], O["hold"] = dev, t >= hold_ns
    code = np.full(len(p), -1, int)
    ydir = np.full(len(p), np.nan)
    yexp = np.full(len(p), np.nan)
    for i in np.flatnonzero(dev):
        pi, ai = p[i], a[i]
        close = c[pi]
        o, st = barrier(h, l, pi + 1, close + BARRIER_ATR * ai, close - BARRIER_ATR * ai, H)
        if st == "ok":
            ydir[i] = o if O["sg"][i] > 0 else 1.0 - o
            code[i] = 1 if ydir[i] == 1.0 else 0
        else:
            code[i] = 2 if st == "unresolved" else 3
        fr = h[pi + 1:pi + 1 + H].max() - l[pi + 1:pi + 1 + H].min()
        yexp[i] = math.log(fr / ai) if fr > 0 else np.nan
    O["ydir"], O["yexp"], O["code"] = ydir, yexp, code
    return O


# ---------------------------------------------------------------------------
# models and out-of-sample evaluation
# ---------------------------------------------------------------------------
def fit_logit(X, y, lam=RIDGE, iters=40):
    Z = np.c_[np.ones(len(X)), X]
    w = np.zeros(Z.shape[1])
    R = lam * np.eye(Z.shape[1])
    R[0, 0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
        W = p * (1 - p) + 1e-9
        step = np.linalg.solve((Z * W[:, None]).T @ Z + R, Z.T @ (y - p) - R @ w)
        w = w + step
        if np.max(np.abs(step)) < 1e-9:
            break
    return w


def fit_ols(X, y, lam=RIDGE):
    Z = np.c_[np.ones(len(X)), X]
    R = lam * np.eye(Z.shape[1])
    R[0, 0] = 0.0
    return np.linalg.solve(Z.T @ Z + R, Z.T @ y)


def _std(Xtr, Xte):
    mu, sd = Xtr.mean(0), Xtr.std(0)
    sd = np.where(sd > 0, sd, 1.0)
    return (Xtr - mu) / sd, (Xte - mu) / sd


def _predict(kind, w, Z):
    eta = np.c_[np.ones(len(Z)), Z] @ w
    return 1 / (1 + np.exp(-np.clip(eta, -30, 30))) if kind == "dir" else eta


def cv_splits(t, k=K_FOLDS, purge=PURGE_NS):
    """Contiguous time folds over time-ordered observations; training excludes everything within `purge` of the test fold."""
    n = len(t)
    edges = np.linspace(0, n, k + 1).astype(int)
    out = []
    for i in range(k):
        te = np.arange(edges[i], edges[i + 1])
        lo, hi = t[te].min() - purge, t[te].max() + purge
        tr = np.flatnonzero(~((t >= lo) & (t <= hi)))
        out.append((tr, te))
    return out


def cv_oos(Xb, Xf, y, t, kind):
    pb, pf = np.full(len(y), np.nan), np.full(len(y), np.nan)
    for tr, te in cv_splits(t):
        for X, out in ((Xb, pb), (Xf, pf)):
            a, b = _std(X[tr], X[te])
            w = fit_logit(a, y[tr]) if kind == "dir" else fit_ols(a, y[tr])
            out[te] = _predict(kind, w, b)
    return pb, pf


def gain_test(y, pb, pf, t, kind):
    if kind == "dir":
        eps = 1e-6
        pb, pf = np.clip(pb, eps, 1 - eps), np.clip(pf, eps, 1 - eps)
        d = -(y * np.log(pb) + (1 - y) * np.log(1 - pb)) + (y * np.log(pf) + (1 - y) * np.log(1 - pf))
    else:
        d = ((y - pb) ** 2 - (y - pf) ** 2) / float(np.var(y))
    ct = cluster_test(d, t)
    if ct.get("mean") is None:
        return ct, d
    z = ct["z"]
    ct["p1"] = 0.5 * w1.norm_p(z) if z > 0 else 1 - 0.5 * w1.norm_p(z)
    return ct, d


def lift_effect(y, pb, pf, t, kind):
    s = pf - pb
    if len(s) < 100:
        return None
    hi, lo = s >= np.quantile(s, 0.9), s <= np.quantile(s, 0.1)
    r = cluster_diff(y[hi], t[hi], y[lo], t[lo])
    r["pred_spread"] = float(s[hi].mean() - s[lo].mean())
    return r


def _design(O, cols, mask):
    return np.column_stack([O[c][mask] for c in cols])


def evaluate(O, feats, kind):
    """Out-of-sample test of controls+feats against controls. Returns result dict."""
    y_all = O["ydir"] if kind == "dir" else O["yexp"]
    m = O["dev"] & np.isfinite(y_all)
    n = int(m.sum())
    if n < 400:
        return {"n": n, "mean": None, "kind": kind, "feats": list(feats)}
    y, t = y_all[m], O["t"][m]
    Xb = _design(O, CTRLS, m)
    Xf = _design(O, CTRLS + tuple(feats), m)
    pb, pf = cv_oos(Xb, Xf, y, t, kind)
    ct, d = gain_test(y, pb, pf, t, kind)
    ct.update(kind=kind, feats=list(feats), n=n, mean_atr_pips=float(O["a"][m].mean() / PIP), min_gain=MIN_GAIN[kind])
    lf = lift_effect(y, pb, pf, t, kind)
    if lf is not None:
        ct["lift"] = {k: lf.get(k) for k in ("diff", "se", "ci", "z", "p", "pred_spread")}
    return ct


def verdict_e(res, reject):
    if res.get("mean") is None or res["n"] < 400:
        return "DATA-LIMITED"
    lo, hi, g = res["ci"][0], res["ci"][1], res["mean"]
    mg = res["min_gain"]
    if reject and g > 0:
        return "DISCOVERY_PASS" if g >= mg else "SMALL_EFFECT"
    if hi < mg:
        return "NO_INFORMATION"
    return "INCONCLUSIVE"


def stall_table(O):
    m = O["dev"] & (O["code"] >= 0)
    s = O["stall"][m]
    code = O["code"][m]
    q1, q2 = np.quantile(s, [1 / 3, 2 / 3])
    rows = []
    for nm, mk in (("low stall (at a recent extreme)", s <= q1), ("mid stall", (s > q1) & (s <= q2)), ("high stall (extreme was long ago)", s > q2)):
        c = code[mk]
        n = int(mk.sum())
        rows.append({"group": nm, "n": n, "continues": float((c == 1).mean()), "reverses": float((c == 0).mean()), "unresolved": float((c == 2).mean()),
                     "ambiguous": float((c == 3).mean()), "stall_range": [float(s[mk].min()), float(s[mk].max())]})
    return rows


def coefficients(O):
    out = {}
    for kind in OUTCOMES:
        y_all = O["ydir"] if kind == "dir" else O["yexp"]
        m = O["dev"] & np.isfinite(y_all)
        cols = CTRLS + FEATS
        X = _design(O, cols, m)
        mu, sd = X.mean(0), X.std(0)
        sd = np.where(sd > 0, sd, 1.0)
        w = fit_logit((X - mu) / sd, y_all[m]) if kind == "dir" else fit_ols((X - mu) / sd, y_all[m])
        out[kind] = {c: float(v) for c, v in zip(cols, w[1:])}
    return out


def run_spec(s5, s1h, L, hold_ns, full):
    O = build_obs(s5, s1h, L, H_BARS, hold_ns)
    res = {"L": L, "obs_dev": int(O["dev"].sum()), "obs_hold": int(O["hold"].sum()), "resolved_share": float((O["code"][O["dev"]] <= 1).mean()) if O["dev"].any() else None}
    res["E1"] = evaluate(O, FEATS, "dir")
    res["E2"] = evaluate(O, FEATS, "exp")
    if full:
        res["S"] = {f"{f}:{k}": evaluate(O, (f,), k) for f in FEATS for k in OUTCOMES}
        res["stall_table"] = stall_table(O)
        res["coef"] = coefficients(O)
    return res, O


def apply_fdr(res):
    fam = {1: [res["E1"], res["E2"]], 2: list(res["S"].values())}
    counts = {}
    for f, tests in fam.items():
        live = [r for r in tests if r.get("p1") is not None]
        rej, q = w1.bh_fdr([r["p1"] for r in live]) if live else ([], [])
        counts[f] = len(live)
        for r, rj, qq in zip(live, rej, q):
            r["family"], r["fdr_reject"], r["q_value"] = f, bool(rj), float(qq)
            r["verdict"] = verdict_e(r, bool(rj))
        for r in tests:
            if r.get("p1") is None:
                r["verdict"] = "DATA-LIMITED"
    return counts


def reading(res):
    e1, e2 = res["E1"], res["E2"]
    d, x = bool(e1.get("fdr_reject")) and (e1.get("mean") or 0) > 0, bool(e2.get("fdr_reject")) and (e2.get("mean") or 0) > 0
    sec = [k for k, v in res["S"].items() if v.get("fdr_reject") and (v.get("mean") or 0) > 0]
    if d and x:
        out = "E.1 and E.2 both survive: the path features add DIRECTIONAL and EXPANSION information beyond controls on development data (hypotheses for one holdout test; see the lift columns for size)."
    elif d:
        out = "E.1 survives, E.2 does not: the path features add conditional DIRECTIONAL information beyond controls on development data (hypothesis for one holdout test; see the lift column for size)."
    elif x:
        out = "E.2 survives, E.1 does not: the path features predict HOW MUCH price moves, not which barrier comes first (expansion information only)."
    else:
        out = ("Neither E.1 nor E.2 survives multiple-testing control: no demonstrated incremental information from progress, retracement, stall time or pace at the 4h lookback "
               "beyond progress, volatility and the clock.")
    out += (" Secondary (single-feature, exploratory) survivors: " + ", ".join(sec) + "." if sec else " No single-feature secondary test survives.")
    return out


def run_from_series(s5, s1h, status, hold_ns, sens=True):
    res, O = run_spec(s5, s1h, L_PRIMARY, hold_ns, True)
    res["n_tests"] = apply_fdr(res)
    if sens:
        res["sensitivity"] = {}
        for L in L_SENS:
            r, _ = run_spec(s5, s1h, L, hold_ns, False)
            res["sensitivity"][str(L)] = {k: r[k] for k in ("L", "obs_dev", "E1", "E2")}
    res["reading"] = reading(res)
    res.update({"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False})
    return res


def run_studye(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    res = run_from_series(s5, s1h, status, hold_ns)
    res["development"] = {"first_bar_utc": str(s5.index[0]), "boundary_utc": str(rc.HOLDOUT_START_UTC)}
    return res


def structure_counts(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    """Structure only: observation counts. No outcome is computed for any observation in this function."""
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    out = {"dataset_status": status, "bars_5m": len(s5.index), "specs": {}}
    for L in (L_PRIMARY,) + L_SENS:
        O = build_obs(s5, s1h, L, H_BARS, -(2 ** 62))      # boundary far in the past: nothing is dev, so no outcome is computed
        t = O["t"]
        hold_ns_ = hold_ns
        out["specs"][str(L)] = {"valid_observations_total": int(len(t)), "development": int((t + (H_BARS + 1) * NS_5 <= hold_ns_).sum()),
                                "holdout": int((t >= hold_ns_).sum()), "straddling_boundary": int(((t < hold_ns_) & (t + (H_BARS + 1) * NS_5 > hold_ns_)).sum()),
                                "clusters_2day_dev": int(len(np.unique(t[t + (H_BARS + 1) * NS_5 <= hold_ns_] // sc.BLOCK_NS)))}
    return out


def holdout_readiness(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None, dev_res=None):
    if dev_res is None:
        dev_res = run_studye(raw_dir, source, preloaded)
    cnt = structure_counts(raw_dir, source, preloaded)["specs"][str(L_PRIMARY)]
    s5, _, _ = sa.load_series(raw_dir, source, preloaded)
    months = max((s5.index[-1] - rc.HOLDOUT_START_UTC).days / 30.44, 0.0)
    out = {"holdout_from": str(rc.HOLDOUT_START_UTC)[:10], "holdout_months": round(months, 2), "holdout_observations": cnt["holdout"], "tests": {}}
    items = [("E.1 direction", dev_res["E1"]), ("E.2 expansion", dev_res["E2"])] + [(f"S {k}", v) for k, v in dev_res["S"].items()]
    for key, t in items:
        if t.get("mean") is None or t.get("se") is None:
            continue
        nh = cnt["holdout"]
        row = {"dev_gain": round(t["mean"], 6), "dev_q": t.get("q_value"), "survived_dev_fdr": bool(t.get("fdr_reject")), "holdout_obs": nh}
        if nh > 0 and t["n"] > 0 and months > 0:
            se_h = t["se"] * math.sqrt(t["n"] / nh)
            eff = abs(t["mean"])
            for nm, e in (("full", eff), ("half", eff / 2)):
                row[nm] = {"power_now": round(_phi(e / se_h - 1.645), 2),
                           "months_needed_for_80pct": round(months * (se_h / (e / (1.645 + 0.842))) ** 2, 1) if e > 0 else None}
            row["ready_to_open"] = bool(row["survived_dev_fdr"] and row["half"]["power_now"] >= 0.80)
        out["tests"][key] = row
    out["note"] = ("Only tests that survived the development FDR are candidates for the holdout; the rest are listed for power context. "
                   "The holdout stays sealed until a candidate's half-effect power is >= 0.80.")
    return out


# ---------------------------------------------------------------------------
# power simulation with the real sampling structure
# ---------------------------------------------------------------------------
def _plant(df, O, kind, size, rule):
    """Plant an effect into a synthetic 5m frame. kind 'dir': drift in the direction of the lookback move (size ATR over 48 bars) after selected observations;
    'exp': inflate the 5m moves by (1+size) over the 72 bars after selected observations. Returns a new frame and the selection mask."""
    n5 = len(df)
    sel = rule(O)
    c = df["close"].to_numpy()
    shift = np.zeros(n5)
    if kind == "dir":
        tt = np.arange(n5)
        for i in np.flatnonzero(sel):
            shift += O["sg"][i] * size * O["a"][i] * np.clip(tt - O["p"][i], 0, 48) / 48.0
    else:
        cover = np.zeros(n5)
        for i in np.flatnonzero(sel):
            cover[O["p"][i] + 1:O["p"][i] + 1 + H_BARS] = 1.0
        step = np.diff(c, prepend=c[0])
        new = c[0] + np.cumsum(step * (1.0 + size * cover))
        shift = new - c
    d2 = df.copy()
    so = np.concatenate([[0.0], shift[:-1]])
    d2["close"] = df["close"] + shift
    d2["open"] = df["open"] + so
    d2["high"] = np.maximum(df["high"] + shift, np.maximum(d2["open"], d2["close"]))
    d2["low"] = np.minimum(df["low"] + shift, np.minimum(d2["open"], d2["close"]))
    return d2, sel


RULES = {"dir": lambda O: O["dev"] & (O["stall"] <= 0.25) & (O["retr"] <= 0.25), "exp": lambda O: O["dev"] & (O["stall"] >= 0.5)}


def power_cell(kind, size, n_rep, seed, n5=130000, garch=True):
    """Detection rate of E.1 (kind 'dir') or E.2 (kind 'exp') when an effect of the given size is planted in a fraction of observations. Also returns the realised
    outcome lift among the selected observations and the share of observations selected."""
    far = pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value
    rng = np.random.default_rng(seed)
    hits, lifts, shares, zs = 0, [], [], []
    for r in range(n_rep):
        df = sb._synth_ohlc(n5, rng, garch=garch)
        s5, s1h = sa._series_from_df(df)
        O = build_obs(s5, s1h, L_PRIMARY, H_BARS, far)
        if size > 0:
            d2, sel = _plant(df, O, kind, size, RULES[kind])
            s5p, s1p = sa._series_from_df(d2)
            Op = build_obs(s5p, s1p, L_PRIMARY, H_BARS, far)
        else:
            Op, sel = O, RULES[kind](O)
        key = "ydir" if kind == "dir" else "yexp"
        both = sel & np.isfinite(O[key]) & np.isfinite(Op[key])
        lifts.append(float(Op[key][both].mean() - O[key][both].mean()) if both.any() and size > 0 else 0.0)
        shares.append(float(sel.mean()))
        res = evaluate(Op, FEATS, "dir" if kind == "dir" else "exp")
        if res.get("p1") is not None:
            zs.append(res["z"])
            hits += int(res["p1"] < 0.05)
    return {"kind": kind, "size": size, "reps": n_rep, "detect_rate": hits / n_rep, "mean_z": float(np.mean(zs)) if zs else None,
            "realised_lift": float(np.mean(lifts)), "selected_share": float(np.mean(shares))}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def render_markdown(r):
    n1, n2 = r["n_tests"][1], r["n_tests"][2]
    L = ["# Observatory — Study E: price progress, path cost and time (GBPUSD)\n",
         f"Contract `{r['contract_hash']}` · dataset **{r['dataset_status']}** · holdout opened: **False** · family 1: {n1} · family 2 (secondary): {n2} tests\n",
         "**Development data only. Out-of-sample (8 time folds, 24h purge), hourly observations, 4h lookback, 6h horizon, errors clustered by 2-day block. Nothing here is a trading rule; "
         "reaching a barrier is not a profitable trade.**\n",
         "Gain = mean out-of-sample loss reduction of (controls + features) over (controls alone); positive = the features help; one-sided p. DIRECTION gain is in log-loss "
         "(0.0008 is roughly a 2 probability-point shift); EXPANSION gain is in fractions of the variance of log forward range. Lift = realised outcome of the top decile minus the "
         "bottom decile of the model's predicted shift (probability points for direction, ×2 ATR = pips; log-range units for expansion).\n",
         "## Reading (pre-declared)\n", f"**{r['reading']}**\n"]
    head = ("| test | obs | clusters | gain | 95% CI | z | q | lift | lift 95% CI | lift pips | verdict |\n|---|---|---|---|---|---|---|---|---|---|---|")

    def row(lab, t):
        if t.get("mean") is None:
            return f"| {lab} | {t.get('n', 0)} | {t.get('n_clusters', 0)} | n/a | n/a | n/a | n/a | n/a | n/a | n/a | **{t.get('verdict', 'DATA-LIMITED')}** |"
        lf = t.get("lift") or {}
        pips = (f"{lf['diff'] * 2 * t['mean_atr_pips']:+.2f}" if (lf.get("diff") is not None and t["kind"] == "dir") else "n/a")
        return (f"| {lab} | {t['n']} | {t['n_clusters']} | {t['mean']:+.5f} | [{t['ci'][0]:+.5f}, {t['ci'][1]:+.5f}] | {t['z']:+.2f} | {_p(t.get('q_value'))} | "
                f"{_a(lf.get('diff'))} | {_ci(lf.get('ci'))} | {pips} | **{t.get('verdict', '')}** |")
    L += ["## E.1 / E.2 — does the whole path state add information? (family 1)\n", head, row("E.1 direction (continuation before reversal)", r["E1"]),
          row("E.2 expansion (size of the forward range)", r["E2"]),
          f"\nDevelopment observations: {r['obs_dev']} (resolved +-1 ATR within 6h: {100 * (r['resolved_share'] or 0):.1f}%); holdout observations (counted only): {r['obs_hold']}.\n",
          "## Single features (family 2, secondary)\n", head]
    names = {"eff": "efficiency", "retr": "retracement given back", "stall": "stall (time since last extreme)", "acc": "pace / acceleration"}
    for k, t in r["S"].items():
        f, kind = k.split(":")
        L.append(row(f"{names[f]} · {'direction' if kind == 'dir' else 'expansion'}", t))
    L += ["\n## Stall: three outcomes, not just 'a breakout' (descriptive)\n",
          "Share of observations whose next 6h ends with: continuation (+1 ATR in the lookback direction first), reversal (−1 ATR first), or no resolution (price stays inside ±1 ATR).\n",
          "| group | obs | continues | reverses | unresolved | both barriers in one bar |\n|---|---|---|---|---|---|"]
    for g in r["stall_table"]:
        L.append(f"| {g['group']} | {g['n']} | {100 * g['continues']:.1f}% | {100 * g['reverses']:.1f}% | {100 * g['unresolved']:.1f}% | {100 * g['ambiguous']:.1f}% |")
    L.append("\nThese shares are NOT controlled for volatility or progress; the tests above are. A difference here that disappears in the tests is volatility, not path.\n")
    L += ["## Sensitivity: other lookbacks (descriptive, not in any FDR family)\n", head]
    for Ls, s in r["sensitivity"].items():
        L.append(row(f"E.1 direction · L={Ls} ({int(Ls) * 5} min)", s["E1"]))
        L.append(row(f"E.2 expansion · L={Ls} ({int(Ls) * 5} min)", s["E2"]))
    L += ["\n## Standardised coefficients of the full model fitted on all development data (descriptive; one standard deviation of the feature)\n",
          "| feature | direction (logit) | expansion (OLS, log range) |\n|---|---|---|"]
    for k in CTRLS + FEATS:
        L.append(f"| {k} | {r['coef']['dir'][k]:+.4f} | {r['coef']['exp'][k]:+.4f} |")
    L.append("\n## Not run\n- Holdout (sealed; no outcome computed). Order-flow / volume measures (the data are OHLC only). Completed-swing leg analysis. Other instruments. "
             "Tradeability with stops and costs. A month-matched placebo for the up/down asymmetry.\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def selftest(null_series=10):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    # ---- feature definitions on hand-built windows (L = 8, ATR = 1)
    up = np.arange(9, dtype=float)                                   # perfectly efficient climb
    F = path_features(up[None, :], np.array([1.0]))
    chk("efficient climb: eff=1, retr=0, stall=0, prog=8", (round(F["eff"][0], 9), F["retr"][0], F["stall"][0], F["prog"][0]), (1.0, 0.0, 0.0, 8.0))
    zz = np.array([0, 2, 1, 3, 2, 4, 3, 5, 4], float)                # net 4, path 2+1+2+1+2+1+2+1 = 12, highest at index 7, now 4
    F = path_features(zz[None, :], np.array([1.0]))
    chk("zig-zag: eff = 4/12, retr = 1, stall = 1/8, prog = 4", (round(F["eff"][0], 9), F["retr"][0], round(F["stall"][0], 9), F["prog"][0]), (round(4 / 12, 9), 1.0, 0.125, 4.0))
    F2 = path_features((-zz)[None, :], np.array([1.0]))
    chk("mirror: the negated window gives identical features and the opposite side", (all(abs(F[k][0] - F2[k][0]) < 1e-12 for k in ("eff", "retr", "stall", "acc", "prog", "path", "rvr")), F["sg"][0], F2["sg"][0]), (True, 1.0, -1.0))
    stl = np.array([0, 3, 5, 6, 6.5, 6.2, 6.1, 6.0, 6.05], float)    # extreme at index 4, 4 bars ago
    chk("stall = bars since the extreme / L", round(path_features(stl[None, :], np.array([1.0]))["stall"][0], 9), 0.5)
    acc_up = np.array([0, 0.5, 1, 1.5, 2, 3, 4, 5, 6], float)       # accelerating: last quarter (2 bars) = 2, even quarter of 6 = 1.5
    chk("pace: last-quarter progress minus an even quarter of the total", round(path_features(acc_up[None, :], np.array([1.0]))["acc"][0], 9), round(2 - 6 / 4, 9))
    chk("ATR scales progress, retracement and pace", abs(path_features(zz[None, :], np.array([2.0]))["prog"][0] - 2.0) < 1e-12, True)
    # ---- ridge models recover planted structure
    rr = np.random.default_rng(3)
    Xs = rr.normal(size=(4000, 3))
    ylog = (rr.random(4000) < 1 / (1 + np.exp(-(0.2 + 0.8 * Xs[:, 0] - 0.5 * Xs[:, 1])))).astype(float)
    wl = fit_logit(Xs, ylog)
    chk("logistic ridge recovers the planted coefficients (0.8, -0.5, 0)", (abs(wl[1] - 0.8) < 0.15, abs(wl[2] + 0.5) < 0.15, abs(wl[3]) < 0.12), (True, True, True))
    yo = 1.0 + 2.0 * Xs[:, 0] + rr.normal(size=4000)
    wo = fit_ols(Xs, yo)
    chk("OLS ridge recovers the planted coefficients (1, 2, 0, 0)", (abs(wo[0] - 1) < 0.1, abs(wo[1] - 2) < 0.1, abs(wo[2]) < 0.1), (True, True, True))
    # ---- cross-validation hygiene
    tt = np.arange(2000, dtype=np.int64) * NS_H
    sp = cv_splits(tt)
    chk("8 contiguous folds cover every observation once", (len(sp), sorted(np.concatenate([te for _, te in sp]).tolist()) == list(range(2000))), (K_FOLDS, True))
    chk("purge: no training observation within 24h of the test fold", all((tt[tr] < tt[te].min() - PURGE_NS).any() or True for tr, te in sp) and
        all(not ((tt[tr] >= tt[te].min() - PURGE_NS) & (tt[tr] <= tt[te].max() + PURGE_NS)).any() for tr, te in sp), True)
    chk("purge removes observations on both sides of the fold", all(len(tr) < 2000 - len(te) for tr, te in sp[1:-1]), True)
    # ---- one-sided cluster test: informative feature is detected, useless one is not
    far = pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value
    rng = np.random.default_rng(31)
    df = sb._synth_ohlc(80000, rng)
    s5, s1h = sa._series_from_df(df)
    O = build_obs(s5, s1h, L_PRIMARY, H_BARS, far)
    chk(f"synthetic world yields observations (>= 3000, got {len(O['p'])})", len(O["p"]) >= 3000, True)
    chk("every observation is the last 5m bar of a UTC hour", bool(np.all(((_ns(s5.index)[O["p"]] + NS_5) % NS_H) == 0)), True)
    chk("features are finite and orientation is non-zero", bool(np.all(np.isfinite(O["eff"])) and np.all(np.isfinite(O["acc"])) and np.all(O["sg"] != 0)), True)
    chk("efficiency in (0, 1], retracement >= 0, stall in [0, 1]", (bool(np.all((O["eff"] > 0) & (O["eff"] <= 1 + 1e-12))), bool(np.all(O["retr"] >= -1e-12)), bool(np.all((O["stall"] >= 0) & (O["stall"] <= 1)))), (True, True, True))
    # no look-ahead: change every bar after p0 and compare features of observations up to p0
    p0 = int(O["p"][len(O["p"]) // 2])
    df2 = df.copy()
    ii = df2.index.get_loc(df2.index[p0 + 1])
    for col in ("open", "high", "low", "close"):
        df2.iloc[ii:, df2.columns.get_loc(col)] += 0.3
    s52, s1h2 = sa._series_from_df(df2)
    O2 = build_obs(s52, s1h2, L_PRIMARY, H_BARS, far)
    m1, m2 = O["p"] <= p0 - 24, O2["p"] <= p0 - 24
    same = all(np.allclose(O[k][m1], O2[k][m2], atol=1e-12) for k in ("prog", "logpath", "logrvr", "eff", "retr", "stall", "acc", "a"))
    chk("no look-ahead: features of observations before the change are identical when later bars are altered", (same, int(m1.sum()) > 500), (True, True))
    # mirror of the whole series
    dn = df.copy()
    dn["open"], dn["close"], dn["high"], dn["low"] = -df["open"], -df["close"], -df["low"], -df["high"]
    s5n, s1hn = sa._series_from_df(dn)
    On = build_obs(s5n, s1hn, L_PRIMARY, H_BARS, far)
    mir = (len(On["p"]) == len(O["p"])) and all(np.allclose(O[k], On[k], atol=1e-10) for k in ("prog", "eff", "retr", "stall", "acc")) and np.allclose(O["ydir"], On["ydir"], equal_nan=True)
    chk("mirror: the negated series gives identical features and identical continuation outcomes", mir, True)
    chk("mirror: the side flips", bool(np.all(O["side"] == 1 - On["side"])), True)
    # outcome definitions on a hand-built tape
    # direction: a clean up-move then a hand-made forward path
    nb = 200
    base = np.full(nb, 10.0)
    c5 = base.copy()
    h5, l5 = c5 + 0.01, c5 - 0.01
    pi, ai = 100, 1.0
    h5[103], l5[102] = c5[pi] + 1.2, c5[pi] - 0.4
    o_, st = barrier(h5, l5, pi + 1, c5[pi] + 1.0, c5[pi] - 1.0, H_BARS)
    chk("direction outcome: +1 ATR first -> 1", (o_, st), (1.0, "ok"))
    h5b, l5b = c5 + 0.01, c5 - 0.01
    l5b[101] = c5[pi] - 1.2
    o_, st = barrier(h5b, l5b, pi + 1, c5[pi] + 1.0, c5[pi] - 1.0, H_BARS)
    chk("direction outcome: -1 ATR first -> 0 (reversal for an up observation)", (o_, st), (0.0, "ok"))
    o_, st = barrier(c5 + 0.01, c5 - 0.01, pi + 1, c5[pi] + 1.0, c5[pi] - 1.0, H_BARS)
    chk("direction outcome: neither barrier -> unresolved (excluded, counted)", (np.isnan(o_), st), (True, "unresolved"))
    # region / holdout sealing
    far_ = int(_ns(s5.index)[50000])
    Oh = build_obs(s5, s1h, L_PRIMARY, H_BARS, far_)
    chk("development observations end their outcome window before the boundary", bool(np.all(Oh["t"][Oh["dev"]] + (H_BARS + 1) * NS_5 <= far_)), True)
    chk("no outcome is computed for any holdout or straddling observation", (bool(np.all(np.isnan(Oh["ydir"][~Oh["dev"]]))), bool(np.all(np.isnan(Oh["yexp"][~Oh["dev"]]))), bool(np.all(Oh["code"][~Oh["dev"]] == -1))), (True, True, True))
    chk("holdout observations exist and start at or after the boundary", (bool(Oh["hold"].sum() > 100), bool(np.all(Oh["t"][Oh["hold"]] >= far_))), (True, True))
    chk("the boundary changes the development outcomes only through the sample", bool(np.array_equal(Oh["ydir"][Oh["dev"]], O["ydir"][np.isin(O["t"], Oh["t"][Oh["dev"]])], equal_nan=True)), True)
    # ---- power / null behaviour of the full pipeline
    ps, nul_z = [], []
    for k in range(null_series):
        d_ = sb._synth_ohlc(110000, rng, garch=(k % 2 == 1))
        s5_, s1_ = sa._series_from_df(d_)
        r_ = run_from_series(s5_, s1_, "PRIMARY_READY", far, sens=False)
        for t_ in [r_["E1"], r_["E2"]] + list(r_["S"].values()):
            if t_.get("p1") is not None:
                ps.append(t_["p1"])
                nul_z.append(t_["z"])
    ps = np.asarray(ps)
    chk(f"null: enough tests ({len(ps)})", len(ps) >= 80, True)
    if len(ps) >= 80:
        chk(f"null: share of one-sided p < 0.05 is at most 10% (got {(ps < 0.05).mean():.1%})", (ps < 0.05).mean() <= 0.10, True)
        chk(f"null: share of one-sided p < 0.01 is at most 3% (got {(ps < 0.01).mean():.1%})", (ps < 0.01).mean() <= 0.03, True)
        chk(f"null: mean gain z is not positive (the overfit makes it negative; got {np.mean(nul_z):+.2f})", np.mean(nul_z) < 0.4, True)
    # planted effects: detected by the right test
    pc = power_cell("dir", 0.6, 1, 11)
    chk(f"planted directional drift after at-extreme observations is detected by E.1 (z={pc['mean_z']:+.2f}, lift {pc['realised_lift']:+.3f})", (pc["detect_rate"] == 1.0), True)
    pe = power_cell("exp", 0.5, 1, 12)
    chk(f"planted expansion after stalls is detected by E.2 (z={pe['mean_z']:+.2f}, lift {pe['realised_lift']:+.3f})", (pe["detect_rate"] == 1.0), True)
    rP, _ = run_spec(s5, s1h, L_PRIMARY, far, True)
    rP["n_tests"] = apply_fdr(rP)
    rP["sensitivity"] = {}
    rP.update({"reading": reading(rP), "contract_hash": CONTRACT_HASH, "dataset_status": "PRIMARY_READY"})
    md = render_markdown(dict(rP, sensitivity={str(L): {"L": L, "obs_dev": 0, "E1": rP["E1"], "E2": rP["E2"]} for L in L_SENS}))
    chk("report renders and JSON serialises", (len(md) > 2000, len(json.dumps(rP, default=str)) > 2000), (True, True))
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": null_series, "tests": int(len(ps)), "share_p_lt_0.05": round(float((ps < 0.05).mean()), 3) if len(ps) else None,
                     "share_p_lt_0.01": round(float((ps < 0.01).mean()), 3) if len(ps) else None, "mean_z": round(float(np.mean(nul_z)), 3) if nul_z else None},
            "planted": {"direction": {k: pc[k] for k in ("size", "mean_z", "realised_lift")}, "expansion": {k: pe[k] for k in ("size", "mean_z", "realised_lift")}}}


def main():
    ap = argparse.ArgumentParser(description="Observatory Study E (price progress, path cost and time). Read-only on raw data; the holdout stays sealed.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--counts", action="store_true", help="observation counts only (no outcome is computed)")
    ap.add_argument("--readiness", action="store_true", help="holdout observation counts and power from development estimates; no holdout outcome is read")
    ap.add_argument("--power", action="store_true", help="simulation of detectable effect sizes with the real sampling structure (synthetic data only)")
    ap.add_argument("--power-reps", type=int, default=12)
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    if a.counts:
        print(json.dumps(structure_counts(a.raw_dir, a.source), indent=2, default=str))
        return
    if a.power:
        cells = []
        for kind, sizes in (("dir", (0.0, 0.1, 0.2, 0.3, 0.5)), ("exp", (0.0, 0.1, 0.2, 0.4))):
            for s in sizes:
                cells.append(power_cell(kind, s, a.power_reps, 1000 + int(s * 100)))
                print(json.dumps(cells[-1]), flush=True)
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "studye_power.json"), "w") as f:
            json.dump(cells, f, indent=2)
        return
    if a.readiness:
        print(json.dumps(holdout_readiness(a.raw_dir, a.source), indent=2, default=str))
        return
    res = run_studye(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "studye_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "studye_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
