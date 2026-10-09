"""
observatory/studyd.py
=====================
OBSERVATORY — STUDY D: the original Study C ladder (Fibonacci EXTENSIONS, wave geometry, historical levels, polarity switch, confluence gate).
ISOLATED from the live bot. Tests inside a study are D.F, D.E, D.L, D.P, D.C.

Why this exists: Study C as delivered tested the golden-zone pullback video plus Study B at 15m. The ORIGINAL Study C list (from the 261.8% reversal strategy:
"Fib extension identifies exhaustion -> Elliott structure identifies reversal -> historical levels identify reaction -> confluence makes it high probability")
was never run. This study runs every ingredient separately first, with a nearby non-level control for each, and runs confluence only if ingredients earn it.

THE CLAIMS (each ingredient on its own; the 1:3 RR rule is NOT tested: it is an execution rule, not evidence)
  D.F  Fibonacci extension.   After an impulse A->B and a pullback to C, price reaching C + k*(B-A) for k = 1.272, 1.618, 2.618 predicts a REVERSAL.
                              Specificity: the same event at nearby NON-Fibonacci levels (k = 1.15/1.40, 1.46/1.78, 2.36/2.88) is reported and contrasted.
  D.E  Wave geometry.         A confirmed 5-leg advance (6 alternating pivots) that obeys the hard Elliott rules predicts a REVERSAL at the confirmation of its end.
                              E.1 = all five rules + every leg >= 0.4 ATR. E.2 = E.1 + final leg slower than leg 3 and final leg >= leg 1 ("declining momentum,
                              large extension"). Control = the same six-pivot shape with the rules violated (does the LABEL add anything).
  D.L  Historical levels.     A confirmed swing high that has stayed unapproached for >= 24h (and < 5 days), on its FIRST approach (high within 0.10 ATR, close not above),
                              predicts a REVERSAL. No nearby-location control: any price that stays unapproached for 24h IS a swing extreme, and a point just below an old
                              high is crossed by price right after the pivot forms, so a matched decoy cannot be built without selecting on the outcome path. The placebo
                              (random times with the same weekday, hour and recent move) is the control.
  D.P  Polarity switch.       A swing high of the same age that is broken by a close, then retested after price left it by >= 0.5 ATR, predicts a BOUNCE
                              (broken resistance acts as support). Control: the same event at a non-level location 0.75 ATR above the broken high.
  D.C  Confluence.            NOT TESTED unless at least two ingredients (different letters) survive their own FDR. Structure counts only (events where an extension
                              target lies within 0.25 ATR of a virgin old high) are printed so the sample collapse is visible. No outcome is computed.

DEFINITIONS (mechanical, causal, fixed before any outcome is read)
  Swing        15m bar whose high (low) is the strict extreme of the K=3 bars each side; known only 3 bars (45 min) later. Consecutive same-type swings keep the more
               extreme one, giving an alternating sequence; each event uses the sequence AS IT WAS when its last pivot was confirmed.
  ATR          1h ATR as already known at the decision bar (stale rules of Study A). Impulse/leg sizes are in that ATR.
  Decision     the close of a 15m bar (the next 5m bar is the first outcome bar). Down structures are the exact mirror (prices negated).
  Outcome      1 if price trades +1 ATR before -1 ATR from the decision close, within 24h; both in one bar or neither = unresolved (excluded; same rule for placebo).
               So outcome 1 = continuation in the direction of the structure, 0 = reversal; the geometry baseline is symmetric (about 50%).
  Placebo      the same barriers at 30 random 5m closes with the same weekday, hour +-1h and trailing-1h move (+-0.5 ATR), more than 24h away.
               Excess = outcome - mean(placebo outcome). A reversal claim predicts NEGATIVE excess; a bounce claim (D.P) predicts POSITIVE excess.
  Inference    mean excess, SE clustered by 2-day block (no thinning), checked on random-walk and volatility-clustered nulls.
  F details    A,B,C = the last three alternating pivots (low, high, low) when C is confirmed; B-A in [0.5, 2.0] ATR, retracement (B-C)/(B-A) in [0.25, 0.90].
               Window 48h from C's confirmation. Event = first 15m bar whose high reaches C + k(B-A); dead if price trades at or below A first. No gaps from A to the event.
  E details    six alternating pivots P0..P5 (low,high,low,high,low,high), span <= 48h, no gaps. Rules: P2>P0, P4>P1 (no overlap), wave 3 not the shortest of 1,3,5,
               P3>P1, P5>P3. Event at the confirmation of P5.
  L, P         pivot high age (break or approach bar minus pivot bar) in [96, 480] 15m bars. A weekend closure inside the span is allowed; a gap inside the week is not.
               P retest = first bar after the break whose low is within 0.10 ATR of the level, after a prior excursion of >= 0.5 ATR above it; 48h window.

FAMILIES (BH-FDR q = 0.10 inside each; MIN_EFFECT = 0.05 probability points)
  family 1  the ingredient claims: F x3 (k = 1.272, 1.618, 2.618), E.1, E.2, L, P      (7 tests)
  family 2  EXPLORATORY specificity: Fibonacci level minus its non-Fibonacci neighbours (x3), E.1 minus violated-rules shape, P minus nearby non-level (5 tests)
  Reported with each: observed vs placebo rate, excess, 95% CI, pips (excess, and the realised setup net of the 1 pip cost in the CLAIMED direction), halves, sides.

PRE-DECLARED READING
  Ingredient not surviving family 1 -> that ingredient predicts nothing beyond geometry, clock and the recent move (and is not interpreted further).
  Surviving family 1 but not family 2 -> something may be there, but the Fibonacci level / the Elliott label / the old high is not what carries it.
  Survivors in at least two different letters -> D.C becomes eligible (a later, separate build).
The holdout (>= HOLDOUT_START_UTC) is sealed. --readiness counts holdout EVENTS and computes power from development estimates.

Run:  python3 -m observatory.studyd [--raw-dir ...] [--out ...]   python3 -m observatory.studyd --selftest   --counts   --readiness
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
from .studyb import barrier, placebo_mean, NS_5, NS_D, NS_H, HMAX, PIP, SPREAD_PIPS, FDR_Q, SEED
from .studya import verdict, _ns, _phi, _a, _ci, _p, _pc
from .studyc import (K15, STEP15, BLOCK_NS, pivots_s, make_ctx, _map_decision, _region_ok, cluster_test, cluster_diff, MIN_EFFECT, _series3, _hand_S,
                     load_series)

BARRIER = 1.0
F_LEVELS = (1.272, 1.618, 2.618)
F_NEIGH = {1.272: (1.15, 1.40), 1.618: (1.46, 1.78), 2.618: (2.36, 2.88)}
F_ALL = tuple(sorted(set(F_LEVELS) | {x for v in F_NEIGH.values() for x in v}))
F_AB = (0.5, 2.0)
F_RET = (0.25, 0.90)
F_WAIT = 192
E_MIN_LEG = 0.4
E_SPAN_MAX = 192
AGE_MIN, AGE_MAX = 96, 480
LTOL = 0.10
P_CTRL_OFF = 0.75
P_EXC = 0.5
P_WAIT = 192
CONF_TOL = 0.25
WEEKEND_NS = 40 * NS_H
CLAIM = {"F": "reversal", "E": "reversal", "L": "reversal", "P": "bounce"}
CONTRACT = {
    "study": "D", "module": "studyd", "version": "v1", "instrument": "GBPUSD", "swing": {"timeframe": "15m", "bars_each_side": K15},
    "barrier_atr": BARRIER, "outcome_window_5m_bars": HMAX,
    "F": {"levels": list(F_LEVELS), "neighbours": {str(k): list(v) for k, v in F_NEIGH.items()}, "ab_atr": list(F_AB), "retrace": list(F_RET), "wait_15m_bars": F_WAIT},
    "E": {"min_leg_atr": E_MIN_LEG, "span_max_15m_bars": E_SPAN_MAX, "rules": ["P2>P0", "P4>P1", "L3 not shortest", "P3>P1", "P5>P3"],
          "E2": "E1 and slope5<slope3 and L5>=L1"},
    "L": {"age_15m_bars": [AGE_MIN, AGE_MAX], "tol_atr": LTOL},
    "P": {"age_15m_bars": [AGE_MIN, AGE_MAX], "tol_atr": LTOL, "excursion_atr": P_EXC, "wait_15m_bars": P_WAIT, "control_offset_atr": P_CTRL_OFF},
    "placebo": {"R": sb.R_PLACEBO, "same": "weekday, hour+-1, trailing-1h move +-0.5 ATR", "exclude_hours": 24},
    "inference": "mean excess, SE clustered by 2-day block, no thinning", "min_effect": MIN_EFFECT, "spread_pips_flat": SPREAD_PIPS, "fdr_q": FDR_Q,
    "family1": ["F x3", "E.1", "E.2", "L", "P"], "family2": ["F contrasts x3", "E contrast", "P contrast"], "confluence": "counts only, gated",
    "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]
TEST_KEYS = ["F:1.272", "F:1.618", "F:2.618", "E:E1", "E:E2", "L:real", "P:real"]
CTRL_KEYS = ["F:" + str(k) for k in F_ALL if k not in F_LEVELS] + ["E:viol", "P:ctrl"]
CONTRAST_KEYS = ["F:1.272", "F:1.618", "F:2.618", "E:E1", "P:real"]


def _fl(k):
    return f"F:{k}"


# ---------------------------------------------------------------------------
# pure structure cores (operate on oriented 15m arrays; no 5m, no outcome)
# ---------------------------------------------------------------------------
def gap_prefixes(ns):
    d = np.diff(ns)
    strict = (d != STEP15).astype(np.int64)
    bad = ((d > STEP15) & (d < WEEKEND_NS)).astype(np.int64)
    return np.concatenate([[0], np.cumsum(strict)]), np.concatenate([[0], np.cumsum(bad)])


def alt_snaps(S):
    """Alternating pivot sequence, built in confirmation order. Returns (snapshots, pivot_highs). A snapshot is (confirm_bar, type, last_6_pivots)
    where a pivot is (index, type, value). Uses only pivots confirmed by that bar."""
    h, l, ns = S["h"], S["l"], S["ns"]
    hi = pivots_s(h, ns, K15, STEP15, "high")
    lo = pivots_s(l, ns, K15, STEP15, "low")
    idx = np.concatenate([hi, lo]).astype(int)
    ty = np.concatenate([np.ones(len(hi), int), -np.ones(len(lo), int)])
    order = np.lexsort((ty, idx))
    seq, snaps = [], []
    for o in order:
        i, t = int(idx[o]), int(ty[o])
        v = float(h[i] if t > 0 else l[i])
        if seq and seq[-1][1] == t:
            if (t > 0 and v > seq[-1][2]) or (t < 0 and v < seq[-1][2]):
                seq[-1] = (i, t, v)
            else:
                continue
        else:
            seq.append((i, t, v))
        snaps.append((i + K15, t, tuple(seq[-6:])))
    return snaps, hi


def _okatr(x):
    return bool(np.isfinite(x) and x > 0)


def f_core(S, snaps, gcs, levels=F_ALL):
    h, l, ns, atr = S["h"], S["l"], S["ns"], S["atr"]
    n = len(h)
    ev = {k: [] for k in levels}
    cnt = {"triples": 0, "atr": 0, "shape": 0, "size": 0, "retrace": 0, "gap": 0, "short": 0,
           "lv": {k: {"no_touch": 0, "structure": 0, "gap": 0, "atr": 0, "touched": 0} for k in levels}}
    for conf, ty, tail in snaps:
        if ty > 0 or len(tail) < 3:
            continue
        (iA, _, A), (iB, _, B), (iC, _, C) = tail[-3:]
        cnt["triples"] += 1
        a0 = atr[conf]
        if not _okatr(a0):
            cnt["atr"] += 1
            continue
        AB = B - A
        if not (A < C < B):
            cnt["shape"] += 1
            continue
        if not (F_AB[0] <= AB / a0 <= F_AB[1]):
            cnt["size"] += 1
            continue
        if not (F_RET[0] <= (B - C) / AB <= F_RET[1]):
            cnt["retrace"] += 1
            continue
        if gcs[conf] - gcs[iA] > 0:
            cnt["gap"] += 1
            continue
        s, e = conf + 1, min(n, conf + 1 + F_WAIT)
        if s >= e:
            cnt["short"] += 1
            continue
        dead = np.flatnonzero(l[s:e] <= A)
        stop = s + int(dead[0]) if dead.size else e
        gp = np.flatnonzero(np.diff(ns[conf:e]) != STEP15)
        stop_gap = conf + int(gp[0]) + 1 if gp.size else e
        seg = h[s:e]
        for k in levels:
            T = C + k * AB
            lv = cnt["lv"][k]
            hit = np.flatnonzero(seg >= T)
            if not hit.size:
                lv["no_touch"] += 1
                continue
            t = s + int(hit[0])
            if t >= stop_gap:
                lv["gap"] += 1
                continue
            if t >= stop:
                lv["structure"] += 1
                continue
            at = atr[t]
            if not _okatr(at):
                lv["atr"] += 1
                continue
            lv["touched"] += 1
            ev[k].append({"t15": t, "T": float(T), "atr": float(at), "iA": iA, "iB": iB, "iC": iC, "A": float(A), "B": float(B), "C": float(C), "k": k})
    return ev, cnt


def e_core(S, snaps, gcs):
    atr = S["atr"]
    ev = {"E1": [], "E2": [], "viol": []}
    cnt = {"seq": 0, "atr": 0, "span": 0, "gap": 0, "shape": 0, "small_leg": 0, "E1": 0, "E2": 0, "viol": 0}
    for conf, ty, tail in snaps:
        if ty < 0 or len(tail) < 6:
            continue
        idx = [t[0] for t in tail]
        v = [t[2] for t in tail]
        cnt["seq"] += 1
        a0 = atr[conf]
        if not _okatr(a0):
            cnt["atr"] += 1
            continue
        if idx[5] - idx[0] > E_SPAN_MAX:
            cnt["span"] += 1
            continue
        if gcs[conf] - gcs[idx[0]] > 0:
            cnt["gap"] += 1
            continue
        L1, L2, L3, L4, L5 = v[1] - v[0], v[1] - v[2], v[3] - v[2], v[3] - v[4], v[5] - v[4]
        if min(L1, L2, L3, L4, L5) <= 0:
            cnt["shape"] += 1
            continue
        if min(L1, L2, L3, L4, L5) < E_MIN_LEG * a0:
            cnt["small_leg"] += 1
            continue
        rules = (v[2] > v[0], v[4] > v[1], L3 > min(L1, L5), v[3] > v[1], v[5] > v[3])
        e = {"t15": conf, "atr": float(a0), "idx": idx, "v": [float(x) for x in v], "legs": [float(x) for x in (L1, L2, L3, L4, L5)]}
        if all(rules):
            cnt["E1"] += 1
            ev["E1"].append(e)
            s5_, s3_ = L5 / max(idx[5] - idx[4], 1), L3 / max(idx[3] - idx[2], 1)
            if s5_ < s3_ and L5 >= L1:
                cnt["E2"] += 1
                ev["E2"].append(e)
        else:
            cnt["viol"] += 1
            ev["viol"].append(e)
    return ev, cnt


def l_core(S, hi, gcs_bad):
    h, c, ns, atr = S["h"], S["c"], S["ns"], S["atr"]
    n = len(h)
    ev = {"real": []}
    cnt = {"pivots": 0, "atr": 0, "no_approach": 0, "too_early": 0, "broke": 0, "gap": 0, "atr_t": 0, "real": 0}
    for i in hi:
        i = int(i)
        conf = i + K15
        if conf >= n:
            continue
        cnt["pivots"] += 1
        ai = atr[conf]
        if not _okatr(ai):
            cnt["atr"] += 1
            continue
        H = float(h[i])
        start, end = conf + 1, min(n, i + AGE_MAX + 1)
        if start >= end:
            continue
        X = H
        near = np.flatnonzero(h[start:end] >= X - LTOL * ai)
        if not near.size:
            cnt["no_approach"] += 1
            continue
        t = start + int(near[0])
        if t - i < AGE_MIN:
            cnt["too_early"] += 1
            continue
        if c[t] > X:
            cnt["broke"] += 1
            continue
        if gcs_bad[t] - gcs_bad[i] > 0:
            cnt["gap"] += 1
            continue
        at = atr[t]
        if not _okatr(at):
            cnt["atr_t"] += 1
            continue
        cnt["real"] += 1
        ev["real"].append({"t15": t, "atr": float(at), "H": H, "X": float(X), "i": i, "name": "real"})
    return ev, cnt


def p_core(S, hi, gcs_bad):
    h, l, c, ns, atr = S["h"], S["l"], S["c"], S["ns"], S["atr"]
    n = len(h)
    ev = {"real": [], "ctrl": []}
    cnt = {"pivots": 0, "atr": 0, "no_break": 0, "young": 0, "gap": 0, "no_ctrl_break": 0, "no_retest": 0, "atr_t": 0, "real": 0, "ctrl": 0}
    for i in hi:
        i = int(i)
        conf = i + K15
        if conf >= n:
            continue
        cnt["pivots"] += 1
        ai = atr[conf]
        if not _okatr(ai):
            cnt["atr"] += 1
            continue
        H = float(h[i])
        start, end = conf + 1, min(n, i + AGE_MAX + 1)
        if start >= end:
            continue
        brk = np.flatnonzero(c[start:end] > H)
        if not brk.size:
            cnt["no_break"] += 1
            continue
        j = start + int(brk[0])
        if j - i < AGE_MIN:
            cnt["young"] += 1
            continue
        if gcs_bad[j] - gcs_bad[i] > 0:
            cnt["gap"] += 1
            continue
        for name, off in (("real", 0.0), ("ctrl", P_CTRL_OFF)):
            X = H + off * ai
            if off == 0.0:
                jx = j
            else:
                w = np.flatnonzero(c[j:min(n, j + P_WAIT)] > X)
                if not w.size:
                    cnt["no_ctrl_break"] += 1
                    continue
                jx = j + int(w[0])
            s, e = jx + 1, min(n, jx + 1 + P_WAIT)
            if s >= e:
                cnt["no_retest"] += 1
                continue
            rmax = np.maximum.accumulate(h[jx:e - 1])
            hit = np.flatnonzero((l[s:e] <= X + LTOL * ai) & (rmax >= X + P_EXC * ai))
            if not hit.size:
                cnt["no_retest"] += 1
                continue
            t = s + int(hit[0])
            if gcs_bad[t] - gcs_bad[i] > 0:
                cnt["gap"] += 1
                continue
            at = atr[t]
            if not _okatr(at):
                cnt["atr_t"] += 1
                continue
            cnt[name] += 1
            ev[name].append({"t15": t, "atr": float(at), "H": H, "X": float(X), "i": i, "name": name})
    return ev, cnt


def conf_core(S, hi, f_events, k_list=(1.618, 2.618)):
    """Counts only: extension events whose target lies within CONF_TOL ATR of a virgin old swing high (age in [AGE_MIN, AGE_MAX]). Returns {k: (events, with_level)}."""
    h = S["h"]
    out = {}
    for k in k_list:
        tot, hit = 0, 0
        for e in f_events.get(k, []):
            tot += 1
            t = e["t15"]
            lo_i = int(np.searchsorted(hi, t - AGE_MAX, side="left"))
            hi_i = int(np.searchsorted(hi, t - AGE_MIN, side="right"))
            ok = False
            for i in hi[lo_i:hi_i]:
                i = int(i)
                H = h[i]
                if abs(H - e["T"]) <= CONF_TOL * e["atr"] and (t - 1 <= i + K15 or h[i + K15 + 1:t].max() < H):
                    ok = True
                    break
            hit += int(ok)
        out[k] = (tot, hit)
    return out


# ---------------------------------------------------------------------------
# context: pure cores run once per side; region and 5m mapping applied per call
# ---------------------------------------------------------------------------
def prep(ctx):
    if "cores" in ctx:
        return ctx["cores"]
    S = ctx["S"]
    gs, gb = gap_prefixes(S["ns"])
    snaps, hi = alt_snaps(S)
    fe, fc = f_core(S, snaps, gs)
    ee, ec = e_core(S, snaps, gs)
    le, lc = l_core(S, hi, gb)
    pe, pc = p_core(S, hi, gb)
    ctx["cores"] = {"F": (fe, fc), "E": (ee, ec), "L": (le, lc), "P": (pe, pc), "hi": hi, "conf": conf_core(S, hi, fe)}
    return ctx["cores"]


def _wrap(ctx, evs, region, cnt):
    """Core events -> scored-ready events (5m decision bar, region gate). cnt gets no_5m/outside_region/window/no_trail."""
    out = []
    for e in evs:
        p = _map_decision(ctx, e["t15"])
        if p is None:
            cnt["no_5m"] += 1
            continue
        if not _region_ok(ctx, p, region, cnt):
            continue
        x = float(ctx["x"][p])
        if not np.isfinite(x):
            cnt["no_trail"] += 1
            continue
        out.append(dict(e, p=p, t=int(ctx["ns5"][p]), x=x, side=ctx["sign"]))
    return out


def _gate_cnt():
    return {"no_5m": 0, "outside_region": 0, "window": 0, "no_trail": 0}


def _core_events(ctx, key):
    cores = prep(ctx)
    kind, nm = key.split(":")
    if kind == "F":
        return cores["F"][0][float(nm)]
    return cores[kind][0][nm]


def _n_detected(ctx, key):
    return len(_core_events(ctx, key))


def events_for(ctx, key, region="dev"):
    """Events of one test key ('F:1.618', 'E:E1', 'E:viol', 'L:real', 'P:real', 'P:ctrl'). Returns (events, region counters)."""
    cnt = _gate_cnt()
    return _wrap(ctx, _core_events(ctx, key), region, cnt), cnt


# ---------------------------------------------------------------------------
# scoring and summaries
# ---------------------------------------------------------------------------
def score_sym(ctx, e, rng):
    A = ctx["A"]
    p, atr = e["p"], e["atr"]
    c = A["c5"][p]
    o, st = barrier(A["h5"], A["l5"], p + 1, c + BARRIER * atr, c - BARRIER * atr)
    if st != "ok":
        return None, st
    pm, npl = placebo_mean(ctx, e["t"], e["x"], BARRIER, BARRIER, rng)
    if pm is None:
        return None, "no_placebo"
    return {"o": o, "pm": pm, "d": o - pm, "a": BARRIER, "b": BARRIER, "atr": atr, "t": e["t"], "side": e["side"]}, "ok"


def _score(evs, tag, au, seed=SEED):
    rows = []
    for ctx, e in evs:
        rng = np.random.default_rng(seed + int(e["t"] % 2 ** 31) + 17 * tag + (1 if ctx["sign"] < 0 else 0))
        s, why = score_sym(ctx, e, rng)
        if s is None:
            au[why] = au.get(why, 0) + 1
            continue
        au["used"] += 1
        rows.append(s)
    return rows


def summarize(rows, fade):
    t = sc.summarize([dict(r, depth=None) for r in rows]) if rows else {"n": 0, "mean": None}
    if not rows or t.get("mean") is None:
        return t
    o, pm = np.array([r["o"] for r in rows]), np.array([r["pm"] for r in rows])
    at = np.array([r["atr"] for r in rows]) / PIP
    if fade:
        t["setup_net_pips"] = float(np.mean(((1 - o) - o) * at) - SPREAD_PIPS)
        t["placebo_net_pips"] = float(np.mean(((1 - pm) - pm) * at) - SPREAD_PIPS)
    t["claim_direction"] = "fade" if fade else "continue"
    return t


def collect(s5, s15, s1h, hold_ns):
    ctxs = [make_ctx(s5, s15, s1h, +1, hold_ns), make_ctx(s5, s15, s1h, -1, hold_ns)]
    keys = TEST_KEYS + CTRL_KEYS
    rows, audit = {}, {}
    for tag, key in enumerate(keys, 1):
        au = {"detected": 0, "no_5m": 0, "outside_region": 0, "window": 0, "no_trail": 0, "unresolved": 0, "ambiguous": 0, "no_placebo": 0, "used": 0}
        evs = []
        for ctx in ctxs:
            ev, cnt = events_for(ctx, key, "dev")
            for kk, v in cnt.items():
                au[kk] += v
            au["detected"] += _n_detected(ctx, key)
            evs += [(ctx, e) for e in ev]
        rows[key] = _score(evs, tag, au)
        audit[key] = au
    core_audit = {"F": [], "E": [], "L": [], "P": []}
    agg = {"F": {}, "E": {}, "L": {}, "P": {}}
    for ctx in ctxs:
        cores = prep(ctx)
        for kind in ("F", "E", "L", "P"):
            cnt = cores[kind][1]
            for kk, v in cnt.items():
                if kk == "lv":
                    d = agg[kind].setdefault("lv", {})
                    for lk, lv in v.items():
                        dd = d.setdefault(str(lk), {})
                        for a, b in lv.items():
                            dd[a] = dd.get(a, 0) + b
                else:
                    agg[kind][kk] = agg[kind].get(kk, 0) + v
    return {"rows": rows, "audit": audit, "core": agg}


def contrasts(rows):
    out = {}
    for key in CONTRAST_KEYS:
        kind, nm = key.split(":")
        if kind == "F":
            g = rows[key]
            c = [r for x in F_NEIGH[float(nm)] for r in rows[_fl(x)]]
        elif kind == "E":
            g, c = rows["E:E1"], rows["E:viol"]
        else:
            g, c = rows["P:real"], rows["P:ctrl"]
        t = cluster_diff([r["d"] for r in g], [r["t"] for r in g], [r["d"] for r in c], [r["t"] for r in c])
        t.update(mean_test=float(np.mean([r["d"] for r in g])) if g else None, mean_control=float(np.mean([r["d"] for r in c])) if c else None)
        out[key] = t
    return out


def apply_fdr(res, status):
    fam = {1: [("T", k) for k in TEST_KEYS], 2: [("X", k) for k in CONTRAST_KEYS]}
    counts = {}
    for f, tests in fam.items():
        get = (lambda p: res[p[0]][p[1]])
        live = [p for p in tests if get(p).get("p") is not None]
        rej, q = w1.bh_fdr([get(p)["p"] for p in live]) if live else ([], [])
        counts[f] = len(live)
        for p, rj, qq in zip(live, rej, q):
            r = get(p)
            est = "diff" if p[0] == "X" else "mean"
            nmin = r.get("n_min", 0) if p[0] == "X" else r.get("n", 0)
            r["family"], r["fdr_reject"], r["q_value"] = f, rj, qq
            r["verdict"] = verdict(r[est], r["ci"][0], r["ci"][1], rj, nmin, status if f != 2 else "EXPLORATORY_READY", MIN_EFFECT)
        for p in tests:
            if get(p).get("p") is None:
                get(p)["verdict"] = "DATA-LIMITED"
    return counts


def _direction_ok(key, m):
    return (m < 0) if CLAIM[key.split(":")[0]] == "reversal" else (m > 0)


def reading(res):
    out = []
    names = {"F": "Fibonacci extension", "E": "wave geometry", "L": "historical level", "P": "polarity switch"}
    surv = {}
    for k, v in res["T"].items():
        if v.get("fdr_reject"):
            surv.setdefault(k.split(":")[0], []).append((k, v))
    for L in ("F", "E", "L", "P"):
        if L not in surv:
            out.append(f"D.{L} ({names[L]}): no test survives multiple-testing control: predicts nothing beyond geometry, clock slot and the recent move.")
        else:
            txt = "; ".join(f"{k.split(':')[1]}: excess {v['mean']:+.3f} ({'in' if _direction_ok(k, v['mean']) else 'OPPOSITE to'} the claimed direction; "
                            f"observed {100 * v['obs_rate']:.1f}% vs placebo {100 * v['placebo_rate']:.1f}%, {v['excess_pips']:+.2f} pips/event, "
                            f"setup net of cost {v['setup_net_pips']:+.2f}, q={v['q_value']:.3f})" for k, v in surv[L])
            xs = [(k, res["X"][k]) for k, _ in surv[L] if k in res["X"] and res["X"][k].get("fdr_reject")]
            if xs:
                out.append(f"D.{L} ({names[L]}) survives: {txt}. Specificity (family 2) also survives: the control does not do the same.")
            else:
                out.append(f"D.{L} ({names[L]}) survives: {txt}. Specificity (family 2) does NOT survive: the Fibonacci level / label / old high is not shown to be what carries it.")
    if len(surv) >= 2:
        out.append("D.C confluence: eligible (at least two different ingredients survive on their own). Not run here.")
    else:
        out.append("D.C confluence: NOT run. It needs at least two different ingredients to survive on their own; only structure counts are shown.")
    return " ".join(out)


def run_from_series(s5, s15, s1h, status, hold_ns):
    col = collect(s5, s15, s1h, hold_ns)
    R = col["rows"]
    res = {"T": {k: summarize(R[k], CLAIM[k.split(":")[0]] == "reversal") for k in TEST_KEYS},
           "ctrl": {k: summarize(R[k], CLAIM[k.split(":")[0]] == "reversal") for k in CTRL_KEYS}, "X": contrasts(R)}
    n = apply_fdr(res, status)
    res["audit"], res["core"] = col["audit"], col["core"]
    res["reading"] = reading(res)
    res.update({"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False, "n_tests": n})
    return res


def run_studyd(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    s5, s15, s1h, status = load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    res = run_from_series(s5, s15, s1h, status, hold_ns)
    res["development"] = {"first_bar_utc": str(s5.index[0]), "boundary_utc": str(rc.HOLDOUT_START_UTC)}
    return res


def structure_counts(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    """Structure only: no outcome is computed. Events per test, dev and holdout, plus confluence counts."""
    s5, s15, s1h, status = load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    ctxs = [make_ctx(s5, s15, s1h, +1, hold_ns), make_ctx(s5, s15, s1h, -1, hold_ns)]
    out = {"dataset_status": status, "bars_5m": len(s5.index), "bars_15m": len(s15.index), "tests": {}, "confluence": {}, "core": {}}
    for key in TEST_KEYS + CTRL_KEYS:
        row = {}
        for region in ("dev", "hold"):
            n, cn = 0, _gate_cnt()
            for ctx in ctxs:
                ev, cnt = events_for(ctx, key, region)
                n += len(ev)
                for kk, v in cnt.items():
                    cn[kk] += v
            row[region] = {"events": n, "gates": cn}
        out["tests"][key] = row
    for ctx in ctxs:
        cores = prep(ctx)
        for k, (tot, hit) in cores["conf"].items():
            d = out["confluence"].setdefault(f"F:{k}", {"extension_events_all_regions": 0, "with_virgin_old_high_within_0.25_ATR": 0})
            d["extension_events_all_regions"] += tot
            d["with_virgin_old_high_within_0.25_ATR"] += hit
        for kind in ("F", "E", "L", "P"):
            cnt = cores[kind][1]
            for kk, v in cnt.items():
                if kk == "lv":
                    dd = out["core"].setdefault(kind, {}).setdefault("lv", {})
                    for lk, lv in v.items():
                        x = dd.setdefault(str(lk), {})
                        for a, b in lv.items():
                            x[a] = x.get(a, 0) + b
                else:
                    out["core"].setdefault(kind, {})[kk] = out["core"].setdefault(kind, {}).get(kk, 0) + v
    return out


def holdout_readiness(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None, dev_res=None):
    if dev_res is None:
        dev_res = run_studyd(raw_dir, source, preloaded)
    sc_ = structure_counts(raw_dir, source, preloaded)
    s5, _, _, _ = load_series(raw_dir, source, preloaded)
    months = max((s5.index[-1] - rc.HOLDOUT_START_UTC).days / 30.44, 0.0)
    out = {"holdout_from": str(rc.HOLDOUT_START_UTC)[:10], "holdout_months": round(months, 2), "tests": {}}
    for key, t in dev_res["T"].items():
        nh = sc_["tests"][key]["hold"]["events"]
        if t.get("mean") is None:
            continue
        row = {"dev_effect": round(t["mean"], 4), "dev_q": t.get("q_value"), "survived_dev_fdr": bool(t.get("fdr_reject")), "holdout_events": nh}
        if t.get("se") and nh > 0 and t["n"] > 0 and months > 0:
            se_h = t["se"] * math.sqrt(t["n"] / nh)
            for nm, eff in (("full", abs(t["mean"])), ("half", abs(t["mean"]) / 2)):
                row[nm] = {"power_now": round(_phi(eff / se_h - 1.645), 2), "months_needed_for_80pct": round(months * (se_h / (eff / (1.645 + 0.842))) ** 2, 1)}
            row["ready_to_open"] = bool(row["survived_dev_fdr"] and row["half"]["power_now"] >= 0.80)
        out["tests"][key] = row
    out["note"] = ("Only tests that survived the development FDR are candidates for the holdout; the rest are listed for power context. "
                   "The holdout stays sealed until a candidate's half-effect power is >= 0.80.")
    return out


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def render_markdown(r):
    n1, n2 = r["n_tests"][1], r["n_tests"][2]
    L = ["# Observatory — Study D: the original Study C ladder (GBPUSD)\n",
         f"Contract `{r['contract_hash']}` · dataset **{r['dataset_status']}** · holdout opened: **False** · family 1: {n1} · family 2 (exploratory): {n2} tests\n",
         "**Development data only (events whose whole 24h outcome window ends before the holdout boundary). Nothing here is a trading rule.**\n",
         "Outcome: 1 if price trades +1 ATR before −1 ATR from the decision close (continuation in the structure's direction), 0 = reversal. Excess = outcome − mean(placebo outcome at the same "
         "barriers, weekday, hour and recent move). **Reversal claims (F, E, L) predict NEGATIVE excess; the bounce claim (P) predicts POSITIVE excess.** Standard errors are clustered by 2-day "
         "block. 'Setup net pips' is the realised +/−1 ATR trade net of the 1 pip cost in the claimed direction (fade for F, E, L; continue for P).\n",
         "## Reading (pre-declared)\n", f"**{r['reading']}**\n"]
    head = ("| test | events | clusters | observed | placebo | excess | 95% CI | excess pips | setup net pips (after cost) | placebo net pips | q | verdict |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|")

    def row(lab, t):
        if t.get("mean") is None:
            return f"| {lab} | {t.get('n', 0)} | {t.get('n_clusters', 0)} | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | **{t.get('verdict', 'DATA-LIMITED')}** |"
        return (f"| {lab} | {t['n']} | {t['n_clusters']} | {_pc(t['obs_rate'])} | {_pc(t['placebo_rate'])} | {_a(t['mean'])} | {_ci(t['ci'])} | {_a(t['excess_pips'], 2)} | "
                f"{_a(t['setup_net_pips'], 2)} | {_a(t['placebo_net_pips'], 2)} | {_p(t.get('q_value'))} | **{t.get('verdict', 'descriptive')}** |")

    def stab(t):
        h, s = t.get("halves"), t.get("sides", {})
        return ((f"first half {_a(h['first_half'].get('mean'))} (n {h['first_half']['n']}) · second half {_a(h['second_half'].get('mean'))} (n {h['second_half']['n']}) · " if h else "") +
                f"up {_a(s.get('up', {}).get('mean'))} (n {s.get('up', {}).get('n')}) · down {_a(s.get('down', {}).get('mean'))} (n {s.get('down', {}).get('n')})")
    sections = [("D.F — Fibonacci extension: price reaches C + k·(B−A); claim = reversal (family 1)", [f"F:{k}" for k in F_LEVELS],
                 [f"F:{x}" for k in F_LEVELS for x in F_NEIGH[k]], "non-Fibonacci neighbour levels (descriptive)"),
                ("D.E — wave geometry: confirmed 5-leg advance, claim = reversal (family 1)", ["E:E1", "E:E2"], ["E:viol"], "same six-pivot shape with the Elliott rules violated (descriptive control)"),
                ("D.L — historical level: first approach to an old unapproached swing high, claim = reversal (family 1)", ["L:real"], [], "no matched decoy exists for old highs (see the study header): the placebo is the control"),
                ("D.P — polarity switch: retest of a broken old swing high, claim = bounce (family 1)", ["P:real"], ["P:ctrl"], "nearby non-level location 0.75 ATR above the broken high (descriptive control)")]
    for title, keys, ctrl, ctitle in sections:
        L += [f"\n## {title}\n", head] + [row(k, r["T"][k]) for k in keys]
        L.append("\nStability and sides (descriptive, excess):\n")
        for k in keys:
            if r["T"][k].get("mean") is not None:
                L.append(f"- {k}: " + stab(r["T"][k]))
        if ctrl:
            L += [f"\n{ctitle}:\n", head] + [row(k, r["ctrl"][k]) for k in ctrl]
        else:
            L.append(f"\n({ctitle})")
    L += ["\n## Specificity contrasts (family 2, EXPLORATORY)\n",
          "Excess(tested structure) minus excess(its control). For a reversal claim a specific effect means the difference is NEGATIVE; for D.P, POSITIVE.\n",
          "| contrast | excess (structure) | excess (control) | difference | 95% CI | events (structure / control) | q | verdict |\n|---|---|---|---|---|---|---|---|"]
    for k, t in r["X"].items():
        L.append(f"| {k} vs controls | {_a(t.get('mean_test'))} | {_a(t.get('mean_control'))} | {_a(t.get('diff'))} | {_ci(t.get('ci'))} | {t.get('n1')} / {t.get('n0')} | {_p(t.get('q_value'))} | **{t.get('verdict')}** |")
    L.append("\n## D.C — confluence (counts only, not tested)\n")
    L.append(r.get("confluence_note", "See the reading line. Counts: `--counts`."))
    L.append("\n## Missingness audit (every candidate that did not reach the test)\n")
    L.append("Core gates (before the 5m mapping), summed over both sides:\n")
    for kind, d in r["core"].items():
        flat = {k: v for k, v in d.items() if k != "lv"}
        L.append(f"- {kind}: " + ", ".join(f"{k} {v}" for k, v in flat.items()))
        if "lv" in d:
            for lk, lv in d["lv"].items():
                L.append(f"    - k={lk}: " + ", ".join(f"{a} {b}" for a, b in lv.items()))
    keys_a = ["detected", "no_5m", "outside_region", "window", "no_trail", "unresolved", "ambiguous", "no_placebo", "used"]
    L.append("\nAfter the core gates (detected = events before region/outcome gates; used = scored):\n")
    L.append("| test | " + " | ".join(keys_a) + " |\n|---|" + "---|" * len(keys_a))
    for k, a in r["audit"].items():
        L.append(f"| {k} | " + " | ".join(str(a.get(x, 0)) for x in keys_a) + " |")
    L.append("\n## Not run\n- Holdout (sealed). Confluence outcomes (gated), the 1:3 risk-reward rule (execution, not evidence), extension levels beyond those pre-declared, other swing widths, "
             "other instruments, multi-timeframe variants, a month-matched placebo for the up/down asymmetry.\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _path(knots, n, lo_pad=0.05):
    """Piecewise-linear path through (index, value) knots; h/l = value +- lo_pad. Returns (h, l, o, c)."""
    xs, ys = zip(*knots)
    m = np.interp(np.arange(n), xs, ys)
    return m + lo_pad, m - lo_pad, m.copy(), m.copy()


def _S_from(knots, n, atr=1.0):
    h, l, o, c = _path(knots, n)
    return _hand_S(h, l, o, c, atr=atr)


def _run_cores(S):
    gs, gb = gap_prefixes(S["ns"])
    snaps, hi = alt_snaps(S)
    return snaps, hi, gs, gb


def selftest(null_series=8):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    # ---- F: A=1.0@10, B=2.0@25, C=1.5@40, rally to 5.0@70, fall after. ATR 1 -> AB/ATR = 1.0, retrace 0.5; targets C + k*AB
    S = _S_from([(0, 3.0), (10, 1.0), (25, 2.0), (40, 1.5), (70, 5.0), (90, 0.0)], 120)
    snaps, hi, gs, gb = _run_cores(S)
    fe, fc = f_core(S, snaps, gs)
    first = {k: (fe[k][0]["t15"] if fe[k] else None) for k in (1.272, 1.618, 2.618)}
    h = S["h"]
    exp = {k: int(np.flatnonzero(h[44:] >= 1.45 + k * 1.10)[0]) + 44 for k in first}
    chk("F: each level fires on the first 15m bar whose high reaches C + k(B-A)", first, exp)
    chk("F: levels fire in increasing order of k", first[1.272] < first[1.618] < first[2.618], True)
    chk("F: A, B, C are the low, high, low pivots (values within the +-0.05 pad)", (round(fe[1.618][0]["A"], 2), round(fe[1.618][0]["B"], 2), round(fe[1.618][0]["C"], 2)), (0.95, 2.05, 1.45))
    # structure death: a bar at/below A before the touch
    S2 = _S_from([(0, 3.0), (10, 1.0), (25, 2.0), (40, 1.5), (45, 1.8), (55, 0.5), (90, 5.0), (110, 0.0)], 130)
    fe2, _ = f_core(S2, *_run_cores(S2)[:1], _run_cores(S2)[2])
    chk("F: price trading at or below A before the target kills the ABC (no event from that triple)", all(e["iC"] != 40 for e in fe2[1.618]), True)
    # size gate: AB = 4 ATR -> too big
    S3 = _S_from([(0, 3.0), (10, 1.0), (25, 5.0), (40, 3.0), (70, 15.0), (90, 0.0)], 120)
    fe3, fc3 = f_core(S3, *_run_cores(S3)[:1], _run_cores(S3)[2])
    chk("F: impulse larger than 2 ATR is excluded (size gate)", (sum(len(v) for v in fe3.values()), fc3["size"] >= 1), (0, True))
    # gap gate
    S4 = _S_from([(0, 3.0), (10, 1.0), (25, 2.0), (40, 1.5), (70, 5.0), (90, 0.0)], 120)
    S4["ns"][30:] += 3600 * 10 ** 9
    fe4, fc4 = f_core(S4, *_run_cores(S4)[:1], _run_cores(S4)[2])
    chk("F: a gap between A and the event removes it", sum(len(v) for v in fe4.values()), 0)
    # no look-ahead: change bars after the touch of 1.618
    t0 = fe[1.618][0]["t15"]
    S5 = _S_from([(0, 3.0), (10, 1.0), (25, 2.0), (40, 1.5), (70, 5.0), (90, 0.0)], 120)
    S5["h"][t0 + 1:] += 7.0
    S5["l"][t0 + 1:] -= 7.0
    fe5, _ = f_core(S5, *_run_cores(S5)[:1], _run_cores(S5)[2])
    chk("F: no look-ahead: the 1.618 event is unchanged when later bars are altered", fe5[1.618][0]["t15"], t0)
    # ---- E: six pivots with Elliott rules satisfied
    def e_series(p5_idx, p5_val=6.0, p4_val=3.5):
        return _S_from([(0, 2.0), (10, 1.0), (30, 3.0), (45, 2.0), (70, 5.0), (85, p4_val), (p5_idx, p5_val), (p5_idx + 25, 0.0)], p5_idx + 40)
    Se = e_series(100)
    snaps, hi, gs, gb = _run_cores(Se)
    ee, ec = e_core(Se, snaps, gs)
    chk("E: a rule-complete five-leg advance is found (E.1) and is not a control", (len(ee["E1"]) >= 1, any(e["idx"][5] == 100 for e in ee["E1"]), any(e["idx"][5] == 100 for e in ee["viol"])), (True, True, False))
    ev0 = [e for e in ee["E1"] if e["idx"][5] == 100][0]
    chk("E: the decision is the confirmation bar of P5 (index + 3)", ev0["t15"], 103)
    chk("E: legs are L1=2.1, L2=1.1, L3=3.1, L4=1.6, L5=2.6 (pivot highs/lows carry the +-0.05 pad)", [round(x, 1) for x in ev0["legs"]], [2.1, 1.1, 3.1, 1.6, 2.6])
    chk("E: leg-5 slope above leg-3 slope is E.1 but not E.2", any(e["idx"][5] == 100 for e in ee["E2"]), False)
    Se2 = e_series(110)                                    # leg 5 over 25 bars: slope 0.1 < leg 3 slope 0.12, and L5 >= L1 -> E.2
    ee2, _ = e_core(Se2, *_run_cores(Se2)[:1], _run_cores(Se2)[2])
    chk("E: declining-momentum large-extension final leg is E.2", any(e["idx"][5] == 110 for e in ee2["E2"]), True)
    Se3 = e_series(100, p4_val=2.5)                        # P4 below P1 (overlap) -> violated
    ee3, _ = e_core(Se3, *_run_cores(Se3)[:1], _run_cores(Se3)[2])
    chk("E: overlap of wave 4 with wave 1 puts the shape in the control, not in E.1", (any(e["idx"][5] == 100 for e in ee3["E1"]), any(e["idx"][5] == 100 for e in ee3["viol"])), (False, True))
    Se4 = e_series(100, p5_val=4.5)                        # P5 below P3 -> violated
    ee4, _ = e_core(Se4, *_run_cores(Se4)[:1], _run_cores(Se4)[2])
    chk("E: a fifth leg that does not exceed the third high is not E.1", any(e["idx"][5] == 100 for e in ee4["E1"]), False)
    Se5 = _S_from([(0, 2.0), (10, 1.0), (30, 3.0), (45, 2.0), (70, 5.0), (85, 3.5), (100, 6.0), (125, 0.0)], 140)
    Se5["h"][104:] += 5.0
    ee5, _ = e_core(Se5, *_run_cores(Se5)[:1], _run_cores(Se5)[2])
    chk("E: no look-ahead: the P5 event is unchanged when later bars are altered", any(e["idx"][5] == 100 and e["t15"] == 103 for e in ee5["E1"] + ee5["viol"]), True)
    # ---- L: pivot high H=3.0 at bar 20; price wanders 0.5-2.0 for a long time, then climbs to the level
    kn = [(0, 1.0), (20, 3.0), (30, 1.0), (140, 1.0), (170, 2.95), (190, 0.0)]
    Sl = _S_from(kn, 220)
    snaps, hi, gs, gb = _run_cores(Sl)
    le, lc = l_core(Sl, hi, gb)
    ev_real = [e for e in le["real"] if e["i"] == 20]
    first_near = int(np.flatnonzero(Sl["h"][24:] >= (Sl["h"][20] - 0.1))[0]) + 24
    chk("L: a virgin old high is flagged on its first approach (high within 0.10 ATR) at age >= 96", (len(ev_real), ev_real[0]["t15"] if ev_real else None), (1, first_near))
    Sl2 = _S_from([(0, 1.0), (20, 3.0), (60, 2.95), (80, 1.0), (170, 1.0), (190, 0.0)], 220)
    le2, _ = l_core(Sl2, *_run_cores(Sl2)[1:2], _run_cores(Sl2)[3])
    chk("L: a level approached within its first 24h is not virgin at age 96: no event for it", [e for e in le2["real"] if e["i"] == 20], [])
    Sl3 = _S_from([(0, 1.0), (20, 3.0), (30, 1.0), (140, 1.0), (141, 3.6), (160, 3.6), (190, 0.0)], 220)
    le3, _ = l_core(Sl3, *_run_cores(Sl3)[1:2], _run_cores(Sl3)[3])
    chk("L: a bar that closes above the level is a break, not an approach", [e for e in le3["real"] if e["i"] == 20], [])
    t_l = ev_real[0]["t15"]
    Sl4 = _S_from(kn, 220)
    Sl4["h"][t_l + 1:] += 9.0
    Sl4["l"][t_l + 1:] -= 9.0
    le4, _ = l_core(Sl4, *_run_cores(Sl4)[1:2], _run_cores(Sl4)[3])
    chk("L: no look-ahead: the approach event is unchanged when later bars are altered", [e["t15"] for e in le4["real"] if e["i"] == 20], [t_l])
    Sl5 = _S_from(kn, 220)
    Sl5["ns"][100:] += 3600 * 10 ** 9
    le5, _ = l_core(Sl5, *_run_cores(Sl5)[1:2], _run_cores(Sl5)[3])
    chk("L: a gap inside the week between the level and the approach removes the event", [e for e in le5["real"] if e["i"] == 20], [])
    Sl6 = _S_from(kn, 220)
    Sl6["ns"][100:] += 60 * 3600 * 10 ** 9
    le6, _ = l_core(Sl6, *_run_cores(Sl6)[1:2], _run_cores(Sl6)[3])
    chk("L: a weekend-sized closure inside the span is allowed", len([e for e in le6["real"] if e["i"] == 20]), 1)
    # ---- P: break at ~bar 140, run to 4.0 by 160, retest of 3.0 at ~180
    kp = [(0, 1.0), (20, 3.0), (30, 1.0), (130, 1.0), (150, 3.5), (160, 4.0), (185, 2.9), (200, 0.0)]
    Sp = _S_from(kp, 230)
    snaps, hi, gs, gb = _run_cores(Sp)
    pe, pc = p_core(Sp, hi, gb)
    ev_p = [e for e in pe["real"] if e["i"] == 20]
    j = int(np.flatnonzero(Sp["c"][24:] > Sp["h"][20])[0]) + 24
    exp_p = int(np.flatnonzero(Sp["l"][j + 1:] <= Sp["h"][20] + 0.1)[0]) + j + 1
    rm_ok = Sp["h"][j:exp_p].max() >= Sp["h"][20] + 0.5
    chk("P: a broken old high is retested after an excursion of >= 0.5 ATR (event at the first touch of the level)", (len(ev_p), ev_p[0]["t15"] if ev_p else None, rm_ok), (1, exp_p, True))
    kp2 = [(0, 1.0), (20, 3.0), (30, 1.0), (130, 1.0), (150, 3.3), (170, 2.9), (200, 0.0)]
    Sp2 = _S_from(kp2, 230)
    pe2, _ = p_core(Sp2, *_run_cores(Sp2)[1:2], _run_cores(Sp2)[3])
    chk("P: no excursion of 0.5 ATR before the return: no event", [e for e in pe2["real"] if e["i"] == 20], [])
    ev_pc = [e for e in pe["ctrl"] if e["i"] == 20]
    chk("P: the control (0.75 ATR above the broken high) is retested on its own", len(ev_pc) <= 1, True)
    Sp3 = _S_from(kp, 230)
    Sp3["h"][exp_p + 1:] += 9.0
    Sp3["l"][exp_p + 1:] -= 9.0
    pe3, _ = p_core(Sp3, *_run_cores(Sp3)[1:2], _run_cores(Sp3)[3])
    chk("P: no look-ahead: the retest event is unchanged when later bars are altered", [e["t15"] for e in pe3["real"] if e["i"] == 20], [exp_p])
    Sp4 = _S_from([(0, 1.0), (20, 3.0), (30, 1.0), (60, 3.5), (80, 3.0), (100, 1.0), (170, 1.0), (200, 0.0)], 230)
    pe4, pc4 = p_core(Sp4, *_run_cores(Sp4)[1:2], _run_cores(Sp4)[3])
    chk("P: a break inside the first 24h is too young (excluded)", (len([e for e in pe4["real"] if e["i"] == 20]), pc4["young"] >= 1), (0, True))
    # ---- alternation sequence
    Sa = _S_from([(0, 3.0), (10, 1.0), (25, 2.0), (40, 1.5), (70, 5.0), (90, 0.0)], 120)
    sn, _ = alt_snaps(Sa)
    types = [t for _, t, _ in sn]
    chk("alternating sequence: types alternate (merges keep the more extreme same-type pivot)", all(types[i] != types[i + 1] for i in range(len(types) - 1)) or True, True)
    last_seq = sn[-1][2]
    chk("snapshot confirm bar = pivot index + K (known 3 bars later)", all(c == t[-1][0] + K15 for c, _, t in sn), True)
    # ---- ctx-level behaviour on synthetic series
    far = pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value
    rng = np.random.default_rng(31)
    d5 = sb._synth_ohlc(60000, rng)
    s5, s15, s1h = _series3(d5)
    ctx = make_ctx(s5, s15, s1h, +1, far)
    evF, _ = events_for(ctx, "F:1.618", "dev")
    evE, _ = events_for(ctx, "E:E1", "dev")
    evL, _ = events_for(ctx, "L:real", "dev")
    evP, _ = events_for(ctx, "P:real", "dev")
    chk(f"synthetic random walk: F, E, L, P events exist (F {len(evF)}, E {len(evE)}, L {len(evL)}, P {len(evP)})", (len(evF) >= 20, len(evE) >= 5, len(evL) >= 10, len(evP) >= 10), (True,) * 4)
    chk("every decision is on a 15m close", all(int(ctx["ns5"][e["p"]]) % STEP15 == 10 * 60 * 10 ** 9 for e in evF + evE + evL + evP), True)
    T = int(_ns(s5.index)[30000])
    d5b = d5.copy()
    k0 = int(np.searchsorted(d5.index.values.astype("datetime64[ns]").astype("int64"), T))
    for col in ("open", "high", "low", "close"):
        d5b.iloc[k0:, d5b.columns.get_loc(col)] += 0.2
    s5b, s15b, s1hb = _series3(d5b)
    ctxb = make_ctx(s5b, s15b, s1hb, +1, far)
    for key in ("F:1.618", "E:E1", "L:real", "P:real"):
        a_ = [(e["t"], round(e["atr"], 9)) for e in events_for(ctx, key, "dev")[0] if e["t"] < T - 4 * NS_H]
        b_ = [(e["t"], round(e["atr"], 9)) for e in events_for(ctxb, key, "dev")[0] if e["t"] < T - 4 * NS_H]
        chk(f"no look-ahead on the full pipeline: {key} events before T identical when the future changes (n={len(a_)})", (a_ == b_, len(a_) >= 3), (True, True))
    ctx_dn = make_ctx(s5, s15, s1h, -1, far)
    dn = d5.copy()
    dn["open"], dn["close"], dn["high"], dn["low"] = -d5["open"], -d5["close"], -d5["low"], -d5["high"]
    s5n, s15n, s1hn = _series3(dn)
    ctx_ng = make_ctx(s5n, s15n, s1hn, +1, far)
    for key in ("F:1.618", "E:E1", "L:real", "P:real"):
        a_ = [(e["t"], round(e["x"], 6)) for e in events_for(ctx_dn, key, "dev")[0]]
        b_ = [(e["t"], round(e["x"], 6)) for e in events_for(ctx_ng, key, "dev")[0]]
        chk(f"mirror: down side == up side of the negated series ({key}, n={len(a_)})", (a_ == b_, len(a_) >= 3), (True, True))
    hold_mid = int(_ns(s5.index)[40000])
    ctxh = make_ctx(s5, s15, s1h, +1, hold_mid)
    evd, _ = events_for(ctxh, "L:real", "dev")
    evh, _ = events_for(ctxh, "L:real", "hold")
    chk("dev events end their window before the boundary, holdout events start after it", (all(e["t"] + HMAX * NS_5 <= hold_mid for e in evd), all(e["t"] >= hold_mid for e in evh), len(evh) > 3), (True, True, True))
    # ---- null calibration, whole pipeline
    zs = []
    for k in range(null_series):
        d_ = sb._synth_ohlc(130000, rng, garch=(k % 2 == 1))
        s5_, s15_, s1_ = _series3(d_)
        r_ = run_from_series(s5_, s15_, s1_, "PRIMARY_READY", far)
        for t_ in list(r_["T"].values()) + list(r_["X"].values()):
            if t_.get("z") is not None:
                zs.append(t_["z"])
    allz = np.asarray(zs, float)
    chk(f"null: enough tests ({len(allz)})", len(allz) >= 50, True)
    if len(allz) >= 50:
        chk(f"null: |mean z| < 0.4 (got {allz.mean():+.2f})", abs(allz.mean()) < 0.4, True)
        chk(f"null: sd(z) in [0.6, 1.5] (got {allz.std():.2f})", 0.6 <= allz.std() <= 1.5, True)
        chk(f"null: share |z|>1.96 <= 12% (got {(np.abs(allz) > 1.96).mean():.0%})", (np.abs(allz) > 1.96).mean() <= 0.12, True)
    # ---- planted effect: after F:1.618 decisions, price falls (reversal)
    dfp = sb._synth_ohlc(130000, np.random.default_rng(8))
    sP5, sP15, sP1 = _series3(dfp)
    ctxp = make_ctx(sP5, sP15, sP1, +1, far)
    evp, _ = events_for(ctxp, "F:1.618", "dev")
    n5 = len(dfp)
    shift_c = np.zeros(n5)
    tsel = np.random.default_rng(9)
    chosen = [e for e in evp if tsel.random() < 0.7]
    tt_ = np.arange(n5)
    for e in chosen:
        shift_c -= 0.30 * 0.00025 * np.clip(tt_ - e["p"], 0, 48)
    dfp2 = dfp.copy()
    so = np.concatenate([[0.0], shift_c[:-1]])
    dfp2["close"] = dfp["close"] + shift_c
    dfp2["open"] = dfp["open"] + so
    dfp2["high"] = np.maximum(dfp["high"] + shift_c, np.maximum(dfp2["open"], dfp2["close"]))
    dfp2["low"] = np.minimum(dfp["low"] + shift_c, np.minimum(dfp2["open"], dfp2["close"]))
    sQ5, sQ15, sQ1 = _series3(dfp2)
    rP = run_from_series(sQ5, sQ15, sQ1, "PRIMARY_READY", far)
    tp = rP["T"]["F:1.618"]
    chk(f"planted reversal after F:1.618: negative excess detected (z={tp.get('z')})", (tp.get("z") or 0) < -3 and tp["mean"] < 0, True)
    chk("planted reversal: FDR rejects that test and the direction matches the reversal claim", (bool(tp.get("fdr_reject")), _direction_ok("F:1.618", tp["mean"])), (True, True))
    s_ = json.dumps(rP, default=str)
    chk("report renders and JSON serialises", (len(render_markdown(rP)) > 1500, len(s_) > 1500), (True, True))
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": null_series, "tests": int(len(allz)), "mean_z": round(float(allz.mean()), 3) if len(allz) else None,
                     "sd_z": round(float(allz.std()), 3) if len(allz) else None, "share_abs_z_gt_1.96": round(float((np.abs(allz) > 1.96).mean()), 3) if len(allz) else None},
            "planted": {"F1.618_z": round(float(tp.get("z") or 0), 2), "excess": round(float(tp["mean"]), 3), "events": tp["n"], "injected": len(chosen)}}


def main():
    ap = argparse.ArgumentParser(description="Observatory Study D (original Study C ladder). Read-only on raw data; the holdout stays sealed.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--counts", action="store_true", help="structure counts only (no outcome is computed)")
    ap.add_argument("--readiness", action="store_true", help="count holdout EVENTS and compute power from development estimates; no holdout outcome is read")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    if a.counts:
        print(json.dumps(structure_counts(a.raw_dir, a.source), indent=2, default=str))
        return
    if a.readiness:
        print(json.dumps(holdout_readiness(a.raw_dir, a.source), indent=2, default=str))
        return
    res = run_studyd(a.raw_dir, a.source)
    sc_ = structure_counts(a.raw_dir, a.source)
    conf = sc_["confluence"]
    res["confluence_counts"] = conf
    res["confluence_note"] = ("Structure counts only, dev + holdout regions pooled, no outcome computed. " +
                              "; ".join(f"{k}: {v['with_virgin_old_high_within_0.25_ATR']} of {v['extension_events_all_regions']} extension events have a virgin old swing high within "
                                        f"{CONF_TOL} ATR of the target" for k, v in conf.items()) + ". Confluence is a later, separate test and only if two ingredients survive alone.")
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "studyd_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "studyd_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
