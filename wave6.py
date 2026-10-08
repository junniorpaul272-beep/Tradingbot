"""
observatory/wave6.py
====================
OBSERVATORY — Wave 6: volatility persistence and market state. ISOLATED from the live bot.
No direction is tested anywhere in this wave.

QUESTION (6A + 6B, primary): after a displacement, are the next bars unusually large compared with ordinary
moments that were equally active BEFORE the event? (If the market was already hot, large bars afterwards prove
nothing about displacement, so the control is matched on pre-event activity.)
Descriptive only (6C, 6D): session activity profile, volatility-state transition matrix, and the split by
pre-event state (quiet / normal / hot = ignition / continuation / exhaustion).

FROZEN CONTRACT (hash printed in every report; frozen 2026-10-07 before any Wave 6 result was seen)
-------------------------------------------------------------------------------------------------
  Timeframes     5m, 15m, 1h, each separately.
  Event          displacement: range / ATR14(prior bars) >= k. PRIMARY k = 1.5 (1.0 and 2.0 descriptive).
                 Episode-first events, K = 12 bars, anchored, per direction (as in Wave 1).
  Control        ordinary bars (range / ATR14(prior) < 1.0), episodes collapsed the same way.
  Baseline       B = mean range of the 100 bars that END 15 bars before the event bar (bars e-114 .. e-15): an
                 OLDER, long yardstick. A 14-bar ATR was rejected as the yardstick: choosing events by their size
                 relative to ATR14 selects moments when ATR14 under-estimates volatility (and choosing ordinary
                 bars selects the opposite), which fakes "persistence" even on constant-volatility random data
                 (measured: +8% unmatched, +1.7% after matching). The older baseline shares no bars with ATR14.
  Outcome        M_h = mean over the next h bars (e+1 .. e+h) of range / B, h in {1,3,6,12,24}.
                 Windows with a data gap are excluded. PRIMARY: h = 6, metric = log(M_6).
  Matching       strata = UTC session (4) x B tercile within the session (3) x pre-event heat tercile (3),
                 where heat = mean range of the 6 bars before the event bar / B. Terciles use development
                 data only. Strata need >= 5 events and >= 5 controls to count.
  PRIMARY test   per timeframe, k = 1.5, h = 6: stratified difference in mean log(M_6), events minus controls
                 (fixed effects by stratum), cluster-robust variance by week (CR1). Relative effect =
                 exp(diff) - 1 (ratio of typical future bar sizes). 3 tests, Benjamini-Hochberg q = 0.10.
                 Practical threshold: a 10% larger typical bar.
  Verdict labels PERSISTENCE (CI above 0, effect >= 10%, survives FDR) / SMALL_PERSISTENCE (CI above 0 but below
                 10%) / NO_INFORMATION (CI inside +-10%) / REVERSAL (CI below 0) / INCONCLUSIVE.
  Descriptive    decay curve over h, arithmetic and median ratios, P(at least one bar >= 1.5 x B in the next
                 h bars), k = 1.0 / 2.0, split by pre-event heat tercile, session profile, transition matrix.
  Split          the frozen absolute holdout date; development events purged; holdout SEALED (this module has
                 no way to open it).
  Deferred       what drives the persistence (session x displacement, FVG, EMA...), duration beyond 24 bars,
                 any use in stops or sizing: separate questions, only if persistence is found and is stable.

Run:  python3 -m observatory.wave6                (real data)
      python3 -m observatory.wave6 --selftest     (clustered-volatility positive control, random-walk null)
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

H_LIST = (1, 3, 6, 12, 24)
H_PRIMARY = 6
K_PRIMARY = 1.5
K_ALL = (1.0, 1.5, 2.0)
MIN_REL_EFFECT = 0.10
FDR_Q = w1.FDR_Q
TFS = ("5m", "15m", "1h")
CONTRACT = {
    "wave": 6, "instrument": "GBPUSD", "timeframes": list(TFS), "k_primary": K_PRIMARY, "k_all": list(K_ALL), "h_list": list(H_LIST),
    "h_primary": H_PRIMARY, "metric": "log mean(range/B) over the next h bars; B = mean range of bars e-114..e-15", "min_relative_effect": MIN_REL_EFFECT,
    "matching": "session x B tercile within session x pre-event heat tercile", "variance": "cluster-robust by week (CR1)",
    "K_episode": w1.K_EPISODE, "fdr_q": FDR_Q, "n_primary_tests": len(TFS), "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
    "purge_bars": max(H_LIST) + 1,
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# features and outcomes
# ---------------------------------------------------------------------------
class Vol:
    def __init__(self, P):
        self.P = P
        s = P.s
        n = P.n
        self.n = n
        rng = s.h - s.l
        self.rng = rng
        self.atr_prior = np.full(n, np.nan)
        self.atr_prior[1:] = s.atr[:-1]                        # used only to DEFINE displacements (as in Wave 1)
        cs = np.concatenate([[0.0], np.cumsum(rng)])
        self.base = np.full(n, np.nan)                         # B: mean range of bars e-114 .. e-15
        e0 = np.arange(115, n)
        self.base[e0] = (cs[e0 - 14] - cs[e0 - 114]) / 100.0
        self.heat = np.full(n, np.nan)                         # mean range of bars e-6..e-1 over B
        i = np.arange(115, n)
        with np.errstate(divide="ignore", invalid="ignore"):
            self.heat[i] = ((cs[i] - cs[i - 6]) / 6.0) / self.base[i]
        gcs = np.concatenate([[0], np.cumsum((s.gap > 0).astype(int))])
        self.M = {}
        self.anybig = {}
        run_max = np.full(n, -np.inf)
        for h in range(1, max(H_LIST) + 1):
            sh = np.full(n, -np.inf)
            sh[: n - h] = rng[h:]
            run_max = np.maximum(run_max, sh)
            if h in H_LIST:
                M = np.full(n, np.nan)
                e = np.arange(0, n - h - 1)
                gapfree = (gcs[e + h + 1] - gcs[e + 1]) == 0
                with np.errstate(divide="ignore", invalid="ignore"):
                    val = ((cs[e + h + 1] - cs[e + 1]) / h) / self.base[e]
                M[e] = np.where(gapfree, val, np.nan)
                self.M[h] = M
                with np.errstate(divide="ignore", invalid="ignore"):
                    big = np.where(np.isfinite(M), (run_max / self.base >= 1.5).astype(float), np.nan)
                self.anybig[h] = big
        self.session = P.session
        dev = np.arange(n) < P.cut
        ok = dev & np.isfinite(self.base) & np.isfinite(self.heat) & (self.base > 0)
        self.atr_edges = {}
        for sess in range(4):
            v = self.base[ok & (self.session == sess)]
            self.atr_edges[sess] = np.quantile(v, [1 / 3, 2 / 3]) if len(v) >= 30 else np.array([np.inf, np.inf])
        self.heat_edges = np.quantile(self.heat[ok], [1 / 3, 2 / 3]) if ok.sum() >= 30 else np.array([np.inf, np.inf])
        self.heat_edges_sess = {}
        for sess in range(4):
            v = self.heat[ok & (self.session == sess)]
            self.heat_edges_sess[sess] = np.quantile(v, [1 / 3, 2 / 3]) if len(v) >= 30 else np.array([np.inf, np.inf])
        self.ok = np.isfinite(self.base) & np.isfinite(self.heat) & (self.base > 0) & np.isfinite(self.atr_prior) & (self.atr_prior > 0)

    def stratum(self, i):
        sess = int(self.session[i])
        return (sess, int(np.digitize(self.base[i], self.atr_edges[sess])), int(np.digitize(self.heat[i], self.heat_edges)))

    def heat_tercile(self, i):
        return int(np.digitize(self.heat[i], self.heat_edges))

    def pick(self, mask, dev=True):
        return self.P.episodes(mask & self.ok & np.isfinite(self.M[H_PRIMARY]), dev)


def _week(P, idx):
    return np.array([int(P.s.index[i].normalize().value // 86_400_000_000_000) // 7 for i in idx])


def stratified_group_diff(y, g, stratum, cluster):
    """
    Fixed-effects (by stratum) difference in the mean of y between group 1 (events) and group 0 (controls),
    CR1 cluster-robust variance. Returns dict(diff, se, ci, z, p, n_events, n_controls, strata_used).
    """
    y = np.asarray(y, float)
    g = np.asarray(g, float)
    st = list(stratum)
    cl = np.asarray(cluster)
    keys = {}
    for i, k in enumerate(st):
        keys.setdefault(k, []).append(i)
    keep = np.zeros(len(y), bool)
    used = 0
    for k, ix in keys.items():
        ix = np.array(ix)
        if (g[ix] == 1).sum() >= 5 and (g[ix] == 0).sum() >= 5:
            keep[ix] = True
            used += 1
    if used == 0:
        return {"diff": None, "se": None, "ci": None, "z": None, "p": None, "n_events": 0, "n_controls": 0, "strata_used": 0}
    gt = np.empty(keep.sum())
    yt = np.empty(keep.sum())
    pos = 0
    for k, ix in keys.items():
        ix = np.array(ix)
        if not keep[ix[0]]:
            continue
        gt[pos:pos + len(ix)] = g[ix] - g[ix].mean()
        yt[pos:pos + len(ix)] = y[ix] - y[ix].mean()
        pos += len(ix)
    # cluster labels must follow the same ordering as gt / yt
    order = np.concatenate([np.array(ix) for k, ix in keys.items() if keep[np.array(ix)[0]]])
    cl_o = cl[order]
    sxx = float((gt ** 2).sum())
    beta = float((gt * yt).sum() / sxx)
    e = yt - beta * gt
    uniq, inv = np.unique(cl_o, return_inverse=True)
    G = len(uniq)
    u = np.bincount(inv, weights=gt * e, minlength=G)
    var = (G / max(G - 1, 1)) * float((u ** 2).sum()) / (sxx ** 2)
    se = math.sqrt(var) if var > 0 else float("nan")
    z = beta / se if se and se > 0 else 0.0
    return {"diff": beta, "se": se, "ci": (beta - 1.96 * se, beta + 1.96 * se), "z": z, "p": w1.norm_p(z),
            "n_events": int((g[keep] == 1).sum()), "n_controls": int((g[keep] == 0).sum()), "strata_used": used}


def _collect(V, ev_idx, ct_idx, h, key="M", log=True):
    idx = np.concatenate([ev_idx, ct_idx]).astype(int)
    g = np.concatenate([np.ones(len(ev_idx)), np.zeros(len(ct_idx))])
    arr = V.M[h] if key == "M" else V.anybig[h]
    y = arr[idx]
    keep = np.isfinite(y) & (y > 0 if log else True)
    idx, g, y = idx[keep], g[keep], y[keep]
    if log:
        y = np.log(y)
    return idx, g, y, [V.stratum(i) for i in idx], _week(V.P, idx)


def rel(diff):
    return None if diff is None else math.exp(diff) - 1.0


def run_tf(V, k=K_PRIMARY, dev=True):
    P = V.P
    ev_idx = V.pick(P.ratio >= k, dev)
    ct_idx = V.pick(P.ratio < 1.0, dev)
    out = {"tf": P.tf, "k": k, "episodes_events": int(len(ev_idx)), "episodes_controls": int(len(ct_idx))}
    idx, g, y, st, wk = _collect(V, ev_idx, ct_idx, H_PRIMARY)
    prim = stratified_group_diff(y, g, st, wk)
    if prim["diff"] is not None:
        prim["relative_effect"] = rel(prim["diff"])
        prim["relative_ci"] = (rel(prim["ci"][0]), rel(prim["ci"][1]))
    out["primary_h6"] = prim
    out["n_min"] = min(prim["n_events"], prim["n_controls"])
    out["sample_label"] = w1.sample_label(out["n_min"])
    # decay curve (descriptive): relative effect at each horizon + simple ratios
    decay = {}
    for h in H_LIST:
        ev_h = ev_idx[np.isfinite(V.M[h][ev_idx])]
        ct_h = ct_idx[np.isfinite(V.M[h][ct_idx])]
        _, g2, y2, st2, wk2 = _collect(V, ev_h, ct_h, h)
        d = stratified_group_diff(y2, g2, st2, wk2)
        raw_e, raw_c = V.M[h][ev_h], V.M[h][ct_h]
        pe, pc = V.anybig[h][ev_h], V.anybig[h][ct_h]
        decay[str(h)] = {"relative_effect": rel(d["diff"]), "ci": None if d["diff"] is None else (rel(d["ci"][0]), rel(d["ci"][1])),
                         "mean_ratio_raw": float(raw_e.mean() / raw_c.mean()) if len(raw_c) and raw_c.mean() > 0 else None,
                         "median_events": float(np.median(raw_e)) if len(raw_e) else None, "median_controls": float(np.median(raw_c)) if len(raw_c) else None,
                         "p_any_big_events": float(np.nanmean(pe)) if len(pe) else None, "p_any_big_controls": float(np.nanmean(pc)) if len(pc) else None}
    out["decay"] = decay
    # split by pre-event heat (ignition / continuation / exhaustion), descriptive
    by_heat = {}
    for t, name in ((0, "quiet_before"), (1, "normal_before"), (2, "hot_before")):
        e_t = np.array([i for i in ev_idx if V.heat_tercile(i) == t], int)
        c_t = np.array([i for i in ct_idx if V.heat_tercile(i) == t], int)
        _, g3, y3, st3, wk3 = _collect(V, e_t, c_t, H_PRIMARY)
        d = stratified_group_diff(y3, g3, st3, wk3)
        by_heat[name] = {"events": int(len(e_t)), "relative_effect": rel(d["diff"]),
                         "ci": None if d["diff"] is None else (rel(d["ci"][0]), rel(d["ci"][1]))}
    out["by_pre_event_heat"] = by_heat
    return out


def run_k_descriptive(V):
    res = {}
    for k in K_ALL:
        r = run_tf(V, k)
        res[str(k)] = {"episodes_events": r["episodes_events"], "relative_effect_h6": r["primary_h6"].get("relative_effect"),
                       "ci": r["primary_h6"].get("relative_ci")}
    return res


# ---------------------------------------------------------------------------
# descriptive: session profile and state transitions (development data only)
# ---------------------------------------------------------------------------
def session_profile(V):
    P = V.P
    dev = np.arange(V.n) < P.cut
    out = {}
    names = ("ASIA 00-07", "LONDON 07-13", "NY 13-21", "LATE 21-24")
    ev_all = P.ratio >= K_PRIMARY
    for sess in range(4):
        m = dev & (V.session == sess) & V.ok
        if m.sum() < 30:
            continue
        pips = V.rng[m] * 1e4
        out[names[sess]] = {"bars": int(m.sum()), "median_range_pips": round(float(np.median(pips)), 2),
                            "median_range_over_B": round(float(np.median(V.rng[m] / V.base[m])), 3),
                            "displacement_rate_k1.5": round(float((ev_all & m).sum() / m.sum()), 4)}
    return out


def transition_matrix(V, step=6):
    """
    State = tercile of the heat of the last `step` bars, taken WITHIN the UTC session (so the usual day/night rhythm is
    removed), development data only; transitions between non-overlapping blocks of `step` bars.
    """
    P = V.P
    dev_end = P.cut - step
    idx = np.arange(115 + step, dev_end, step)
    idx = idx[V.ok[idx]]
    nxt = idx + step
    ok = np.isfinite(V.heat[nxt]) & V.ok[np.clip(nxt, 0, V.n - 1)]
    idx, nxt = idx[ok], nxt[ok]

    def state(i):
        return int(np.digitize(V.heat[i], V.heat_edges_sess[int(V.session[i])]))
    m = np.zeros((3, 3), int)
    for a, b in zip(idx, nxt):
        m[state(a), state(b)] += 1
    rows = {}
    for a, name in enumerate(("quiet", "normal", "hot")):
        tot = m[a].sum()
        rows[name] = {"n": int(tot), "to_quiet": round(m[a, 0] / tot, 3) if tot else None, "to_normal": round(m[a, 1] / tot, 3) if tot else None,
                      "to_hot": round(m[a, 2] / tot, 3) if tot else None}
    return {"step_bars": step, "matrix": rows, "note": "terciles within session; 1/3 each = no memory"}


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def verdict(prim, n_min, reject):
    if prim["diff"] is None or n_min < 30:
        return "DATA-LIMITED"
    lo, hi = prim["relative_ci"]
    if lo > 0:
        if prim["relative_effect"] >= MIN_REL_EFFECT and reject:
            return "PERSISTENCE (FDR-surviving, >= 10%)"
        if prim["relative_effect"] >= MIN_REL_EFFECT:
            return "PERSISTENCE_NOT_FDR_SURVIVING"
        return "SMALL_PERSISTENCE" + (" (FDR-surviving)" if reject else "")
    if hi < 0:
        return "REVERSAL (CI below 0)"
    if lo > -MIN_REL_EFFECT and hi < MIN_REL_EFFECT:
        return "NO_INFORMATION"
    return "INCONCLUSIVE"


def run_wave6(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    clean = {tf: (preloaded[tf] if preloaded else rc.clean_timeframe(tf, raw_dir, source)[0]) for tf in TFS}
    s5 = rm.Series(clean["5m"], 5)
    P = {tf: w1.Prepared(tf, clean[tf], s_lo=s5 if tf != "5m" else None) for tf in TFS}
    rep15 = rc.clean_timeframe("15m", raw_dir, source)[2] if not preloaded else {
        "first": None, "last": None, "raw_lines_parsed": 0, "unique_timestamps": 0, "duplicate_lines": 0, "conflicting_duplicates": 0,
        "dropped_market_closed": 0, "dropped_off_grid": 0, "dropped_invalid_ohlc": 0, "kept_bars": len(clean["15m"]),
        "unexplained_gaps": 0, "missing_open_bars": 0}
    a6 = rg.gate_a6({"15m": clean["15m"]}, {"15m": rep15}, {"15m": P["15m"].s})
    status = rg.gate_a7({"15m": clean["15m"]}, {"15m": P["15m"].s}, a6)["details"]["status"]
    V = {tf: Vol(P[tf]) for tf in TFS}
    res = {tf: run_tf(V[tf]) for tf in TFS}
    tests = [(tf, res[tf]["primary_h6"]["p"]) for tf in TFS if res[tf]["primary_h6"]["p"] is not None]
    reject, qv = w1.bh_fdr([p for _, p in tests])
    fdr = {t: (rj, q) for (t, _), rj, q in zip(tests, reject, qv)}
    for tf in TFS:
        rj, q = fdr.get(tf, (False, None))
        res[tf]["fdr_reject"], res[tf]["q_value"] = rj, q
        res[tf]["verdict"] = verdict(res[tf]["primary_h6"], res[tf]["n_min"], rj)
        res[tf]["k_descriptive"] = run_k_descriptive(V[tf])
        res[tf]["session_profile"] = session_profile(V[tf])
        res[tf]["transition_matrix"] = transition_matrix(V[tf])
    return {"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False,
            "n_primary_tests": len(tests), **res}


def _p(x):
    return "n/a" if x is None else f"{100 * x:+.1f}%"


def render_markdown(r):
    L = ["# Observatory — Wave 6 report (volatility persistence and market state)\n",
         f"Contract hash `{r['contract_hash']}` · dataset status **{r['dataset_status']}** · holdout opened: **False** · primary tests: {r['n_primary_tests']}\n",
         "**No direction is tested here. Development data only. Nothing is a trading rule.**\n",
         "Outcome: size of the next h bars relative to the market's older typical bar size (100 bars ending 15 bars before the event) (typical bar = geometric mean). "
         "\"+x%\" means bars after a displacement are x% larger than after an equally active ordinary moment (matched on session, "
         "long-run activity level and the heat of the 6 bars before). Practical threshold: +10%.\n"]
    for tf in TFS:
        t = r[tf]
        p = t["primary_h6"]
        L.append(f"## {tf}\n")
        if p["diff"] is None:
            L.append("- DATA-LIMITED\n")
            continue
        L.append(f"- episodes: displacements {t['episodes_events']}, controls {t['episodes_controls']} · strata used {p['strata_used']} · sample: {t['sample_label']}")
        L.append(f"- **primary (k ≥ 1.5, next 6 bars): {_p(p['relative_effect'])}** (CI {_p(p['relative_ci'][0])} to {_p(p['relative_ci'][1])}) · z={p['z']:.2f} · p={p['p']:.4f} · q={t['q_value']:.4f} · **{t['verdict']}**")
        L.append("- decay curve (relative size of the next h bars vs matched controls):")
        for h, d in t["decay"].items():
            ci = d["ci"]
            L.append(f"  - h={h}: {_p(d['relative_effect'])}" + ("" if not ci or ci[0] is None else f" (CI {_p(ci[0])} to {_p(ci[1])})") +
                     f" · raw ratio {d['mean_ratio_raw']:.2f} · P(≥1 bar ≥ 1.5×B) {d['p_any_big_events']:.2f} vs {d['p_any_big_controls']:.2f}")
        L.append("- by pre-event heat (quiet / normal / hot before the displacement): " + " · ".join(
            f"{k_}: {_p(v['relative_effect'])} (n={v['events']})" for k_, v in t["by_pre_event_heat"].items()))
        L.append("- other thresholds (h=6): " + " · ".join(f"k≥{k_}: {_p(v['relative_effect_h6'])} (n={v['episodes_events']})" for k_, v in t["k_descriptive"].items()))
        L.append("- session profile (development): " + " · ".join(
            f"{n_}: median {v['median_range_pips']} pips, displacement rate {v['displacement_rate_k1.5']:.1%}" for n_, v in t["session_profile"].items()))
        tm = t["transition_matrix"]["matrix"]
        L.append(f"- state transitions every 6 bars (terciles within session, so the day/night rhythm is removed; 1/3 each = no memory): " + " · ".join(
            f"{a}→ quiet {v['to_quiet']}, normal {v['to_normal']}, hot {v['to_hot']}" for a, v in tm.items()))
        L.append("")
    L.append("## Not run in this wave\n- Drivers of persistence (session × displacement, FVG, EMA), duration beyond 24 bars, use in stops/sizing.\n- Holdout: sealed.\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test: positive control (clustered volatility) and null (constant volatility)
# ---------------------------------------------------------------------------
def _garch_series(n, rng, step=5, alpha=0.10, beta=0.88):
    from . import gates as g
    sub = rng.normal(0, 1, size=(n, 4))
    o = np.empty(n)
    c = np.empty(n)
    h = np.empty(n)
    l = np.empty(n)
    var = 1.0
    prev = 1.30
    base = 0.0002
    omega = 1.0 - alpha - beta
    for i in range(n):
        sig = base * math.sqrt(var)
        path = prev + np.cumsum(sub[i] * sig)
        o[i], c[i] = prev, path[-1]
        h[i], l[i] = max(prev, path.max()), min(prev, path.min())
        r2 = ((path[-1] - prev) / base) ** 2 / 4.0
        var = omega + alpha * r2 + beta * var
        prev = c[i]
    t0 = pd.Timestamp("2026-01-05 00:00", tz="UTC")
    df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c},
                      index=pd.DatetimeIndex([t0 + pd.Timedelta(minutes=step * i) for i in range(n)]))
    df["gap_before_missing"], df["closure_before"] = 0, False
    return df


def selftest(null_series=14, pos_series=8):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    from . import gates as g
    rng = np.random.default_rng(404)
    # features use the past only
    d = g._random_walk_series(8000, rng, step=5).df
    P_full = w1.Prepared("5m", d)
    V_full = Vol(P_full)
    P_cut = w1.Prepared("5m", d.iloc[:5001])
    V_cut = Vol(P_cut)
    same = np.allclose(V_full.atr_prior[:5000], V_cut.atr_prior[:5000], equal_nan=True) and np.allclose(V_full.heat[:5000], V_cut.heat[:5000], equal_nan=True)
    chk("ATR_prior and heat use the past only", bool(same), True)
    # outcome windows: hand check of M_h and B on a hand-built series
    bars = [(100, 101, 99, 100)] * 140 + [(100, 104, 98, 103)] + [(103, 104, 101, 102), (102, 104, 100, 101), (101, 103, 99, 100)] + [(100, 101, 99, 100)] * 40
    t0 = pd.Timestamp("2026-03-10 00:00", tz="UTC")
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex([t0 + pd.Timedelta(minutes=5 * i) for i in range(len(bars))]))
    df["gap_before_missing"], df["closure_before"] = 0, False
    Vh = Vol(w1.Prepared("5m", df))
    i = 140
    chk("B = mean range of bars e-114..e-15 (flat series of range 2)", bool(np.isclose(Vh.base[i], 2.0)), True)
    chk("M_3 = mean range of the next 3 bars / B", bool(np.isclose(Vh.M[3][i], ((104 - 101) + (104 - 100) + (103 - 99)) / 3 / 2.0)), True)
    chk("the event bar and the 14 bars before it are NOT in B", bool(np.isclose(_with_spike(df, 130).base[i], 2.0)), True)
    chk("window with a data gap is excluded", bool(np.isnan(_with_gap(df, 142).M[3][140])), True)
    # fixed-effects difference recovers a planted effect and ignores a stratum-only difference
    rr = np.random.default_rng(9)
    ns = 3000
    stratum = rr.integers(0, 4, ns)
    gg = (rr.random(ns) < 0.3).astype(float)
    y = 0.5 * stratum + 0.2 * gg + rr.normal(0, 1, ns)
    wk = rr.integers(0, 100, ns)
    r = stratified_group_diff(y, gg, [(int(s),) for s in stratum], wk)
    chk("planted +0.2 recovered (strata absorb the stratum effect)", bool(abs(r["diff"] - 0.2) < 0.08 and r["z"] > 3), True)
    y0 = 0.5 * stratum + rr.normal(0, 1, ns)
    chk("no effect found when none is planted", abs(stratified_group_diff(y0, gg, [(int(s),) for s in stratum], wk)["z"]) < 3, True)
    # null: constant-volatility random walk shows no displacement persistence
    fp, used, rels = 0, 0, []
    for _ in range(null_series):
        dd = g._random_walk_series(45000, rng, step=5).df
        r_ = run_tf(Vol(w1.Prepared("5m", dd)))
        p_ = r_["primary_h6"]
        if p_["diff"] is None:
            continue
        used += 1
        rels.append(p_["relative_effect"])
        fp += 1 if p_["p"] < 0.05 else 0
    chk("null series usable", used >= 10, True)
    chk("null false-positive rate <= 20%", used > 0 and fp / used <= 0.20, True)
    chk("null relative effect near 0 (|mean| < 2%)", abs(float(np.mean(rels))) < 0.02, True)
    # positive control: clustered volatility must show persistence beyond the matched controls
    hits, effs = 0, []
    for _ in range(pos_series):
        dd = _garch_series(45000, rng)
        r_ = run_tf(Vol(w1.Prepared("5m", dd)))
        p_ = r_["primary_h6"]
        if p_["diff"] is None:
            continue
        effs.append(p_["relative_effect"])
        hits += 1 if (p_["relative_ci"][0] > 0) else 0
    chk("positive control: clustered volatility detected in most series", len(effs) >= 6 and hits / len(effs) >= 0.6, True)
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": used, "false_positive_rate": round(fp / used, 3) if used else None, "mean_relative_effect": round(float(np.mean(rels)), 4) if rels else None},
            "positive_control": {"series": len(effs), "detected": hits, "mean_relative_effect": round(float(np.mean(effs)), 3) if effs else None}}


def _with_spike(df, pos):
    d = df.copy()
    d.iloc[pos, d.columns.get_loc("high")] = 150.0
    return Vol(w1.Prepared("5m", d))


def _with_gap(df, pos):
    d = df.copy()
    d.iloc[pos, d.columns.get_loc("gap_before_missing")] = 2
    return Vol(w1.Prepared("5m", d))


def main():
    ap = argparse.ArgumentParser(description="Observatory Wave 6 (volatility persistence). Read-only on raw data.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    res = run_wave6(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "wave6_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "wave6_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
