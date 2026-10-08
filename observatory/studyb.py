"""
observatory/studyb.py
=====================
OBSERVATORY — STUDY B: structure break (BOS) and pullback penetration. ISOLATED from the live bot. Tests inside a study are B.1, B.2, ...

THE QUESTION
------------
"Price breaks a swing high, pulls back, may even trade back through the broken high, and still continues as long as the swing low that
started the move survives."  Does the DEPTH of the pullback relative to the broken high carry information about what happens next?
Everything is measured against a placebo that has the same geometry, the same clock slot and the same recent move but NO structure.

DEFINITIONS (mechanical, causal, fixed before any outcome is read)
-------------------------------------------------------------------
  Pivot high   a 1h bar whose high is the strict maximum of the K_PIV=3 bars on each side. KNOWN only at the close of bar i+K_PIV.
  BOS (up)     the first 1h CLOSE above an unbroken pivot high H (pivot younger than MAX_AGE=48 bars; when one close breaks several pivots
               the most recent one is used). Known at that close, t_b. Down-BOS is the exact mirror (prices negated).
  L            structural low = lowest low of the 1h bars after the pivot up to the break bar. M = highest high since the pivot break
               (running, so it only uses the past). Required: 1 <= (H-L)/ATR <= 6 (ATR = 1h ATR14 at the break).
  Depth d      levels d in {-0.25, 0, +0.25, +0.5, +1.0} ATR BELOW H (-0.25 = a retest that stays above H; 0 = touches H; +x = penetrates).
  FIRST PASSAGE  the first 5m bar after t_b whose LOW is at or below the level H - d*ATR (within 48h, and not already at or below L). The
               decision is taken at that bar's CLOSE c. a = (M - c)/ATR (room to a new high), b = (c - L)/ATR (room to the structural low).
               Both must lie in [0.25, 5].
  Outcome      1 if price trades above M before it trades below L, 0 if L goes first, from the next 5m bar, within 24h. Both in the same
               bar, or neither within 24h: unresolved (counted, excluded) - the SAME rule is applied to the placebo.
  PLACEBO      the same barriers (a ATR above, b ATR below the close) placed at R=30 random 5m closes with the same weekday, the same hour
               (+-1h) and the same trailing 1h move (+-0.5 ATR), outside +-24h of the event. Excess = outcome - mean(placebo outcomes).
               Under no structure the excess has mean 0, whatever the geometry (a, b) is.

TESTS
-----
  B.1  (family 1, 1 test)    BOS vs placebo: from the close of the break bar, does price reach +1 ATR before -1 ATR (in the break direction)
                              more often than at placebo times with the same clock slot and the same trailing 1h move?
  B.2  (family 1, 5 tests)   pullback depth profile: excess continuation (new high before the structural low) at each depth.
  B.4  (family 2, EXPLORATORY, 6 tests) at depth 0: does the excess differ by impulse size, time to pullback, age of the broken high,
                              4h efficiency before the break, side, session?
  A test is a mean of per-event excesses (n >= 10). Events are thinned so that no two events of a test fall within 24h of each other.
  Reported with each: observed rate, placebo rate, excess, 95% CI, excess in pips, the realised pips of "buy at the close of the passage bar,
  target new high, stop at L" net of the flat 1 pip cost, first vs second half, up vs down side, and a missingness audit.

WHAT B CANNOT SEPARATE (pre-declared)
  B.2 measures the whole setup (impulse + broken level + pullback) against bare geometry. An excess does not prove the BROKEN HIGH is the
  cause: a follow-up with context-matched fake levels would be needed. No excess means structure adds nothing at all.

PRE-DECLARED READING
  No family-1 test survives FDR -> no evidence that a break of structure, or any pullback depth, predicts anything beyond geometry,
                                   clock and the recent move. Depth cut-offs from the video cannot be justified.
  Some survive                  -> the profile says at which depth, and the pips columns say whether it could matter after cost.
The holdout (>= HOLDOUT_START_UTC) is sealed: only events whose whole outcome window ends before it are used. --readiness counts holdout EVENTS.

Run:  python3 -m observatory.studyb [--raw-dir ...] [--out ...]   python3 -m observatory.studyb --selftest   python3 -m observatory.studyb --readiness
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
from . import wave1 as w1
from . import studya as sa
from .studya import mean_test, verdict, _ns, NS_H, ATR_STALE, PIP, SPREAD_PIPS, FDR_Q, load_series, _phi, _a, _ci, _p, _pc

K_PIV = 3
MAX_AGE = 48
RANGE_MIN, RANGE_MAX = 1.0, 6.0
DEPTHS = (-0.25, 0.0, 0.25, 0.5, 1.0)
GEOM_MIN, GEOM_MAX = 0.25, 5.0
WAIT = 576          # 5m bars (48h) to reach a level after the break
HMAX = 288          # 5m bars (24h) outcome window
TRAIL = 12          # 5m bars (1h) trailing move used for matching
TRAIL_TOL = 0.5
R_PLACEBO = 30
MIN_PLACEBO = 8
MIN_POOL = 10
B1_BARRIER = 1.0
MIN_EFFECT = 0.05
NS_5 = 5 * 60 * 10 ** 9
NS_D = 24 * NS_H
SEED = 20260418
CONTRACT = {
    "study": "B", "module": "studyb", "version": "v1", "instrument": "GBPUSD", "pivot_bars_each_side": K_PIV, "max_age_bars": MAX_AGE,
    "range_atr": [RANGE_MIN, RANGE_MAX], "depths_atr_below_H": list(DEPTHS), "geometry_atr": [GEOM_MIN, GEOM_MAX], "wait_bars_5m": WAIT,
    "outcome_window_bars_5m": HMAX, "placebo": {"R": R_PLACEBO, "same": "weekday, hour+-1, trailing-1h move +-0.5 ATR", "exclude_hours": 24},
    "b1_barrier_atr": B1_BARRIER, "spacing": "no two events of a test within 24h", "min_effect": MIN_EFFECT, "spread_pips_flat": SPREAD_PIPS,
    "fdr_q": FDR_Q, "family1": ["B.1", "B.2 x 5 depths"], "family2": ["B.4 x 6"], "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]
B4_NAMES = ("impulse", "wait", "age", "efficiency4h", "side", "session")


def weekday(ns):
    """Monday = 0 (1970-01-01 was a Thursday)."""
    return ((np.asarray(ns, dtype=np.int64) // NS_D) + 3) % 7


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------
def pivot_highs(h, ns, k=K_PIV):
    """Indices i whose high is the strict maximum of bars i-k..i+k, all 2k+1 bars contiguous hourly. A pivot is known only at i+k."""
    out = []
    for i in range(k, len(h) - k):
        w = h[i - k:i + k + 1]
        if h[i] == w.max() and int((w == h[i]).sum()) == 1 and ns[i + k] - ns[i - k] == 2 * k * NS_H:
            out.append(i)
    return np.asarray(out, dtype=int)


def find_bos(h, l, c, atr, ns, k=K_PIV, max_age=MAX_AGE, rng_min=RANGE_MIN, rng_max=RANGE_MAX):
    """Up-BOS events on 1h arrays. Returns (events, counters). Event j = break bar, i = pivot bar; uses bars up to j only."""
    cnt = {"pivots": 0, "breaks": 0, "range_out": 0, "atr_missing": 0}
    first = {}
    piv = pivot_highs(h, ns, k)
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
        if not (rng_min <= r <= rng_max):
            cnt["range_out"] += 1
            continue
        ev.append({"j": j, "i": i, "H": float(H), "L": L, "atr": float(a), "M0": float(h[j]), "age": j - i, "range_atr": float(r)})
    return ev, cnt


def barrier(h5, l5, start, up, dn, hmax=HMAX):
    """First barrier touched from bar `start` on: (1.0 up first | 0.0 down first | nan, status)."""
    sh, sl = h5[start:start + hmax], l5[start:start + hmax]
    u, d = np.flatnonzero(sh > up), np.flatnonzero(sl < dn)
    iu = int(u[0]) if u.size else None
    idn = int(d[0]) if d.size else None
    if iu is None and idn is None:
        return np.nan, "unresolved"
    if iu is None:
        return 0.0, "ok"
    if idn is None:
        return 1.0, "ok"
    if iu == idn:
        return np.nan, "ambiguous"
    return (1.0 if iu < idn else 0.0), "ok"


def window_ok(ns5, start, hmax=HMAX):
    return start + hmax <= len(ns5) and ns5[start + hmax - 1] - ns5[start] == (hmax - 1) * NS_5


def trail_ok(ns5, r, trail=TRAIL):
    return r >= trail and ns5[r] - ns5[r - trail] == trail * NS_5


def first_passage(h5, l5, c5, q, H, L, M0, atr, depth, wait=WAIT):
    """First 5m bar p >= q with low <= H - depth*atr. Returns (dict | None, reason)."""
    level = H - depth * atr
    seg = l5[q:q + wait]
    hit = np.flatnonzero(seg <= level)
    if hit.size == 0:
        return None, "no_pullback"
    p = q + int(hit[0])
    if l5[p] <= L:
        return None, "structure_first"
    M = max(M0, float(h5[q:p].max())) if p > q else M0
    a, b = (M - c5[p]) / atr, (c5[p] - L) / atr
    if not (GEOM_MIN <= a <= GEOM_MAX and GEOM_MIN <= b <= GEOM_MAX):
        return None, "geometry"
    return {"p": p, "M": float(M), "a": float(a), "b": float(b)}, None


def thin(times_ns, gap_ns=HMAX * NS_5):
    """Greedy thinning in time order: keep an event only if it is at least gap_ns after the last kept one. Returns kept positions."""
    order = np.argsort(times_ns, kind="mergesort")
    keep, last = [], None
    for k in order:
        if last is None or times_ns[k] - last >= gap_ns:
            keep.append(int(k))
            last = times_ns[k]
    return sorted(keep)


# ---------------------------------------------------------------------------
# series context (one per side: the down side is the exact mirror, prices negated)
# ---------------------------------------------------------------------------
def atr_at(ns_t, ns1, atr1):
    j = np.searchsorted(ns1, ns_t - NS_H, side="right") - 1
    jc = np.clip(j, 0, None)
    fresh = (j >= 0) & ((ns_t - (ns1[jc] + NS_H)) <= ATR_STALE.value)
    return np.where(fresh, atr1[jc], np.nan)


def make_ctx(s5, s1h, sign, hold_ns):
    ns5, ns1 = _ns(s5.index), _ns(s1h.index)
    if sign > 0:
        A = {"o5": s5.o, "h5": s5.h, "l5": s5.l, "c5": s5.c, "h1": s1h.h, "l1": s1h.l, "c1": s1h.c}
    else:
        A = {"o5": -s5.o, "h5": -s5.l, "l5": -s5.h, "c5": -s5.c, "h1": -s1h.l, "l1": -s1h.h, "c1": -s1h.c}
    atr_t = atr_at(ns5, ns1, s1h.atr)
    n = len(ns5)
    okw = np.zeros(n, bool)
    m = n - HMAX - 1
    if m > 0:
        q = np.arange(m)
        okw[:m] = (ns5[q + HMAX] - ns5[q + 1]) == (HMAX - 1) * NS_5
    okt = np.zeros(n, bool)
    okt[TRAIL:] = (ns5[TRAIL:] - ns5[:-TRAIL]) == TRAIL * NS_5
    x = np.full(n, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        x[TRAIL:] = (A["c5"][TRAIL:] - A["c5"][:-TRAIL]) / atr_t[TRAIL:]
    good = np.isfinite(atr_t) & (atr_t > 0) & okw & okt & (ns5 + (HMAX + 1) * NS_5 <= hold_ns)
    qs = np.flatnonzero(good)
    key = weekday(ns5[qs]) * 24 + (ns5[qs] // NS_H) % 24
    order = np.argsort(key, kind="stable")
    qs, key = qs[order], key[order]
    bounds = np.flatnonzero(np.diff(key)) + 1
    pools = {}
    for chunk in np.split(np.arange(len(qs)), bounds):
        if len(chunk):
            pools[int(key[chunk[0]])] = (qs[chunk], x[qs[chunk]], atr_t[qs[chunk]])
    return {"A": A, "ns5": ns5, "ns1": ns1, "atr1": s1h.atr, "atr_t": atr_t, "x": x, "pools": pools, "hold_ns": hold_ns, "sign": sign, "n": n}


def break_events(ctx):
    """Up-BOS (in this ctx's orientation) mapped to the 5m clock. Returns (events, counters)."""
    A, ns5, ns1 = ctx["A"], ctx["ns5"], ctx["ns1"]
    raw, cnt = find_bos(A["h1"], A["l1"], A["c1"], ctx["atr1"], ns1)
    out = []
    cnt["no_5m_at_break"] = 0
    for b in raw:
        tb = int(ns1[b["j"]] + NS_H)
        q = int(np.searchsorted(ns5, tb))
        if q >= len(ns5) or q < 1 or ns5[q] != tb or ns5[q - 1] != tb - NS_5:
            cnt["no_5m_at_break"] += 1
            continue
        b = dict(b, t_b=tb, q=q, side=ctx["sign"])
        out.append(b)
    return out, cnt


def draw_placebo(ctx, t_ns, x_e, rng):
    """Placebo reference bars: same weekday, hour +-1, trailing move within TRAIL_TOL, more than 24h away. Returns (q array, atr array) or None."""
    qs, xs, ats = [], [], []
    for dh in (-1, 0, 1):
        t = t_ns + dh * NS_H
        key = int(weekday(t)) * 24 + int((t // NS_H) % 24)
        if key in ctx["pools"]:
            q, x, a = ctx["pools"][key]
            qs.append(q), xs.append(x), ats.append(a)
    if not qs:
        return None
    q, x, a = np.concatenate(qs), np.concatenate(xs), np.concatenate(ats)
    m = (np.abs(x - x_e) <= TRAIL_TOL) & (np.abs(ctx["ns5"][q] - t_ns) > HMAX * NS_5)
    q, a = q[m], a[m]
    if len(q) < MIN_POOL:
        return None
    take = rng.choice(len(q), size=min(R_PLACEBO, len(q)), replace=False)
    return q[take], a[take]


def placebo_mean(ctx, t_ns, x_e, a, b, rng):
    pl = draw_placebo(ctx, t_ns, x_e, rng)
    if pl is None:
        return None, 0
    A = ctx["A"]
    vals = []
    for q, at in zip(*pl):
        c = A["c5"][q]
        o, st = barrier(A["h5"], A["l5"], q + 1, c + a * at, c - b * at)
        if st == "ok":
            vals.append(o)
    if len(vals) < MIN_PLACEBO:
        return None, len(vals)
    return float(np.mean(vals)), len(vals)


def _efficiency(A, ns5, q, nb=48):
    if q < nb or ns5[q] - ns5[q - nb] != nb * NS_5:
        return np.nan
    o = A["o5"][q - nb:q + 1]
    path = np.abs(np.diff(o)).sum()
    return float(abs(o[-1] - o[0]) / path) if path > 0 else np.nan


def detect_events(ctx, depth, region="dev"):
    """All first-passage events at a depth (structure only, no outcome). region 'dev' keeps events whose outcome window ends before the
    holdout boundary; 'hold' keeps events that start at/after it (counts only). Returns (events, counters)."""
    A, ns5 = ctx["A"], ctx["ns5"]
    brk, bcnt = break_events(ctx)
    cnt = {"breaks": len(brk), "no_pullback": 0, "structure_first": 0, "geometry": 0, "outside_region": 0, "no_trail": 0, "window": 0}
    ev = []
    for b in brk:
        pas, why = first_passage(A["h5"], A["l5"], A["c5"], b["q"], b["H"], b["L"], b["M0"], b["atr"], depth)
        if pas is None:
            cnt[why] += 1
            continue
        p = pas["p"]
        t = int(ns5[p])
        if region == "dev":
            if t + (HMAX + 1) * NS_5 > ctx["hold_ns"]:
                cnt["outside_region"] += 1
                continue
            if not window_ok(ns5, p + 1):
                cnt["window"] += 1
                continue
        else:
            if t < ctx["hold_ns"]:
                cnt["outside_region"] += 1
                continue
        if not trail_ok(ns5, p):
            cnt["no_trail"] += 1
            continue
        ev.append(dict(b, **pas, t=t, depth=depth, x=float(ctx["x"][p]), c=float(A["c5"][p])))
    return ev, cnt, bcnt


def score_event(ctx, ev, kind, rng):
    """kind 'pull': barriers M above / L below the close of the passage bar. kind 'bos': +-B1_BARRIER ATR from the close of the break bar."""
    A, ns5 = ctx["A"], ctx["ns5"]
    if kind == "pull":
        r, a, b = ev["p"], ev["a"], ev["b"]
        o, st = barrier(A["h5"], A["l5"], r + 1, ev["M"], ev["L"])
        t, x = ev["t"], ev["x"]
        atr = ev["atr"]
    else:
        r, a, b = ev["q"] - 1, B1_BARRIER, B1_BARRIER
        atr = ev["atr"]
        c = A["c5"][r]
        o, st = barrier(A["h5"], A["l5"], r + 1, c + a * atr, c - b * atr)
        t = int(ns5[r])
        x = float((A["c5"][r] - A["c5"][r - TRAIL]) / atr) if trail_ok(ns5, r) else np.nan
    if st != "ok":
        return None, st
    if not np.isfinite(x):
        return None, "no_trail"
    pm, npl = placebo_mean(ctx, t, x, a, b, rng)
    if pm is None:
        return None, "no_placebo"
    return {"o": o, "pm": pm, "d": o - pm, "a": a, "b": b, "atr": atr, "t": t, "n_pl": npl, "side": ev["side"]}, "ok"


# ---------------------------------------------------------------------------
# one full pass over a (5m, 1h) pair
# ---------------------------------------------------------------------------
def collect(s5, s1h, hold_ns, seed=SEED):
    ctxs = [make_ctx(s5, s1h, +1, hold_ns), make_ctx(s5, s1h, -1, hold_ns)]
    out = {"tests": {}, "audit": {}, "breaks": {}}
    # ---- B.1
    rows, au = [], {"breaks": 0, "no_5m_at_break": 0, "range_out": 0, "outside_region": 0, "unresolved": 0, "ambiguous": 0, "no_trail": 0, "no_placebo": 0, "thinned": 0, "used": 0}
    cand = []
    for ctx in ctxs:
        brk, bc = break_events(ctx)
        au["breaks"] += bc["breaks"]
        au["no_5m_at_break"] += bc["no_5m_at_break"]
        au["range_out"] += bc["range_out"]
        for b in brk:
            r = b["q"] - 1
            if ctx["ns5"][r] + (HMAX + 1) * NS_5 > hold_ns or not window_ok(ctx["ns5"], r + 1):
                au["outside_region"] += 1
                continue
            cand.append((ctx, b))
    keep = thin(np.array([c["ns5"][b["q"] - 1] for c, b in cand], dtype=np.int64)) if cand else []
    au["thinned"] = len(cand) - len(keep)
    for k in keep:
        ctx, b = cand[k]
        rng = np.random.default_rng(seed + int(ctx["ns5"][b["q"]] % 2 ** 31) + (1 if ctx["sign"] < 0 else 0))
        s, why = score_event(ctx, b, "bos", rng)
        if s is None:
            au[why] += 1
        else:
            au["used"] += 1
            rows.append(dict(s, depth="BOS", H=b["H"], j=b["j"]))
    out["tests"]["B.1"], out["audit"]["B.1"] = rows, au
    # ---- B.2 at each depth
    for d in DEPTHS:
        cand, au = [], {"breaks": 0, "no_pullback": 0, "structure_first": 0, "geometry": 0, "outside_region": 0, "no_trail": 0, "window": 0,
                        "thinned": 0, "unresolved": 0, "ambiguous": 0, "no_placebo": 0, "used": 0}
        for ctx in ctxs:
            ev, cnt, _ = detect_events(ctx, d, "dev")
            for k in ("breaks", "no_pullback", "structure_first", "geometry", "outside_region", "no_trail", "window"):
                au[k] += cnt[k]
            cand += [(ctx, e) for e in ev]
        keep = thin(np.array([e["t"] for _, e in cand], dtype=np.int64)) if cand else []
        au["thinned"] = len(cand) - len(keep)
        rows = []
        for k in keep:
            ctx, e = cand[k]
            rng = np.random.default_rng(seed + int(e["t"] % 2 ** 31) + 7 * int(round((d + 1) * 100)) + (1 if ctx["sign"] < 0 else 0))
            s, why = score_event(ctx, e, "pull", rng)
            if s is None:
                au[why] += 1
                continue
            au["used"] += 1
            rows.append(dict(s, depth=d, impulse=(e["M"] - e["H"]) / e["atr"], wait=e["p"] - e["q"], age=e["age"], eff4=_efficiency(ctx["A"], ctx["ns5"], e["q"]),
                             hour=int((ctx["ns5"][e["q"]] // NS_H) % 24), rng=e["range_atr"]))
        out["tests"][f"B.2@{d:+.2f}"], out["audit"][f"B.2@{d:+.2f}"] = rows, au
    return out


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------
def welch(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    out = {"diff": None, "se": None, "ci": None, "z": None, "p": None, "n": 0, "n1": len(a), "n0": len(b), "n_min": min(len(a), len(b))}
    if len(a) < 10 or len(b) < 10:
        return out
    diff = float(a.mean() - b.mean())
    se = math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    z = diff / se if se > 0 else 0.0
    out.update(diff=diff, se=se, ci=(diff - 1.96 * se, diff + 1.96 * se), z=z, p=w1.norm_p(z), n=len(a) + len(b))
    return out


def summarize(rows):
    """mean_test of the per-event excess plus descriptive columns."""
    d = np.array([r["d"] for r in rows], float)
    t = mean_test(d)
    if not rows:
        return t
    o, pm = np.array([r["o"] for r in rows]), np.array([r["pm"] for r in rows])
    a, b, at = np.array([r["a"] for r in rows]), np.array([r["b"] for r in rows]), np.array([r["atr"] for r in rows]) / PIP
    t.update(obs_rate=float(o.mean()), placebo_rate=float(pm.mean()), obs_ci=w1.wilson(int(o.sum()), len(o)),
             excess_pips=float(np.mean(d * (a + b) * at)),
             setup_net_pips=float(np.mean((o * a - (1 - o) * b) * at) - SPREAD_PIPS),
             placebo_net_pips=float(np.mean((pm * a - (1 - pm) * b) * at) - SPREAD_PIPS),
             mean_a=float(a.mean()), mean_b=float(b.mean()), mean_atr_pips=float(at.mean()))
    ts = np.array([r["t"] for r in rows])
    if len(rows) >= 40:
        mid = np.median(ts)
        t["halves"] = {nm: mean_test(d[m]) for nm, m in (("first_half", ts < mid), ("second_half", ts >= mid))}
    sd = np.array([r["side"] for r in rows])
    t["sides"] = {"up": mean_test(d[sd > 0]), "down": mean_test(d[sd < 0])}
    return t


def b4_tests(rows):
    """Exploratory contrasts at depth 0 (top minus bottom tercile of a pre-event variable, or two groups). Differences in mean excess."""
    out = {}
    d = np.array([r["d"] for r in rows], float)
    if len(rows) < 60:
        return {nm: dict(welch([], []), var=nm) for nm in B4_NAMES}
    for nm, key in (("impulse", "impulse"), ("wait", "wait"), ("age", "age"), ("efficiency4h", "eff4")):
        v = np.array([r[key] for r in rows], float)
        ok = np.isfinite(v)
        lo, hi = np.quantile(v[ok], [1 / 3, 2 / 3])
        t = welch(d[ok & (v >= hi)], d[ok & (v <= lo)])
        t.update(var=nm, edges=(float(lo), float(hi)), mean_top=float(d[ok & (v >= hi)].mean()) if (ok & (v >= hi)).any() else None,
                 mean_bottom=float(d[ok & (v <= lo)].mean()) if (ok & (v <= lo)).any() else None)
        out[nm] = t
    side = np.array([r["side"] for r in rows])
    t = welch(d[side > 0], d[side < 0])
    t.update(var="side", mean_top=float(d[side > 0].mean()) if (side > 0).any() else None, mean_bottom=float(d[side < 0].mean()) if (side < 0).any() else None)
    out["side"] = t
    hr = np.array([r["hour"] for r in rows])
    sess = (hr >= 7) & (hr < 16)
    t = welch(d[sess], d[~sess])
    t.update(var="session", mean_top=float(d[sess].mean()) if sess.any() else None, mean_bottom=float(d[~sess].mean()) if (~sess).any() else None)
    out["session"] = t
    return out


def apply_fdr(res, status):
    fam = {1: [("B1", None)] + [("B2", k) for k in res["B2"]], 2: [("B4", k) for k in res["B4"]]}
    counts = {}
    for f, tests in fam.items():
        get = (lambda p: res[p[0]] if p[1] is None else res[p[0]][p[1]])
        live = [p for p in tests if get(p).get("p") is not None]
        rej, q = w1.bh_fdr([get(p)["p"] for p in live]) if live else ([], [])
        counts[f] = len(live)
        for p, rj, qq in zip(live, rej, q):
            r = get(p)
            est = "mean" if p[0] in ("B1", "B2") else "diff"
            nmin = r.get("n") if p[0] in ("B1", "B2") else r.get("n_min", 0)
            r["family"], r["fdr_reject"], r["q_value"] = f, rj, qq
            r["verdict"] = verdict(r[est], r["ci"][0], r["ci"][1], rj, nmin, status if f == 1 else "EXPLORATORY_READY", MIN_EFFECT)
        for p in tests:
            if get(p).get("p") is None:
                get(p)["verdict"] = "DATA-LIMITED"
    return counts


def reading(res):
    s = [("B.1", res["B1"])] + [(f"B.2 depth {k}", v) for k, v in res["B2"].items()]
    s = [(k, v) for k, v in s if v.get("fdr_reject")]
    if not s:
        return ("No family-1 test survives multiple-testing control: no evidence that a break of structure, or any pullback depth relative to the "
                "broken high, predicts the outcome beyond geometry, clock slot and the recent move. Depth cut-offs cannot be justified from this data. B.4 is not interpreted.")
    parts = []
    for k, v in s:
        parts.append(f"{k}: excess {v['mean']:+.3f} (observed {100 * v['obs_rate']:.1f}% vs placebo {100 * v['placebo_rate']:.1f}%), excess {v['excess_pips']:+.2f} pips/event, "
                     f"q={v['q_value']:.3f}, setup net of cost {v['setup_net_pips']:+.2f} pips")
    return ("Survivors: " + "; ".join(parts) + ". These are development-data findings: hypotheses for one holdout test, and only the pips columns say whether they could matter. "
            "They do not show that the BROKEN HIGH is the cause (see 'what B cannot separate').")


def run_from_series(s5, s1h, status, hold_ns):
    col = collect(s5, s1h, hold_ns)
    res = {"B1": summarize(col["tests"]["B.1"]), "B2": {}, "B4": {}}
    for d in DEPTHS:
        res["B2"][f"{d:+.2f}"] = summarize(col["tests"][f"B.2@{d:+.2f}"])
    res["B4"] = b4_tests(col["tests"]["B.2@+0.00"])
    n = apply_fdr(res, status)
    res["audit"] = col["audit"]
    res["reading"] = reading(res)
    res.update({"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False, "n_tests": n})
    return res


def run_studyb(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    s5, s1h, status = load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    res = run_from_series(s5, s1h, status, hold_ns)
    res["development"] = {"first_bar_utc": str(s5.index[0]), "boundary_utc": str(rc.HOLDOUT_START_UTC)}
    return res


def structure_counts(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    """Structure only (no outcome is computed): pivots, breaks, and how many events survive each depth gate, dev and holdout."""
    s5, s1h, status = load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    ctxs = [make_ctx(s5, s1h, +1, hold_ns), make_ctx(s5, s1h, -1, hold_ns)]
    out = {"dataset_status": status, "bars_5m": len(s5.index), "bars_1h": len(s1h.index), "depths": {}}
    tot = {"pivots": 0, "breaks": 0, "range_out": 0, "no_5m_at_break": 0}
    for ctx in ctxs:
        _, c = break_events(ctx)
        for k in tot:
            tot[k] += c[k]
    out["breaks"] = tot
    for d in DEPTHS:
        row = {}
        for region in ("dev", "hold"):
            n_ev, tm, cnts = 0, [], {}
            for ctx in ctxs:
                ev, cnt, _ = detect_events(ctx, d, region)
                tm += [e["t"] for e in ev]
                for k, v in cnt.items():
                    cnts[k] = cnts.get(k, 0) + v
            row[region] = {"events": len(tm), "after_thinning": len(thin(np.array(tm, dtype=np.int64))) if tm else 0, "gates": cnts}
        out["depths"][f"{d:+.2f}"] = row
    return out


# ---------------------------------------------------------------------------
# holdout readiness: EVENT counts (structure only) and power from development estimates
# ---------------------------------------------------------------------------
def holdout_readiness(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None, dev_res=None):
    if dev_res is None:
        dev_res = run_studyb(raw_dir, source, preloaded)
    sc = structure_counts(raw_dir, source, preloaded)
    s5, _, _ = load_series(raw_dir, source, preloaded)
    months = max((s5.index[-1] - rc.HOLDOUT_START_UTC).days / 30.44, 0.0)
    out = {"holdout_from": str(rc.HOLDOUT_START_UTC)[:10], "holdout_months": round(months, 2), "tests": {}}
    for key, t in [("B.2@" + k, v) for k, v in dev_res["B2"].items()]:
        if t.get("mean") is None:
            continue
        nh = sc["depths"][key.split("@")[1]]["hold"]["after_thinning"]
        row = {"dev_effect": round(t["mean"], 4), "dev_q": t.get("q_value"), "survived_dev_fdr": bool(t.get("fdr_reject")), "holdout_events": nh}
        sd = t.get("sd")
        if sd and nh > 0 and months > 0:
            se_h = sd / math.sqrt(nh)
            for nm, eff in (("full", abs(t["mean"])), ("half", abs(t["mean"]) / 2)):
                row[nm] = {"power_now": round(_phi(eff / se_h - 1.645), 2),
                           "months_needed_for_80pct": round(months * (se_h / (eff / (1.645 + 0.842))) ** 2, 1)}
            row["ready_to_open"] = bool(row["survived_dev_fdr"] and row["half"]["power_now"] >= 0.80)
        out["tests"][key] = row
    out["note"] = ("Only tests that survived the development FDR are candidates for the holdout; the rest are listed for power context. "
                   "The holdout stays sealed until a candidate's half-effect power is >= 0.80.")
    return out


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def render_markdown(r):
    L = ["# Observatory — Study B: structure break and pullback penetration (GBPUSD)\n",
         f"Contract `{r['contract_hash']}` · dataset **{r['dataset_status']}** · holdout opened: **False** · family 1: {r['n_tests'][1]} tests · family 2 (exploratory): {r['n_tests'][2]} tests\n",
         "**Development data only (events whose whole 24h outcome window ends before the holdout boundary). Nothing here is a trading rule.**\n",
         "Excess = outcome − mean(placebo outcome at the same barriers/geometry, weekday, hour and recent move). 0 = structure adds nothing. "
         "B.2 outcome: 1 if price trades above M (the running high) before below L (the structural low), from the close of the bar that first reaches the depth.\n",
         "## Reading (pre-declared)\n", f"**{r['reading']}**\n"]

    def row(lab, t):
        if t.get("mean") is None:
            return f"| {lab} | {t.get('n', 0)} | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | **{t.get('verdict', 'DATA-LIMITED')}** |"
        return (f"| {lab} | {t['n']} | {_pc(t['obs_rate'])} | {_pc(t['placebo_rate'])} | {_a(t['mean'])} | {_ci(t['ci'])} | {_a(t['excess_pips'], 2)} | "
                f"{_a(t['setup_net_pips'], 2)} | {_a(t['placebo_net_pips'], 2)} | {_p(t.get('q_value'))} | **{t.get('verdict')}** |")
    head = ("| test | events | observed | placebo | excess | 95% CI | excess pips | setup net pips (after cost) | placebo net pips | q | verdict |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|")
    L += ["## B.1 — does a close beyond a swing high continue (+1 ATR before −1 ATR)? (family 1)\n", head, row("BOS", r["B1"]),
          "\n## B.2 — pullback depth below the broken high (family 1)\n", "Depth in ATR below H: −0.25 = retest that stays above H, 0 = touches H, + = penetrates. Nested: an event at +0.5 also appears at 0.\n", head]
    for k, t in r["B2"].items():
        L.append(row(f"depth {k}", t))
    L.append("\nStability and sides (descriptive, excess):\n")
    for k, t in [("B.1", r["B1"])] + [(f"depth {k}", v) for k, v in r["B2"].items()]:
        if t.get("mean") is None:
            continue
        h, s = t.get("halves"), t.get("sides", {})
        L.append(f"- {k}: " + (f"first half {_a(h['first_half'].get('mean'))} (n {h['first_half']['n']}) · second half {_a(h['second_half'].get('mean'))} (n {h['second_half']['n']}) · " if h else "") +
                 f"up-BOS {_a(s.get('up', {}).get('mean'))} (n {s.get('up', {}).get('n')}) · down-BOS {_a(s.get('down', {}).get('mean'))} (n {s.get('down', {}).get('n')})")
    L.append("\n## B.4 — what changes the excess at depth 0? (family 2, EXPLORATORY: top tercile minus bottom tercile / group difference)\n")
    L.append("| variable | top / group 1 | bottom / group 2 | difference | 95% CI | n | q | verdict |\n|---|---|---|---|---|---|---|---|")
    for k, t in r["B4"].items():
        L.append(f"| {k} | {_a(t.get('mean_top'))} | {_a(t.get('mean_bottom'))} | {_a(t.get('diff'))} | {_ci(t.get('ci'))} | {t.get('n')} | {_p(t.get('q_value'))} | **{t.get('verdict')}** |")
    L.append("\n## Missingness audit (every event that did not reach the test)\n")
    L.append("| test | " + " | ".join(["breaks", "no pullback", "structure first", "geometry", "outside dev window", "window/closure", "thinned (<24h apart)", "unresolved", "ambiguous", "no placebo", "used"]) + " |\n|---|" + "---|" * 11)
    for k, a in r["audit"].items():
        L.append(f"| {k} | " + " | ".join(str(a.get(x, 0)) for x in ("breaks", "no_pullback", "structure_first", "geometry", "outside_region", "window", "thinned", "unresolved", "ambiguous", "no_placebo", "used")) + " |")
    L.append("\n## Not run\n- Holdout (sealed). Context-matched fake levels (needed to say the broken high itself is the cause), 4H structure, Fibonacci/levels (Study C), EURUSD, any pivot width other than 3: only after this profile is read.\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _synth_ohlc(n5, rng, garch=False, sigma=0.00025, start="2026-01-05 00:00"):
    sub = 5
    eps = rng.normal(size=n5 * sub)
    sig = np.full(n5, sigma)
    if garch:
        v = sigma ** 2
        e5 = rng.normal(size=n5)
        for i in range(1, n5):
            v = 0.02 * sigma ** 2 + 0.08 * (sig[i - 1] * e5[i - 1]) ** 2 + 0.90 * v
            sig[i] = math.sqrt(v)
    steps = (eps.reshape(n5, sub) * (sig[:, None] / math.sqrt(sub)))
    return _ohlc_from_steps(steps, start)


def _ohlc_from_steps(steps, start="2026-01-05 00:00"):
    n5, sub = steps.shape
    path = 1.30 + np.cumsum(steps.reshape(-1))
    path = path.reshape(n5, sub)
    c = path[:, -1]
    o = np.concatenate([[1.30], c[:-1]])
    h = np.maximum(path.max(axis=1), o)
    l = np.minimum(path.min(axis=1), o)
    idx = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=n5, freq="5min")
    df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)
    df["gap_before_missing"], df["closure_before"] = 0, False
    return df


def _series(df):
    return sa._series_from_df(df)


def selftest(null_series=10):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    H1 = NS_H
    ns = np.arange(40, dtype=np.int64) * H1
    # ---- hand-built 1h structure: pivot high at bar 5 (2.0), swing low 0.9, break at bar 12 (close 2.2)
    h = np.array([1.0, 1.1, 1.2, 1.3, 1.4, 2.0, 1.4, 1.3, 1.2, 1.1, 1.3, 1.6, 2.3] + [2.4] * 27)
    l = np.array([0.9, 1.0, 1.1, 1.2, 1.3, 1.8, 1.2, 1.1, 0.9, 1.0, 1.1, 1.3, 1.9] + [2.0] * 27)
    c = np.array([0.95, 1.05, 1.15, 1.25, 1.35, 1.9, 1.3, 1.2, 1.0, 1.1, 1.2, 1.5, 2.2] + [2.3] * 27)
    chk("pivot high found only with k bars each side", list(pivot_highs(h, ns, 3)), [5])
    chk("a tie for the maximum is not a pivot", list(pivot_highs(np.array([1, 2, 3, 3, 1.0, 0.5, 0.2, 0.1]), ns[:8], 3)), [])
    ev, cnt = find_bos(h, l, c, np.full(40, 0.3), ns, 3)
    chk("BOS: first close above the unbroken pivot", [(e["j"], e["i"], e["H"], e["L"]) for e in ev], [(12, 5, 2.0, 0.9)])
    chk("BOS is known only after the pivot is confirmed (j >= i+k+1)", all(e["j"] >= e["i"] + 4 for e in ev), True)
    chk("a younger-than-age limit applies", find_bos(h, l, c, np.full(40, 0.3), ns, 3, max_age=5)[0], [])
    ev2, cnt2 = find_bos(h, l, c, np.full(40, 3.0), ns, 3)
    chk("range filter: swing smaller than 1 ATR is dropped and counted", (ev2, cnt2["range_out"]), ([], 1))
    ev_dn, _ = find_bos(-l, -h, -c, np.full(40, 0.3), ns, 3)
    chk("an up structure is NOT a down structure (no BOS on the negated arrays)", ev_dn, [])
    ev_up2, _ = find_bos(-(-h), -(-l), -(-c), np.full(40, 0.3), ns, 3)
    chk("double mirror returns the original event", [(e["j"], e["i"]) for e in ev_up2], [(12, 5)])
    # a gap inside the pivot window disqualifies it
    ns_gap = ns.copy()
    ns_gap[8:] += 48 * H1
    chk("a closure inside the pivot window disqualifies the pivot", list(pivot_highs(h, ns_gap, 3)), [])
    # ---- barrier
    h5 = np.array([1.0, 1.1, 1.2, 1.3, 1.4])
    l5 = np.array([0.9, 0.95, 0.9, 0.8, 0.7])
    chk("barrier: up first", barrier(h5, l5, 0, 1.15, 0.75), (1.0, "ok"))
    chk("barrier: down first", barrier(np.array([1.0, 1.0, 1.0, 1.5]), np.array([0.9, 0.7, 0.9, 0.9]), 0, 1.4, 0.8), (0.0, "ok"))
    chk("barrier: both in one bar is ambiguous", barrier(np.array([1.0, 1.6]), np.array([0.9, 0.5]), 0, 1.5, 0.6)[1], "ambiguous")
    chk("barrier: neither is unresolved", barrier(h5, l5, 0, 5.0, -5.0)[1], "unresolved")
    chk("barrier uses strict inequality (touching is not crossing)", barrier(np.array([1.0, 1.5]), np.array([0.9, 0.9]), 0, 1.5, 0.1)[1], "unresolved")
    # ---- first passage geometry (depth 0 = H=2.0, atr=0.3, L=0.9)
    hh5 = np.array([2.3, 2.5, 2.4, 2.2, 2.1, 2.1])
    ll5 = np.array([2.2, 2.3, 2.15, 1.95, 1.9, 1.9])
    cc5 = np.array([2.3, 2.4, 2.2, 2.0, 2.0, 2.0])
    pas, why = first_passage(hh5, ll5, cc5, 0, 2.0, 0.9, 2.3, 0.3, 0.0)
    chk("first passage: first bar whose low <= H, M excludes that bar's own high", (pas["p"], round(pas["M"], 6)), (3, 2.5))
    chk("geometry a and b are measured from that bar's close", (round(pas["a"], 6), round(pas["b"], 6)), (round((2.5 - 2.0) / 0.3, 6), round((2.0 - 0.9) / 0.3, 6)))
    pas2, why2 = first_passage(hh5, ll5, cc5, 0, 2.0, 0.9, 2.3, 0.3, 0.25)
    chk("a deeper level is reached later or not at all", pas2["p"] >= pas["p"], True)
    chk("a pullback that never reaches the level is reported", first_passage(np.full(6, 3.0), np.full(6, 2.9), np.full(6, 2.95), 0, 2.0, 0.9, 3.0, 0.3, 0.0)[1], "no_pullback")
    chk("a bar that reaches L in the same bar is structural failure", first_passage(np.array([2.3, 2.4]), np.array([2.2, 0.8]), np.array([2.3, 1.0]), 0, 2.0, 0.9, 2.3, 0.3, 0.0)[1], "structure_first")
    chk("geometry outside [0.25, 5] ATR is dropped", first_passage(np.array([2.3, 2.45]), np.array([2.2, 1.95]), np.array([2.3, 2.45]), 0, 2.0, 0.9, 2.3, 0.3, 0.0)[1], "geometry")
    # ---- window / weekday / thinning
    nsg = np.arange(700, dtype=np.int64) * NS_5
    chk("window_ok on a contiguous clock", window_ok(nsg, 10), True)
    nsg2 = nsg.copy()
    nsg2[200:] += 2 * 24 * NS_H
    chk("window_ok is false across a closure", window_ok(nsg2, 10), False)
    chk("weekday: 1970-01-01 Thursday, 2026-10-08 Thursday, 2026-10-05 Monday",
        (int(weekday(0)), int(weekday(pd.Timestamp("2026-10-08", tz="UTC").value)), int(weekday(pd.Timestamp("2026-10-05", tz="UTC").value))), (3, 3, 0))
    chk("thinning keeps events at least 24h apart", thin(np.array([0, 1, 2, 3], dtype=np.int64) * 8 * NS_H), [0, 3])
    # ---- synthetic worlds
    rng = np.random.default_rng(23)
    dfs = _synth_ohlc(60000, rng)
    s5, s1h = _series(dfs)
    far = pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value
    hold_mid = int(_ns(s5.index)[40000])
    ctx = make_ctx(s5, s1h, +1, far)
    ev0, c0, _ = detect_events(ctx, 0.0, "dev")
    chk("synthetic random walk produces events at depth 0 (>= 20)", len(ev0) >= 20, True)
    # no look-ahead: change prices after time T; events with passage <= T are unchanged
    T = int(_ns(s5.index)[30000])
    df2 = dfs.copy()
    k = int(np.searchsorted(dfs.index.values.astype("datetime64[ns]").astype("int64"), T))
    for col in ("open", "high", "low", "close"):
        df2.iloc[k:, df2.columns.get_loc(col)] += 0.2
    s5b, s1hb = _series(df2)
    ctxb = make_ctx(s5b, s1hb, +1, far)
    evb, _, _ = detect_events(ctxb, 0.0, "dev")
    a_ = [(e["t"], round(e["M"], 9), round(e["a"], 6), round(e["b"], 6)) for e in ev0 if e["t"] < T - 3 * NS_H]
    b_ = [(e["t"], round(e["M"], 9), round(e["a"], 6), round(e["b"], 6)) for e in evb if e["t"] < T - 3 * NS_H]
    chk("no look-ahead: events before T are identical when the future is altered", (a_ == b_, len(a_) > 10), (True, True))
    # structure before the boundary only: the dev region excludes events whose outcome window crosses it
    ctxh = make_ctx(s5, s1h, +1, hold_mid)
    evd, _, _ = detect_events(ctxh, 0.0, "dev")
    evh, _, _ = detect_events(ctxh, 0.0, "hold")
    chk("dev events end their window before the boundary, holdout events start after it", (all(e["t"] + HMAX * NS_5 <= hold_mid for e in evd), all(e["t"] >= hold_mid for e in evh), len(evh) > 5), (True, True, True))
    # placebo pool never contains bars within 24h of the event, and is never outside development
    t0 = ev0[len(ev0) // 2]["t"]
    pl = draw_placebo(ctx, t0, ev0[len(ev0) // 2]["x"], np.random.default_rng(1))
    chk("placebo bars are > 24h from the event and share the weekday/hour window", (pl is not None and bool(np.all(np.abs(ctx["ns5"][pl[0]] - t0) > HMAX * NS_5))), True)
    ctx_h = make_ctx(s5, s1h, +1, hold_mid)
    allq = np.concatenate([v[0] for v in ctx_h["pools"].values()])
    chk("placebo pool is development-only (windows end before the boundary)", bool(np.all(ctx_h["ns5"][allq] + (HMAX + 1) * NS_5 <= hold_mid)), True)
    # mirror symmetry of the machinery: a down-BOS on the original series is an up-BOS on the negated series
    ctx_dn = make_ctx(s5, s1h, -1, far)
    dfn = dfs.copy()
    dfn["open"], dfn["close"], dfn["high"], dfn["low"] = -dfs["open"], -dfs["close"], -dfs["low"], -dfs["high"]
    s5n, s1hn = _series(dfn)
    ctx_neg = make_ctx(s5n, s1hn, +1, far)
    e_dn, _, _ = detect_events(ctx_dn, 0.0, "dev")
    e_ng, _, _ = detect_events(ctx_neg, 0.0, "dev")
    chk("down side == up side of the negated series", ([(e["t"], round(e["a"], 6)) for e in e_dn] == [(e["t"], round(e["a"], 6)) for e in e_ng], len(e_dn) > 10), (True, True))
    # ---- null calibration: random walk and GARCH, the whole pipeline
    zs = {"B1": [], "B2": []}
    obs, plc = [], []
    for k in range(null_series):
        df_ = _synth_ohlc(130000, rng, garch=(k % 2 == 1))
        s5_, s1_ = _series(df_)
        r_ = run_from_series(s5_, s1_, "PRIMARY_READY", far)
        if r_["B1"].get("z") is not None:
            zs["B1"].append(r_["B1"]["z"])
        for t_ in r_["B2"].values():
            if t_.get("z") is not None:
                zs["B2"].append(t_["z"])
                obs.append(t_["obs_rate"])
                plc.append(t_["placebo_rate"])
    allz = np.asarray(zs["B1"] + zs["B2"], float)
    chk(f"null: enough tests ({len(allz)})", len(allz) >= 40, True)
    if len(allz) >= 40:
        chk(f"null: |mean z| < 0.4 (got {allz.mean():+.2f})", abs(allz.mean()) < 0.4, True)
        chk(f"null: sd(z) in [0.6, 1.5] (got {allz.std():.2f})", 0.6 <= allz.std() <= 1.5, True)
        chk(f"null: share |z|>1.96 <= 12% (got {(np.abs(allz) > 1.96).mean():.0%})", (np.abs(allz) > 1.96).mean() <= 0.12, True)
    # ---- planted structure effect: after each passage at depth 0 (up side) a drift toward M is injected; the excess must appear
    dfp = _synth_ohlc(130000, np.random.default_rng(5))
    sP, hP = _series(dfp)
    ctxp = make_ctx(sP, hP, +1, far)
    evp, _, _ = detect_events(ctxp, 0.0, "dev")
    n5 = len(dfp)
    shift_c = np.zeros(n5)
    tsel = np.random.default_rng(6)
    chosen = [e for e in evp if tsel.random() < 0.7]
    for e in chosen:
        t_ = np.arange(n5)
        shift_c += 0.30 * 0.00025 * np.clip(t_ - e["p"], 0, 48)
    dfp2 = dfp.copy()
    sc = shift_c
    so = np.concatenate([[0.0], sc[:-1]])
    dfp2["close"] = dfp["close"] + sc
    dfp2["open"] = dfp["open"] + so
    dfp2["high"] = np.maximum(dfp["high"] + sc, np.maximum(dfp2["open"], dfp2["close"]))
    dfp2["low"] = np.minimum(dfp["low"] + sc, np.minimum(dfp2["open"], dfp2["close"]))
    sP2, hP2 = _series(dfp2)
    rP = run_from_series(sP2, hP2, "PRIMARY_READY", far)
    t0_ = rP["B2"]["+0.00"]
    chk(f"planted drift after the depth-0 passage: excess detected (z={t0_.get('z')})", (t0_.get("z") or 0) > 3 and t0_["mean"] > 0, True)
    chk("planted drift: FDR rejects that test", bool(t0_.get("fdr_reject")), True)
    chk("planted drift: B.1 is unaffected in sign convention (finite)", rP["B1"].get("mean") is not None, True)
    s_ = json.dumps(rP, default=str)
    chk("report renders and JSON serialises", (len(render_markdown(rP)) > 500, len(s_) > 500), (True, True))
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": null_series, "tests": int(len(allz)), "mean_z": round(float(allz.mean()), 3) if len(allz) else None,
                     "sd_z": round(float(allz.std()), 3) if len(allz) else None,
                     "obs_rate_mean": round(float(np.mean(obs)), 3) if obs else None, "placebo_rate_mean": round(float(np.mean(plc)), 3) if plc else None},
            "planted": {"depth0_z": round(float(t0_.get("z") or 0), 2), "depth0_excess": round(float(t0_["mean"]), 3), "events": t0_["n"], "injected": len(chosen)}}


def main():
    ap = argparse.ArgumentParser(description="Observatory Study B (BOS / pullback penetration). Read-only on raw data; the holdout stays sealed.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--readiness", action="store_true", help="count holdout EVENTS and compute power from development estimates; no holdout outcome is read")
    ap.add_argument("--counts", action="store_true", help="structure counts only (no outcome is computed)")
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
    res = run_studyb(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "studyb_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "studyb_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
