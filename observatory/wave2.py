"""
observatory/wave2.py
====================
OBSERVATORY — Wave 2: Fair Value Gap experiments. ISOLATED from the live bot.

  C1  Does an FVG add information beyond the displacement that created it?
  C5  Among FVGs, does gap size matter? (dose-response)
  C7  Immediate entry vs waiting for the retest, counted over ALL eligible FVGs.

The FVG is built here from raw candles with a frozen RESEARCH definition. It is NOT the
scanner's "significant FVG" and uses no scanner code or logs.

FROZEN CONTRACT (hash printed in every report)
----------------------------------------------
  FVG            3 consecutive, gap-free candles (i-2, i-1, i). Bullish: low[i] > high[i-2],
                 zone = [high[i-2], low[i]]. Bearish: high[i] < low[i-2], zone = [high[i], low[i-2]].
                 No size filter (raw). Known at the CLOSE of bar i. No weekend/closure/data gap inside.
  Displacement   the MIDDLE candle: range[i-1] / ATR14(before i-1) >= k, k in {1.0, 1.5, 2.0}.
                 Its direction = trade direction (continuation).
  Group A (FVG)  displacement + FVG in the same direction.
  Group B (none) displacement + NO FVG at bar i (the control: same middle candle, no gap).
  C1             stratified (direction x session x ATR tercile x displacement-size tercile)
                 difference in P(+1R first) = A - B. Entry = open of bar i+1, R = ATR14 at bar i,
                 target +1R, stop -1R, horizon 24 bars, first-touch (ambiguous 15m/1h bars settled
                 with 5m where possible; worst case also run).
  C5             group A at k >= 1.0, quintiles of gap width / ATR(i); Cochran-Armitage trend.
  C7             group A at k >= 1.5. IMMEDIATE = enter at the open of bar i+1. RETEST (primary) =
                 wait for price to come back and TOUCH the zone edge nearest price (bull: low[i]; bear:
                 high[i]) within 24 bars, then enter at the OPEN of the bar AFTER the touch bar (the
                 touch bar has closed, so everything is knowable). Same R-unit (ATR at i), +-1R, 24-bar
                 horizon from that entry, first-touch. Unfilled = missed. Market outcome of every event is
                 stored; a missed entry is accounted as 0 R ONLY for the execution comparison.
                 Primary metric: paired mean R per eligible event, retest minus immediate. Min effect 0.10 R.
                 SECONDARY (bounds only, not a point estimate): a LIMIT order at the zone edge. Where price
                 went inside the touch bar is unknowable from OHLC, so it is bracketed by an optimistic rule
                 (nothing in the touch bar counts) and a pessimistic rule (a stop in the touch bar counts,
                 a target does not). On random data the primary rule is unbiased; the two limit rules are
                 biased in opposite directions, which is why they are bounds.
  Episodes       K = 12 bars, anchored, per direction, per group. Same as Wave 1.
  Split          the frozen ABSOLUTE holdout date (clean.HOLDOUT_START_UTC); development events are
                 purged so their outcome windows end before it. Holdout stays SEALED.
  Multiple tests Benjamini-Hochberg across C1 (9) + C5 (3) + C7 (3) = 15 primary tests, q = 0.10.
  Timeframes     5m, 15m, 1h run separately. Higher-timeframe context (T4-T7) NOT in this wave.

Run:  python3 -m observatory.wave2                (real data)
      python3 -m observatory.wave2 --selftest     (hand-built paths + random-walk null check)
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

HORIZON = w1.HORIZON
K_EPISODE = w1.K_EPISODE
DISPLACEMENT_K = w1.DISPLACEMENT_K
MIN_EFFECT_P = 0.05
MIN_EFFECT_R = 0.10
FDR_Q = w1.FDR_Q
SPREAD_PIPS = w1.SPREAD_PIPS
C7_K = 1.5
TFS = w1.TFS

CONTRACT = {
    "wave": 2, "instrument": "GBPUSD", "timeframes": list(TFS), "fvg_definition": "raw 3-candle gap, no size filter",
    "displacement_k": list(DISPLACEMENT_K), "C7_k": C7_K, "K_episode": K_EPISODE, "horizon_bars": HORIZON,
    "R_unit": "ATR14 at event bar i", "target_R": 1.0, "stop_R": 1.0, "retest_window_bars": HORIZON,
    "retest_entry_rule": "primary=next open after the touch bar; secondary bounds=limit order (start_next_bar / stop_counts)",
    "min_effect_probability": MIN_EFFECT_P, "min_effect_R": MIN_EFFECT_R, "fdr_q": FDR_Q,
    "holdout_start_utc": str(rc.HOLDOUT_START_UTC), "purge_bars": HORIZON + 1, "spread_pips_net_variant": SPREAD_PIPS,
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# FVG detection (research definition)
# ---------------------------------------------------------------------------
def fvg_arrays(s):
    """Returns (fvg_dir[+1 bull / -1 bear / 0], gap_lo, gap_hi, contiguous). Index = bar i (third candle)."""
    n = s.n
    bull = np.zeros(n, bool)
    bear = np.zeros(n, bool)
    bull[2:] = s.l[2:] > s.h[:-2]
    bear[2:] = s.h[2:] < s.l[:-2]
    contig = np.zeros(n, bool)
    contig[2:] = (s.gap[1:-1] == 0) & (s.gap[2:] == 0) & (~s.closure[1:-1]) & (~s.closure[2:])
    gap_lo = np.full(n, np.nan)
    gap_hi = np.full(n, np.nan)
    gap_lo[2:] = np.where(bull[2:], s.h[:-2], np.where(bear[2:], s.h[2:], np.nan))
    gap_hi[2:] = np.where(bull[2:], s.l[2:], np.where(bear[2:], s.l[:-2], np.nan))
    fvg_dir = np.where(bull, 1, np.where(bear, -1, 0))
    fvg_dir = np.where(contig, fvg_dir, 0)
    return fvg_dir, gap_lo, gap_hi, contig


class Ctx:
    """Wave-2 view over a wave1.Prepared object."""

    def __init__(self, P):
        self.P = P
        s = P.s
        self.fvg_dir, self.gap_lo, self.gap_hi, self.contig = fvg_arrays(s)
        n = P.n
        self.ratio_mid = np.full(n, np.nan)
        self.ratio_mid[1:] = P.ratio[:-1]
        self.dir_mid = np.zeros(n, int)
        self.dir_mid[1:] = P.dirn[:-1]
        self.ok_base = self.contig & np.isfinite(s.atr) & (np.arange(n) >= 16) & (self.dir_mid != 0) & np.isfinite(self.ratio_mid)
        self.gap_width_atr = np.where(np.isfinite(self.gap_hi) & np.isfinite(s.atr) & (s.atr > 0),
                                      np.abs(self.gap_hi - self.gap_lo) / s.atr, np.nan)

    def groups(self, k):
        ok = self.ok_base & (self.ratio_mid >= k)
        a = ok & (self.fvg_dir == self.dir_mid)
        b = ok & (self.fvg_dir == 0)
        return a, b

    def pick(self, mask, dev=True):
        P = self.P
        idx = np.where(mask)[0]
        idx = idx[idx < P.cut] if dev else idx[idx >= P.hold_start]
        if len(idx) == 0:
            return idx
        _, kept = rm.collapse_episodes(idx, self.dir_mid[idx], K_EPISODE)
        return idx[kept]


def _tally(P, idxs, dirs, strat, spread=0.0, worst=False):
    strata, counts = {}, {rm.TARGET_FIRST: 0, rm.STOP_FIRST: 0, rm.NEITHER: 0, rm.AMBIGUOUS: 0, rm.DATA_GAP: 0}
    for i in idxs:
        d = int(dirs[i])
        st, _ = P.outcome_dir(i, d, spread)
        counts[st] += 1
        if st == rm.AMBIGUOUS and worst:
            st = rm.STOP_FIRST
        if st in (rm.TARGET_FIRST, rm.STOP_FIRST):
            cell = strata.setdefault(strat(i), [0, 0])
            cell[0 if st == rm.TARGET_FIRST else 1] += 1
    return {k: tuple(v) for k, v in strata.items()}, counts


# ---------------------------------------------------------------------------
# C1
# ---------------------------------------------------------------------------
def run_c1(C, k, status, dev=True):
    P = C.P
    a_mask, b_mask = C.groups(k)
    a_idx, b_idx = C.pick(a_mask, dev), C.pick(b_mask, dev)
    dev_all = np.where((a_mask | b_mask) & (np.arange(P.n) < P.cut))[0]
    edges = np.quantile(C.ratio_mid[dev_all], [1 / 3, 2 / 3]) if len(dev_all) >= 30 else np.array([np.inf, np.inf])

    def strat(i):
        return (int(C.dir_mid[i]), int(P.session[i]), int(P.atrb[i]), int(np.digitize(C.ratio_mid[i], edges)))
    out = {"tf": P.tf, "k": k, "episodes_A_fvg": int(len(a_idx)), "episodes_B_no_fvg": int(len(b_idx))}
    total_disp = int(((a_mask | b_mask) & (np.arange(P.n) < P.cut)).sum())
    out["share_of_displacements_with_fvg_(raw_bars)"] = round(float((a_mask & (np.arange(P.n) < P.cut)).sum() / total_disp), 4) if total_disp else None
    for tag, spread, worst in (("gross", 0.0, False), ("worst_case_ambiguous", 0.0, True)):
        sa, ca = _tally(P, a_idx, C.dir_mid, strat, spread, worst)
        sb, cb = _tally(P, b_idx, C.dir_mid, strat, spread, worst)
        sd = w1.stratified_diff(sa, sb)
        ta, na = sum(v[0] for v in sa.values()), sum(sum(v) for v in sa.values())
        tb, nb = sum(v[0] for v in sb.values()), sum(sum(v) for v in sb.values())
        out[tag] = {"counts_A": ca, "counts_B": cb, "p_A": (ta / na) if na else None, "p_A_ci": w1.wilson(ta, na), "n_A": na,
                    "p_B": (tb / nb) if nb else None, "p_B_ci": w1.wilson(tb, nb), "n_B": nb, **sd}
        if sd["diff"] is not None:
            out[tag]["diff_ci"] = (sd["diff"] - 1.96 * sd["se"], sd["diff"] + 1.96 * sd["se"])
    out["n_min_resolved"] = min(out["gross"]["n_A"], out["gross"]["n_B"])
    out["sample_label"] = w1.sample_label(out["n_min_resolved"])
    if k == 1.5 and dev and out["gross"].get("diff") is not None:
        q = {}
        qa = np.asarray(P.quarter)
        for qn in sorted(set(qa[a_idx])):
            ea = [i for i in a_idx if qa[i] == qn]
            eb = [i for i in b_idx if qa[i] == qn]
            sa, _ = _tally(P, ea, C.dir_mid, strat)
            sb, _ = _tally(P, eb, C.dir_mid, strat)
            sd = w1.stratified_diff(sa, sb)
            q[qn] = None if sd["diff"] is None else round(sd["diff"], 4)
        vals = [v for v in q.values() if v is not None]
        out["by_quarter_diff"] = q
        out["quarters_same_sign_as_overall"] = (sum(1 for v in vals if v * out["gross"]["diff"] > 0), len(vals))
    return out


# ---------------------------------------------------------------------------
# C5
# ---------------------------------------------------------------------------
def run_c5(C, dev=True):
    P = C.P
    a_mask, _ = C.groups(1.0)
    idx = C.pick(a_mask, dev)
    out = {"tf": P.tf, "episodes": int(len(idx))}
    idx = idx[np.isfinite(C.gap_width_atr[idx])]
    if len(idx) < 50:
        out["status"] = "DATA-LIMITED"
        return out
    w = C.gap_width_atr[idx]
    edges = np.quantile(w, [0.2, 0.4, 0.6, 0.8])
    grp = np.digitize(w, edges)
    groups, table = [], []
    for g in range(5):
        members = idx[grp == g]
        _, cnt = _tally(P, members, C.dir_mid, lambda i: 0)
        t, s_ = cnt[rm.TARGET_FIRST], cnt[rm.STOP_FIRST]
        groups.append((t, s_))
        table.append({"quintile": g + 1, "gap_x_ATR": (round(float(w[grp == g].min()), 3), round(float(w[grp == g].max()), 3)),
                      "n_resolved": t + s_, "p": round(t / (t + s_), 4) if t + s_ else None, "ci": w1.wilson(t, t + s_)})
    z, p = w1.trend_test(groups)
    out.update({"quintiles": table, "trend_z": round(z, 3), "trend_p": p,
                "top_minus_bottom": (table[4]["p"] - table[0]["p"]) if table[4]["p"] is not None and table[0]["p"] is not None else None,
                "n_min_resolved": min(g[0] + g[1] for g in groups)})
    out["sample_label"] = w1.sample_label(out["n_min_resolved"])
    return out


# ---------------------------------------------------------------------------
# C7
# ---------------------------------------------------------------------------
def retest_result(s, i, d, entry_limit, r_unit, spread_price=0.0, window=HORIZON, horizon=HORIZON, fill_bar_rule="next_open_after_touch"):
    """
    Limit order at entry_limit, valid for bars i+1..i+window. Returns dict:
      status: FILLED / MISSED / EXCLUDED, R (gross, +1/-1/0) when FILLED, outcome, fill_bar, reason.
    fill_bar_rule:
      "next_open_after_touch" (PRIMARY): enter at the open of the bar after the touch bar; unbiased.
      "start_next_bar" / "stop_counts": limit order at entry_limit; bounds only (see module docstring).
    """
    n = s.n
    last_fill = i + window
    if last_fill >= n:
        return {"status": "EXCLUDED", "reason": "end_of_data"}
    fill = None
    for j in range(i + 1, last_fill + 1):
        if s.gap[j] > 0:
            return {"status": "EXCLUDED", "reason": "gap_before_fill"}
        touched = (s.l[j] <= entry_limit) if d == 1 else (s.h[j] >= entry_limit)
        if touched:
            fill = j
            break
    if fill is None:
        return {"status": "MISSED"}
    if fill_bar_rule == "next_open_after_touch":
        j0 = fill + 1
        end = j0 + horizon - 1
        if end >= n:
            return {"status": "EXCLUDED", "reason": "end_of_data_after_fill"}
        entry = float(s.o[j0]) + d * spread_price
        stop = entry - d * r_unit
        target = entry + d * r_unit
        for j in range(j0, end + 1):
            if s.gap[j] > 0:
                return {"status": "EXCLUDED", "reason": "gap_after_fill"}
            hit_t = (s.h[j] >= target) if d == 1 else (s.l[j] <= target)
            hit_s = (s.l[j] <= stop) if d == 1 else (s.h[j] >= stop)
            if hit_t and hit_s:
                return {"status": "FILLED", "outcome": rm.AMBIGUOUS, "R": None, "fill_bar": fill, "entry": entry}
            if hit_t:
                return {"status": "FILLED", "outcome": rm.TARGET_FIRST, "R": 1.0, "fill_bar": fill, "entry": entry}
            if hit_s:
                return {"status": "FILLED", "outcome": rm.STOP_FIRST, "R": -1.0, "fill_bar": fill, "entry": entry}
        return {"status": "FILLED", "outcome": rm.NEITHER, "R": 0.0, "fill_bar": fill, "entry": entry}
    entry = entry_limit + d * spread_price
    stop = entry - d * r_unit
    target = entry + d * r_unit
    stopped_in_fill_bar = (s.l[fill] <= stop) if d == 1 else (s.h[fill] >= stop)
    if fill_bar_rule == "stop_counts" and stopped_in_fill_bar:
        return {"status": "FILLED", "outcome": rm.STOP_FIRST, "R": -1.0, "fill_bar": fill, "entry": entry}
    end = fill + horizon
    if end >= n:
        return {"status": "EXCLUDED", "reason": "end_of_data_after_fill"}
    for j in range(fill + 1, end + 1):
        if s.gap[j] > 0:
            return {"status": "EXCLUDED", "reason": "gap_after_fill"}
        hit_t = (s.h[j] >= target) if d == 1 else (s.l[j] <= target)
        hit_s = (s.l[j] <= stop) if d == 1 else (s.h[j] >= stop)
        if hit_t and hit_s:
            return {"status": "FILLED", "outcome": rm.AMBIGUOUS, "R": None, "fill_bar": fill, "entry": entry}
        if hit_t:
            return {"status": "FILLED", "outcome": rm.TARGET_FIRST, "R": 1.0, "fill_bar": fill, "entry": entry}
        if hit_s:
            return {"status": "FILLED", "outcome": rm.STOP_FIRST, "R": -1.0, "fill_bar": fill, "entry": entry}
    return {"status": "FILLED", "outcome": rm.NEITHER, "R": 0.0, "fill_bar": fill, "entry": entry}


def _r_of(status):
    return {rm.TARGET_FIRST: 1.0, rm.STOP_FIRST: -1.0, rm.NEITHER: 0.0}.get(status)


def run_c7(C, k=C7_K, dev=True, spread_pips=0.0, worst=False, fill_bar_rule="next_open_after_touch"):
    P, s = C.P, C.P.s
    a_mask, _ = C.groups(k)
    idx = C.pick(a_mask, dev)
    sp = spread_pips * 1e-4
    rows = []
    excl = {"immediate_ambiguous_or_gap": 0, "retest_excluded": 0, "retest_ambiguous": 0}
    for i in idx:
        d = int(C.dir_mid[i])
        st_imm, res_imm = P.outcome_dir(i, d, sp)
        if st_imm == rm.AMBIGUOUS and worst:
            st_imm = rm.STOP_FIRST
        r_imm = _r_of(st_imm)
        if r_imm is None:
            excl["immediate_ambiguous_or_gap"] += 1
            continue
        r_unit = float(s.atr[i])
        limit = float(C.gap_hi[i] if d == 1 else C.gap_lo[i])
        rr = retest_result(s, i, d, limit, r_unit, sp, fill_bar_rule=fill_bar_rule)
        if rr["status"] == "EXCLUDED":
            excl["retest_excluded"] += 1
            continue
        if rr["status"] == "FILLED" and rr["outcome"] == rm.AMBIGUOUS:
            if worst:
                rr["R"] = -1.0
            else:
                excl["retest_ambiguous"] += 1
                continue
        if rr["status"] == "MISSED":
            r_ret, filled, improve = 0.0, False, None
        else:
            r_ret, filled = rr["R"], True
            improve = (float(s.o[i + 1]) - float(rr["entry"] - d * sp)) * d / r_unit      # >0: the retest entry price was better than the immediate one
        # Spread is applied ONCE, by shifting the entry price inside the first-touch logic (long pays +spread,
        # short -spread). R is therefore not reduced a second time here.
        rows.append({"i": int(i), "d": d, "filled": filled, "r_imm": r_imm, "r_ret": r_ret if filled else 0.0,
                     "market_outcome": st_imm, "improve": improve})
    n = len(rows)
    out = {"tf": P.tf, "k": k, "fill_bar_rule": fill_bar_rule, "events_considered": int(len(idx)), "eligible": n, "excluded": excl}
    if n < 30:
        out["status"] = "DATA-LIMITED"
        return out
    r_imm = np.array([r["r_imm"] for r in rows])
    r_ret = np.array([r["r_ret"] for r in rows])
    filled = np.array([r["filled"] for r in rows])
    diff = r_ret - r_imm
    m, sd = float(diff.mean()), float(diff.std(ddof=1))
    se = sd / math.sqrt(n)
    z = m / se if se > 0 else 0.0
    missed = ~filled
    mk = [rows[j]["market_outcome"] for j in np.where(missed)[0]]
    out.update({
        "mean_R_immediate": float(r_imm.mean()), "mean_R_retest": float(r_ret.mean()),
        "diff_retest_minus_immediate": m, "diff_ci": (m - 1.96 * se, m + 1.96 * se), "se": se, "z": z, "p": w1.norm_p(z),
        "fill_rate": float(filled.mean()), "missed_n": int(missed.sum()),
        "missed_market_outcomes": {"TARGET_FIRST": mk.count(rm.TARGET_FIRST), "STOP_FIRST": mk.count(rm.STOP_FIRST), "NEITHER": mk.count(rm.NEITHER)},
        "missed_immediate_mean_R": float(r_imm[missed].mean()) if missed.any() else None,
        "filled_immediate_mean_R": float(r_imm[filled].mean()) if filled.any() else None,
        "filled_retest_mean_R": float(r_ret[filled].mean()) if filled.any() else None,
        "mean_entry_improvement_R_when_filled": float(np.mean([r["improve"] for r in rows if r["improve"] is not None])) if filled.any() else None,
        "n_min_resolved": n, "sample_label": w1.sample_label(n),
    })
    return out


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def run_wave2(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", open_holdout=False, ledger=None, preloaded=None):
    clean = {tf: (preloaded[tf] if preloaded else rc.clean_timeframe(tf, raw_dir, source)[0]) for tf in TFS}
    s5 = rm.Series(clean["5m"], 5)
    Ps = {tf: w1.Prepared(tf, clean[tf], s_lo=s5 if tf != "5m" else None) for tf in TFS}
    Cs = {tf: Ctx(Ps[tf]) for tf in TFS}
    rep15 = rc.clean_timeframe("15m", raw_dir, source)[2] if not preloaded else {
        "first": None, "last": None, "raw_lines_parsed": 0, "unique_timestamps": 0, "duplicate_lines": 0, "conflicting_duplicates": 0,
        "dropped_market_closed": 0, "dropped_off_grid": 0, "dropped_invalid_ohlc": 0, "kept_bars": len(clean["15m"]),
        "unexplained_gaps": 0, "missing_open_bars": 0}
    a6 = rg.gate_a6({"15m": clean["15m"]}, {"15m": rep15}, {"15m": Ps["15m"].s})
    a7 = rg.gate_a7({"15m": clean["15m"]}, {"15m": Ps["15m"].s}, a6)["details"]
    status, dev = a7["status"], not open_holdout
    c1, c5, c7, c7n, c7lo, c7lp = [], [], [], [], [], []
    for tf in TFS:
        C = Cs[tf]
        for k in DISPLACEMENT_K:
            if open_holdout:
                w1.ledger_check_and_write(ledger, "C1", tf, k)
            c1.append(run_c1(C, k, status, dev))
        if open_holdout:
            w1.ledger_check_and_write(ledger, "C5", tf, 1.0)
            w1.ledger_check_and_write(ledger, "C7", tf, C7_K)
        c5.append(run_c5(C, dev))
        c7.append(run_c7(C, C7_K, dev))
        c7n.append(run_c7(C, C7_K, dev, spread_pips=SPREAD_PIPS))
        c7lo.append(run_c7(C, C7_K, dev, fill_bar_rule="start_next_bar"))
        c7lp.append(run_c7(C, C7_K, dev, fill_bar_rule="stop_counts"))
    tests = [(("C1", r["tf"], r["k"]), r["gross"]["p"]) for r in c1 if r["gross"].get("p") is not None] + \
            [(("C5", r["tf"], 1.0), r["trend_p"]) for r in c5 if "trend_p" in r] + \
            [(("C7", r["tf"], C7_K), r["p"]) for r in c7 if "p" in r]
    reject, qv = w1.bh_fdr([p for _, p in tests])
    fdr = {t: (rj, q) for (t, _), rj, q in zip(tests, reject, qv)}
    for r in c1:
        rj, q = fdr.get(("C1", r["tf"], r["k"]), (False, None))
        r["fdr_reject"], r["q_value"] = rj, q
        g = r["gross"]
        if g.get("diff") is not None:
            lo, hi = g["diff_ci"]
            r["verdict"] = w1.verdict(g["diff"], lo, hi, rj, r["n_min_resolved"], status, MIN_EFFECT_P)
            wc = r["worst_case_ambiguous"]
            if wc.get("diff") is not None and g["diff"] * wc["diff"] <= 0:
                r["verdict"] += " [FRAGILE: sign flips under worst-case ambiguity]"
        else:
            r["verdict"] = "DATA-LIMITED"
    for r in c5:
        if "trend_p" in r:
            r["fdr_reject"], r["q_value"] = fdr.get(("C5", r["tf"], 1.0), (False, None))
    for r in c7:
        if "p" in r:
            rj, q = fdr.get(("C7", r["tf"], C7_K), (False, None))
            r["fdr_reject"], r["q_value"] = rj, q
            lo, hi = r["diff_ci"]
            r["verdict"] = w1.verdict(r["diff_retest_minus_immediate"], lo, hi, rj, r["n_min_resolved"], status, MIN_EFFECT_R)
        else:
            r["verdict"] = "DATA-LIMITED"
    return {"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "a7": a7, "holdout_opened": open_holdout,
            "n_primary_tests": len(tests), "C1": c1, "C5": c5, "C7": c7, "C7_net_of_spread": c7n, "C7_limit_optimistic": c7lo, "C7_limit_pessimistic": c7lp}


def _pp(x):
    return "n/a" if x is None else f"{100 * x:+.1f}pp"


def _r(x):
    return "n/a" if x is None else f"{x:+.3f}R"


def render_markdown(r):
    L = ["# Observatory — Wave 2 report (FVG: C1, C5, C7)\n",
         f"Contract hash `{r['contract_hash']}` · dataset status **{r['dataset_status']}** · holdout opened: **{r['holdout_opened']}** · "
         f"primary tests (FDR family): {r['n_primary_tests']}\n",
         "**Development data only. FVG is built from raw candles with a frozen research definition (no size filter). Nothing here is a trading rule.**\n",
         "## C1 — does an FVG add information beyond the displacement that created it?\n",
         "Group A = displacement whose next candle left an FVG. Group B = the same kind of displacement with NO FVG. "
         "p = P(+1R before −1R), trading the displacement direction from the next open. diff = A − B, stratified by "
         "direction × session × volatility × displacement size. 95% CI.\n"]
    for r_ in r["C1"]:
        g = r_["gross"]
        L.append(f"### {r_['tf']} · displacement ≥ {r_['k']} ATR")
        if g.get("diff") is None:
            L.append(f"- DATA-LIMITED (A {r_['episodes_A_fvg']}, B {r_['episodes_B_no_fvg']})\n")
            continue
        lo, hi = g["diff_ci"]
        L.append(f"- episodes: A (FVG) {r_['episodes_A_fvg']}, B (none) {r_['episodes_B_no_fvg']} · share of displacement bars followed by an FVG: {r_['share_of_displacements_with_fvg_(raw_bars)']:.1%} · sample: {r_['sample_label']}")
        L.append(f"- p(A) {g['p_A']:.3f} {g['p_A_ci']} · p(B) {g['p_B']:.3f} {g['p_B_ci']}")
        L.append(f"- **diff {_pp(g['diff'])}** (CI {_pp(lo)} to {_pp(hi)}) · p={g['p']:.4f} · q={r_['q_value']:.4f} · matched share {g['matched_share']:.0%}")
        L.append(f"- worst-case ambiguity diff {_pp(r_['worst_case_ambiguous']['diff'])}")
        if "by_quarter_diff" in r_:
            L.append(f"- by quarter {r_['by_quarter_diff']} · same sign as overall in {r_['quarters_same_sign_as_overall'][0]}/{r_['quarters_same_sign_as_overall'][1]}")
        L.append(f"- **verdict: {r_['verdict']}**\n")
    L.append("## C5 — does FVG size matter? (displacement ≥ 1.0 ATR, gap width ÷ ATR in quintiles)\n")
    for r_ in r["C5"]:
        L.append(f"### {r_['tf']}")
        if "quintiles" not in r_:
            L.append("- DATA-LIMITED\n")
            continue
        for q in r_["quintiles"]:
            L.append(f"- Q{q['quintile']} (gap {q['gap_x_ATR'][0]}–{q['gap_x_ATR'][1]}×ATR): p={q['p']} {q['ci']} n={q['n_resolved']}")
        L.append(f"- trend z={r_['trend_z']} p={r_['trend_p']:.4f} q={r_['q_value']:.4f} · top−bottom {_pp(r_['top_minus_bottom'])}\n")
    L.append("## C7 — immediate entry vs waiting for the retest (all eligible FVGs, displacement ≥ 1.5 ATR)\n")
    L.append("Immediate = enter at the next open. Retest = wait up to 24 bars for price to touch the zone edge, then enter at the open of the bar after "
             "the touch; unfilled = missed (0 R **for this comparison only**; the market outcome of every missed event is kept below).\n")
    for r_, rn, ro, rp in zip(r["C7"], r["C7_net_of_spread"], r["C7_limit_optimistic"], r["C7_limit_pessimistic"]):
        L.append(f"### {r_['tf']}")
        if "diff_retest_minus_immediate" not in r_:
            L.append(f"- DATA-LIMITED (eligible {r_['eligible']})\n")
            continue
        lo, hi = r_["diff_ci"]
        L.append(f"- eligible events {r_['eligible']} (excluded {r_['excluded']}) · retest fill rate {r_['fill_rate']:.1%} · missed {r_['missed_n']}")
        L.append(f"- mean R per eligible event: immediate {_r(r_['mean_R_immediate'])} · retest {_r(r_['mean_R_retest'])}")
        L.append(f"- **retest − immediate {_r(r_['diff_retest_minus_immediate'])}** (CI {_r(lo)} to {_r(hi)}) · p={r_['p']:.4f} · q={r_['q_value']:.4f}")
        L.append(f"- the missed events (price never came back): immediate would have averaged {_r(r_['missed_immediate_mean_R'])}; market outcomes {r_['missed_market_outcomes']}")
        L.append(f"- among filled: immediate {_r(r_['filled_immediate_mean_R'])} vs retest {_r(r_['filled_retest_mean_R'])} · mean entry improvement {_r(r_['mean_entry_improvement_R_when_filled'])}")
        if "diff_retest_minus_immediate" in rn:
            L.append(f"- net of {SPREAD_PIPS} pip spread: immediate {_r(rn['mean_R_immediate'])} · retest {_r(rn['mean_R_retest'])} · diff {_r(rn['diff_retest_minus_immediate'])}")
        if "diff_retest_minus_immediate" in rp and "diff_retest_minus_immediate" in ro:
            L.append(f"- limit-order variant (bounds only, price path inside the touch bar is unknowable): diff between {_r(rp['diff_retest_minus_immediate'])} (pessimistic) and {_r(ro['diff_retest_minus_immediate'])} (optimistic)")
        L.append(f"- **verdict: {r_['verdict']}**\n")
    L.append("## Not run in this wave\n- FVG vs placebo zones (C2), revisit probability (C3), higher-timeframe context (T4–T7).\n- Holdout: sealed.\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _mk(bars, step=5):
    t0 = pd.Timestamp("2026-03-10 00:00", tz="UTC")
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"],
                      index=pd.DatetimeIndex([t0 + pd.Timedelta(minutes=step * i) for i in range(len(bars))]))
    df["gap_before_missing"], df["closure_before"] = 0, False
    ser = rm.Series(df, step)
    ser.gap, ser.closure = ser.gap.copy(), ser.closure.copy()      # writable copies so tests can inject gaps
    return ser


def selftest(null_series=25):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    flat = [(100, 100.2, 99.8, 100)] * 3
    # --- FVG detection
    bars = flat + [(100, 100.3, 99.9, 100.2), (100.2, 102.0, 100.2, 101.9), (101.9, 102.5, 101.0, 102.2)]   # i=5: low 101.0 > high[3]=100.3 -> bull FVG
    s = _mk(bars)
    fd, lo, hi, _ = fvg_arrays(s)
    chk("bull FVG at i=5", int(fd[5]), 1)
    chk("bull zone lo = high[i-2]", float(lo[5]), 100.3)
    chk("bull zone hi = low[i]", float(hi[5]), 101.0)
    bars2 = flat + [(100, 100.3, 99.9, 100.2), (100.2, 102.0, 100.2, 101.9), (101.9, 102.5, 100.3, 102.2)]  # low == high[i-2] -> no gap
    chk("touching is not a gap", int(fvg_arrays(_mk(bars2))[0][5]), 0)
    bars3 = flat + [(100, 100.1, 99.7, 99.8), (99.8, 99.8, 98.0, 98.1), (98.1, 99.0, 97.6, 97.9)]            # high[5]=99.0 < low[3]=99.7 -> bear FVG
    fd3, lo3, hi3, _ = fvg_arrays(_mk(bars3))
    chk("bear FVG at i=5", int(fd3[5]), -1)
    chk("bear zone lo = high[i]", float(lo3[5]), 99.0)
    chk("bear zone hi = low[i-2]", float(hi3[5]), 99.7)
    s_gap = _mk(bars)
    s_gap.gap[5] = 3
    chk("data gap inside blocks FVG", int(fvg_arrays(s_gap)[0][5]), 0)
    s_clo = _mk(bars)
    s_clo.closure[4] = True
    chk("closure inside blocks FVG", int(fvg_arrays(s_clo)[0][5]), 0)
    # --- retest mechanics (long, i=0, limit 101, R=1 -> stop 100, target 102)
    FAR = (103, 104, 102.5, 103.5)          # never touches the limit, always above target
    MID = (101.5, 101.8, 100.5, 101.0)      # inside the band: touches the limit, never hits stop/target

    def rt(rest, filler=MID, rule="start_next_bar", window=HORIZON, spread=0.0):   # limit-order variants unless rule is passed
        s_ = _mk([(100, 100.5, 99.5, 100)] + rest + [filler] * 60)
        return retest_result(s_, 0, 1, 101.0, 1.0, spread, window, fill_bar_rule=rule)
    far = [FAR] * 5
    chk("never returns -> MISSED", rt(far, filler=FAR)["status"], "MISSED")
    r = rt(far + [(103, 103.5, 100.9, 101.5), (101.5, 102.5, 101.2, 102.3)])
    chk("fill then target next bar", (r["status"], r["outcome"], r["R"]), ("FILLED", rm.TARGET_FIRST, 1.0))
    r = rt(far + [(103, 103.5, 99.5, 100.5)], rule="stop_counts")
    chk("pessimistic: stop in the fill bar counts", (r["outcome"], r["R"]), (rm.STOP_FIRST, -1.0))
    r = rt(far + [(103, 103.5, 99.5, 100.5)], rule="start_next_bar")
    chk("primary: nothing in the fill bar counts", r["outcome"], rm.NEITHER)
    r = rt(far + [(103, 102.6, 100.9, 101.5)], rule="stop_counts")
    chk("pessimistic: target in fill bar not counted", r["outcome"], rm.NEITHER)
    r = rt(far + [(103, 103.5, 100.9, 101.5), (101.5, 102.5, 99.8, 100.5)])
    chk("both in the next bar -> ambiguous", r["outcome"], rm.AMBIGUOUS)
    r = rt(far + [(103, 103.5, 100.9, 101.5), (101.5, 101.8, 99.9, 100.2)])
    chk("fill then stop next bar", r["outcome"], rm.STOP_FIRST)
    chk("fill price is the zone edge", rt(far + [(103, 103.5, 100.9, 101.5)])["entry"], 101.0)
    chk("outside the window -> missed", rt([FAR] * 30 + [(103, 103.5, 100.9, 101.5)], filler=FAR, window=24)["status"], "MISSED")
    sg = _mk([(100, 100.5, 99.5, 100)] + far + [(103, 103.5, 100.9, 101.5)] + [MID] * 60)
    sg.gap[3] = 4
    chk("gap before the fill excluded", retest_result(sg, 0, 1, 101.0, 1.0)["status"], "EXCLUDED")
    sh = _mk([(100, 100.5, 99.5, 100)] + [FAR] * 3 + [(103, 103.5, 100.9, 101.5)] + [MID] * 5)
    chk("end of data after fill excluded", retest_result(sh, 0, 1, 101.0, 1.0)["status"], "EXCLUDED")
    # primary rule: enter at the open AFTER the touch bar
    prim = [(103, 103.5, 100.9, 101.5), (101.2, 102.5, 101.0, 102.3)]     # touch bar, then entry bar opens 101.2 -> target 102.2
    r = rt(far + prim, rule="next_open_after_touch")
    chk("primary: entry = open after the touch bar", r["entry"], 101.2)
    chk("primary: target next", (r["outcome"], r["R"]), (rm.TARGET_FIRST, 1.0))
    r = rt(far + [(103, 103.5, 99.0, 99.5), (100.5, 100.8, 99.4, 99.6)], rule="next_open_after_touch")
    chk("primary: nothing in the touch bar counts (stop in touch bar ignored)", r["outcome"], rm.STOP_FIRST)      # entry 100.5, stop 99.5 hit next bar
    r = rt(far + [(103, 103.5, 100.9, 101.5), (101.0, 101.5, 100.5, 101.2)], rule="next_open_after_touch")
    chk("primary: neither within horizon", r["outcome"], rm.NEITHER)
    chk("primary: spread worsens entry", rt(far + prim, rule="next_open_after_touch", spread=0.1)["entry"], 101.3)
    sx = _mk([(100, 100.5, 99.5, 100)] + far + [(103, 103.5, 100.9, 101.5)])
    chk("primary: touch on the last bar -> excluded", retest_result(sx, 0, 1, 101.0, 1.0)["status"], "EXCLUDED")
    # short mirror: limit 99 (short entry), stop 100, target 98
    # touch bar high 99.5 >= 99 (fill), next bar opens 98.5 -> short entry 98.5, target 97.5 (low 97.2), stop 99.5 not hit
    ss = _mk([(100, 100.5, 99.5, 100)] + [(97, 97.5, 96, 96.5)] * 3 + [(97, 99.5, 96.9, 98.5), (98.5, 98.8, 97.2, 97.6)] + [(98.5, 98.8, 98.1, 98.5)] * 60)
    r = retest_result(ss, 0, -1, 99.0, 1.0)
    chk("short primary: fill then target", (r["status"], r["outcome"], r["entry"]), ("FILLED", rm.TARGET_FIRST, 98.5))
    r = retest_result(ss, 0, -1, 99.0, 1.0, fill_bar_rule="start_next_bar")
    chk("short limit (optimistic): fill then neither/target logic runs", r["status"], "FILLED")
    # --- accounting
    chk("R of target/stop/neither", (_r_of(rm.TARGET_FIRST), _r_of(rm.STOP_FIRST), _r_of(rm.NEITHER), _r_of(rm.AMBIGUOUS)), (1.0, -1.0, 0.0, None))
    # --- random-walk null: FVG presence and retest should not look informative
    from . import gates as g
    rng = np.random.default_rng(777)
    fp, used, mean_diffs, c7_fp = 0, 0, [], 0
    for _ in range(null_series):
        ser = g._random_walk_series(14000, rng, step=5)
        P = w1.Prepared("5m", ser.df)
        C = Ctx(P)
        r1 = run_c1(C, 1.0, "EXPLORATORY_READY")
        if r1["gross"].get("p") is None:
            continue
        used += 1
        mean_diffs.append(r1["gross"]["diff"])
        fp += 1 if r1["gross"]["p"] < 0.05 else 0
        r7 = run_c7(C, 1.0)
        if "p" in r7 and r7["p"] < 0.05:
            c7_fp += 1
    chk("null series usable", used >= 15, True)
    chk("null C1 false-positive rate <= 20%", used > 0 and fp / used <= 0.20, True)
    chk("null C1 mean diff near zero", abs(float(np.mean(mean_diffs))) < 0.02, True)
    chk("null C7 false-positive rate <= 20%", used > 0 and c7_fp / used <= 0.20, True)
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": used, "C1_false_positive_rate": round(fp / used, 3) if used else None,
                     "C7_false_positive_rate": round(c7_fp / used, 3) if used else None,
                     "C1_mean_diff": round(float(np.mean(mean_diffs)), 4) if mean_diffs else None}}


def main():
    ap = argparse.ArgumentParser(description="Observatory Wave 2 (FVG: C1, C5, C7). Read-only on raw data.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--open-holdout", action="store_true")
    ap.add_argument("--ledger", default="observatory_data/holdout_ledger.jsonl")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    res = run_wave2(a.raw_dir, a.source, a.open_holdout, a.ledger)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "wave2_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "wave2_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
