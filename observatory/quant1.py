"""
observatory/quant1.py
=====================
OBSERVATORY — Quantitative Market Behaviour programme, Phases 1 and 2. ISOLATED from the live bot.

PHASE 1  Reconstruct Bachelier's (1900) arithmetic Brownian motion  P_t = P_0 + mu t + sigma W_t  and PROVE the
         simulator reproduces the mathematics before it is used on market data (a pass/fail gate, Q1-GATE).
PHASE 2  Reality check on GBPUSD (development data only, holdout sealed):
           2.1 distribution of price changes at 8 horizons
           2.2 the square-root-of-time rule (non-overlapping ratio, Lo-MacKinlay variance ratios, by session/quarter)
           2.3 independence of increments (ACF of r, |r|, r^2, Ljung-Box, runs test, volatility persistence)
           2.4 tail frequencies against the normal model, with Wilson intervals

No direction is predicted and no trade is simulated here. Nothing is a trading rule.

FROZEN CONTRACT (hash printed in every report; frozen 2026-10-07 before any Phase 2 number was seen)
-------------------------------------------------------------------------------------------------
  Data           clean 5m GBPUSD closes (observatory.clean). Changes in PIPS (x 1e4). 15m / 1h are NOT re-derived;
                 a consistency check compares the 1h closes with the 5m closes at the same instants.
  Horizons       1, 3, 6, 12, 24, 48, 96, 288 five-minute bars = 5m, 15m, 30m, 1h, 2h, 4h, 8h, 1 day (24 h of
                 OPEN-market time: windows that cross a weekend closure or a data gap are dropped).
  Samples        NON-OVERLAPPING changes (start index a multiple of the horizon) for distribution statistics and
                 the scaling ratio; overlapping windows are used only inside the variance-ratio test, whose
                 heteroskedasticity-robust z (Lo and MacKinlay 1988) accounts for the overlap.
  Uncertainty    day-block bootstrap (resample UTC dates with replacement, 400 resamples, fixed seed) for the
                 standard deviation and the scaling ratio; Wilson intervals for tail frequencies.
  Split          the frozen absolute holdout date (clean.HOLDOUT_START_UTC): only windows that END before it are
                 used. The holdout is not read.
  Multiple tests every reported p-value is listed in the test registry at the end of the report; the variance-ratio
                 family (6 horizons) is corrected with Benjamini-Hochberg q = 0.10.
  Labels         a result with fewer than 30 independent observations is INSUFFICIENT; a tail frequency whose
                 expected count under the model is below 5 is INCONCLUSIVE.

Run:  python3 -m observatory.quant1                (Phase 1 gate + Phase 2 on the real data)
      python3 -m observatory.quant1 --selftest     (statistics machinery against known synthetic processes)
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

HORIZONS = {"5m": 1, "15m": 3, "30m": 6, "1h": 12, "2h": 24, "4h": 48, "8h": 96, "1day": 288}
VR_Q = (3, 6, 12, 24, 48, 96)
TAIL_K = (1, 2, 3, 4)
ACF_LAGS = (1, 2, 3, 4, 5, 6, 12, 24, 48, 288)
BOOT = 400
BOOT_SEED = 20261007
FDR_Q = 0.10
CONTRACT = {
    "programme": "quantitative-1", "instrument": "GBPUSD", "horizons_bars_5m": HORIZONS, "vr_q": list(VR_Q), "tail_k": list(TAIL_K),
    "acf_lags": list(ACF_LAGS), "bootstrap": {"resamples": BOOT, "seed": BOOT_SEED, "block": "UTC date"}, "fdr_q": FDR_Q,
    "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]
_PHI = lambda x: 0.5 * math.erfc(-x / math.sqrt(2))


# ===========================================================================
# PHASE 1 — Bachelier simulation and its proof
# ===========================================================================
def sim_stats(mu, sigma, T, steps, n_paths, a, b, seed, chunk=2000, probe_fracs=(0.25, 0.5, 1.0), mfe_level=None):
    """
    Simulate arithmetic Brownian motion P_t - P_0 = mu t + sigma W_t on a grid of `steps` steps over horizon T.
    Returns finals, first-passage outcome counts for barriers +a / -b, and path summaries. Fixed seed.
    """
    rng = np.random.default_rng(seed)
    dt = T / steps
    probe_idx = [int(round(f * steps)) for f in probe_fracs]
    finals, probes = [], {f: [] for f in probe_fracs}
    up_first = dn_first = neither = 0
    exit_steps = []
    max_ge = 0
    done = 0
    while done < n_paths:
        m = min(chunk, n_paths - done)
        inc = mu * dt + sigma * math.sqrt(dt) * rng.standard_normal((m, steps))
        path = np.cumsum(inc, axis=1)
        finals.append(path[:, -1].copy())
        for f, k in zip(probe_fracs, probe_idx):
            probes[f].append(path[:, k - 1].copy())
        hit_up = path >= a
        hit_dn = path <= -b
        iu = np.where(hit_up.any(axis=1), hit_up.argmax(axis=1), steps + 1)
        idn = np.where(hit_dn.any(axis=1), hit_dn.argmax(axis=1), steps + 1)
        up_first += int(((iu < idn) & (iu <= steps)).sum())
        dn_first += int(((idn < iu) & (idn <= steps)).sum())
        neither += int(((iu > steps) & (idn > steps)).sum())
        exit_steps.append(np.minimum(iu, idn)[np.minimum(iu, idn) <= steps])
        if mfe_level is not None:
            max_ge += int((path.max(axis=1) >= mfe_level).sum())
        done += m
    return {"finals": np.concatenate(finals), "probes": {f: np.concatenate(v) for f, v in probes.items()},
            "up_first": up_first, "dn_first": dn_first, "neither": neither, "n": n_paths, "dt": dt,
            "mean_exit_time": float(np.concatenate(exit_steps).mean() * dt) if exit_steps and sum(len(e) for e in exit_steps) else None,
            "mfe_ge": max_ge}


def theory_first_passage(mu, sigma, a, b):
    """P(hit +a before -b), infinite horizon, arithmetic Brownian motion started at 0 (scale-function result)."""
    if abs(mu) < 1e-14:
        return b / (a + b)
    s = lambda x: math.exp(-2.0 * mu * x / sigma ** 2)
    return (s(0.0) - s(-b)) / (s(a) - s(-b))


def bachelier_gate(seed=7, n_paths=20000, steps=2000):
    """
    Q1-GATE: every check compares the simulation with an exact result. Barriers are moved outwards by the
    discrete-monitoring correction 0.5826 sigma sqrt(dt) (Broadie-Glasserman-Kou) before comparing with the continuous theory.
    """
    checks = []

    def add(name, observed, expected, tol, kind="abs"):
        err = abs(observed - expected)
        checks.append({"check": name, "observed": round(float(observed), 5), "expected": round(float(expected), 5), "tolerance": round(float(tol), 5), "pass": bool(err <= tol)})
    # --- zero drift
    mu, sigma, T = 0.0, 10.0, 1.0
    a = b = 1.0 * sigma * math.sqrt(T)
    sim = sim_stats(mu, sigma, T, steps, n_paths, a, b, seed, mfe_level=sigma * math.sqrt(T))
    fin = sim["finals"]
    sd_T = sigma * math.sqrt(T)
    se_mean = sd_T / math.sqrt(n_paths)
    add("zero drift: mean of final change = 0", fin.mean(), 0.0, 4 * se_mean)
    add("zero drift: sd of final change = sigma*sqrt(T)", fin.std(), sd_T, 4 * sd_T / math.sqrt(2 * n_paths))
    for k, p in ((1, 0.682689), (2, 0.954500), (3, 0.997300)):
        cover = float((np.abs(fin) <= k * sd_T).mean())
        add(f"zero drift: share within {k} sd = {p:.4f}", cover, p, 4 * math.sqrt(p * (1 - p) / n_paths))
    add("zero drift: P(final > 0) = 0.5", float((fin > 0).mean()), 0.5, 4 * math.sqrt(0.25 / n_paths))
    # square-root-of-time inside the simulation
    for f in (0.25, 0.5):
        add(f"sqrt-time: sd at {f}T = sigma*sqrt({f}T)", sim["probes"][f].std(), sigma * math.sqrt(f * T), 4 * sigma * math.sqrt(f * T) / math.sqrt(2 * n_paths))
    # reflection principle: P(max >= m) = 2 (1 - Phi(m / (sigma sqrt T))) with the discrete-monitoring shift
    m_lvl = sigma * math.sqrt(T)
    shift = 0.5826 * sigma * math.sqrt(sim["dt"])
    p_theory = 2.0 * (1.0 - _PHI((m_lvl + shift) / sd_T))
    add("reflection principle: P(max >= 1 sd)", sim["mfe_ge"] / n_paths, p_theory, 4 * math.sqrt(p_theory * (1 - p_theory) / n_paths))
    # first passage, symmetric barriers, zero drift (horizon long enough that 'neither' is negligible)
    big_T = 400.0
    sim2 = sim_stats(0.0, sigma, big_T, steps * 2, 6000, 10.0, 10.0, seed + 1)
    shift2 = 0.5826 * sigma * math.sqrt(sim2["dt"])
    a2 = 10.0 + shift2
    exp_up = a2 / (2 * a2)
    resolved = sim2["up_first"] + sim2["dn_first"]
    add("first passage (mu=0, +10 vs -10): P(up first) = 0.5", sim2["up_first"] / max(resolved, 1), exp_up, 4 * math.sqrt(0.25 / max(resolved, 1)))
    # asymmetric barriers (gambler's ruin): P(+a before -b) = b/(a+b)
    sim3 = sim_stats(0.0, sigma, big_T, steps * 2, 6000, 10.0, 20.0, seed + 2)
    shift3 = 0.5826 * sigma * math.sqrt(sim3["dt"])
    exp_ab = theory_first_passage(0.0, sigma, 10.0 + shift3, 20.0 + shift3)
    r3 = sim3["up_first"] + sim3["dn_first"]
    add("first passage (mu=0, +10 vs -20): P(up first) = b/(a+b)", sim3["up_first"] / max(r3, 1), exp_ab, 4 * math.sqrt(exp_ab * (1 - exp_ab) / max(r3, 1)))
    # drift: scale-function formula
    mu4 = 0.25
    sim4 = sim_stats(mu4, sigma, 3000.0, steps * 2, 6000, 10.0, 10.0, seed + 3)
    shift4 = 0.5826 * sigma * math.sqrt(sim4["dt"])
    exp4 = theory_first_passage(mu4, sigma, 10.0 + shift4, 10.0 + shift4)
    r4 = sim4["up_first"] + sim4["dn_first"]
    add("first passage with drift mu=0.25: P(up first), scale-function formula", sim4["up_first"] / max(r4, 1), exp4, 4 * math.sqrt(exp4 * (1 - exp4) / max(r4, 1)))
    # drift moves the mean of the final change
    sim5 = sim_stats(0.3, sigma, 1.0, steps, 20000, 5.0, 5.0, seed + 4)
    add("drift mu=0.3: mean of final change = mu*T", sim5["finals"].mean(), 0.3, 4 * sd_T / math.sqrt(20000))
    # reproducibility
    s_a = sim_stats(0.0, sigma, 1.0, 400, 3000, 5.0, 5.0, 99)
    s_b = sim_stats(0.0, sigma, 1.0, 400, 3000, 5.0, 5.0, 99)
    s_c = sim_stats(0.0, sigma, 1.0, 400, 3000, 5.0, 5.0, 100)
    checks.append({"check": "same seed reproduces the identical simulation; a different seed does not", "observed": 1.0, "expected": 1.0, "tolerance": 0.0,
                   "pass": bool(np.array_equal(s_a["finals"], s_b["finals"]) and not np.array_equal(s_a["finals"], s_c["finals"]))})
    return {"passed": all(c["pass"] for c in checks), "n_checks": len(checks), "checks": checks,
            "note": "barrier theory uses the 0.5826 sigma sqrt(dt) discrete-monitoring correction; continuous-time exact results"}


# ===========================================================================
# PHASE 2 — data access
# ===========================================================================
class Q5:
    """5-minute closes in pips with the contiguity flags needed to build valid windows (development part only)."""

    def __init__(self, clean5, step_min=5):
        self.index = clean5.index
        self.close = clean5["close"].to_numpy(float) * 1e4
        self.open = clean5["open"].to_numpy(float) * 1e4
        self.high = clean5["high"].to_numpy(float) * 1e4
        self.low = clean5["low"].to_numpy(float) * 1e4
        gap = clean5["gap_before_missing"].to_numpy() > 0 if "gap_before_missing" in clean5 else np.zeros(len(clean5), bool)
        clo = clean5["closure_before"].to_numpy().astype(bool) if "closure_before" in clean5 else np.zeros(len(clean5), bool)
        self.bad = gap | clo                                   # bad[i]: a break exists between bar i-1 and bar i
        self.cs_bad = np.concatenate([[0], np.cumsum(self.bad.astype(int))])
        self.n = len(clean5)
        self.hold = int(self.index.searchsorted(rc.HOLDOUT_START_UTC, side="left"))
        self.days = np.asarray((self.index.normalize() - pd.Timestamp("1970-01-01", tz="UTC")).days)
        sess_edges = (7, 13, 21)
        self.session = np.digitize(np.asarray(self.index.hour), sess_edges)
        self.quarter = np.asarray(self.index.tz_convert("UTC").tz_localize(None).to_period("Q").astype(str))

    def window_ok(self, h):
        """ok[i] True when bars i+1..i+h are contiguous and the whole window ends before the holdout."""
        i = np.arange(self.n - h)
        ok = (self.cs_bad[i + h + 1] - self.cs_bad[i + 1]) == 0
        ok &= (i + h) < self.hold
        return i, ok

    def changes(self, h, non_overlapping=True):
        i, ok = self.window_ok(h)
        start = i[ok]
        if non_overlapping:
            start = start[start % h == 0]
        d = self.close[start + h] - self.close[start]
        return d, start


def moments(d):
    n = len(d)
    if n < 4:
        return {"n": n}
    mu, sd = float(d.mean()), float(d.std(ddof=1))
    z = (d - mu) / sd if sd > 0 else d * 0
    return {"n": n, "mean": mu, "sd": sd, "median_abs": float(np.median(np.abs(d))), "skew": float((z ** 3).mean()),
            "excess_kurtosis": float((z ** 4).mean() - 3.0), "pct_positive": float((d > 0).mean()), "pct_negative": float((d < 0).mean())}


def tail_table(d, sd=None):
    sd = float(d.std(ddof=1)) if sd is None else sd
    out = {}
    m = float(d.mean())
    for k in TAIL_K:
        p_norm = math.erfc(k / math.sqrt(2))
        obs = int((np.abs(d - m) > k * sd).sum())
        n = len(d)
        lo, hi = w1.wilson(obs, n)
        exp_count = n * p_norm
        out[str(k)] = {"observed": obs, "share": obs / n if n else None, "ci": (lo, hi), "normal_share": p_norm, "expected_count": exp_count,
                       "ratio": (obs / n) / p_norm if n else None,
                       "label": "INCONCLUSIVE (expected count < 5)" if exp_count < 5 else ("EXCEEDS_NORMAL" if lo > p_norm else ("BELOW_NORMAL" if hi < p_norm else "CONSISTENT"))}
    return out


def boot_sd(d, days, rng, B=BOOT):
    """Day-block bootstrap of the standard deviation. Returns (lo, hi)."""
    u = np.unique(days)
    if len(u) < 10 or len(d) < 30:
        return (None, None)
    groups = [d[days == x] for x in u]
    sds = []
    for _ in range(B):
        pick = rng.integers(0, len(u), len(u))
        v = np.concatenate([groups[j] for j in pick])
        sds.append(v.std(ddof=1))
    return (float(np.percentile(sds, 2.5)), float(np.percentile(sds, 97.5)))


# ===========================================================================
# 2.1 / 2.2 / 2.4
# ===========================================================================
def exp_distributions(Q, rng):
    sd1 = float(np.diff(Q.close)[(~Q.bad[1:]) & (np.arange(1, Q.n) < Q.hold)].std(ddof=1))
    out = {"sd_5m_bar_pips": sd1, "horizons": {}}
    for name, h in HORIZONS.items():
        d, start = Q.changes(h, True)
        m = moments(d)
        row = {"bars": h, **m}
        if m["n"] >= 30:
            day = Q.days[start]
            lo, hi = boot_sd(d, day, rng)
            pred = sd1 * math.sqrt(h)
            row.update({"sd_ci": (lo, hi), "sqrt_time_prediction": pred, "ratio_actual_to_predicted": m["sd"] / pred,
                        "ratio_ci": (None if lo is None else lo / pred, None if hi is None else hi / pred),
                        "error_pct": 100 * (m["sd"] / pred - 1), "tails": tail_table(d)})
            # by start session (descriptive)
            sess = {}
            for s_, nm in enumerate(("ASIA 00-07", "LONDON 07-13", "NY 13-21", "LATE 21-24")):
                ds = d[Q.session[start] == s_]
                if len(ds) >= 30:
                    sess[nm] = {"n": len(ds), "sd": float(ds.std(ddof=1))}
            row["by_start_session"] = sess
            # stability by quarter (descriptive)
            qd = {}
            for q in sorted(set(Q.quarter[start])):
                dq = d[Q.quarter[start] == q]
                if len(dq) >= 30:
                    qd[q] = {"n": len(dq), "ratio": float(dq.std(ddof=1) / pred)}
            row["by_quarter_ratio"] = qd
        else:
            row["label"] = "INSUFFICIENT"
        out["horizons"][name] = row
    return out


def variance_ratio(Q, q):
    """
    Lo-MacKinlay variance ratio on overlapping q-bar changes, heteroskedasticity-robust z*.
    Returns dict(vr, z, p, n_returns, n_windows).
    """
    r_valid = (~Q.bad[1:]) & (np.arange(1, Q.n) < Q.hold)
    r = np.diff(Q.close)
    mu = float(r[r_valid].mean())
    e = (r - mu) ** 2
    N = int(r_valid.sum())
    var1 = float(e[r_valid].mean())
    i, ok = Q.window_ok(q)
    Xq = Q.close[i[ok] + q] - Q.close[i[ok]]
    varq = float(((Xq - q * mu) ** 2).mean())
    vr = varq / (q * var1)
    # delta_j over valid return pairs (t, t-j)
    ev = np.where(r_valid, e, np.nan)
    denom = float(np.nansum(ev) ** 2)
    theta = 0.0
    for j in range(1, q):
        prod = ev[j:] * ev[:-j]
        dj = N * float(np.nansum(prod)) / denom
        theta += (2.0 * (q - j) / q) ** 2 * dj
    z = math.sqrt(N) * (vr - 1.0) / math.sqrt(theta) if theta > 0 else 0.0
    return {"q": q, "vr": vr, "z": z, "p": w1.norm_p(z), "n_returns": N, "n_windows": int(ok.sum())}


def exp_variance_ratios(Q):
    rows = [variance_ratio(Q, q) for q in VR_Q]
    rej, qv = w1.bh_fdr([r["p"] for r in rows], FDR_Q)
    for r, rj, qq in zip(rows, rej, qv):
        r["q_value"], r["fdr_reject"] = qq, rj
        r["horizon"] = {v: k for k, v in HORIZONS.items()}.get(r["q"], f"{r['q']} bars")
    return rows


# ===========================================================================
# 2.3 independence
# ===========================================================================
def _pairs_acf(x, valid, lag):
    n = len(x)
    if lag >= n:
        return None, 0
    both = valid[lag:] & valid[:-lag]
    m = float(x[valid].mean())
    num = float(((x[lag:][both] - m) * (x[:-lag][both] - m)).sum())
    den = float(((x[valid] - m) ** 2).sum())
    npairs = int(both.sum())
    return (num / den * (valid.sum() / max(npairs, 1))) if den > 0 else None, npairs


def exp_independence(Q):
    r = np.diff(Q.close)
    valid = (~Q.bad[1:]) & (np.arange(1, Q.n) < Q.hold)
    N = int(valid.sum())
    series = {"returns": r, "abs_returns": np.abs(r), "squared_returns": r ** 2}
    out = {"n_returns": N, "acf": {}}
    band = 1.96 / math.sqrt(N)
    for nm, x in series.items():
        row = {}
        for lag in ACF_LAGS:
            a, npairs = _pairs_acf(x, valid, lag)
            row[str(lag)] = None if a is None else round(a, 4)
        out["acf"][nm] = row
    out["bartlett_band_iid"] = band
    out["band_note"] = ("the +-1.96/sqrt(N) band is valid only for independent, homoskedastic data; it is far too narrow for |r| and r^2, "
                        "whose autocorrelations are tested through the persistence curve below, not through this band")
    # Ljung-Box on returns (lags 1..20), heteroskedasticity makes it anti-conservative: shown with that warning
    q_stat = 0.0
    for lag in range(1, 21):
        a, npairs = _pairs_acf(r, valid, lag)
        q_stat += (a ** 2) / (N - lag)
    q_stat *= N * (N + 2)
    from math import exp
    k = 10.0
    out["ljung_box_returns_lag20"] = {"Q": q_stat, "df": 20, "warning": "anti-conservative under volatility clustering; read with the robust variance ratios"}
    # runs test on signs (contiguous valid returns only)
    sgn = np.sign(r)[valid]
    sgn = sgn[sgn != 0]
    runs = 1 + int((sgn[1:] != sgn[:-1]).sum())
    n1, n2 = int((sgn > 0).sum()), int((sgn < 0).sum())
    mu_r = 2 * n1 * n2 / (n1 + n2) + 1
    var_r = (mu_r - 1) * (mu_r - 2) / (n1 + n2 - 1)
    zr = (runs - mu_r) / math.sqrt(var_r)
    out["runs_test"] = {"runs": runs, "expected": mu_r, "z": zr, "p": w1.norm_p(zr), "n_pos": n1, "n_neg": n2}
    # volatility persistence curve: mean next |r| by quintile of the previous |r|
    prev, nxt = np.abs(r[:-1]), np.abs(r[1:])
    pv = valid[:-1] & valid[1:]
    prev, nxt = prev[pv], nxt[pv]
    edges = np.quantile(prev, [0.2, 0.4, 0.6, 0.8])
    grp = np.digitize(prev, edges)
    base = float(nxt.mean())
    out["volatility_persistence"] = [{"quintile_of_previous_abs_return": g + 1, "n": int((grp == g).sum()), "mean_next_abs_over_overall": float(nxt[grp == g].mean() / base)} for g in range(5)]
    return out


# ===========================================================================
# data-quality add-ons
# ===========================================================================
def jump_audit(clean5):
    o, c, h, l = (clean5[k].to_numpy(float) * 1e4 for k in ("open", "close", "high", "low"))
    prev_c = np.concatenate([[np.nan], c[:-1]])
    ok = ((clean5["gap_before_missing"].to_numpy() == 0) & (~clean5["closure_before"].to_numpy().astype(bool)))
    ok[0] = False
    jump = np.abs(o - prev_c)
    rng_ = h - l
    big = ok & (jump > np.maximum(rng_ * 0.5, 2.0))
    return {"bars_checked": int(ok.sum()), "bars_with_open_far_from_previous_close": int(big.sum()),
            "share": float(big.sum() / max(ok.sum(), 1)),
            "note": "open further than max(0.5*range, 2 pips) from the previous close: the bar-to-bar price path is not continuous there"}


def consistency_1h_vs_5m(clean5, clean1h):
    c5 = clean5["close"]
    t_last = clean1h.index + pd.Timedelta(minutes=55)
    m = c5.reindex(t_last)
    ok = m.notna().to_numpy()
    diff = np.abs(m.to_numpy()[ok] - clean1h["close"].to_numpy()[ok]) * 1e4
    return {"hours_compared": int(ok.sum()), "share_within_0.5_pip": float((diff <= 0.5).mean()) if len(diff) else None,
            "median_abs_diff_pips": float(np.median(diff)) if len(diff) else None}


# ===========================================================================
# orchestration and report
# ===========================================================================
def run_quant1(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    gate = bachelier_gate()
    clean5 = preloaded["5m"] if preloaded else rc.clean_timeframe("5m", raw_dir, source)[0]
    clean1h = (preloaded["1h"] if preloaded else rc.clean_timeframe("1h", raw_dir, source)[0]) if (preloaded is None or "1h" in preloaded) else None
    Q = Q5(clean5)
    rng = np.random.default_rng(BOOT_SEED)
    res = {"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "holdout_opened": False, "phase1_gate": gate,
           "development_bars": int(Q.hold), "development_from": str(Q.index[0]), "development_to": str(Q.index[Q.hold - 1]) if Q.hold > 0 else None}
    if not gate["passed"]:
        res["phase2"] = "SKIPPED: Phase 1 gate failed; the simulator is not trusted"
        return res
    res["data_quality"] = {"jumps": jump_audit(clean5.iloc[: Q.hold]), "consistency_1h_vs_5m": consistency_1h_vs_5m(clean5.iloc[: Q.hold], clean1h) if clean1h is not None else None}
    res["distributions_and_scaling"] = exp_distributions(Q, rng)
    res["variance_ratios"] = exp_variance_ratios(Q)
    res["independence"] = exp_independence(Q)
    return res


def _f(x, nd=2):
    return "n/a" if x is None else f"{x:.{nd}f}"


def render_markdown(r):
    L = ["# Observatory — Quantitative programme, Phases 1–2 report\n",
         f"Contract hash `{r['contract_hash']}` · holdout opened: **False** · development data {r['development_from'][:10]} → {r['development_to'][:10] if r['development_to'] else 'n/a'} ({r['development_bars']} five-minute bars)\n",
         "**Descriptive research on GBPUSD price behaviour. No direction is predicted and nothing here is a trading rule.**\n",
         "## Phase 1 — does the simulator reproduce Bachelier's mathematics? (Q1-GATE)\n"]
    g = r["phase1_gate"]
    L.append(f"**{'PASS' if g['passed'] else 'FAIL'}** — {sum(c['pass'] for c in g['checks'])}/{g['n_checks']} checks\n")
    for c in g["checks"]:
        L.append(f"- {'✓' if c['pass'] else '✗'} {c['check']}: simulated {c['observed']} vs exact {c['expected']} (tolerance ±{c['tolerance']})")
    if isinstance(r.get("phase2"), str):
        L.append("\n" + r["phase2"])
        return "\n".join(L)
    dq = r["data_quality"]
    L.append("\n## Data quality\n")
    L.append(f"- bars whose open is far from the previous close (continuity breaks): {dq['jumps']['bars_with_open_far_from_previous_close']} of {dq['jumps']['bars_checked']} ({100 * dq['jumps']['share']:.2f}%)")
    if dq.get("consistency_1h_vs_5m"):
        c = dq["consistency_1h_vs_5m"]
        L.append(f"- 1h closes vs the 5m close at the same instant: {c['hours_compared']} hours compared, {100 * c['share_within_0.5_pip']:.1f}% within 0.5 pip, median difference {c['median_abs_diff_pips']:.2f} pips")
    d = r["distributions_and_scaling"]
    L.append(f"\n## 2.1 / 2.2 — price changes and the square-root-of-time rule (5m bar sd = {d['sd_5m_bar_pips']:.3f} pips)\n")
    L.append("Non-overlapping changes. Predicted sd = sd(5m) × √(bars). Ratio 1.00 = Bachelier exactly; CI from a day-block bootstrap.\n")
    L.append("| horizon | n | sd (pips) | predicted | ratio (95% CI) | skew | excess kurt | % up |")
    L.append("|---|---|---|---|---|---|---|---|")
    for nm, row in d["horizons"].items():
        if "sd" not in row:
            L.append(f"| {nm} | {row['n']} | INSUFFICIENT | | | | | |")
            continue
        rc_ = row["ratio_ci"]
        ci = f"{_f(rc_[0], 3)}–{_f(rc_[1], 3)}" if rc_[0] is not None else "n/a"
        L.append(f"| {nm} | {row['n']} | {row['sd']:.2f} | {row['sqrt_time_prediction']:.2f} | {row['ratio_actual_to_predicted']:.3f} ({ci}) | {row['skew']:+.2f} | {row['excess_kurtosis']:+.1f} | {100 * row['pct_positive']:.1f}% |")
    L.append("\n**Variance-ratio test (Lo–MacKinlay, overlapping windows, heteroskedasticity-robust).** VR = 1: random walk; < 1: mean reversion; > 1: trending. BH-corrected over 6 horizons.\n")
    L.append("| horizon | VR | z* | p | q | verdict |")
    L.append("|---|---|---|---|---|---|")
    for v in r["variance_ratios"]:
        verdict = "VR differs from 1" if v["fdr_reject"] else "consistent with 1"
        L.append(f"| {v['horizon']} | {v['vr']:.3f} | {v['z']:+.2f} | {v['p']:.4f} | {v['q_value']:.4f} | {verdict} |")
    L.append("\n**By starting session** (sd in pips): " + " · ".join(
        f"{nm} " + ", ".join(f"{s[:5]} {v['sd']:.1f}" for s, v in row["by_start_session"].items()) for nm, row in d["horizons"].items() if row.get("by_start_session") and nm in ("1h", "4h")))
    L.append("\n**Stability** (ratio actual/predicted by quarter): " + " · ".join(
        f"{nm}: " + ", ".join(f"{q} {v['ratio']:.2f}" for q, v in row["by_quarter_ratio"].items()) for nm, row in d["horizons"].items() if row.get("by_quarter_ratio") and nm in ("1h", "4h")))
    L.append("\n## 2.4 — tails: how often do changes exceed k standard deviations? (normal model vs actual)\n")
    L.append("| horizon | k | actual share (95% CI) | normal share | ratio | label |")
    L.append("|---|---|---|---|---|---|")
    for nm, row in d["horizons"].items():
        if "tails" not in row:
            continue
        for k, t in row["tails"].items():
            L.append(f"| {nm} | {k} | {100 * t['share']:.2f}% ({100 * t['ci'][0]:.2f}–{100 * t['ci'][1]:.2f}%) | {100 * t['normal_share']:.3f}% | {t['ratio']:.1f}× | {t['label']} |")
    ind = r["independence"]
    L.append(f"\n## 2.3 — are increments independent? ({ind['n_returns']} five-minute returns)\n")
    L.append("Autocorrelation at lags " + ", ".join(str(x) for x in ACF_LAGS) + f" (iid band ±{ind['bartlett_band_iid']:.4f}; see note).\n")
    for nm, row in ind["acf"].items():
        L.append(f"- {nm}: " + " · ".join(f"{k}: {v:+.3f}" for k, v in row.items() if v is not None))
    L.append(f"- note: {ind['band_note']}")
    rt = ind["runs_test"]
    L.append(f"- runs test on signs: {rt['runs']} runs vs {rt['expected']:.0f} expected, z={rt['z']:+.2f}, p={rt['p']:.4f}")
    lb = ind["ljung_box_returns_lag20"]
    L.append(f"- Ljung–Box Q(20) on returns: {lb['Q']:.1f} ({lb['warning']})")
    L.append("- volatility persistence (mean next |return| ÷ overall mean, by quintile of the previous |return|): " + " · ".join(
        f"Q{x['quintile_of_previous_abs_return']}: {x['mean_next_abs_over_overall']:.2f}" for x in ind["volatility_persistence"]))
    L.append("\n## Not run in this programme step\n- Phase 3 (GBM, bootstrap), Phase 4 (ATR and range estimates), Phase 5 (direction and barrier probabilities, calibration), Phase 6 (tradeability).\n- Holdout: sealed.\n")
    return "\n".join(L)


# ===========================================================================
# self-test: the statistics must recover known properties of known processes
# ===========================================================================
def _df_from_returns(ret_pips, start="2026-01-05 00:00"):
    t0 = pd.Timestamp(start, tz="UTC")
    c = 1.30 + np.cumsum(ret_pips) / 1e4
    idx = pd.DatetimeIndex([t0 + pd.Timedelta(minutes=5 * i) for i in range(len(c))])
    df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c}, index=idx)
    df["gap_before_missing"], df["closure_before"] = 0, False
    return df


def selftest(fast=False):
    fails, n = [], 0

    def chk(label, cond):
        nonlocal n
        n += 1
        if not cond:
            fails.append(label)
    g = bachelier_gate(seed=11, n_paths=8000 if fast else 20000)
    chk("Phase 1 gate passes (all %d checks)" % g["n_checks"], g["passed"])
    rng = np.random.default_rng(3)
    N = 40000
    # 1. iid normal: random walk -> variance ratios near 1, no excess kurtosis, tails near normal, ACF near 0
    r = rng.normal(0, 3.0, N)
    Q = Q5(_df_from_returns(r))
    ind = exp_independence(Q)
    vr = [variance_ratio(Q, q) for q in (6, 24, 96)]
    chk("iid: variance ratios close to 1 (|z*| < 3 for all)", all(abs(v["z"]) < 3 for v in vr))
    m = moments(np.diff(Q.close)[~Q.bad[1:]])
    chk("iid: excess kurtosis within +-0.2", abs(m["excess_kurtosis"]) < 0.2)
    chk("iid: |ACF(1)| of returns < 0.02", abs(ind["acf"]["returns"]["1"]) < 0.02)
    tt = tail_table(np.diff(Q.close))
    chk("iid: 2-sd tail share within 10% of 4.55%", abs(tt["2"]["share"] / tt["2"]["normal_share"] - 1) < 0.1)
    d12, _ = Q.changes(12)
    chk("iid: sqrt-time ratio within 3% at 12 bars", abs(d12.std(ddof=1) / (3.0 * math.sqrt(12)) - 1) < 0.03)
    # 2. mean-reverting returns (AR(1), phi = -0.15): VR < 1 detected, ACF(1) about -0.15
    eps = rng.normal(0, 3.0, N)
    ar = np.zeros(N)
    for t in range(1, N):
        ar[t] = -0.15 * ar[t - 1] + eps[t]
    Qa = Q5(_df_from_returns(ar))
    va = variance_ratio(Qa, 12)
    chk("AR(1) phi=-0.15: VR(12) < 0.85 and z* < -3", va["vr"] < 0.85 and va["z"] < -3)
    chk("AR(1): ACF(1) near -0.15", abs(exp_independence(Qa)["acf"]["returns"]["1"] + 0.15) < 0.03)
    # 3. trending returns (phi = +0.15): VR > 1
    ar2 = np.zeros(N)
    for t in range(1, N):
        ar2[t] = 0.15 * ar2[t - 1] + eps[t]
    vb = variance_ratio(Q5(_df_from_returns(ar2)), 12)
    chk("AR(1) phi=+0.15: VR(12) > 1.15 and z* > 3", vb["vr"] > 1.15 and vb["z"] > 3)
    # 4. clustered volatility: fat tails, |r| autocorrelated, volatility persistence curve rising, VR still near 1
    var, rg_ = 1.0, np.zeros(N)
    for t in range(N):
        sig = 3.0 * math.sqrt(var)
        rg_[t] = rng.normal(0, sig)
        var = 0.02 + 0.10 * (rg_[t] / 3.0) ** 2 + 0.88 * var
    Qg = Q5(_df_from_returns(rg_))
    ig = exp_independence(Qg)
    chk("GARCH: excess kurtosis > 1", moments(np.diff(Qg.close))["excess_kurtosis"] > 1.0)
    chk("GARCH: ACF(1) of |r| > 0.1", ig["acf"]["abs_returns"]["1"] > 0.1)
    chk("GARCH: persistence curve rises from Q1 to Q5", ig["volatility_persistence"][4]["mean_next_abs_over_overall"] > ig["volatility_persistence"][0]["mean_next_abs_over_overall"] + 0.3)
    chk("GARCH: returns themselves uncorrelated (|ACF(1)| < 0.03)", abs(ig["acf"]["returns"]["1"]) < 0.03)
    # 5. heavy tails without clustering (Student t, df=3): kurtosis large, |r| uncorrelated, tail table flags it
    rt3 = rng.standard_t(3, N) * 1.7
    Qt = Q5(_df_from_returns(rt3))
    chk("Student-t(3): excess kurtosis > 3", moments(np.diff(Qt.close))["excess_kurtosis"] > 3)
    chk("Student-t(3): ACF(1) of |r| < 0.03", exp_independence(Qt)["acf"]["abs_returns"]["1"] < 0.03)
    tt3 = tail_table(np.diff(Qt.close))
    chk("Student-t(3): 3-sd tail flagged EXCEEDS_NORMAL", tt3["3"]["label"] == "EXCEEDS_NORMAL")
    chk("rare 4-sd tail with expected count < 5 is labelled INCONCLUSIVE, not judged", tt3["4"]["label"].startswith("INCONCLUSIVE"))
    # 6. windows crossing a break are dropped and the holdout is respected
    dfb = _df_from_returns(rng.normal(0, 3.0, 2000))
    dfb.iloc[1000, dfb.columns.get_loc("gap_before_missing")] = 3
    Qb = Q5(dfb)
    i, ok = Qb.window_ok(12)
    chk("windows spanning a data gap are dropped", not ok[995:1000].any() and ok[980])
    late = Q5(_df_from_returns(rng.normal(0, 3.0, 2000), start="2026-06-17 20:00"))
    chk("windows ending at or after the holdout date are excluded", late.hold < late.n and not late.window_ok(12)[1][late.hold - 11:].any())
    # 7. tail table wiring
    chk("normal shares for k=1,2,3,4", all(abs(tail_table(rng.normal(0, 1, 100000))[str(k)]["normal_share"] - math.erfc(k / math.sqrt(2))) < 1e-12 for k in TAIL_K))
    return {"checks": n, "failures": fails, "passed": not fails}


def main():
    ap = argparse.ArgumentParser(description="Observatory quantitative programme, Phases 1-2. Read-only on raw data.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    res = run_quant1(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "quant1_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "quant1_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
