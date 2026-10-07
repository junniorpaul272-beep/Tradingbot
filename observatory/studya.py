"""
observatory/studya.py
=====================
OBSERVATORY — STUDY A: market persistence. ISOLATED from the live bot. (Studies are lettered, tests inside a study are A.1, A.2, ...)

THE QUESTION
------------
"GBPUSD is trending" can mean three different things:
  (1) directional persistence  - does the sign of the past move predict the sign of the next move?
  (2) directional efficiency   - does price travel mostly one way inside a window ("trendiness"), and does trendiness persist?
  (3) volatility state         - are moves large or small, with no directional content at all?
A behaviour can be common without being informative: every claim below is measured AGAINST a baseline, then against a cost.

DESIGN (small, fixed, pre-declared grid; nothing is tuned on outcomes)
----------------------------------------------------------------------
  Blocks    consecutive, NON-OVERLAPPING blocks of length L, anchored on the UTC clock (minute-of-day multiple of L), measured on the
            5m series: block return r = open(end anchor) - open(start anchor). A block is valid only if all its 5m bars exist
            (no missing candle, no weekend closure inside it). A PAIR = block k (the past) and block k+1 (the future).
  L grid    5m 15m 30m 1h 2h 4h 8h 12h 24h (nine horizons; the same grid for every test that uses it).
  Scale     z = sign(r_k) * r_{k+1} / (sqrt(L in hours) * ATR14(1h)), ATR from the last 1h candle CLOSED at the start of block k+1's
            past block. Under a driftless random walk E[z] = 0 at every L and z has about unit spread, so horizons are comparable.

  A.1  CONTINUATION PROFILE  (family 1, one test per L, 9 tests)
       mean z over pairs vs 0. Positive = continuation (trend following), negative = reversal. Descriptive companions: hit rate
       P(next sign = past sign) vs 0.5 (Wilson CI), gross pips of "trade the past direction for one block", and its net of the flat
       1 pip cost for BOTH the follow and the fade side (one trade per block), first vs second half of development.
  A.3  TRENDINESS PERSISTENCE (family 1, L = 1h 4h 12h, 3 tests)
       efficiency ER = |net move| / path length of the 5m opens inside the block. ER has a time-of-day pattern, so it is
       standardised inside its own time-of-day slot first. Test: mean of u_k * u_{k+1} (a de-seasonalised autocorrelation) vs 0.
  A.2  STATE-CONDITIONAL CONTINUATION (family 2, EXPLORATORY, L = 1h 4h 12h x 2 states = 6 tests)
       state known at the END of the past block only: ER_k (efficiency) or |r_k|/(sqrt(L)*ATR) (expansion). Top tercile minus bottom
       tercile of the mean z, matched on the time-of-day slot. Tercile edges come from the state's own development distribution
       (no outcome is used).
  Descriptive only: ER percentiles vs a Gaussian random walk with the same number of steps; volatility clustering
  (de-seasonalised autocorrelation of the size of consecutive blocks); sample sizes.

STATISTICS
  Robust (sample s.d.) mean tests; adjacent pairs share a block but products of past-sign and future-return are uncorrelated under no
  persistence (checked below on random-walk AND volatility-clustered nulls). Benjamini-Hochberg q = 0.10 inside each family.
  Verdict labels as elsewhere; practical threshold 0.05 sigma-units (a correlation-sized effect).

PRE-DECLARED READING
  No A.1/A.3 test survives FDR  -> no detectable directional persistence or trendiness persistence at these scales: "trade the trend"
                                    cannot be justified by past direction alone, and A.2 is not interpreted.
  Some survive                  -> the profile says at WHICH scales and in WHICH direction (continuation or reversal). It is a hypothesis
                                    for one future holdout test, and only the net-of-cost columns say whether it could matter.
  Volatility clustering large but A.1 empty -> the useful state is expansion/compression, not trend/range.

The holdout (>= HOLDOUT_START_UTC) is sealed: pairs whose future block ends after the boundary are excluded, there is no way to open it.
--readiness counts holdout PAIRS (structure only) and computes power from DEVELOPMENT estimates.

Run:  python3 -m observatory.studya [--raw-dir ...] [--out ...]    python3 -m observatory.studya --selftest    python3 -m observatory.studya --readiness
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

PIP = 1e-4
SPREAD_PIPS = w1.SPREAD_PIPS
FDR_Q = w1.FDR_Q
MIN_EFFECT = 0.05
L_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240, "8h": 480, "12h": 720, "24h": 1440}
ER_LS = ("1h", "4h", "12h")
STATE_LS = ("1h", "4h", "12h")
STATE_VARS = ("efficiency", "expansion")
ATR_STALE = pd.Timedelta(hours=6)
NS_H = 3600 * 10 ** 9
CONTRACT = {
    "study": "A", "module": "studya", "version": "v1", "instrument": "GBPUSD", "horizons": L_MIN, "er_horizons": list(ER_LS),
    "state_horizons": list(STATE_LS), "state_vars": list(STATE_VARS), "blocks": "non-overlapping, UTC-anchored, 5m opens, no gap/closure inside",
    "scale": "sign(r_k)*r_{k+1}/(sqrt(L_h)*ATR14(1h, last closed candle))", "efficiency": "|net|/path of 5m opens, de-seasonalised by time-of-day slot",
    "terciles": "development distribution of the state, matched on slot", "spread_pips_flat": SPREAD_PIPS, "min_effect": MIN_EFFECT, "fdr_q": FDR_Q,
    "family1": ["A.1 x 9 horizons", "A.3 x 3 horizons"], "family2": ["A.2 x 6"], "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# statistics (local copies: this study does not depend on any other wave's internals)
# ---------------------------------------------------------------------------
def verdict(diff, ci_lo, ci_hi, reject, n_min, dataset_status, min_effect):
    if n_min < 30 or diff is None:
        return "DATA-LIMITED"
    if ci_lo > 0 or ci_hi < 0:
        if abs(diff) >= min_effect:
            if reject:
                return "DISCOVERY_PASS" if dataset_status == "PRIMARY_READY" else "EXPLORATORY_SIGNAL"
            return "EXPLORATORY_SIGNAL_NOT_FDR_SURVIVING"
        return "SMALL_EFFECT"
    if max(abs(ci_lo), abs(ci_hi)) < min_effect:
        return "NO_INFORMATION"
    return "INCONCLUSIVE"


def mean_test(vals):
    v = np.asarray(vals, float)
    v = v[np.isfinite(v)]
    n = len(v)
    if n < 10:
        return {"n": n, "mean": None, "se": None, "ci": None, "z": None, "p": None}
    m, sd = float(v.mean()), float(v.std(ddof=1))
    se = sd / math.sqrt(n)
    z = m / se if se > 0 else 0.0
    return {"n": n, "mean": m, "se": se, "sd": sd, "ci": (m - 1.96 * se, m + 1.96 * se), "z": z, "p": w1.norm_p(z)}


def strat_mean_diff(a_by, b_by):
    """a_by, b_by: stratum -> list of values. Weighted by the arm-a count; a stratum needs >= 10 in both arms."""
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
    na_all, nb_all = sum(len(v) for v in a_by.values()), sum(len(v) for v in b_by.values())
    out = {"diff": None, "se": None, "ci": None, "z": None, "p": None, "n": 0, "n1": na_all, "n0": nb_all, "n_min": min(na_all, nb_all), "matched_share": 0.0}
    if wsum == 0:
        return out
    diff, se = num / wsum, math.sqrt(var) / wsum
    z = diff / se if se > 0 else 0.0
    out.update(diff=diff, se=se, ci=(diff - 1.96 * se, diff + 1.96 * se), z=z, p=w1.norm_p(z), n=wsum, matched_share=wsum / na_all if na_all else 0.0)
    return out


def _ns(idx):
    """UTC nanoseconds since the epoch, whatever time resolution pandas stored (it may be us or ns)."""
    return idx.values.astype("datetime64[ns]").astype("int64")


def _ranks(x):
    return np.argsort(np.argsort(x, kind="mergesort"), kind="mergesort").astype(float)


# ---------------------------------------------------------------------------
# blocks and pairs
# ---------------------------------------------------------------------------
def block_pairs(s5, s1h, l_min, atr_const=None):
    """All valid (past block k, future block k+1) pairs for block length l_min minutes. Returns a dict of numpy arrays (time order).
    Validity = three consecutive anchors exist exactly l_min apart (so no candle is missing and no closure lies inside either block),
    and a closed 1h ATR exists at the start of the past block. Everything about block k uses data up to the END of block k only."""
    nb = l_min // 5
    ns = _ns(s5.index)
    md = s5.index.hour.values * 60 + s5.index.minute.values
    a = np.nonzero(md % l_min == 0)[0]
    a = a[a + 2 * nb < s5.n]
    lns = l_min * 60 * 10 ** 9
    ok = ((ns[a + nb] - ns[a]) == lns) & ((ns[a + 2 * nb] - ns[a + nb]) == lns)
    a = a[ok]
    o = s5.o
    cs = np.concatenate([[0.0], np.cumsum(np.abs(np.diff(o)))])
    r0, r1 = o[a + nb] - o[a], o[a + 2 * nb] - o[a + nb]
    p0, p1 = cs[a + nb] - cs[a], cs[a + 2 * nb] - cs[a + nb]
    t0 = ns[a]
    if atr_const is not None:
        atr = np.full(len(a), float(atr_const))
    else:
        h1ns = _ns(s1h.index)
        j = np.searchsorted(h1ns, t0 - NS_H, side="right") - 1
        jc = np.clip(j, 0, None)
        fresh = (j >= 0) & ((t0 - (h1ns[jc] + NS_H)) <= ATR_STALE.value)
        atr = np.where(fresh, s1h.atr[jc], np.nan)
    lh = l_min / 60.0
    scale = math.sqrt(lh) * atr
    good = np.isfinite(atr) & (atr > 0) & (r0 != 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.sign(r0) * r1 / scale
        er0 = np.where(p0 > 0, np.abs(r0) / p0, np.nan)
        er1 = np.where(p1 > 0, np.abs(r1) / p1, np.nan)
        exp0 = np.abs(r0) / scale
        exp1 = np.abs(r1) / (math.sqrt(lh) * atr)
    return {"t0": t0[good], "slot": md[a][good], "r0": r0[good], "r1": r1[good], "z": z[good], "er0": er0[good], "er1": er1[good],
            "exp0": exp0[good], "exp1": exp1[good], "l_min": l_min, "t_end": t0[good] + 2 * lns}


def split_dev(P, hold_ns):
    """Development pairs: the FUTURE block ends at or before the holdout boundary. Holdout pairs (counts only): start at/after it."""
    dev = P["t_end"] <= hold_ns
    hold = P["t0"] >= hold_ns
    take = lambda m: {k: (v[m] if isinstance(v, np.ndarray) else v) for k, v in P.items()}
    return take(dev), take(hold)


def deseason_autocorr(x0, x1, slot0, l_min):
    """Mean of u0*u1 where u = (x - slot mean)/slot sd, the slot statistics taken from the past-block values. A time-of-day pattern in x
    cannot produce a positive value on its own. Returns the mean_test dict of the products."""
    x0, x1, slot0 = np.asarray(x0, float), np.asarray(x1, float), np.asarray(slot0)
    ok = np.isfinite(x0) & np.isfinite(x1)
    x0, x1, slot0 = x0[ok], x1[ok], slot0[ok]
    mu, sd = {}, {}
    for s in np.unique(slot0):
        m = slot0 == s
        if m.sum() >= 10 and x0[m].std(ddof=1) > 0:
            mu[s], sd[s] = x0[m].mean(), x0[m].std(ddof=1)
    prod = []
    for a, b, s in zip(x0, x1, slot0):
        s1 = (s + l_min) % 1440
        if s in mu and s1 in mu:
            prod.append((a - mu[s]) / sd[s] * (b - mu[s1]) / sd[s1])
    return mean_test(prod)


def rw_er_percentiles(n_steps, sims=20000, seed=7, q=(10, 25, 50, 75, 90)):
    rng = np.random.default_rng(seed)
    out = np.empty(sims)
    for k in range(0, sims, 2000):
        w = np.cumsum(rng.normal(size=(2000, n_steps)), axis=1)
        steps = np.abs(np.diff(np.concatenate([np.zeros((2000, 1)), w], axis=1), axis=1)).sum(axis=1)
        out[k:k + 2000] = np.abs(w[:, -1]) / steps
    return [float(np.percentile(out, p)) for p in q]


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------
def analyze_pairs(PL):
    """PL: {label: development pairs dict}. Returns the full result dict (no FDR yet)."""
    res = {"A1": {}, "A3": {}, "A2": {}, "descriptive": {"efficiency_percentiles": {}, "vol_clustering": {}, "pairs": {}}}
    for lab, P in PL.items():
        lm = P["l_min"]
        n = len(P["z"])
        res["descriptive"]["pairs"][lab] = n
        t = mean_test(P["z"])
        sg0, sg1 = np.sign(P["r0"]), np.sign(P["r1"])
        nz = sg1 != 0
        hits = int(((sg0 == sg1) & nz).sum())
        pips = sg0 * P["r1"] / PIP
        pm = mean_test(pips)
        t.update(hit_rate=(hits / nz.sum()) if nz.sum() else None, hit_ci=w1.wilson(hits, int(nz.sum())), pips=pm)
        if pm["mean"] is not None:
            t["follow_net_pips"] = pm["mean"] - SPREAD_PIPS
            t["fade_net_pips"] = -pm["mean"] - SPREAD_PIPS
        if n >= 40:
            mid = np.median(P["t0"])
            t["halves"] = {nm: mean_test(P["z"][m]) for nm, m in (("first_half", P["t0"] < mid), ("second_half", P["t0"] >= mid))}
        t["n_pairs"] = n
        res["A1"][lab] = t
        res["descriptive"]["vol_clustering"][lab] = deseason_autocorr(P["exp0"], P["exp1"], P["slot"], lm)
        if lab in ER_LS:
            res["A3"][lab] = deseason_autocorr(P["er0"], P["er1"], P["slot"], lm)
            er = P["er0"][np.isfinite(P["er0"])]
            if len(er) >= 30:
                res["descriptive"]["efficiency_percentiles"][lab] = {"observed": [float(np.percentile(er, q)) for q in (10, 25, 50, 75, 90)],
                                                                      "random_walk": rw_er_percentiles(lm // 5)}
        if lab in STATE_LS:
            for var in STATE_VARS:
                st = P["er0"] if var == "efficiency" else P["exp0"]
                ok = np.isfinite(st) & np.isfinite(P["z"])
                if ok.sum() < 90:
                    res["A2"][f"{var}@{lab}"] = dict(strat_mean_diff({}, {}), var=var, L=lab)
                    continue
                lo, hi = np.quantile(st[ok], [1 / 3, 2 / 3])
                a_by, b_by = {}, {}
                for s_, z_, sl in zip(st[ok], P["z"][ok], P["slot"][ok]):
                    if s_ >= hi:
                        a_by.setdefault(int(sl), []).append(z_)
                    elif s_ <= lo:
                        b_by.setdefault(int(sl), []).append(z_)
                d = strat_mean_diff(a_by, b_by)
                d.update(var=var, L=lab, edges=(float(lo), float(hi)),
                         mean_top=float(np.mean([x for v in a_by.values() for x in v])) if a_by else None,
                         mean_bottom=float(np.mean([x for v in b_by.values() for x in v])) if b_by else None)
                res["A2"][f"{var}@{lab}"] = d
    return res


def apply_fdr(res, status):
    fam = {1: [(("A1", k), "mean") for k in res["A1"]] + [(("A3", k), "mean") for k in res["A3"]],
           2: [(("A2", k), "diff") for k in res["A2"]]}
    counts = {}
    for f, tests in fam.items():
        live = [(path, est) for path, est in tests if res[path[0]][path[1]].get("p") is not None]
        rej, q = w1.bh_fdr([res[p[0]][p[1]]["p"] for p, _ in live]) if live else ([], [])
        counts[f] = len(live)
        for (path, est), rj, qq in zip(live, rej, q):
            r = res[path[0]][path[1]]
            r["family"], r["fdr_reject"], r["q_value"] = f, rj, qq
            n_min = r.get("n_min", r.get("n", 0)) if path[0] == "A2" else r["n"]
            r["verdict"] = verdict(r[est], r["ci"][0], r["ci"][1], rj, n_min, "PRIMARY_READY" if f == 1 and res.get("_status") == "PRIMARY_READY" else (res.get("_status") if f == 1 else "EXPLORATORY_READY"), MIN_EFFECT)
        for path, est in tests:
            if res[path[0]][path[1]].get("p") is None:
                res[path[0]][path[1]]["verdict"] = "DATA-LIMITED"
    return counts


def reading(res):
    s1 = [(k, v) for k, v in res["A1"].items() if v.get("fdr_reject")]
    s3 = [(k, v) for k, v in res["A3"].items() if v.get("fdr_reject")]
    out = []
    if not s1 and not s3:
        out.append("No A.1 or A.3 test survives multiple-testing control: no detectable directional persistence (or reversal) and no persistence of "
                   "trendiness at these scales. Past direction alone cannot justify 'trade the trend' or 'fade the move'. A.2 is not interpreted.")
    else:
        if s1:
            out.append("A.1 survives at: " + ", ".join(f"{k} ({'continuation' if v['mean'] > 0 else 'reversal'} {v['mean']:+.3f}σ, q={v['q_value']:.3f})" for k, v in s1) + ".")
        if s3:
            out.append("A.3 survives at: " + ", ".join(f"{k} (trendiness autocorrelation {v['mean']:+.3f}, q={v['q_value']:.3f})" for k, v in s3) + ".")
        out.append("These are development-data findings: hypotheses for one holdout test, and only the net-of-cost columns say whether they could matter.")
    vc = [v["mean"] for v in res["descriptive"]["vol_clustering"].values() if v.get("mean") is not None]
    if vc and max(vc) > 0.1 and not s1:
        out.append("Volatility clusters strongly (max de-seasonalised autocorrelation of block size %+.2f) while direction does not persist: the useful state, if any, is expansion/compression, not trend/range." % max(vc))
    return " ".join(out)


def load_series(raw_dir, source, preloaded=None):
    clean = {tf: (preloaded[tf] if preloaded else rc.clean_timeframe(tf, raw_dir, source)[0]) for tf in ("5m", "15m", "1h")}
    s5, s15, s1h = rm.Series(clean["5m"], 5), rm.Series(clean["15m"], 15), rm.Series(clean["1h"], 60)
    rep15 = rc.clean_timeframe("15m", raw_dir, source)[2] if not preloaded else {
        "first": None, "last": None, "raw_lines_parsed": 0, "unique_timestamps": 0, "duplicate_lines": 0, "conflicting_duplicates": 0,
        "dropped_market_closed": 0, "dropped_off_grid": 0, "dropped_invalid_ohlc": 0, "kept_bars": len(clean["15m"]),
        "unexplained_gaps": 0, "missing_open_bars": 0}
    a6 = rg.gate_a6({"15m": clean["15m"]}, {"15m": rep15}, {"15m": s15})
    status = rg.gate_a7({"15m": clean["15m"]}, {"15m": s15}, a6)["details"]["status"]
    return s5, s1h, status


def run_studya(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    s5, s1h, status = load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    PL = {lab: split_dev(block_pairs(s5, s1h, lm), hold_ns)[0] for lab, lm in L_MIN.items()}
    res = analyze_pairs(PL)
    res["_status"] = status
    n = apply_fdr(res, status)
    res.pop("_status")
    first = min((P["t0"].min() for P in PL.values() if len(P["t0"])), default=None)
    res["development"] = {"first_pair_utc": None if first is None else str(pd.Timestamp(first, tz="UTC")), "boundary_utc": str(rc.HOLDOUT_START_UTC)}
    res["reading"] = reading(res)
    res.update({"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False, "n_tests": n})
    return res


# ---------------------------------------------------------------------------
# holdout readiness: counts PAIRS (structure only) and power from DEVELOPMENT estimates
# ---------------------------------------------------------------------------
def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def holdout_readiness(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None, dev_res=None):
    s5, s1h, status = load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    if dev_res is None:
        dev_res = run_studya(raw_dir, source, preloaded)
    counts, months = {}, max((s5.index[-1] - rc.HOLDOUT_START_UTC).days / 30.44, 0.0)
    for lab, lm in L_MIN.items():
        counts[lab] = int(split_dev(block_pairs(s5, s1h, lm), hold_ns)[1]["z"].shape[0])
    out = {"holdout_from": str(rc.HOLDOUT_START_UTC)[:10], "holdout_months": round(months, 2), "holdout_pairs": counts, "tests": {}}
    for fam, key in (("A1", "A1"), ("A3", "A3")):
        for lab, t in dev_res[key].items():
            if t.get("mean") is None:
                continue
            surv = bool(t.get("fdr_reject"))
            nh = counts[lab]
            sd = t.get("sd")
            row = {"dev_effect": round(t["mean"], 4), "dev_q": t.get("q_value"), "survived_dev_fdr": surv, "holdout_pairs": nh}
            if sd and nh > 0 and months > 0:
                se_h = sd / math.sqrt(nh)
                for nm, eff in (("full", abs(t["mean"])), ("half", abs(t["mean"]) / 2)):
                    pw = _phi(eff / se_h - 1.645)
                    need = months * (se_h / (eff / (1.645 + 0.842))) ** 2
                    row[nm] = {"power_now": round(pw, 2), "months_needed_for_80pct": round(need, 1)}
                row["ready_to_open"] = bool(surv and row["half"]["power_now"] >= 0.80)
            out["tests"][f"{fam}:{lab}"] = row
    out["note"] = ("Only tests that survived the development FDR are candidates for the holdout; the rest are listed for power context. "
                   "The holdout stays sealed until a candidate's half-effect power is >= 0.80.")
    return out


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _a(x, nd=3):
    return "n/a" if x is None else f"{x:+.{nd}f}"


def _ci(ci, nd=3):
    return "n/a" if not ci else f"[{_a(ci[0], nd)}, {_a(ci[1], nd)}]"


def _p(x):
    return "n/a" if x is None else f"{x:.4f}"


def _pc(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def render_markdown(r):
    L = ["# Observatory — Study A: market persistence (GBPUSD)\n",
         f"Contract `{r['contract_hash']}` · dataset **{r['dataset_status']}** · holdout opened: **False** · family 1: {r['n_tests'][1]} tests · family 2 (exploratory): {r['n_tests'][2]} tests\n",
         "**Development data only (pairs whose future block ends before the holdout boundary). Nothing here is a trading rule.**\n",
         "z = sign(past block) × (next block) / (√hours × ATR(1h)): **continuation in σ-units**. 0 = random walk, + = trend-following would have worked, − = fading would have worked. "
         "Pairs are consecutive non-overlapping blocks anchored on the UTC clock; each line shows the 95% CI, the largest effect the data cannot rule out.\n",
         "## Reading (pre-declared)\n", f"**{r['reading']}**\n",
         "## A.1 — continuation profile (family 1)\n",
         "| L | pairs | continuation z | 95% CI | hit rate | gross pips / block | net if FOLLOW | net if FADE | q | verdict |\n|---|---|---|---|---|---|---|---|---|---|"]
    for lab, t in r["A1"].items():
        pm = t["pips"]
        L.append(f"| {lab} | {t['n_pairs']} | {_a(t.get('mean'))} | {_ci(t.get('ci'))} | {_pc(t.get('hit_rate'))} {t.get('hit_ci')} | {_a(pm.get('mean'), 2)} | {_a(t.get('follow_net_pips'), 2)} | {_a(t.get('fade_net_pips'), 2)} | {_p(t.get('q_value'))} | **{t.get('verdict')}** |")
    L.append("\nStability (first vs second half of development, descriptive):\n")
    for lab, t in r["A1"].items():
        h = t.get("halves")
        if h:
            L.append(f"- {lab}: first half {_a(h['first_half'].get('mean'))} {_ci(h['first_half'].get('ci'))} (n {h['first_half']['n']}) · second half {_a(h['second_half'].get('mean'))} {_ci(h['second_half'].get('ci'))} (n {h['second_half']['n']})")
    L.append("\n## A.3 — does trendiness persist? (family 1)\n")
    L.append("| L | pairs | autocorrelation of efficiency (de-seasonalised) | 95% CI | q | verdict |\n|---|---|---|---|---|---|")
    for lab, t in r["A3"].items():
        L.append(f"| {lab} | {t.get('n')} | {_a(t.get('mean'))} | {_ci(t.get('ci'))} | {_p(t.get('q_value'))} | **{t.get('verdict')}** |")
    L.append("\n## A.2 — does the past state change the continuation? (family 2, EXPLORATORY)\n")
    L.append("| state @ L | top tercile z | bottom tercile z | difference | 95% CI | matched | q | verdict |\n|---|---|---|---|---|---|---|---|")
    for k, t in r["A2"].items():
        L.append(f"| {k} | {_a(t.get('mean_top'))} | {_a(t.get('mean_bottom'))} | {_a(t.get('diff'))} | {_ci(t.get('ci'))} | {_pc(t.get('matched_share'))} | {_p(t.get('q_value'))} | **{t.get('verdict')}** |")
    d = r["descriptive"]
    L.append("\n## Descriptive baselines\n")
    L.append("Efficiency (|net| / path) percentiles 10/25/50/75/90, observed vs a Gaussian random walk with the same number of 5m steps:\n")
    for lab, v in d["efficiency_percentiles"].items():
        L.append(f"- {lab}: observed " + " / ".join(f"{x:.2f}" for x in v["observed"]) + " · random walk " + " / ".join(f"{x:.2f}" for x in v["random_walk"]))
    L.append("\nVolatility clustering (de-seasonalised autocorrelation of the SIZE of consecutive blocks; positive = big moves follow big moves):\n")
    L.append("| L | " + " | ".join(d["vol_clustering"].keys()) + " |\n|---|" + "---|" * len(d["vol_clustering"]))
    L.append("| autocorr | " + " | ".join(_a(v.get("mean"), 2) for v in d["vol_clustering"].values()) + " |")
    L.append("\n## Not run\n- Holdout (sealed; `--readiness` counts pairs and power). 4H/daily from native bars, EURUSD, barrier ('+20 before −20') probabilities, any state beyond the two above: only after this profile is read.\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _frame(o, c, start="2026-03-10 00:00", drop=()):
    n = len(o)
    t0 = pd.Timestamp(start, tz="UTC")
    idx = [t0 + pd.Timedelta(minutes=5 * i) for i in range(n) if i not in set(drop)]
    keep = [i for i in range(n) if i not in set(drop)]
    o, c = np.asarray(o, float)[keep], np.asarray(c, float)[keep]
    pad = np.abs(o - c) * 0.1 + 1e-6
    df = pd.DataFrame({"open": o, "high": np.maximum(o, c) + pad, "low": np.minimum(o, c) - pad, "close": c}, index=pd.DatetimeIndex(idx))
    df["gap_before_missing"], df["closure_before"] = 0, False
    return df


def _series_from_df(df):
    s5 = rm.Series(df, 5)
    h1 = df[["open", "high", "low", "close"]].resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    h1["gap_before_missing"], h1["closure_before"] = 0, False
    return s5, rm.Series(h1, 60)


def _synth(n, rng, phi=0.0, garch=False, regimes=None, sigma=0.00025, drift=0.25):
    """5m synthetic world. regimes: None | 'trendrange' (AR(1) +-0.25 in alternating runs) | 'drift' (a persistent drift of +-drift*sigma per
    bar for runs of 4-12 h) | 'driftalt' (drift runs alternate with zero-drift runs)."""
    eps = rng.normal(size=n)
    sig = np.full(n, sigma)
    if garch:
        v = sigma ** 2
        for i in range(1, n):
            v = 0.02 * sigma ** 2 + 0.08 * (sig[i - 1] * eps[i - 1]) ** 2 + 0.90 * v
            sig[i] = math.sqrt(v)
    ph = np.full(n, phi)
    mu = np.zeros(n)
    if regimes:
        k, sgn, on = 0, 1, True
        while k < n:
            ln = int(rng.integers(48, 145))
            if regimes == "trendrange":
                ph[k:k + ln] = 0.25 * sgn
            elif regimes == "drift":
                mu[k:k + ln] = drift * sigma * (1 if rng.random() < 0.5 else -1)
            elif regimes == "driftalt":
                mu[k:k + ln] = (drift * sigma * (1 if rng.random() < 0.5 else -1)) if on else 0.0
                on = not on
            sgn, k = -sgn, k + ln
    ret = np.empty(n)
    prev = 0.0
    for i in range(n):
        prev = ph[i] * prev + mu[i] + sig[i] * eps[i]
        ret[i] = prev
    c = 1.30 + np.cumsum(ret)
    o = np.concatenate([[1.30], c[:-1]])
    return _frame(o, c, start="2026-01-05 00:00")


def selftest(null_series=8):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    # ---- hand-built blocks: 15m blocks (3 bars). Price path chosen so r = +3, -1, +2, +2
    p = [100, 101, 102, 103, 103.5, 102.5, 102, 102.5, 103, 103.5, 104, 105, 106, 106, 106, 106]
    o = p[:-1]
    c = p[1:]
    s5, s1h = _series_from_df(_frame(o, c))
    P = block_pairs(s5, s1h, 15, atr_const=1.0)
    chk("pairs from 3 consecutive anchors", len(P["z"]), 3)
    chk("continuation z = sign(past)*future/(sqrt(0.25 h)*ATR)", [round(x, 6) for x in P["z"]], [-2.0, -3.0, 5.0])
    chk("past and future returns", ([round(x, 6) for x in P["r0"]], [round(x, 6) for x in P["r1"]]), ([3.0, -1.0, 1.5], [-1.0, 1.5, 2.5]))
    t = analyze_pairs({"15m": P})["A1"]["15m"]
    chk("hit rate counts agreeing signs only", (round(t["hit_rate"], 6)), round(1 / 3, 6))
    # cost accounting on a long deterministic series: net(follow) = gross - cost, net(fade) = -gross - cost, one trade per block
    rng_c = np.random.default_rng(1)
    wk = 1.30 + np.cumsum(rng_c.normal(0, 0.0003, 3000))
    sc5, sc1 = _series_from_df(_frame(np.concatenate([[1.30], wk[:-1]]), wk))
    tc = analyze_pairs({"1h": block_pairs(sc5, sc1, 60)})["A1"]["1h"]
    chk("follow/fade net pips are gross -/+ the flat cost", (round(tc["follow_net_pips"], 6), round(tc["fade_net_pips"], 6)), (round(tc["pips"]["mean"] - SPREAD_PIPS, 6), round(-tc["pips"]["mean"] - SPREAD_PIPS, 6)))
    # a missing 5m candle invalidates the pairs that touch it (a weekend closure is the same thing: a hole in the clock)
    s5h, s1hh = _series_from_df(_frame(o, c, drop=[4]))
    Ph = block_pairs(s5h, s1hh, 15, atr_const=1.0)
    chk("a hole inside a block removes the pairs that use it", len(Ph["z"]) < 3, True)
    big = [100 + 0.1 * i for i in range(60)]
    s5w, s1w = _series_from_df(_frame(big[:-1], big[1:], drop=list(range(20, 30))))
    Pw = block_pairs(s5w, s1w, 60, atr_const=1.0)
    chk("a long closure leaves no pair that spans it", all(not (t0 < pd.Timestamp("2026-03-10 01:40", tz="UTC").value < t0 + 2 * 3600 * 10 ** 9) for t0 in Pw["t0"]), True)
    # no lookahead: changing prices AFTER the pair's future block leaves earlier pairs untouched; the state of block k uses block k only
    rng0 = np.random.default_rng(3)
    walk = 1.30 + np.cumsum(rng0.normal(0, 0.0003, 4000))
    o2, c2 = np.concatenate([[1.30], walk[:-1]]), walk
    sa, ha = _series_from_df(_frame(o2, c2))
    Pa = block_pairs(sa, ha, 60)
    c3 = c2.copy()
    o3 = o2.copy()
    cut = 2000
    c3[cut:] += 0.05
    o3[cut:] += 0.05
    sb, hb = _series_from_df(_frame(o3, c3))
    Pb = block_pairs(sb, hb, 60)
    early = Pa["t0"] + 2 * 3600 * 10 ** 9 <= pd.Timestamp("2026-03-10", tz="UTC").value + cut * 5 * 60 * 10 ** 9 - 3600 * 10 ** 9
    mk = {t: i for i, t in enumerate(Pb["t0"])}
    same = all(abs(Pa["z"][i] - Pb["z"][mk[t]]) < 1e-12 and (np.isnan(Pa["er0"][i]) or abs(Pa["er0"][i] - Pb["er0"][mk[t]]) < 1e-12) for i, t in enumerate(Pa["t0"]) if early[i] and t in mk)
    chk("pairs (z and the past state) unchanged when the future is altered", (same, int(early.sum()) > 100), (True, True))
    # the ATR used at a block start never includes a 1h candle that is still open
    j_hour = 25
    t_hour = ha.index[j_hour].value
    hb_df = ha.df.copy()
    hb_df.iloc[j_hour, hb_df.columns.get_loc("high")] += 0.5
    hb_df.iloc[j_hour, hb_df.columns.get_loc("low")] -= 0.5
    ha2 = rm.Series(hb_df, 60)
    Pc = block_pairs(sa, ha2, 60)
    ia = list(Pa["t0"]).index(t_hour) if t_hour in set(Pa["t0"]) else None
    ic = list(Pc["t0"]).index(t_hour) if t_hour in set(Pc["t0"]) else None
    chk("ATR at an anchor ignores the 1h candle that opens at that anchor", (ia is not None and ic is not None and abs(Pa["z"][ia] - Pc["z"][ic]) < 1e-12), True)
    # de-seasonalised autocorrelation: a pure time-of-day pattern gives ~0, a persistent state gives > 0
    rr = np.random.default_rng(4)
    slots = np.tile(np.arange(0, 1440, 240), 400)[:2400]
    seas = np.where((slots // 240) % 2 == 0, 1.0, 0.0)          # alternating big/small slots, closed under +4h
    x_noise = seas + rr.normal(0, 0.1, len(slots))
    x_next = np.roll(seas, -1) + rr.normal(0, 0.1, len(slots))
    raw = float(np.corrcoef(x_noise[:-1], x_next[:-1])[0, 1])
    ds = deseason_autocorr(x_noise[:-1], x_next[:-1], slots[:-1], 240)
    chk("pure seasonality: raw correlation is large, de-seasonalised is ~0", (abs(raw) > 0.3, abs(ds["mean"]) < 0.1), (True, True))
    ar = np.zeros(2400)
    for i in range(1, 2400):
        ar[i] = 0.6 * ar[i - 1] + rr.normal(0, 1)
    ds2 = deseason_autocorr(ar[:-1], ar[1:], slots[:-1], 240)
    chk("a persistent state is detected by the de-seasonalised autocorrelation", ds2["z"] > 5, True)
    er12 = rw_er_percentiles(1, sims=2000)
    chk("random-walk efficiency with one step is exactly 1", er12, [1.0] * 5)
    e12 = rw_er_percentiles(12, sims=4000)
    chk("random-walk efficiency is below 1 and rising across percentiles", (e12[0] < e12[2] < e12[4] < 1.0), True)
    # ---- planted persistence is found, nulls are not (random walk, volatility clustering)
    rng = np.random.default_rng(11)
    sP, hP = _series_from_df(_synth(90000, rng, regimes="drift"))
    rP = analyze_pairs({lab: block_pairs(sP, hP, L_MIN[lab]) for lab in ("1h", "4h")})
    chk("planted persistent drift: continuation detected at 1h (z>3, positive)", (rP["A1"]["1h"]["z"] > 3, rP["A1"]["1h"]["mean"] > 0), (True, True))
    chk("planted persistent drift: the hit rate is above 50%", rP["A1"]["1h"]["hit_ci"][0] > 0.5, True)
    sR, hR = _series_from_df(_synth(90000, rng, regimes="driftalt", drift=0.35))
    rR = analyze_pairs({"1h": block_pairs(sR, hR, 60)})
    chk("planted drift/no-drift regimes: trendiness persists at 1h (A.3 z>3)", rR["A3"]["1h"]["z"] > 3, True)
    chk("planted regimes: the efficiency state separates continuation (A.2 diff>0, z>2)", (rR["A2"]["efficiency@1h"]["diff"] or 0) > 0 and (rR["A2"]["efficiency@1h"]["z"] or 0) > 2, True)
    rR["_status"] = "PRIMARY_READY"
    apply_fdr(rR, "PRIMARY_READY")
    chk("FDR wiring: the planted A.3 test is rejected", rR["A3"]["1h"].get("fdr_reject"), True)
    # null calibration: Gaussian random walk and GARCH(1,1) volatility clustering, all labels
    zs = {"A1": [], "A3": [], "A2": []}
    used = 0
    labs = ("5m", "30m", "1h", "4h", "12h")
    for k in range(null_series):
        d = _synth(60000, rng, garch=(k % 2 == 1))
        s5_, h1_ = _series_from_df(d)
        PLn = {lab: block_pairs(s5_, h1_, L_MIN[lab]) for lab in labs}
        r_ = analyze_pairs(PLn)
        used += 1
        for key in zs:
            for t_ in r_[key].values():
                if t_.get("z") is not None:
                    zs[key].append(t_["z"])
    allz = np.asarray([z for v in zs.values() for z in v], float)
    chk(f"null: enough tests ({len(allz)})", len(allz) >= 60, True)
    if len(allz) >= 60:
        chk(f"null: |mean z| < 0.4 (got {allz.mean():+.2f})", abs(allz.mean()) < 0.4, True)
        chk(f"null: sd(z) in [0.6, 1.5] (got {allz.std():.2f})", 0.6 <= allz.std() <= 1.5, True)
        chk(f"null: share |z|>1.96 <= 12% (got {(np.abs(allz) > 1.96).mean():.0%})", (np.abs(allz) > 1.96).mean() <= 0.12, True)
    json.dumps(rP, default=str)
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": used, **{f"{k}_mean_z": round(float(np.mean(v)), 3) if v else None for k, v in zs.items()},
                     **{f"{k}_sd_z": round(float(np.std(v)), 3) if v else None for k, v in zs.items()}}}


def main():
    ap = argparse.ArgumentParser(description="Observatory Study A (market persistence). Read-only on raw data; the holdout stays sealed.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--readiness", action="store_true", help="count holdout PAIRS and compute power from development estimates; no holdout outcome is read")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    if a.readiness:
        print(json.dumps(holdout_readiness(a.raw_dir, a.source), indent=2, default=str))
        return
    res = run_studya(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "studya_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "studya_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
