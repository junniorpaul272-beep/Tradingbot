"""
observatory/studyc.py
=====================
OBSERVATORY — STUDY C: Fibonacci retracement zones and pullback depth, at higher power. ISOLATED from the live bot.
Tests inside a study are C.1, C.2, ...  (Elliott waves, historical levels, polarity switch and confluence are later tests of this study; NOT run here.)

THE CLAIMS
----------
  C.1  "A pullback is valid only if price retraces 38.2%-61.8% of the preceding impulse and then leaves the zone with visible momentum."
  C.2  Study B at higher power: after a break of a swing high, does the depth of the pullback below the broken high carry information?
       (Study B used 1h swings and 24h-thinned events: about 200 events per test and CIs of +-0.07. This study uses 15m swings and
       all events, with error bars clustered by 2-day block, so dependence between neighbouring events is respected without throwing them away.)
  C.3  Is anything SPECIFIC to the Fibonacci numbers? The golden zone is compared with equally wide zones that are not Fibonacci.

DEFINITIONS (mechanical, causal, fixed before any outcome is read)
-------------------------------------------------------------------
  Swing        15m bar whose low (high) is the strict minimum (maximum) of the K=3 bars on each side. Known only 3 bars (45 min) later.
  Impulse      from a confirmed swing low A to M = the highest high since A (a RUNNING value: it only uses the past). Required size
               LEG_MIN <= (M - A)/ATR1h <= LEG_MAX at the moment the pullback enters the zone. Down impulses are the exact mirror.
  Retracement  r = (M - low)/(M - A), M fixed at zone entry. A bar that reaches the zone and ALSO makes a new high is skipped (order unknown).
  Zone [lo,hi] ENTRY the first 15m bar whose low reaches M - lo*(M-A).  ABORT if price trades at or below A (structure failed), if the
               retracement exceeds hi (too deep for this zone) or if a gap/closure occurs. A new high before the exit resets the impulse.
  EXIT         the first 15m bar AFTER entry that closes back above the shallow edge M - lo*(M-A) AND satisfies one pre-declared exit rule:
                 P plain         nothing else (control: is momentum doing anything?)
                 S strong bar    body >= 0.3 ATR1h and close in the top 25% of the bar's range
                 D displacement  close above the highest high of the previous 4 bars (a micro-structure break)
  DECISION     at the close of the exit bar. a = (M - close)/ATR, b = (close - A)/ATR must both lie in [0.25, 5] (ATR1h at that time).
  OUTCOME      1 if price trades above M before it trades below A, from the next 5m bar, within 24h; both in one bar or neither = unresolved
               (counted, excluded; the same rule applies to the placebo).
  PLACEBO      the same barriers (a ATR above, b ATR below the close) at R=30 random 5m closes with the same weekday, the same hour (+-1h)
               and the same trailing 1h move (+-0.5 ATR), more than 24h away. Excess = outcome - mean(placebo outcome). 0 = structure adds nothing.
  Inference    excess is averaged over events; the standard error is clustered by 2-day block (so overlapping outcome windows of neighbouring
               events do not inflate precision) and checked on random-walk and volatility-clustered nulls.

ZONES   golden [0.382, 0.618]  (the claim) | shallow [0.146, 0.382] and deep [0.618, 0.854] (same width, disjoint, NOT Fibonacci) |
        wide [0.35, 0.65] (reported, not tested). The clause "excluding exactly 50%" has probability zero on continuous prices; it is not implemented.

TESTS
-----
  family 1  C.1 x 3 exits: golden zone, excess vs placebo                       (the claim)
  family 2  C.2 x 6: BOS on 15m swings (B.1-style +-1 ATR) and the 5 depths of Study B (-0.25, 0, 0.25, 0.5, 1.0 ATR below the broken high)
  family 3  C.3 x 3 (EXPLORATORY): excess(golden) - excess(shallow and deep controls pooled), per exit rule: a Fibonacci-specific effect
            exists only if the golden zone beats equally wide non-Fibonacci zones.
  Reported with each: observed vs placebo rate, excess, 95% CI, pips (excess, and the realised setup net of the flat 1 pip cost), halves, sides.

PRE-DECLARED READING
  No family-1 test survives FDR -> the golden-zone pullback with these exits predicts nothing beyond geometry, clock and the recent move.
  Family 1 survives but C.3 does not -> pullback timing may carry information but the Fibonacci numbers are not what carries it.
  Both survive -> a Fibonacci-specific effect on development data: a hypothesis for one holdout test, and only the pips columns say whether it matters.
The holdout (>= HOLDOUT_START_UTC) is sealed. --readiness counts holdout EVENTS and computes power from development estimates.

Run:  python3 -m observatory.studyc [--raw-dir ...] [--out ...]   python3 -m observatory.studyc --selftest   --counts   --readiness
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
from . import studya as sa
from . import studyb as sb
from .studyb import (barrier, window_ok, trail_ok, weekday, atr_at, score_event, first_passage, NS_5, NS_D, NS_H, HMAX, GEOM_MIN, GEOM_MAX,
                     PIP, SPREAD_PIPS, FDR_Q, SEED)
from .studya import verdict, _ns, _phi, _a, _ci, _p, _pc

K15 = 3
STEP15 = 15 * 60 * 10 ** 9
WAIT15 = 96                 # 15m bars (24h) from the confirmed swing to the exit
MAX_AGE15 = 192             # 15m bars (48h) a swing high stays eligible for a BOS
LEG_MIN, LEG_MAX = 1.0, 6.0
BOS_RANGE = (1.0, 6.0)
DEPTHS = sb.DEPTHS
WAIT_BOS = sb.WAIT
BLOCK_NS = 2 * NS_D
MIN_EFFECT = 0.05
STRONG_BODY, STRONG_LOC = 0.3, 0.75
DISP_BARS = 4
ZONES = {"golden": (0.382, 0.618), "shallow": (0.146, 0.382), "deep": (0.618, 0.854), "wide": (0.35, 0.65)}
EXITS = ("P", "S", "D")
CONTRAST_CONTROLS = ("shallow", "deep")
CONTRACT = {
    "study": "C", "module": "studyc", "version": "v1", "instrument": "GBPUSD", "swing": {"timeframe": "15m", "bars_each_side": K15},
    "leg_atr1h": [LEG_MIN, LEG_MAX], "wait_15m_bars": WAIT15, "zones": ZONES, "exits": {"P": "plain", "S": [STRONG_BODY, STRONG_LOC], "D": DISP_BARS},
    "geometry_atr": [GEOM_MIN, GEOM_MAX], "outcome_window_5m_bars": HMAX, "placebo": {"R": sb.R_PLACEBO, "same": "weekday, hour+-1, trailing-1h move +-0.5 ATR",
                                                                                      "exclude_hours": 24},
    "bos15": {"range_atr": list(BOS_RANGE), "max_age_15m_bars": MAX_AGE15, "depths": list(DEPTHS), "barrier_atr": sb.B1_BARRIER},
    "inference": "mean excess, SE clustered by 2-day block, no thinning", "min_effect": MIN_EFFECT, "spread_pips_flat": SPREAD_PIPS, "fdr_q": FDR_Q,
    "family1": ["C.1 golden x 3 exits"], "family2": ["C.2 BOS + 5 depths"], "family3": ["C.3 contrasts x 3"], "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 15m structure
# ---------------------------------------------------------------------------
def pivots_s(x, ns, k, step_ns, kind):
    """Indices whose value is the strict max ('high') or min ('low') of bars i-k..i+k, all bars contiguous. Known only at i+k."""
    out = []
    for i in range(k, len(x) - k):
        w = x[i - k:i + k + 1]
        v = x[i]
        ext = w.max() if kind == "high" else w.min()
        if v == ext and int((w == v).sum()) == 1 and ns[i + k] - ns[i - k] == 2 * k * step_ns:
            out.append(i)
    return np.asarray(out, dtype=int)


def fib_scan(S, i, lo, hi, exit_kind, k=K15, wait=WAIT15, leg_min=LEG_MIN, leg_max=LEG_MAX):
    """State machine for one confirmed swing low at bar i. S: dict of arrays o,h,l,c,ns,atr (15m, orientation already applied).
    Returns (event | None, reason). Uses only bars up to the exit bar."""
    h, l, o, c, ns, atr = S["h"], S["l"], S["o"], S["c"], S["ns"], S["atr"]
    n = len(h)
    A = l[i]
    M = float(h[i:i + k + 1].max())
    state, Me, Re, dmax, lvl = 0, None, None, 0.0, None
    end = min(n, i + k + 1 + wait)
    for t in range(i + k + 1, end):
        if ns[t] - ns[t - 1] != STEP15:
            return None, "gap"
        a_t = atr[t]
        if not (np.isfinite(a_t) and a_t > 0):
            return None, "atr"
        if l[t] <= A:
            return None, "structure"
        if state == 0:
            R = M - A
            if leg_min <= R / a_t <= leg_max and l[t] <= M - lo * R:
                if h[t] > M:
                    M = float(h[t])
                    continue
                state, Me, Re = 1, M, R
                lvl = Me - lo * Re
                dmax = (Me - l[t]) / Re
                if dmax > hi:
                    return None, "too_deep"
                continue                                  # the entry bar itself is never an exit bar
            M = max(M, float(h[t]))
            continue
        dmax = max(dmax, (Me - l[t]) / Re)
        if dmax > hi:
            return None, "too_deep"
        if c[t] > lvl:
            ok = True
            if exit_kind == "S":
                rng = h[t] - l[t]
                ok = (c[t] - o[t] >= STRONG_BODY * a_t) and rng > 0 and (c[t] - l[t]) / rng >= STRONG_LOC
            elif exit_kind == "D":
                ok = t - DISP_BARS >= 0 and c[t] > h[t - DISP_BARS:t].max()
            if ok:
                return {"t15": t, "M": float(max(Me, h[t])), "A": float(A), "depth": float(dmax), "R_atr": float(Re / a_t), "atr": float(a_t), "lvl": float(lvl)}, None
        if h[t] > Me:                                     # new high before a valid exit: the impulse extended, start over
            state, M = 0, float(h[t])
    return None, "timeout"


def find_bos_s(h, l, c, atr, ns, k=K15, max_age=MAX_AGE15, rng=BOS_RANGE, step_ns=STEP15):
    """Up-BOS on bars of any length (same rules as Study B, 15m by default). Returns (events, counters)."""
    cnt = {"pivots": 0, "breaks": 0, "range_out": 0, "atr_missing": 0}
    first = {}
    piv = pivots_s(h, ns, k, step_ns, "high")
    cnt["pivots"] = int(len(piv))
    for i in piv:
        start, end = i + k + 1, min(len(c), i + max_age + 1)
        if start >= end:
            continue
        brk = np.flatnonzero(c[start:end] > h[i])
        if brk.size == 0:
            continue
        j = start + int(brk[0])
        if j not in first or i > first[j]:
            first[j] = int(i)
    ev = []
    for j, i in sorted(first.items()):
        cnt["breaks"] += 1
        a = atr[j]
        if not (np.isfinite(a) and a > 0):
            cnt["atr_missing"] += 1
            continue
        H, L = h[i], float(l[i + 1:j + 1].min())
        r = (H - L) / a
        if not (rng[0] <= r <= rng[1]):
            cnt["range_out"] += 1
            continue
        ev.append({"j": j, "i": i, "H": float(H), "L": L, "atr": float(a), "M0": float(h[j]), "age": j - i, "range_atr": float(r)})
    return ev, cnt


# ---------------------------------------------------------------------------
# context (one per side; the down side is the exact mirror, prices negated)
# ---------------------------------------------------------------------------
def make_ctx(s5, s15, s1h, sign, hold_ns):
    ctx = sb.make_ctx(s5, s1h, sign, hold_ns)
    ns15 = _ns(s15.index)
    if sign > 0:
        S = {"o": s15.o, "h": s15.h, "l": s15.l, "c": s15.c}
    else:
        S = {"o": -s15.o, "h": -s15.l, "l": -s15.h, "c": -s15.c}
    S["ns"] = ns15
    S["atr"] = atr_at(ns15 + STEP15, ctx["ns1"], s1h.atr)
    ctx["S"] = S
    ctx["fib_cache"] = {}
    return ctx


def _map_decision(ctx, t15):
    """5m reference bar for the close of 15m bar t15: the 5m bar opening 10 minutes after it. Returns index or None."""
    ns5, S = ctx["ns5"], ctx["S"]
    tt = int(S["ns"][t15] + STEP15 - NS_5)
    p = int(np.searchsorted(ns5, tt))
    if p + 1 >= len(ns5) or ns5[p] != tt or ns5[p + 1] != tt + NS_5:
        return None
    return p


def _region_ok(ctx, p, region, cnt):
    ns5 = ctx["ns5"]
    t = int(ns5[p])
    if region == "dev":
        if t + (HMAX + 1) * NS_5 > ctx["hold_ns"]:
            cnt["outside_region"] += 1
            return False
        if not window_ok(ns5, p + 1):
            cnt["window"] += 1
            return False
    elif t < ctx["hold_ns"]:
        cnt["outside_region"] += 1
        return False
    if not trail_ok(ns5, p):
        cnt["no_trail"] += 1
        return False
    return True


def detect_fib(ctx, zone, exit_kind, region="dev"):
    """All decision events of a (zone, exit). Structure only (no outcome). Returns (events, counters)."""
    lo, hi = ZONES[zone]
    S, A5 = ctx["S"], ctx["A"]
    cnt = {"swings": 0, "gap": 0, "atr": 0, "structure": 0, "too_deep": 0, "timeout": 0, "no_5m": 0, "geometry": 0, "outside_region": 0, "window": 0, "no_trail": 0}
    piv = ctx.get("piv_low")
    if piv is None:
        piv = pivots_s(S["l"], S["ns"], K15, STEP15, "low")
        ctx["piv_low"] = piv
    cnt["swings"] = int(len(piv))
    ev = []
    for i in piv:
        e, why = fib_scan(S, int(i), lo, hi, exit_kind)
        if e is None:
            cnt[why] += 1
            continue
        p = _map_decision(ctx, e["t15"])
        if p is None:
            cnt["no_5m"] += 1
            continue
        c = A5["c5"][p]
        a, b = (e["M"] - c) / e["atr"], (c - e["A"]) / e["atr"]
        if not (GEOM_MIN <= a <= GEOM_MAX and GEOM_MIN <= b <= GEOM_MAX):
            cnt["geometry"] += 1
            continue
        if not _region_ok(ctx, p, region, cnt):
            continue
        ev.append({"p": p, "M": e["M"], "L": e["A"], "a": float(a), "b": float(b), "atr": e["atr"], "t": int(ctx["ns5"][p]), "x": float(ctx["x"][p]),
                   "side": ctx["sign"], "depth": e["depth"], "R_atr": e["R_atr"], "c": float(c), "swing": int(i)})
    return ev, cnt


def bos_breaks(ctx):
    S, A5, ns5 = ctx["S"], ctx["A"], ctx["ns5"]
    raw, cnt = find_bos_s(S["h"], S["l"], S["c"], S["atr"], S["ns"])
    cnt["no_5m_at_break"] = 0
    out = []
    for b in raw:
        tb = int(S["ns"][b["j"]] + STEP15)
        q = int(np.searchsorted(ns5, tb))
        if q >= len(ns5) or q < 1 or ns5[q] != tb or ns5[q - 1] != tb - NS_5:
            cnt["no_5m_at_break"] += 1
            continue
        out.append(dict(b, t_b=tb, q=q, side=ctx["sign"]))
    return out, cnt


def detect_bos(ctx, depth, region="dev"):
    """First-passage events below the broken high on 15m swings (Study B's definitions, more events). depth=None -> the break itself (B.1-style)."""
    A5, ns5 = ctx["A"], ctx["ns5"]
    brk, bc = ctx.get("bos_cache") or (None, None)
    if brk is None:
        brk, bc = bos_breaks(ctx)
        ctx["bos_cache"] = (brk, bc)
    cnt = {"breaks": len(brk), "no_pullback": 0, "structure_first": 0, "geometry": 0, "outside_region": 0, "no_trail": 0, "window": 0}
    ev = []
    for b in brk:
        if depth is None:
            r = b["q"] - 1
            if not _region_ok(ctx, r, region, cnt):
                continue
            ev.append(dict(b, t=int(ns5[r])))
            continue
        pas, why = first_passage(A5["h5"], A5["l5"], A5["c5"], b["q"], b["H"], b["L"], b["M0"], b["atr"], depth, WAIT_BOS)
        if pas is None:
            cnt[why] += 1
            continue
        p = pas["p"]
        if not _region_ok(ctx, p, region, cnt):
            continue
        ev.append(dict(b, **pas, t=int(ns5[p]), depth=depth, x=float(ctx["x"][p]), c=float(A5["c5"][p])))
    return ev, cnt, bc


# ---------------------------------------------------------------------------
# clustered statistics
# ---------------------------------------------------------------------------
def cluster_test(d, t_ns):
    d = np.asarray(d, float)
    t_ns = np.asarray(t_ns, np.int64)
    ok = np.isfinite(d)
    d, t_ns = d[ok], t_ns[ok]
    n = len(d)
    if n < 10:
        return {"n": n, "n_clusters": 0, "mean": None, "se": None, "ci": None, "z": None, "p": None}
    uniq, inv = np.unique(t_ns // BLOCK_NS, return_inverse=True)
    G = len(uniq)
    if G < 10:
        return {"n": n, "n_clusters": G, "mean": None, "se": None, "ci": None, "z": None, "p": None}
    m = float(d.mean())
    s = np.bincount(inv, weights=d - m)
    se = math.sqrt(float((s ** 2).sum()) * G / (G - 1)) / n
    z = m / se if se > 0 else 0.0
    return {"n": n, "n_clusters": int(G), "mean": m, "se": se, "sd": float(d.std(ddof=1)), "ci": (m - 1.96 * se, m + 1.96 * se), "z": z, "p": w1.norm_p(z)}


def cluster_diff(dA, tA, dB, tB):
    dA, tA, dB, tB = (np.asarray(x) for x in (dA, tA, dB, tB))
    out = {"diff": None, "se": None, "ci": None, "z": None, "p": None, "n": 0, "n1": len(dA), "n0": len(dB), "n_min": min(len(dA), len(dB))}
    if len(dA) < 10 or len(dB) < 10:
        return out
    blk = np.concatenate([tA // BLOCK_NS, tB // BLOCK_NS]).astype(np.int64)
    uniq, inv = np.unique(blk, return_inverse=True)
    G = len(uniq)
    if G < 10:
        return out
    mA, mB = float(dA.mean()), float(dB.mean())
    contrib = np.concatenate([(dA - mA) / len(dA), -(dB - mB) / len(dB)])
    u = np.bincount(inv, weights=contrib)
    se = math.sqrt(float((u ** 2).sum()) * G / (G - 1))
    diff = mA - mB
    z = diff / se if se > 0 else 0.0
    out.update(diff=diff, se=se, ci=(diff - 1.96 * se, diff + 1.96 * se), z=z, p=w1.norm_p(z), n=len(dA) + len(dB), n_clusters=int(G))
    return out


def summarize(rows):
    d = np.array([r["d"] for r in rows], float)
    ts = np.array([r["t"] for r in rows], np.int64)
    t = cluster_test(d, ts) if rows else {"n": 0, "mean": None}
    if not rows:
        return t
    o, pm = np.array([r["o"] for r in rows]), np.array([r["pm"] for r in rows])
    a, b, at = np.array([r["a"] for r in rows]), np.array([r["b"] for r in rows]), np.array([r["atr"] for r in rows]) / PIP
    t.update(obs_rate=float(o.mean()), placebo_rate=float(pm.mean()), obs_ci=w1.wilson(int(o.sum()), len(o)),
             excess_pips=float(np.mean(d * (a + b) * at)), setup_net_pips=float(np.mean((o * a - (1 - o) * b) * at) - SPREAD_PIPS),
             placebo_net_pips=float(np.mean((pm * a - (1 - pm) * b) * at) - SPREAD_PIPS), mean_a=float(a.mean()), mean_b=float(b.mean()),
             mean_atr_pips=float(at.mean()))
    if len(rows) >= 60:
        mid = np.median(ts)
        t["halves"] = {nm: cluster_test(d[m], ts[m]) for nm, m in (("first_half", ts < mid), ("second_half", ts >= mid))}
    sd = np.array([r["side"] for r in rows])
    t["sides"] = {"up": cluster_test(d[sd > 0], ts[sd > 0]), "down": cluster_test(d[sd < 0], ts[sd < 0])}
    return t


# ---------------------------------------------------------------------------
# one full pass
# ---------------------------------------------------------------------------
def _score_all(ctxs, evs, kind, tag, au, seed=SEED):
    rows = []
    for ctx, e in evs:
        rng = np.random.default_rng(seed + int(e["t"] % 2 ** 31) + 13 * tag + (1 if ctx["sign"] < 0 else 0))
        s, why = score_event(ctx, e, kind, rng)
        if s is None:
            au[why] = au.get(why, 0) + 1
            continue
        au["used"] += 1
        rows.append(dict(s, depth=e.get("depth"), R_atr=e.get("R_atr")))
    return rows


def collect(s5, s15, s1h, hold_ns):
    ctxs = [make_ctx(s5, s15, s1h, +1, hold_ns), make_ctx(s5, s15, s1h, -1, hold_ns)]
    out = {"fib": {}, "bos": {}, "audit": {}}
    tag = 0
    for zone in ZONES:
        for ex in EXITS:
            tag += 1
            au = {k: 0 for k in ("swings", "gap", "atr", "structure", "too_deep", "timeout", "no_5m", "geometry", "outside_region", "window", "no_trail",
                                 "unresolved", "ambiguous", "no_placebo", "used")}
            evs = []
            for ctx in ctxs:
                ev, cnt = detect_fib(ctx, zone, ex, "dev")
                for k, v in cnt.items():
                    au[k] += v
                evs += [(ctx, e) for e in ev]
            out["fib"][(zone, ex)] = _score_all(ctxs, evs, "pull", tag, au)
            out["audit"][f"C.1 {zone}/{ex}"] = au
    for d in (None,) + tuple(DEPTHS):
        tag += 1
        au = {k: 0 for k in ("breaks", "no_pullback", "structure_first", "geometry", "outside_region", "window", "no_trail", "unresolved", "ambiguous", "no_placebo", "used")}
        evs = []
        for ctx in ctxs:
            ev, cnt, bc = detect_bos(ctx, d, "dev")
            for k, v in cnt.items():
                if k in au:
                    au[k] += v
            evs += [(ctx, e) for e in ev]
        key = "BOS" if d is None else f"{d:+.2f}"
        out["bos"][key] = _score_all(ctxs, evs, "bos" if d is None else "pull", tag, au)
        out["audit"][f"C.2 {key}"] = au
    return out


def contrast_tests(fib):
    out = {}
    for ex in EXITS:
        g = fib[("golden", ex)]
        c = fib[("shallow", ex)] + fib[("deep", ex)]
        t = cluster_diff([r["d"] for r in g], [r["t"] for r in g], [r["d"] for r in c], [r["t"] for r in c])
        t.update(exit=ex, mean_golden=float(np.mean([r["d"] for r in g])) if g else None, mean_controls=float(np.mean([r["d"] for r in c])) if c else None)
        out[ex] = t
    return out


def apply_fdr(res, status):
    fam = {1: [("C1", ex) for ex in EXITS], 2: [("C2", k) for k in res["C2"]], 3: [("C3", ex) for ex in EXITS]}
    counts = {}
    for f, tests in fam.items():
        get = (lambda p: res[p[0]][p[1]])
        live = [p for p in tests if get(p).get("p") is not None]
        rej, q = w1.bh_fdr([get(p)["p"] for p in live]) if live else ([], [])
        counts[f] = len(live)
        for p, rj, qq in zip(live, rej, q):
            r = get(p)
            est = "diff" if p[0] == "C3" else "mean"
            nmin = r.get("n_min", 0) if p[0] == "C3" else r.get("n", 0)
            r["family"], r["fdr_reject"], r["q_value"] = f, rj, qq
            r["verdict"] = verdict(r[est], r["ci"][0], r["ci"][1], rj, nmin, status if f != 3 else "EXPLORATORY_READY", MIN_EFFECT)
        for p in tests:
            if get(p).get("p") is None:
                get(p)["verdict"] = "DATA-LIMITED"
    return counts


def reading(res):
    s1 = [(ex, v) for ex, v in res["C1"].items() if v.get("fdr_reject")]
    s2 = [(k, v) for k, v in res["C2"].items() if v.get("fdr_reject")]
    s3 = [(ex, v) for ex, v in res["C3"].items() if v.get("fdr_reject")]
    out = []
    if not s1:
        out.append("C.1: no golden-zone test survives multiple-testing control: no evidence that a 38.2-61.8% pullback followed by any of the three pre-declared exits "
                   "predicts continuation beyond geometry, clock slot and the recent move. C.3 is not interpreted.")
    else:
        out.append("C.1 survivors: " + "; ".join(f"exit {ex}: excess {v['mean']:+.3f} (observed {100 * v['obs_rate']:.1f}% vs placebo {100 * v['placebo_rate']:.1f}%, "
                                                  f"{v['excess_pips']:+.2f} pips/event, setup net of cost {v['setup_net_pips']:+.2f}, q={v['q_value']:.3f})" for ex, v in s1) + ".")
        if s3:
            out.append("C.3: the golden zone beats equally wide non-Fibonacci zones for exit(s) " + ", ".join(ex for ex, _ in s3) + ": a Fibonacci-specific effect on development data (hypothesis for one holdout test).")
        else:
            out.append("C.3: the golden zone does NOT beat equally wide non-Fibonacci zones: whatever C.1 shows is not specific to the Fibonacci numbers.")
    if not s2:
        out.append("C.2 (Study B at higher power): no break-of-structure test or pullback depth survives multiple-testing control.")
    else:
        out.append("C.2 survivors: " + "; ".join(f"{k}: excess {v['mean']:+.3f} (q={v['q_value']:.3f}, {v['excess_pips']:+.2f} pips/event)" for k, v in s2) + ".")
    return " ".join(out)


def run_from_series(s5, s15, s1h, status, hold_ns):
    col = collect(s5, s15, s1h, hold_ns)
    res = {"C1": {ex: summarize(col["fib"][("golden", ex)]) for ex in EXITS}, "C2": {k: summarize(v) for k, v in col["bos"].items()},
           "controls": {f"{z}/{ex}": summarize(col["fib"][(z, ex)]) for z in ZONES if z != "golden" for ex in EXITS}}
    res["C3"] = contrast_tests(col["fib"])
    n = apply_fdr(res, status)
    res["audit"] = col["audit"]
    res["reading"] = reading(res)
    res.update({"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False, "n_tests": n})
    return res


def load_series(raw_dir, source, preloaded=None):
    clean = {tf: (preloaded[tf] if preloaded else rc.clean_timeframe(tf, raw_dir, source)[0]) for tf in ("5m", "15m", "1h")}
    s5, s15, s1h = rm.Series(clean["5m"], 5), rm.Series(clean["15m"], 15), rm.Series(clean["1h"], 60)
    s5_, s1h_, status = sa.load_series(raw_dir, source, preloaded)
    return s5, s15, s1h, status


def run_studyc(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    s5, s15, s1h, status = load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    res = run_from_series(s5, s15, s1h, status, hold_ns)
    res["development"] = {"first_bar_utc": str(s5.index[0]), "boundary_utc": str(rc.HOLDOUT_START_UTC)}
    return res


def structure_counts(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None, leg_min=None, leg_max=None):
    """Structure only: no outcome is computed. Events per (zone, exit) and per BOS depth, dev and holdout."""
    global LEG_MIN, LEG_MAX
    old = (LEG_MIN, LEG_MAX)
    if leg_min is not None:
        LEG_MIN = leg_min
    if leg_max is not None:
        LEG_MAX = leg_max
    try:
        s5, s15, s1h, status = load_series(raw_dir, source, preloaded)
        hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
        ctxs = [make_ctx(s5, s15, s1h, +1, hold_ns), make_ctx(s5, s15, s1h, -1, hold_ns)]
        out = {"dataset_status": status, "bars_5m": len(s5.index), "bars_15m": len(s15.index), "leg_atr": [LEG_MIN, LEG_MAX], "fib": {}, "bos": {}}
        for zone in ZONES:
            for ex in EXITS:
                row = {}
                for region in ("dev", "hold"):
                    n, cnts = 0, {}
                    for ctx in ctxs:
                        ev, cnt = detect_fib(ctx, zone, ex, region)
                        n += len(ev)
                        for k, v in cnt.items():
                            cnts[k] = cnts.get(k, 0) + v
                    row[region] = {"events": n, "gates": cnts}
                out["fib"][f"{zone}/{ex}"] = row
        for d in (None,) + tuple(DEPTHS):
            row = {}
            for region in ("dev", "hold"):
                n, cnts = 0, {}
                for ctx in ctxs:
                    ev, cnt, bc = detect_bos(ctx, d, region)
                    n += len(ev)
                    for k, v in cnt.items():
                        cnts[k] = cnts.get(k, 0) + v
                row[region] = {"events": n, "gates": cnts}
            out["bos"]["BOS" if d is None else f"{d:+.2f}"] = row
        return out
    finally:
        LEG_MIN, LEG_MAX = old


# ---------------------------------------------------------------------------
# holdout readiness
# ---------------------------------------------------------------------------
def holdout_readiness(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None, dev_res=None):
    if dev_res is None:
        dev_res = run_studyc(raw_dir, source, preloaded)
    sc = structure_counts(raw_dir, source, preloaded)
    s5, _, _, _ = load_series(raw_dir, source, preloaded)
    months = max((s5.index[-1] - rc.HOLDOUT_START_UTC).days / 30.44, 0.0)
    out = {"holdout_from": str(rc.HOLDOUT_START_UTC)[:10], "holdout_months": round(months, 2), "tests": {}}
    items = [(f"C.1 golden/{ex}", t, sc["fib"][f"golden/{ex}"]["hold"]["events"]) for ex, t in dev_res["C1"].items()]
    items += [(f"C.2 {k}", t, sc["bos"][k]["hold"]["events"]) for k, t in dev_res["C2"].items()]
    for key, t, nh in items:
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
    L = ["# Observatory — Study C: Fibonacci zones and pullback depth (GBPUSD)\n",
         f"Contract `{r['contract_hash']}` · dataset **{r['dataset_status']}** · holdout opened: **False** · family 1: {r['n_tests'][1]} · family 2: {r['n_tests'][2]} · family 3 (exploratory): {r['n_tests'][3]} tests\n",
         "**Development data only (events whose whole 24h outcome window ends before the holdout boundary). Nothing here is a trading rule.**\n",
         "Excess = outcome − mean(placebo outcome at the same barriers/geometry, weekday, hour and recent move). 0 = structure adds nothing. Outcome: 1 if price trades above M (the impulse high) "
         "before below A (the impulse start / structural low) within 24h. Standard errors are clustered by 2-day block.\n",
         "## Reading (pre-declared)\n", f"**{r['reading']}**\n"]
    head = ("| test | events | clusters | observed | placebo | excess | 95% CI | excess pips | setup net pips (after cost) | placebo net pips | q | verdict |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|")

    def row(lab, t):
        if t.get("mean") is None:
            return f"| {lab} | {t.get('n', 0)} | {t.get('n_clusters', 0)} | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | **{t.get('verdict', 'DATA-LIMITED')}** |"
        return (f"| {lab} | {t['n']} | {t['n_clusters']} | {_pc(t['obs_rate'])} | {_pc(t['placebo_rate'])} | {_a(t['mean'])} | {_ci(t['ci'])} | {_a(t['excess_pips'], 2)} | "
                f"{_a(t['setup_net_pips'], 2)} | {_a(t['placebo_net_pips'], 2)} | {_p(t.get('q_value'))} | **{t.get('verdict', '')}** |")
    names = {"P": "plain exit", "S": "strong-bar exit", "D": "displacement exit"}
    L += ["## C.1 — golden zone 38.2–61.8% then an exit (family 1)\n", head] + [row(f"golden · {names[ex]}", t) for ex, t in r["C1"].items()]
    L.append("\nStability and sides (descriptive, excess):\n")
    for ex, t in r["C1"].items():
        h, s = t.get("halves"), t.get("sides", {})
        if t.get("mean") is not None:
            L.append(f"- golden · {names[ex]}: " + (f"first half {_a(h['first_half'].get('mean'))} (n {h['first_half']['n']}) · second half {_a(h['second_half'].get('mean'))} (n {h['second_half']['n']}) · " if h else "") +
                     f"up {_a(s.get('up', {}).get('mean'))} (n {s.get('up', {}).get('n')}) · down {_a(s.get('down', {}).get('mean'))} (n {s.get('down', {}).get('n')})")
    L += ["\n## C.2 — Study B at higher power: break of a 15m swing high and pullback depth below it (family 2)\n",
          "Depth in ATR below the broken high: −0.25 = retest that stays above it, 0 = touches it, + = penetrates. 'BOS' = from the break close, +1 ATR before −1 ATR.\n", head]
    for k, t in r["C2"].items():
        L.append(row("BOS break" if k == "BOS" else f"depth {k}", t))
    L.append("\nStability and sides (descriptive, excess):\n")
    for k, t in r["C2"].items():
        h, s = t.get("halves"), t.get("sides", {})
        if t.get("mean") is not None:
            L.append(f"- {k}: " + (f"first half {_a(h['first_half'].get('mean'))} (n {h['first_half']['n']}) · second half {_a(h['second_half'].get('mean'))} (n {h['second_half']['n']}) · " if h else "") +
                     f"up {_a(s.get('up', {}).get('mean'))} (n {s.get('up', {}).get('n')}) · down {_a(s.get('down', {}).get('mean'))} (n {s.get('down', {}).get('n')})")
    L += ["\n## C.3 — is anything specific to the Fibonacci numbers? (family 3, EXPLORATORY)\n", "Excess(golden zone) minus excess(shallow 14.6–38.2% and deep 61.8–85.4% zones pooled; equally wide, not Fibonacci).\n",
          "| exit | golden excess | controls excess | difference | 95% CI | events (golden / controls) | q | verdict |\n|---|---|---|---|---|---|---|---|"]
    for ex, t in r["C3"].items():
        L.append(f"| {names[ex]} | {_a(t.get('mean_golden'))} | {_a(t.get('mean_controls'))} | {_a(t.get('diff'))} | {_ci(t.get('ci'))} | {t.get('n1')} / {t.get('n0')} | {_p(t.get('q_value'))} | **{t.get('verdict')}** |")
    L += ["\n## Control zones (descriptive, no tests)\n", head] + [row(k, t) for k, t in r["controls"].items()]
    L.append("\n## Missingness audit (every candidate that did not reach the test)\n")
    L.append("Fib rows: swings = confirmed swing lows scanned; the rest are the reasons a swing produced no scored event. BOS rows: breaks and the gates after them.\n")
    keys_f = ["swings", "gap", "atr", "structure", "too_deep", "timeout", "no_5m", "geometry", "outside_region", "window", "no_trail", "unresolved", "ambiguous", "no_placebo", "used"]
    L.append("| test | " + " | ".join(keys_f) + " |\n|---|" + "---|" * len(keys_f))
    for k, a in r["audit"].items():
        if k.startswith("C.1"):
            L.append(f"| {k} | " + " | ".join(str(a.get(x, 0)) for x in keys_f) + " |")
    keys_b = ["breaks", "no_pullback", "structure_first", "geometry", "outside_region", "window", "no_trail", "unresolved", "ambiguous", "no_placebo", "used"]
    L.append("\n| test | " + " | ".join(keys_b) + " |\n|---|" + "---|" * len(keys_b))
    for k, a in r["audit"].items():
        if k.startswith("C.2"):
            L.append(f"| {k} | " + " | ".join(str(a.get(x, 0)) for x in keys_b) + " |")
    L.append("\n## Not run\n- Holdout (sealed). Elliott waves, historical levels, polarity switch, confluence (later tests of Study C), other swing widths, EURUSD, context-matched fake levels.\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _frame15_from(df5):
    return df5[["open", "high", "low", "close"]].resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna().assign(
        gap_before_missing=0, closure_before=False)


def _series3(df5):
    s5, s1h = sa._series_from_df(df5)
    return s5, rm.Series(_frame15_from(df5), 15), s1h


def _hand_S(h, l, o, c, atr=0.25):
    n = len(h)
    return {"o": np.asarray(o, float), "h": np.asarray(h, float), "l": np.asarray(l, float), "c": np.asarray(c, float),
            "ns": np.arange(n, dtype=np.int64) * STEP15, "atr": np.full(n, atr)}


def selftest(null_series=10):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    # ---- hand-built impulse: swing low A=1.0 at bar 4 (k=3 -> confirmed at 7), rally to M=2.0, R=1.0, atr 0.25 (R/atr = 4)
    #        bars: 0-3 above 1.0, bar 4 low 1.0, bars 5-7 above, then rally.
    base_l = [1.4, 1.3, 1.2, 1.1, 1.0, 1.1, 1.2, 1.3]
    base_h = [1.5, 1.4, 1.3, 1.2, 1.1, 1.3, 1.4, 1.5]
    base_o = [1.45, 1.35, 1.25, 1.15, 1.05, 1.15, 1.3, 1.4]
    base_c = [1.4, 1.3, 1.2, 1.1, 1.1, 1.3, 1.4, 1.5]
    # rally bars 8-10 to M=2.0
    rally_h, rally_l, rally_o, rally_c = [1.7, 1.9, 2.0], [1.5, 1.7, 1.9], [1.5, 1.7, 1.9], [1.7, 1.9, 1.95]

    def mk(extra):
        h = base_h + rally_h + [e[0] for e in extra]
        l = base_l + rally_l + [e[1] for e in extra]
        o = base_o + rally_o + [e[2] for e in extra]
        c = base_c + rally_c + [e[3] for e in extra]
        return _hand_S(h, l, o, c)
    S0 = mk([(1.95, 1.60, 1.95, 1.65)])          # bar 11 dips to 1.60: retracement (2.0-1.60)/1.0 = 0.40 -> entry of the golden zone
    chk("pivot low found only with k bars each side (and is known at i+k)", list(pivots_s(S0["l"], S0["ns"], 3, STEP15, "low")), [4])
    e, why = fib_scan(S0, 4, 0.382, 0.618, "P")
    chk("entry bar is never the exit bar", (e, why), (None, "timeout"))
    S1 = mk([(1.95, 1.60, 1.95, 1.65), (1.8, 1.60, 1.65, 1.75)])      # bar 12: close 1.75 > level 1.618 -> plain exit
    e, why = fib_scan(S1, 4, 0.382, 0.618, "P")
    chk("plain exit fires at the first close above the shallow edge", (e["t15"], round(e["M"], 6), round(e["depth"], 6), round(e["lvl"], 6)), (12, 2.0, 0.4, 1.618))
    S2 = mk([(1.95, 1.60, 1.95, 1.65), (1.8, 1.62, 1.65, 1.7)])       # exit bar body 0.05 < 0.3*0.25 -> not strong
    chk("strong exit rejects a weak bar", fib_scan(S2, 4, 0.382, 0.618, "S")[0], None)
    S3 = mk([(1.95, 1.60, 1.95, 1.65), (1.9, 1.62, 1.63, 1.89)])      # body 0.26>=0.075, close in the top 25% of range 0.28 -> strong
    chk("strong exit accepts body>=0.3 ATR with the close in the top 25%", fib_scan(S3, 4, 0.382, 0.618, "S")[0]["t15"], 12)
    chk("displacement exit needs a close above the previous 4 highs (here not)", fib_scan(S1, 4, 0.382, 0.618, "D")[0], None)
    S4 = mk([(1.95, 1.60, 1.95, 1.65), (1.9, 1.62, 1.63, 1.89), (2.1, 1.85, 1.9, 2.05)])
    chk("displacement exit fires when the close exceeds the previous 4 highs", fib_scan(S4, 4, 0.382, 0.618, "D")[0]["t15"], 13)
    S5 = mk([(1.95, 1.60, 1.95, 1.65), (1.7, 1.30, 1.65, 1.35), (1.8, 1.3, 1.35, 1.75)])    # pulls back to 70% (0.70 > 0.618) before exiting
    chk("a pullback deeper than the zone aborts (too_deep)", fib_scan(S5, 4, 0.382, 0.618, "P")[1], "too_deep")
    S6 = mk([(1.95, 1.60, 1.95, 1.65), (1.7, 0.95, 1.65, 1.0)])      # takes out A
    chk("price at or below A aborts (structure)", fib_scan(S6, 4, 0.382, 0.618, "P")[1], "structure")
    S7 = mk([(2.2, 1.9, 1.95, 2.1), (2.2, 1.95, 2.1, 2.15)])           # new high before any entry: M extends, no entry
    chk("a rally that never pulls back produces no event", fib_scan(S7, 4, 0.382, 0.618, "P")[0], None)
    S8 = mk([(2.3, 1.62, 1.95, 2.25)])                                 # one bar reaches the zone AND makes a new high: skipped
    chk("a bar that reaches the zone and makes a new high is skipped", fib_scan(S8, 4, 0.382, 0.618, "P")[0], None)
    S9 = mk([(1.95, 1.60, 1.95, 1.65), (2.1, 1.58, 1.7, 1.61), (2.0, 1.6, 1.61, 1.7), (1.95, 1.65, 1.7, 1.9)])
    e9, why9 = fib_scan(S9, 4, 0.382, 0.618, "P")
    chk("a new high during the pullback resets the impulse (the exit then uses the new M)", (e9 is not None, None if e9 is None else round(e9["M"], 6)), (True, 2.1))
    Ssm = _hand_S(*[[x for x in arr] for arr in (S1["h"], S1["l"], S1["o"], S1["c"])], atr=2.0)
    chk("an impulse smaller than LEG_MIN ATR is never entered", fib_scan(Ssm, 4, 0.382, 0.618, "P")[0], None)
    # no look-ahead: changing bars after the exit bar leaves the event untouched
    e1, _ = fib_scan(S1, 4, 0.382, 0.618, "P")
    S1c = mk([(1.95, 1.60, 1.95, 1.65), (1.8, 1.60, 1.65, 1.75), (9.0, 0.1, 1.0, 5.0)])
    e1c, _ = fib_scan(S1c, 4, 0.382, 0.618, "P")
    chk("no look-ahead: bars after the exit bar do not change the event", (e1["t15"], e1["M"], e1["depth"]) == (e1c["t15"], e1c["M"], e1c["depth"]), True)
    Sg = mk([(1.95, 1.60, 1.95, 1.65), (1.8, 1.60, 1.65, 1.75)])
    Sg["ns"][11:] += 24 * 3600 * 10 ** 9
    chk("a gap inside the leg aborts the scan", fib_scan(Sg, 4, 0.382, 0.618, "P")[1], "gap")
    # ---- cluster statistics
    rr = np.random.default_rng(2)
    t_ = np.sort(rr.integers(0, 400, 800)) * BLOCK_NS + rr.integers(0, BLOCK_NS, 800)
    dd = rr.normal(0, 1, 800)
    ct = cluster_test(dd, t_)
    chk("cluster test: independent noise gives |z| < 4 and uses the cluster count", (abs(ct["z"]) < 4, ct["n_clusters"] > 100), (True, True))
    big = np.repeat(rr.normal(0, 1, 100), 8) + rr.normal(0, 0.05, 800)           # 100 clusters, each 8 near-identical events
    tb = np.repeat(np.arange(100), 8) * BLOCK_NS
    chk("cluster test: perfectly dependent events do not inflate precision (se about 1/sqrt(100), not 1/sqrt(800))", 0.07 < cluster_test(big, tb)["se"] < 0.2, True)
    cd = cluster_diff(dd[:400], t_[:400], dd[400:], t_[400:])
    chk("cluster diff: no true difference, |z| < 4", abs(cd["z"]) < 4, True)
    # ---- synthetic worlds
    far = pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value
    rng = np.random.default_rng(31)
    d5 = sb._synth_ohlc(60000, rng)
    s5, s15, s1h = _series3(d5)
    ctx = make_ctx(s5, s15, s1h, +1, far)
    ev, cnt = detect_fib(ctx, "golden", "P", "dev")
    chk(f"synthetic random walk produces golden/plain events (>= 30, got {len(ev)})", len(ev) >= 30, True)
    chk("every decision is on a 15m close (5m bar opens 10 minutes into a 15m bar)", all(int(ctx["ns5"][e["p"]]) % STEP15 == 10 * 60 * 10 ** 9 for e in ev), True)
    chk("geometry gates hold for every event", all(GEOM_MIN <= e["a"] <= GEOM_MAX and GEOM_MIN <= e["b"] <= GEOM_MAX for e in ev), True)
    chk("zone depth is within [lo, hi] for every golden event", all(0.382 - 1e-9 <= e["depth"] <= 0.618 + 1e-9 for e in ev), True)
    # no look-ahead on the whole pipeline
    T = int(_ns(s5.index)[30000])
    d5b = d5.copy()
    k0 = int(np.searchsorted(d5.index.values.astype("datetime64[ns]").astype("int64"), T))
    for col in ("open", "high", "low", "close"):
        d5b.iloc[k0:, d5b.columns.get_loc(col)] += 0.2
    s5b, s15b, s1hb = _series3(d5b)
    ctxb = make_ctx(s5b, s15b, s1hb, +1, far)
    evb, _ = detect_fib(ctxb, "golden", "P", "dev")
    a_ = [(e["t"], round(e["M"], 9), round(e["a"], 6)) for e in ev if e["t"] < T - 4 * NS_H]
    b_ = [(e["t"], round(e["M"], 9), round(e["a"], 6)) for e in evb if e["t"] < T - 4 * NS_H]
    chk("no look-ahead: golden events before T are identical when the future is altered", (a_ == b_, len(a_) > 10), (True, True))
    # mirror
    ctx_dn = make_ctx(s5, s15, s1h, -1, far)
    dn = d5.copy()
    dn["open"], dn["close"], dn["high"], dn["low"] = -d5["open"], -d5["close"], -d5["low"], -d5["high"]
    s5n, s15n, s1hn = _series3(dn)
    ctx_ng = make_ctx(s5n, s15n, s1hn, +1, far)
    e_dn, _ = detect_fib(ctx_dn, "golden", "S", "dev")
    e_ng, _ = detect_fib(ctx_ng, "golden", "S", "dev")
    chk("down side == up side of the negated series", ([(e["t"], round(e["a"], 6)) for e in e_dn] == [(e["t"], round(e["a"], 6)) for e in e_ng], len(e_dn) > 5), (True, True))
    hold_mid = int(_ns(s5.index)[40000])
    ctxh = make_ctx(s5, s15, s1h, +1, hold_mid)
    evd, _ = detect_fib(ctxh, "golden", "P", "dev")
    evh, _ = detect_fib(ctxh, "golden", "P", "hold")
    chk("dev events end their window before the boundary, holdout events start after it", (all(e["t"] + HMAX * NS_5 <= hold_mid for e in evd), all(e["t"] >= hold_mid for e in evh), len(evh) > 5), (True, True, True))
    # BOS machinery on 15m swings
    brk, bc = bos_breaks(ctx)
    chk("15m BOS: breaks found on a random walk (>= 30)", len(brk) >= 30, True)
    chk("15m BOS: a break happens after the swing is confirmed (j >= i + k + 1)", all(b["j"] >= b["i"] + K15 + 1 for b in brk), True)
    chk("15m BOS: break close is above the broken high", all(ctx["S"]["c"][b["j"]] > b["H"] for b in brk), True)
    # ---- null calibration (random walk and GARCH): all tests, whole pipeline
    zs, obs, plc, ns_ev = [], [], [], []
    for k in range(null_series):
        d_ = sb._synth_ohlc(130000, rng, garch=(k % 2 == 1))
        s5_, s15_, s1_ = _series3(d_)
        r_ = run_from_series(s5_, s15_, s1_, "PRIMARY_READY", far)
        for t_ in list(r_["C1"].values()) + list(r_["C2"].values()):
            if t_.get("z") is not None:
                zs.append(t_["z"])
                obs.append(t_["obs_rate"])
                plc.append(t_["placebo_rate"])
                ns_ev.append(t_["n"])
        for t_ in r_["C3"].values():
            if t_.get("z") is not None:
                zs.append(t_["z"])
    allz = np.asarray(zs, float)
    chk(f"null: enough tests ({len(allz)})", len(allz) >= 60, True)
    if len(allz) >= 60:
        chk(f"null: |mean z| < 0.4 (got {allz.mean():+.2f})", abs(allz.mean()) < 0.4, True)
        chk(f"null: sd(z) in [0.6, 1.5] (got {allz.std():.2f})", 0.6 <= allz.std() <= 1.5, True)
        chk(f"null: share |z|>1.96 <= 12% (got {(np.abs(allz) > 1.96).mean():.0%})", (np.abs(allz) > 1.96).mean() <= 0.12, True)
    # ---- planted effect: drift toward M after a share of golden/plain decisions
    dfp = sb._synth_ohlc(130000, np.random.default_rng(8))
    sP5, sP15, sP1 = _series3(dfp)
    ctxp = make_ctx(sP5, sP15, sP1, +1, far)
    evp, _ = detect_fib(ctxp, "golden", "P", "dev")
    n5 = len(dfp)
    shift_c = np.zeros(n5)
    tsel = np.random.default_rng(9)
    chosen = [e for e in evp if tsel.random() < 0.6]
    tt_ = np.arange(n5)
    for e in chosen:
        shift_c += 0.30 * 0.00025 * np.clip(tt_ - e["p"], 0, 48)
    dfp2 = dfp.copy()
    so = np.concatenate([[0.0], shift_c[:-1]])
    dfp2["close"] = dfp["close"] + shift_c
    dfp2["open"] = dfp["open"] + so
    dfp2["high"] = np.maximum(dfp["high"] + shift_c, np.maximum(dfp2["open"], dfp2["close"]))
    dfp2["low"] = np.minimum(dfp["low"] + shift_c, np.minimum(dfp2["open"], dfp2["close"]))
    sQ5, sQ15, sQ1 = _series3(dfp2)
    rP = run_from_series(sQ5, sQ15, sQ1, "PRIMARY_READY", far)
    tp = rP["C1"]["P"]
    chk(f"planted drift after golden/plain decisions: excess detected (z={tp.get('z')})", (tp.get("z") or 0) > 3 and tp["mean"] > 0, True)
    chk("planted drift: FDR rejects that test", bool(tp.get("fdr_reject")), True)
    chk("planted drift: the effect is specific to the golden zone (C.3 contrast positive)", (rP["C3"]["P"].get("diff") or 0) > 0, True)
    s_ = json.dumps(rP, default=str)
    chk("report renders and JSON serialises", (len(render_markdown(rP)) > 800, len(s_) > 800), (True, True))
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": null_series, "tests": int(len(allz)), "mean_z": round(float(allz.mean()), 3) if len(allz) else None,
                     "sd_z": round(float(allz.std()), 3) if len(allz) else None, "share_abs_z_gt_1.96": round(float((np.abs(allz) > 1.96).mean()), 3) if len(allz) else None,
                     "obs_rate_mean": round(float(np.mean(obs)), 3) if obs else None, "placebo_rate_mean": round(float(np.mean(plc)), 3) if plc else None,
                     "mean_events_per_test": round(float(np.mean(ns_ev)), 0) if ns_ev else None},
            "planted": {"golden_plain_z": round(float(tp.get("z") or 0), 2), "excess": round(float(tp["mean"]), 3), "events": tp["n"], "injected": len(chosen)}}


def main():
    ap = argparse.ArgumentParser(description="Observatory Study C (Fibonacci zones, pullback depth at higher power). Read-only on raw data; the holdout stays sealed.")
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
    res = run_studyc(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "studyc_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "studyc_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
