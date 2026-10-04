"""
observatory/wave1.py
====================
OBSERVATORY — Wave 1 experiments (PRICE-ONLY). ISOLATED from the live bot.

  B1  Does a displacement bar predict continuation? (vs matched ordinary bars)
  B2  Does continuation scale with displacement size? (quintile trend)
  B6  What happens after a displacement? (no-pullback / shallow / deep / failure)

No FVG / BOS / CHoCH / IFVG / premium-discount anywhere. Candles only.

FROZEN CONTRACT (printed at the top of every report, with a hash)
-----------------------------------------------------------------
  Instrument     GBPUSD. Timeframes: 5m, 15m, 1h run SEPARATELY (singles T1-T3).
                 Higher-timeframe context configs (T4-T7) are NOT run here: they need
                 a frozen HTF-direction definition first.
  Displacement   range(bar) / ATR14(prior bars) >= k, k in {1.0, 1.5, 2.0}, all reported.
                 Direction = the bar's own direction. Known at the bar close.
  Entry          open of the next bar. Trade = CONTINUATION (same direction).
  R-unit         ATR(14) through the event bar.   Target +1R, stop -1R.
  Horizon        24 bars of the event timeframe.
  Outcome        first touch (TARGET_FIRST / STOP_FIRST / NEITHER / AMBIGUOUS / DATA_GAP).
                 AMBIGUOUS 15m/1h bars are settled with 5m data where possible.
  p              P(+1R first) = T / (T + S). NEITHER, AMBIGUOUS, DATA_GAP are excluded
                 from p but always counted and shown. Worst case (ambiguous = stop) also run.
  Episodes       one event per (direction) within K=12 bars (anchored). Controls are
                 collapsed the same way.
  Control (N2)   ordinary bars (range/ATR < 1.0), same direction rule, matched by strata
                 (direction x UTC session x ATR tercile). Stratified difference, event-weighted.
  Primary        B1: stratified difference in p (event - control), two-sided z-test.
                 B2: Cochran-Armitage trend across size quintiles.   B6: descriptive only.
  Minimum effect 5 percentage points (practical importance, not significance).
  Multiple tests Benjamini-Hochberg across all B1 + B2 primary tests, q = 0.10.
  Data split     chronological 70% development / 30% HOLDOUT. Holdout stays SEALED:
                 outcomes for it are never analysed in a normal run. Opening it needs
                 --open-holdout, writes a ledger line, and works once per (test, tf, k).
  Verdict cap    while the dataset is not PRIMARY_READY (A7), the strongest verdict
                 allowed is EXPLORATORY_SIGNAL.

Imports: stdlib + numpy + pandas + observatory.clean/measure/gates. Raw data only read.
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

K_EPISODE = 12
HORIZON = 24
DISPLACEMENT_K = (1.0, 1.5, 2.0)
DEV_FRACTION = 0.70
MIN_EFFECT = 0.05
FDR_Q = 0.10
SPREAD_PIPS = 1.0
SESSION_EDGES = (7, 13, 21)             # UTC: <7 ASIA, <13 LONDON, <21 NY, else LATE
SESSION_NAMES = ("ASIA", "LONDON", "NY", "LATE")
B6_SHALLOW, B6_DEEP = 0.25, 0.50         # retracement depth as a fraction of the displacement bar's range
TFS = ("5m", "15m", "1h")

CONTRACT = {
    "wave": 1, "instrument": "GBPUSD", "timeframes": list(TFS), "K_episode": K_EPISODE, "horizon_bars": HORIZON,
    "displacement_k": list(DISPLACEMENT_K), "R_unit": "ATR14 through event bar", "target_R": 1.0, "stop_R": 1.0,
    "dev_fraction": DEV_FRACTION, "min_effect": MIN_EFFECT, "fdr_q": FDR_Q, "spread_pips_net_variant": SPREAD_PIPS,
    "sessions_utc": dict(zip(SESSION_NAMES, ["0-7", "7-13", "13-21", "21-24"])),
    "b6_depth_classes": {"shallow_from": B6_SHALLOW, "deep_from": B6_DEEP},
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def wilson(x, n, z=1.96):
    if n == 0:
        return (None, None)
    p = x / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(c - h, 4), round(c + h, 4))


def norm_p(z):
    return math.erfc(abs(z) / math.sqrt(2))


def bh_fdr(pvals, q=FDR_Q):
    """Benjamini-Hochberg. Returns (reject list, qvalue list) in the original order."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    qv = [None] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, pvals[i] * m / rank)
        qv[i] = prev
    return [x <= q for x in qv], qv


def stratified_diff(ev, ct):
    """
    ev, ct: dict stratum -> (T, S). Event-weighted stratified difference of p = T/(T+S).
    Returns dict with diff, se, z, p, matched_share. Strata need >= 5 resolved in BOTH arms.
    """
    num = 0.0
    var = 0.0
    wsum = 0
    total_ev = sum(t + s for t, s in ev.values())
    for st, (te, se_) in ev.items():
        ne = te + se_
        tc, sc = ct.get(st, (0, 0))
        nc = tc + sc
        if ne < 5 or nc < 5:
            continue
        pe, pc = te / ne, tc / nc
        pe_v, pc_v = (te + 2) / (ne + 4), (tc + 2) / (nc + 4)          # smoothed, for the variance only
        wsum += ne
        num += ne * (pe - pc)
        var += ne * ne * (pe_v * (1 - pe_v) / ne + pc_v * (1 - pc_v) / nc)
    if wsum == 0:
        return {"diff": None, "se": None, "z": None, "p": None, "matched_share": 0.0}
    diff = num / wsum
    se = math.sqrt(var) / wsum
    z = diff / se if se > 0 else 0.0
    return {"diff": diff, "se": se, "z": z, "p": norm_p(z), "matched_share": wsum / total_ev if total_ev else 0.0}


def trend_test(groups):
    """Cochran-Armitage trend over ordered groups [(T, S), ...]. Returns (z, p)."""
    n = [t + s for t, s in groups]
    x = [t for t, s in groups]
    N, X = sum(n), sum(x)
    if N == 0 or X in (0, N):
        return (0.0, 1.0)
    pbar = X / N
    sc = list(range(1, len(groups) + 1))
    T = sum(si * (xi - ni * pbar) for si, xi, ni in zip(sc, x, n))
    v = pbar * (1 - pbar) * (sum(ni * si * si for ni, si in zip(n, sc)) - sum(ni * si for ni, si in zip(n, sc)) ** 2 / N)
    if v <= 0:
        return (0.0, 1.0)
    z = T / math.sqrt(v)
    return (z, norm_p(z))


def sample_label(n):
    return "INSUFFICIENT" if n < 30 else ("EXPLORATORY" if n < 100 else "PRIMARY-ELIGIBLE(by N)")


def verdict(diff, ci_lo, ci_hi, reject, n_min, dataset_status):
    if n_min < 30:
        return "DATA-LIMITED"
    if diff is None:
        return "DATA-LIMITED"
    if max(abs(ci_lo), abs(ci_hi)) < MIN_EFFECT:
        return "NO_INFORMATION"
    if ci_lo > 0 or ci_hi < 0:
        if abs(diff) >= MIN_EFFECT and reject:
            return "DISCOVERY_PASS" if dataset_status == "PRIMARY_READY" else "EXPLORATORY_SIGNAL"
        return "EXPLORATORY_SIGNAL_NOT_FDR_SURVIVING" if abs(diff) >= MIN_EFFECT else "SMALL_EFFECT"
    return "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# data preparation
# ---------------------------------------------------------------------------
class Prepared:
    def __init__(self, tf, clean_df, s_lo=None):
        self.tf = tf
        self.s = rm.Series(clean_df, rc.STEP_MINUTES[tf])
        self.s_lo = s_lo
        s = self.s
        n = s.n
        self.n = n
        prior = np.full(n, np.nan)
        prior[1:] = s.atr[:-1]
        self.rng = s.h - s.l
        with np.errstate(divide="ignore", invalid="ignore"):
            self.ratio = np.where(prior > 0, self.rng / prior, np.nan)
        self.dirn = np.where(s.c > s.o, 1, np.where(s.c < s.o, -1, 0))
        ev_time = s.index + s.step
        self.hour = np.asarray(ev_time.hour)
        self.session = np.digitize(self.hour, SESSION_EDGES)
        self.cut = int(n * DEV_FRACTION)
        pips = s.atr * 1e4
        dev_pips = pips[: self.cut]
        dev_pips = dev_pips[np.isfinite(dev_pips)]
        q1, q2 = np.quantile(dev_pips, [1 / 3, 2 / 3])
        self.atrb = np.where(pips <= q1, 0, np.where(pips <= q2, 1, 2))
        self.valid = np.isfinite(self.ratio) & np.isfinite(s.atr) & (self.dirn != 0) & (self.rng > 0)
        self.quarter = np.asarray(ev_time.tz_convert("UTC").tz_localize(None).to_period("Q").astype(str))
        self._cache = {}

    def stratum(self, i):
        return (int(self.dirn[i]), int(self.session[i]), int(self.atrb[i]))

    def episodes(self, mask, dev=True):
        idx = np.where(mask & self.valid)[0]
        if dev:
            idx = idx[idx < self.cut]
        else:
            idx = idx[idx >= self.cut]
        if len(idx) == 0:
            return idx
        _, kept = rm.collapse_episodes(idx, self.dirn[idx], K_EPISODE)
        return idx[kept]

    def outcome(self, i, spread):
        key = (int(i), spread)
        if key in self._cache:
            return self._cache[key]
        d = int(self.dirn[i])
        res = rm.first_touch(self.s, int(i), d, horizon=HORIZON, spread=spread)
        status, reason = res["status"], res["reason"]
        if status == rm.AMBIGUOUS and self.s_lo is not None:
            status, reason = rm.resolve_ambiguous(self.s, self.s_lo, res, d)
        out = (status, res)
        self._cache[key] = out
        return out


def tally(P, idxs, spread=0.0, worst_case=False):
    """Returns (strata dict -> [T, S], counts dict of every status)."""
    strata = {}
    counts = {rm.TARGET_FIRST: 0, rm.STOP_FIRST: 0, rm.NEITHER: 0, rm.AMBIGUOUS: 0, rm.DATA_GAP: 0}
    for i in idxs:
        status, _ = P.outcome(i, spread)
        counts[status] += 1
        if status == rm.AMBIGUOUS and worst_case:
            status = rm.STOP_FIRST
        if status in (rm.TARGET_FIRST, rm.STOP_FIRST):
            cell = strata.setdefault(P.stratum(i), [0, 0])
            cell[0 if status == rm.TARGET_FIRST else 1] += 1
    return {k: tuple(v) for k, v in strata.items()}, counts


# ---------------------------------------------------------------------------
# B1
# ---------------------------------------------------------------------------
def run_b1(P, k, dataset_status, dev=True):
    ev_idx = P.episodes(P.ratio >= k, dev)
    ct_idx = P.episodes(P.ratio < 1.0, dev)
    out = {"tf": P.tf, "k": k, "episodes_events": int(len(ev_idx)), "episodes_controls": int(len(ct_idx))}
    for tag, spread, worst in (("gross", 0.0, False), ("worst_case_ambiguous", 0.0, True)):
        ev_s, ev_c = tally(P, ev_idx, spread, worst)
        ct_s, ct_c = tally(P, ct_idx, spread, worst)
        sd = stratified_diff(ev_s, ct_s)
        t_e = sum(v[0] for v in ev_s.values())
        n_e = sum(sum(v) for v in ev_s.values())
        t_c = sum(v[0] for v in ct_s.values())
        n_c = sum(sum(v) for v in ct_s.values())
        out[tag] = {"event_counts": ev_c, "control_counts": ct_c,
                    "p_event": (t_e / n_e) if n_e else None, "p_event_ci": wilson(t_e, n_e), "n_event_resolved": n_e,
                    "p_control": (t_c / n_c) if n_c else None, "p_control_ci": wilson(t_c, n_c), "n_control_resolved": n_c, **sd}
        if sd["diff"] is not None:
            out[tag]["diff_ci"] = (sd["diff"] - 1.96 * sd["se"], sd["diff"] + 1.96 * sd["se"])
    out["n_min_resolved"] = min(out["gross"]["n_event_resolved"], out["gross"]["n_control_resolved"])
    out["sample_label"] = sample_label(out["n_min_resolved"])
    if k == 1.5 and dev:                                    # net-of-spread variant + quarter stability (secondary)
        sp = SPREAD_PIPS * 1e-4
        ev_s, _ = tally(P, ev_idx, sp)
        ct_s, _ = tally(P, ct_idx, sp)
        out["net_spread_variant"] = {"spread_pips": SPREAD_PIPS, **{kk: v for kk, v in stratified_diff(ev_s, ct_s).items()}}
        q = {}
        for qn in sorted(set(P.quarter[ev_idx])):
            e_q = [i for i in ev_idx if P.quarter[i] == qn]
            c_q = [i for i in ct_idx if P.quarter[i] == qn]
            es, _ = tally(P, e_q)
            cs, _ = tally(P, c_q)
            sd = stratified_diff(es, cs)
            q[qn] = None if sd["diff"] is None else round(sd["diff"], 4)
        out["by_quarter_diff"] = q
        vals = [v for v in q.values() if v is not None]
        out["quarters_same_sign_as_overall"] = (
            sum(1 for v in vals if out["gross"]["diff"] is not None and v * out["gross"]["diff"] > 0), len(vals))
    return out


# ---------------------------------------------------------------------------
# B2
# ---------------------------------------------------------------------------
def run_b2(P, dev=True):
    ev_idx = P.episodes(P.ratio >= 1.0, dev)
    out = {"tf": P.tf, "episodes": int(len(ev_idx))}
    if len(ev_idx) < 50:
        out.update({"status": "DATA-LIMITED"})
        return out
    ratios = P.ratio[ev_idx]
    edges = np.quantile(ratios, [0.2, 0.4, 0.6, 0.8])
    grp = np.digitize(ratios, edges)
    groups, table = [], []
    for g in range(5):
        members = ev_idx[grp == g]
        _, counts = tally(P, members)
        t, s_ = counts[rm.TARGET_FIRST], counts[rm.STOP_FIRST]
        groups.append((t, s_))
        lo = float(ratios[grp == g].min()) if (grp == g).any() else None
        hi = float(ratios[grp == g].max()) if (grp == g).any() else None
        table.append({"quintile": g + 1, "size_x_ATR": (round(lo, 2), round(hi, 2)), "n_resolved": t + s_,
                      "p": round(t / (t + s_), 4) if (t + s_) else None, "ci": wilson(t, t + s_)})
    z, p = trend_test(groups)
    out.update({"quintiles": table, "trend_z": round(z, 3), "trend_p": p,
                "top_minus_bottom": (table[4]["p"] - table[0]["p"]) if table[4]["p"] is not None and table[0]["p"] is not None else None,
                "n_min_resolved": min(g[0] + g[1] for g in groups)})
    out["sample_label"] = sample_label(out["n_min_resolved"])
    return out


# ---------------------------------------------------------------------------
# B6
# ---------------------------------------------------------------------------
def run_b6(P, k=1.5, dev=True):
    ev_idx = P.episodes(P.ratio >= k, dev)
    cls = {"A_no_pullback": 0, "B_shallow": 0, "C_deep": 0, "D_failure": 0, "N_unresolved(neither)": 0,
           "excluded_ambiguous": 0, "excluded_data_gap": 0}
    bins = [0] * 9                                    # 0-10, ..., 70-80, 80+  (target-first events only)
    s = P.s
    for i in ev_idx:
        status, res = P.outcome(i, 0.0)
        if status == rm.DATA_GAP:
            cls["excluded_data_gap"] += 1
            continue
        if status == rm.AMBIGUOUS:
            cls["excluded_ambiguous"] += 1
            continue
        if status == rm.NEITHER:
            cls["N_unresolved(neither)"] += 1
            continue
        if status == rm.STOP_FIRST:
            cls["D_failure"] += 1
            continue
        d = int(P.dirn[i])
        end = res["bar"]
        rg_ = P.rng[i]
        depth = (s.c[i] - s.l[i + 1: end + 1].min()) / rg_ if d == 1 else (s.h[i + 1: end + 1].max() - s.c[i]) / rg_
        depth = max(0.0, float(depth))
        bins[min(8, int(depth * 10))] += 1
        key = "A_no_pullback" if depth < B6_SHALLOW else ("B_shallow" if depth < B6_DEEP else "C_deep")
        cls[key] += 1
    resolved = sum(cls[k_] for k_ in ("A_no_pullback", "B_shallow", "C_deep", "D_failure", "N_unresolved(neither)"))
    shares = {k_: {"n": v, "share": round(v / resolved, 4) if resolved else None, "ci": wilson(v, resolved)}
              for k_, v in cls.items() if not k_.startswith("excluded")}
    return {"tf": P.tf, "k": k, "episodes": int(len(ev_idx)), "classified": resolved, "classes": shares,
            "excluded": {"ambiguous": cls["excluded_ambiguous"], "data_gap": cls["excluded_data_gap"]},
            "target_first_depth_bins_10pct": bins,
            "definitions": "depth = max adverse move from the event close before the outcome, as a fraction of the displacement bar's range; "
                           f"A < {B6_SHALLOW}, B < {B6_DEEP}, C >= {B6_DEEP} (target first); D = stop first; N = neither within {HORIZON} bars",
            "sample_label": sample_label(resolved)}


# ---------------------------------------------------------------------------
# holdout ledger
# ---------------------------------------------------------------------------
def ledger_check_and_write(ledger_path, test, tf, k):
    seen = set()
    if os.path.exists(ledger_path):
        for line in open(ledger_path):
            if line.strip():
                r = json.loads(line)
                seen.add((r["test"], r["tf"], r["k"]))
    if (test, tf, k) in seen:
        raise RuntimeError(f"holdout already opened for {(test, tf, k)}; it is spent")
    os.makedirs(os.path.dirname(ledger_path) or ".", exist_ok=True)
    with open(ledger_path, "a") as f:
        f.write(json.dumps({"test": test, "tf": tf, "k": k, "contract": CONTRACT_HASH,
                            "opened_utc": pd.Timestamp.now(tz="UTC").isoformat()}) + "\n")


# ---------------------------------------------------------------------------
# run + report
# ---------------------------------------------------------------------------
def run_wave1(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", open_holdout=False, ledger=None, preloaded=None):
    clean = {}
    for tf in TFS:
        clean[tf] = preloaded[tf] if preloaded else rc.clean_timeframe(tf, raw_dir, source)[0]
    series5 = rm.Series(clean["5m"], 5)
    P = {}
    for tf in TFS:
        P[tf] = Prepared(tf, clean[tf], s_lo=series5 if tf != "5m" else None)
    # dataset status (A7) from the 15m inventory
    a6 = rg.gate_a6({"15m": clean["15m"]}, {"15m": rc.clean_timeframe("15m", raw_dir, source)[2] if not preloaded else
                                          {"first": None, "last": None, "raw_lines_parsed": 0, "unique_timestamps": 0, "duplicate_lines": 0,
                                           "conflicting_duplicates": 0, "dropped_market_closed": 0, "dropped_off_grid": 0,
                                           "dropped_invalid_ohlc": 0, "kept_bars": len(clean["15m"]), "unexplained_gaps": 0, "missing_open_bars": 0}},
                    {"15m": P["15m"].s})
    a7 = rg.gate_a7({"15m": clean["15m"]}, {"15m": P["15m"].s}, a6)["details"]
    status = a7["status"]
    dev = not open_holdout
    b1, b2, b6 = [], [], []
    for tf in TFS:
        for k in DISPLACEMENT_K:
            if open_holdout:
                ledger_check_and_write(ledger, "B1", tf, k)
            b1.append(run_b1(P[tf], k, status, dev))
        if open_holdout:
            ledger_check_and_write(ledger, "B2", tf, 1.0)
        b2.append(run_b2(P[tf], dev))
        b6.append(run_b6(P[tf], 1.5, dev))
    # FDR across all primary tests in this wave
    tests = [(("B1", r["tf"], r["k"]), r["gross"]["p"]) for r in b1 if r["gross"]["p"] is not None] + \
            [(("B2", r["tf"], 1.0), r["trend_p"]) for r in b2 if "trend_p" in r]
    reject, qv = bh_fdr([p for _, p in tests])
    fdr = {t: (rj, q) for (t, _), rj, q in zip(tests, reject, qv)}
    for r in b1:
        rj, q = fdr.get(("B1", r["tf"], r["k"]), (False, None))
        r["fdr_reject"], r["q_value"] = rj, q
        g = r["gross"]
        if g.get("diff") is not None:
            lo, hi = g["diff_ci"]
            r["verdict"] = verdict(g["diff"], lo, hi, rj, r["n_min_resolved"], status)
            w = r["worst_case_ambiguous"]
            if w.get("diff") is not None and g["diff"] * w["diff"] <= 0:
                r["verdict"] += " [FRAGILE: sign flips under worst-case ambiguity]"
        else:
            r["verdict"] = "DATA-LIMITED"
    for r in b2:
        if "trend_p" in r:
            rj, q = fdr.get(("B2", r["tf"], 1.0), (False, None))
            r["fdr_reject"], r["q_value"] = rj, q
    return {"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "a7": a7,
            "holdout_opened": open_holdout, "n_primary_tests": len(tests), "B1": b1, "B2": b2, "B6": b6}


def _pct(x):
    return "n/a" if x is None else f"{100 * x:+.1f}pp"


def render_markdown(r):
    L = []
    L.append("# Observatory — Wave 1 report (B1, B2, B6)\n")
    L.append(f"Contract hash `{r['contract_hash']}` · dataset status **{r['dataset_status']}** · "
             f"holdout opened: **{r['holdout_opened']}** · primary tests (FDR family): {r['n_primary_tests']}\n")
    L.append("**This is exploratory characterization of 12 months of GBPUSD, development data only. Nothing here is a trading rule.**\n")
    a7 = r["a7"]
    L.append(f"Data: {a7['calendar_months']} months · regime weeks {a7['regime_weeks']} · volatility terciles {a7['volatility_week_terciles']}\n")
    L.append("## B1 — does displacement predict continuation?\n")
    L.append("p = P(+1R before -1R) trading the displacement direction. diff = event minus matched ordinary bars (stratified). "
             "CI is 95%. Verdict uses the 5pp practical threshold and FDR across the wave.\n")
    for r_ in r["B1"]:
        g = r_["gross"]
        L.append(f"### {r_['tf']} · k ≥ {r_['k']} ATR")
        if g.get("diff") is None:
            L.append(f"- DATA-LIMITED (episodes: events {r_['episodes_events']}, controls {r_['episodes_controls']})\n")
            continue
        lo, hi = g["diff_ci"]
        L.append(f"- episodes: events {r_['episodes_events']}, controls {r_['episodes_controls']} · resolved: {g['n_event_resolved']} / {g['n_control_resolved']} · sample: {r_['sample_label']}")
        L.append(f"- p(event) {g['p_event']:.3f} {g['p_event_ci']} · p(control) {g['p_control']:.3f} {g['p_control_ci']}")
        L.append(f"- **diff {_pct(g['diff'])}** (CI {_pct(lo)} to {_pct(hi)}) · p={g['p']:.4f} · q={r_['q_value']:.4f} · matched share {g['matched_share']:.0%}")
        w = r_["worst_case_ambiguous"]
        L.append(f"- worst-case ambiguity diff {_pct(w['diff'])} · events: {g['event_counts']}")
        if "net_spread_variant" in r_ and r_["net_spread_variant"].get("diff") is not None:
            L.append(f"- net of {SPREAD_PIPS} pip spread diff {_pct(r_['net_spread_variant']['diff'])} · by quarter {r_['by_quarter_diff']} · same sign in {r_['quarters_same_sign_as_overall'][0]}/{r_['quarters_same_sign_as_overall'][1]} quarters")
        L.append(f"- **verdict: {r_['verdict']}**\n")
    L.append("## B2 — does continuation scale with displacement size?\n")
    for r_ in r["B2"]:
        L.append(f"### {r_['tf']} (k ≥ 1.0, quintiles by size)")
        if "quintiles" not in r_:
            L.append("- DATA-LIMITED\n")
            continue
        for q in r_["quintiles"]:
            L.append(f"- Q{q['quintile']} ({q['size_x_ATR'][0]}–{q['size_x_ATR'][1]}×ATR): p={q['p']} {q['ci']} n={q['n_resolved']}")
        L.append(f"- trend z={r_['trend_z']} p={r_['trend_p']:.4f} q={r_['q_value']:.4f} · top−bottom {_pct(r_['top_minus_bottom'])}\n")
    L.append("## B6 — what happens after a displacement (k ≥ 1.5)?\n")
    for r_ in r["B6"]:
        L.append(f"### {r_['tf']} · episodes {r_['episodes']} · classified {r_['classified']}")
        for name, v in r_["classes"].items():
            L.append(f"- {name}: {v['n']} ({v['share']:.1%}) {v['ci']}")
        L.append(f"- target-first depth bins (10% steps, last = 80%+): {r_['target_first_depth_bins_10pct']}")
        L.append(f"- excluded: {r_['excluded']}\n")
    L.append("## Not run in this wave\n- T4–T7 (higher-timeframe context): needs a frozen HTF-direction definition.\n"
             "- Holdout: sealed.\n- FVG / BOS / CHoCH / IFVG / premium-discount: later waves.\n")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Observatory Wave 1 (B1, B2, B6). Read-only on raw data.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--open-holdout", action="store_true", help="spends the single-use holdout for these tests")
    ap.add_argument("--ledger", default="observatory_data/holdout_ledger.jsonl")
    a = ap.parse_args()
    res = run_wave1(a.raw_dir, a.source, a.open_holdout, a.ledger)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "wave1_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "wave1_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
