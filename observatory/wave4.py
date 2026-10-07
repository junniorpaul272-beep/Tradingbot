"""
observatory/wave4.py
====================
OBSERVATORY — Wave 4 (v2): CRT sweep-and-reclaim, outside bar, stops/targets and HTF zones, tested as
PARAMETERISED VARIANTS against one dataset. ISOLATED from the live bot.

Design rule (from the research brief): we do not decide what "CRT", "outside bar" or "order block" mean.
We freeze a finite set of objective definitions, measure what price does afterwards WITHOUT choosing a
stop or target, and ask (1) which definitions carry information and (2) whether each extra condition adds
information AFTER the previous one. A null result means "these definitions, these timeframes, this data" and
nothing wider. Every result prints its confidence interval = the largest effect the data cannot rule out.

Higher timeframe = 1h (the reference candle C1 and the sweep candle C2 are 1h candles). 4H is not used.
Every outcome is measured on the 5m candles (the finest series). The trigger timeframe (5m or 15m) matters only
where it changes WHAT HAPPENED: variant B (the intrabar return is detected on 5m or on 15m closes), the
outside-bar variants (bars of that timeframe) and the outside-bar confirmation H2. Variants A, D and E are decided on
1h candles, enter at the same price and are measured on the same 5m path whatever the trigger timeframe, so they
are run ONCE (running them on 15m as well would repeat the same numbers and double-count the evidence).

LAYERS (kept apart on purpose)
------------------------------
  A. EVENT      what happened (variants below)
  B. CONTEXT    what was true around it (outside-bar confirmation, HTF zone location)
  C. OUTCOME    raw excursions are recorded for every event, independent of any stop/target:
                MFE, MAE (in ATR14(1h) units) at 3h / 6h / 12h, time to MFE / MAE, whether the 50% level,
                the far side and the sweep extreme were reached. Stop/target constructions are then
                evaluated on top of that record (Layer 3, descriptive).

FROZEN CONTRACT v2 (frozen 2026-10-06, before any v2 result was seen; hash printed in every report)
-----------------------------------------------------------------------------------------------------
  C1, C2         two consecutive gap-free 1h candles. C1 defines [low1, high1], mid = (low1 + high1) / 2.
  SWEEP          C2 trades beyond ONE side of C1 and (at C2's close) not the other.
                   sweep-high: high2 > high1 and low2 >= low1   -> trade SHORT (toward low1)
                   sweep-low : low2  < low1  and high2 <= high1 -> trade LONG  (toward high1)
  CRT variants   A  sweep, C2 closes back inside C1                       (known at C2 close)
                 B  sweep, then a CLOSED trigger-timeframe bar inside C2 closes back inside C1 (the
                    "intrabar return"; known at that bar's close; C2 may later break the other side,
                    which a trader could not know either)
                 D  A and C2 closes beyond the midpoint of C1 (a subset of A)
                 E  A and C3 (next 1h candle) closes beyond C2's close in the trade direction;
                    entry after C3 closes; stop = extreme since C1 closed
                 (the brief's "C" duplicates A, so it is not run)
                 ACC  C2 closes BEYOND the swept side (control, descriptive only)
  Entry          open of the first 5m bar after the event is known.
  Trade geometry (stop/target layers ONLY) stop = sweep extreme (highest high / lowest low since C1 closed, up to the
                 event); far = the other side of C1; mid = midpoint of C1. A stop/target play is run only if the stop
                 distance and the far distance are both >= 0.5 x ATR14(1h) measured at C1's close. This filter does NOT
                 apply to the NET test or the raw excursion record: those use every episode with clean data, because the
                 filter would otherwise redefine the event (it keeps only deep sweeps and cuts the sample by roughly two
                 thirds). Changed from the first draft after seeing sample sizes only, before any outcome was viewed.
  OUTCOME (primary) NET = (MFE - MAE) / ATR14(1h) over 6 hours from entry. Under a driftless market MFE and MAE
                 have the same distribution, so E[NET] = 0 exactly, whatever the geometry.
                 Practical threshold 0.10 ATR. 3h and 12h are descriptive (12h windows overlap).
  Episodes       per (variant, side), anchored, K = 6 hours (= the primary horizon, windows never overlap).

  OUTSIDE BAR    (trigger-timeframe bar vs the previous bar, direction = bar colour, trade = continuation,
                 +-1R with R = ATR14, 24 bars, next-open entry; control = ordinary (non-outside) bars matched on
                 direction x session x ATR tercile x bar-size bin (0.8, 1.3, 1.8 x prior ATR))
                 OB-A high > prev high and low < prev low
                 OB-B A and close beyond the previous bar's extreme in the bar's direction (nested in C)
                 OB-C A and close beyond the previous bar's BODY in the bar's direction (nested in A)
                 OB-D A and range >= 1.5 x prior ATR
  HTF ZONES      3 constructions x 2 extents on 1h bars. Zone side = direction of the displacement leaving it
                 (+1 demand, -1 supply). The zone is the LAST bar of opposite colour within 5 bars before:
                 ZA  a displacement bar (range >= 1.5 x prior ATR)                 known at that bar's close
                 ZB  the first 1h close beyond the previous 10-bar extreme         known at that bar's close
                 ZC  a 3-bar move >= 50% of the preceding 24-bar range             known at the 3rd bar's close
                 extent  full = bar range; body = open-close
                 Active for an event when: known before C1 opens, age <= 72 bars, no 1h close beyond its far
                 side before C2, side == trade direction, and the sweep extreme lies inside the zone +- 0.25 ATR.

  TESTS          Tier 1 (FDR family 1, BH q = 0.10): 5 CRT configurations {A, D, E, B@5m, B@15m} (NET mean) + 4
                 outside-bar variants x 2 timeframes (P(+1R first) difference vs matched ordinary bars) = 13 tests.
                 Tier 2 (FDR family 2, EXPLORATORY ONLY): additivity inside CRT-A episodes (NET difference, flagged
                   minus unflagged): H1 beyond-midpoint, H2 outside-bar confirmation (the last closed trigger bar before
                   entry is an outside bar in the trade direction) on 5m and on 15m, H3 zone-located x 6 zone
                   variants = 9 tests.
  RAW DATASET    every measured episode is written to wave4_events.jsonl.gz with its full 5m favourable / adverse
                 excursion path (ATR14(1h) units, hundredths, 12h), so any stop/target/horizon can be evaluated later
                 without re-running the history (post-hoc evaluation is exploratory and must be declared as such).
                 Layer 3 (descriptive, NOT tested): stop x target grid {SWEEP, 1.0 ATR(1h)} x {MID, FAR, 1R, 1.5R, 2R},
                 scored as mean R with a time exit at 6h (exactly 0 under a driftless market), net of 1 pip.
  Split          the frozen absolute holdout date; development events are purged by the longest horizon (12h);
                 the holdout is SEALED (this module has no way to open it).
  NOT RUN        P4+ combinations beyond the hierarchy above, zone proximity beyond the location flag, 4H, any
                 stop/target search with a verdict, the holdout.

Run:  python3 -m observatory.wave4                (real data)
      python3 -m observatory.wave4 --selftest     (hand-built geometry, lookahead, nesting, null calibration)
"""
import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

from . import clean as rc
from . import measure as rm
from . import gates as rg
from . import wave1 as w1

HORIZONS_H = (3, 6, 12)
PRIMARY_H = 6
MAX_H = max(HORIZONS_H)
K_EP_HOURS = 6
MIN_DIST_ATR = 0.5
MIN_EFFECT_ATR = 0.10
MIN_EFFECT_PP = w1.MIN_EFFECT
FDR_Q = w1.FDR_Q
SPREAD_PIPS = w1.SPREAD_PIPS
LTFS = ("5m", "15m")
CRT_VARIANTS = ("A", "B", "D", "E")
CRT_CONFIGS = ("A", "D", "E", "B@5m", "B@15m")                 # Tier-1 CRT tests (B differs by trigger timeframe; A, D, E do not)
OB_VARIANTS = ("A", "B", "C", "D")
OB_RATIO_BINS = (0.8, 1.3, 1.8)
OB_D_MIN_RATIO = 1.5
OB_HORIZON = 24
STOPS = ("SWEEP", "ATR1.0")
TARGETS = ("MID", "FAR", "1R", "1.5R", "2R")
STOP_ATR = 1.0
ZONE_CONSTRUCTIONS = ("ZA", "ZB", "ZC")
ZONE_EXTENTS = ("full", "body")
ZONE_VARIANTS = tuple(f"{c}-{e}" for c in ZONE_CONSTRUCTIONS for e in ZONE_EXTENTS)
ZONE_BACK = 5
ZONE_MAX_AGE = 72
ZONE_TOL_ATR = 0.25
ZA_K = 1.5
ZB_N = 10
ZC_BARS, ZC_RANGE_BARS, ZC_FRAC = 3, 24, 0.5
HIER_TESTS = ("H1_beyond_mid", "H2_outside_confirm@5m", "H2_outside_confirm@15m") + tuple(f"H3_zone_{z}" for z in ZONE_VARIANTS)
PATH_BARS_PER_H = 12
RR_BUCKETS = ((0.0, 1.0), (1.0, 1.5), (1.5, 2.5), (2.5, 1e9))

CONTRACT = {
    "wave": 4, "version": "v2.1 parameterised variants", "instrument": "GBPUSD", "htf": "1h", "trigger_timeframes": list(LTFS),
    "crt_variants": list(CRT_VARIANTS), "ob_variants": list(OB_VARIANTS), "zone_variants": list(ZONE_VARIANTS),
    "horizons_h": list(HORIZONS_H), "primary_h": PRIMARY_H, "episode_hours": K_EP_HOURS, "min_distance_atr1h": MIN_DIST_ATR,
    "primary_outcome": "NET=(MFE-MAE)/ATR1h", "min_effect_atr": MIN_EFFECT_ATR, "min_effect_pp": MIN_EFFECT_PP,
    "ob_ratio_bins": list(OB_RATIO_BINS), "ob_d_min_ratio": OB_D_MIN_RATIO, "ob_horizon_bars": OB_HORIZON,
    "stops": list(STOPS), "targets": list(TARGETS), "stop_atr": STOP_ATR,
    "zones": {"za_k": ZA_K, "zb_n": ZB_N, "zc": [ZC_BARS, ZC_RANGE_BARS, ZC_FRAC], "back": ZONE_BACK, "max_age": ZONE_MAX_AGE, "tol_atr": ZONE_TOL_ATR},
    "tier1_tests": len(CRT_CONFIGS) + len(OB_VARIANTS) * len(LTFS), "tier2_tests": len(HIER_TESTS),
    "crt_configs": list(CRT_CONFIGS), "measurement": "5m path for every outcome", "path_hundredths_atr": True,
    "fdr_q": FDR_Q, "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# small statistics
# ---------------------------------------------------------------------------
def verdict(diff, ci_lo, ci_hi, reject, n_min, dataset_status, min_effect):
    """Local copy so this wave does not depend on wave1's label order. A CI that excludes zero is classified BEFORE
    the 'CI inside +-min_effect' rule, otherwise a real but small effect would be labelled NO_INFORMATION."""
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


def fragile_suffix(label, diff, worst_diff):
    """Sign flip under worst-case ambiguity only matters when the verdict claims something."""
    if diff is None or worst_diff is None:
        return ""
    claims = label.startswith(("DISCOVERY_PASS", "EXPLORATORY_SIGNAL", "SMALL_EFFECT"))
    return " [FRAGILE: sign flips under worst-case ambiguity]" if claims and diff * worst_diff <= 0 else ""


def _mean_test(vals):
    v = np.asarray([x for x in vals if x is not None], float)
    n = len(v)
    if n < 10:
        return {"n": n, "mean": None, "se": None, "ci": None, "z": None, "p": None}
    m, sd = float(v.mean()), float(v.std(ddof=1))
    se = sd / math.sqrt(n)
    z = m / se if se > 0 else 0.0
    return {"n": n, "mean": m, "se": se, "ci": (m - 1.96 * se, m + 1.96 * se), "z": z, "p": w1.norm_p(z)}


def _welch(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    na, nb = len(a), len(b)
    out = {"n1": na, "n0": nb, "n_min": min(na, nb), "diff": None, "se": None, "ci": None, "z": None, "p": None}
    if na < 10 or nb < 10:
        return out
    diff = float(a.mean() - b.mean())
    se = math.sqrt(a.var(ddof=1) / na + b.var(ddof=1) / nb)
    z = diff / se if se > 0 else 0.0
    out.update(diff=diff, se=se, ci=(diff - 1.96 * se, diff + 1.96 * se), z=z, p=w1.norm_p(z))
    return out


def _strat_mean_diff(a_by, b_by):
    """a_by, b_by: dict stratum -> list of values. Weighted by the event count; strata need >= 10 in both arms."""
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
    if wsum == 0:
        return {"diff": None, "se": None, "ci": None, "z": None, "p": None, "n": 0}
    diff, se = num / wsum, math.sqrt(var) / wsum
    z = diff / se if se > 0 else 0.0
    return {"diff": diff, "se": se, "ci": (diff - 1.96 * se, diff + 1.96 * se), "z": z, "p": w1.norm_p(z), "n": wsum}


# ---------------------------------------------------------------------------
# CRT events (Layer A)
# ---------------------------------------------------------------------------
def sweep_signals(s):
    """One-sided sweeps of C1 by C2 on the 1h Series. Known at C2's close. One dict per sweep."""
    out = []
    for j in range(1, s.n):
        if s.gap[j] > 0 or s.closure[j] or not np.isfinite(s.atr[j - 1]):
            continue
        h1, l1, h2, l2, c2 = s.h[j - 1], s.l[j - 1], s.h[j], s.l[j], s.c[j]
        if h2 > h1 and l2 >= l1:
            side, d, ext, far, reclaim = "high", -1, h2, l1, bool(c2 < h1)
        elif l2 < l1 and h2 <= h1:
            side, d, ext, far, reclaim = "low", +1, l2, h1, bool(c2 > l1)
        else:
            continue
        mid = 0.5 * (h1 + l1)
        beyond_mid = bool(c2 < mid) if side == "high" else bool(c2 > mid)
        out.append({"j": j, "side": side, "d": d, "reclaim": reclaim, "beyond_mid": beyond_mid, "stop": float(ext), "far": float(far),
                    "mid": float(mid), "atr": float(s.atr[j - 1]), "c2": float(c2), "t_known": s.index[j] + s.step, "c1_range": float(h1 - l1)})
    return out


def crt_events_1h(s, sweeps):
    """Variants decided on 1h candles only: A, D, E and the ACC control."""
    ev = {"A": [], "D": [], "E": [], "ACC": []}
    for sg in sweeps:
        base = {k: sg[k] for k in ("side", "d", "far", "mid", "atr", "c2", "c1_range", "j")}
        if not sg["reclaim"]:
            ev["ACC"].append(dict(base, variant="ACC", jk=sg["j"], t_known=sg["t_known"], stop=sg["stop"], beyond_mid=False))
            continue
        a = dict(base, variant="A", jk=sg["j"], t_known=sg["t_known"], stop=sg["stop"], beyond_mid=sg["beyond_mid"])
        ev["A"].append(a)
        if sg["beyond_mid"]:
            ev["D"].append(dict(a, variant="D"))
        j = sg["j"]
        if j + 1 < s.n and s.gap[j + 1] == 0 and not s.closure[j + 1]:
            c3 = s.c[j + 1]
            if sg["side"] == "high" and c3 < sg["c2"]:
                ev["E"].append(dict(base, variant="E", jk=j + 1, t_known=s.index[j + 1] + s.step, stop=float(max(sg["stop"], s.h[j + 1])), beyond_mid=sg["beyond_mid"]))
            elif sg["side"] == "low" and c3 > sg["c2"]:
                ev["E"].append(dict(base, variant="E", jk=j + 1, t_known=s.index[j + 1] + s.step, stop=float(min(sg["stop"], s.l[j + 1])), beyond_mid=sg["beyond_mid"]))
    return ev


def crt_events_b(s1h, s_ltf):
    """Variant B: inside C2, the first CLOSED trigger bar that closes back inside C1 after the sweep began.
    One-sided so far: the other side of C1 must not have been touched up to and including the trigger bar."""
    ratio = int(round(60 / s_ltf.step_minutes))
    out = []
    for j in range(1, s1h.n):
        if s1h.gap[j] > 0 or s1h.closure[j] or not np.isfinite(s1h.atr[j - 1]):
            continue
        t0 = s1h.index[j]
        a = int(s_ltf.index.searchsorted(t0, side="left"))
        b = a + ratio - 1
        if b >= s_ltf.n or s_ltf.index[a] != t0 or s_ltf.index[b] != t0 + (ratio - 1) * s_ltf.step:
            continue
        if s_ltf.gap[a + 1:b + 1].any() or s_ltf.closure[a + 1:b + 1].any():
            continue
        h1, l1 = s1h.h[j - 1], s1h.l[j - 1]
        mid = 0.5 * (h1 + l1)
        run_hi, run_lo = -np.inf, np.inf
        done = {"high": False, "low": False}
        for k in range(a, b + 1):
            run_hi, run_lo = max(run_hi, s_ltf.h[k]), min(run_lo, s_ltf.l[k])
            ck = s_ltf.c[k]
            if not done["high"] and run_hi > h1 and run_lo >= l1 and ck < h1:
                done["high"] = True
                out.append({"variant": "B", "side": "high", "d": -1, "j": j, "jk": j, "t_known": s_ltf.index[k] + s_ltf.step, "stop": float(run_hi),
                            "far": float(l1), "mid": float(mid), "atr": float(s1h.atr[j - 1]), "c1_range": float(h1 - l1), "beyond_mid": False})
            if not done["low"] and run_lo < l1 and run_hi <= h1 and ck > l1:
                done["low"] = True
                out.append({"variant": "B", "side": "low", "d": +1, "j": j, "jk": j, "t_known": s_ltf.index[k] + s_ltf.step, "stop": float(run_lo),
                            "far": float(h1), "mid": float(mid), "atr": float(s1h.atr[j - 1]), "c1_range": float(h1 - l1), "beyond_mid": False})
    return out


def collapse(events):
    """Anchored K_EP_HOURS episodes per side. Returns the first event of each episode, in time order."""
    if not events:
        return []
    ev = sorted(events, key=lambda x: (x["jk"], x["t_known"]))
    idx = np.array([x["jk"] for x in ev])
    keys = np.array([0 if x["side"] == "high" else 1 for x in ev])
    _, kept = rm.collapse_episodes(idx, keys, K_EP_HOURS)
    return [x for x, k in zip(ev, kept) if k]


# ---------------------------------------------------------------------------
# outcomes (Layer C): raw excursions + stop/target plays on top
# ---------------------------------------------------------------------------
def _play(s, s_lo, e, d, sd, td, H, spread=0.0, worst=False):
    """One stop/target play. R is exact-zero-mean under a driftless market: win +td/sd, loss -1, time exit = mark at the horizon."""
    tr = td / sd
    res = rm.first_touch(s, e, d, r_unit=sd, target_r=tr, stop_r=1.0, horizon=H, spread=spread)
    st = res["status"]
    if st == rm.AMBIGUOUS and s_lo is not None:
        st, _ = rm.resolve_ambiguous(s, s_lo, res, d)
    if st == rm.AMBIGUOUS and worst:
        st = rm.STOP_FIRST
    if st == rm.TARGET_FIRST:
        return {"status": st, "R": float(tr), "win": True, "tr": float(tr)}
    if st == rm.STOP_FIRST:
        return {"status": st, "R": -1.0, "win": False, "tr": float(tr)}
    if st == rm.NEITHER:
        return {"status": st, "R": float((s.c[e + H] - res["entry"]) * d / sd), "win": None, "tr": float(tr)}
    return {"status": st, "R": None, "win": None, "tr": float(tr)}


def locate(ev, s):
    """LTF index e of the last bar before the event is known; the entry bar is e+1. None if the grid does not line up."""
    e = int(s.index.get_indexer([ev["t_known"] - s.step])[0])
    if e < 0 or e + 1 >= s.n or s.index[e + 1] != ev["t_known"]:
        return None
    return e


def _last_outside(s, t_known, d):
    """Is the last CLOSED bar of series s before t_known an outside bar (high above AND low below the previous bar) coloured in direction d?"""
    e = int(s.index.get_indexer([t_known - s.step])[0])
    if e < 1 or s.gap[e] != 0 or s.closure[e]:
        return False
    return bool(s.h[e] > s.h[e - 1] and s.l[e] < s.l[e - 1] and (s.c[e] - s.o[e]) * d > 0)


def measure_event(ev, s, trig, keep_path=False):
    """Everything recorded for one episode, measured on the 5m series `s`.
    trig: {'5m': Series, '15m': Series} used only for the outside-bar confirmation flag. {'ok': False, 'reason': ...} if excluded."""
    e = locate(ev, s)
    if e is None:
        return {"ok": False, "reason": "no_trigger_bar"}
    d, atr = ev["d"], ev["atr"]
    entry = float(s.o[e + 1])
    stop_d, far_d, mid_d = (entry - ev["stop"]) * d, (ev["far"] - entry) * d, (ev["mid"] - entry) * d
    eligible = bool(stop_d >= MIN_DIST_ATR * atr and far_d >= MIN_DIST_ATR * atr)      # only the stop/target layers need this
    sm = s.step_minutes
    nb = {h: int(h * 60 / sm) for h in HORIZONS_H}
    last = e + nb[MAX_H]
    if last >= s.n:
        return {"ok": False, "reason": "end_of_data"}
    if s.gap[e + 1:last + 1].any():
        return {"ok": False, "reason": "data_gap"}
    hi_all, lo_all = s.h[e + 1:last + 1], s.l[e + 1:last + 1]
    fav_all, adv_all = (hi_all - entry, entry - lo_all) if d == 1 else (entry - lo_all, hi_all - entry)
    exc = {}
    for h, n_ in nb.items():
        mfe, mae = float(fav_all[:n_].max()), float(adv_all[:n_].max())
        exc[h] = {"mfe": mfe / atr, "mae": mae / atr, "net": (mfe - mae) / atr}
    n6 = nb[PRIMARY_H]
    mfe6, mae6 = float(fav_all[:n6].max()), float(adv_all[:n6].max())
    t_mfe, t_mae = (int(fav_all[:n6].argmax()) + 1) * sm, (int(adv_all[:n6].argmax()) + 1) * sm
    reach = {"mid": (bool(mfe6 >= mid_d) if (eligible and mid_d > 0) else None), "far": (bool(mfe6 >= far_d) if eligible else None),
             "stop_level": (bool(mae6 >= stop_d) if eligible else None)}
    cells = {f"{sn}|{tn}": None for sn in STOPS for tn in TARGETS}
    for sn in (STOPS if eligible else ()):
        sd = stop_d if sn == "SWEEP" else STOP_ATR * atr
        for tn in TARGETS:
            td = {"MID": mid_d, "FAR": far_d, "1R": sd, "1.5R": 1.5 * sd, "2R": 2 * sd}[tn]
            cells[f"{sn}|{tn}"] = _play(s, None, e, d, sd, td, n6) if (td > 0 and sd > 0) else None
    nat = cells["SWEEP|FAR"]
    sp = SPREAD_PIPS * 1e-4
    nat_net = _play(s, None, e, d, stop_d + sp, far_d - sp, n6, spread=sp) if (eligible and far_d - sp > 0) else None
    nat_worst = _play(s, None, e, d, stop_d, far_d, n6, worst=True) if eligible else None
    rr = far_d / stop_d if eligible else None
    out = {"ok": True, "ev": ev, "side": ev["side"], "rr": rr, "exc": exc, "t_mfe_min": t_mfe, "t_mae_min": t_mae, "reach": reach,
           "eligible": eligible, "cells": cells, "nat": nat, "nat_net": nat_net, "nat_worst": nat_worst, "e": e, "entry": entry,
           "stop_d": stop_d, "far_d": far_d, "mid_d": mid_d, "atr": atr,
           "ob_confirm": {tf: _last_outside(sr, ev["t_known"], d) for tf, sr in (trig or {}).items()},
           "excess": ((1.0 if nat["win"] else 0.0) - 1.0 / (1.0 + rr)) if nat and nat["win"] is not None else None}
    if keep_path:
        out["path_fav"] = np.rint(fav_all / atr * 100).astype(int).tolist()
        out["path_adv"] = np.rint(adv_all / atr * 100).astype(int).tolist()
    return out


def baseline_net(s1h, s5, dev_end):
    """Unconditional reference for NET: the 6h (MFE - MAE)/ATR(1h) of a LONG entered at every contiguous hour boundary
    (a short is the exact negative). Under a symmetric market this is ~0; it shows how far this data is from the null the
    CRT tests assume. Mean over all hours; the standard error uses every 6th hour (non-overlapping windows)."""
    nb = PRIMARY_H * PATH_BARS_PER_H
    vals = []
    for k in range(1, s1h.n):
        T = s1h.index[k]
        if s1h.index[k - 1] + s1h.step != T or s1h.gap[k] > 0 or s1h.closure[k] or not np.isfinite(s1h.atr[k - 1]) or s1h.atr[k - 1] <= 0:
            continue
        if T + pd.Timedelta(hours=PRIMARY_H) >= dev_end:
            continue
        a = int(s5.index.searchsorted(T, side="left"))
        if a + nb > s5.n or s5.index[a] != T or s5.gap[a:a + nb].any():
            continue
        entry = s5.o[a]
        vals.append(((s5.h[a:a + nb].max() - entry) - (entry - s5.l[a:a + nb].min())) / s1h.atr[k - 1])
    v = np.asarray(vals, float)
    if len(v) < 60:
        return {"n": len(v), "mean_long": None, "se_6h": None}
    sub = v[::6]
    return {"n": int(len(v)), "mean_long": float(v.mean()), "mean_short": float(-v.mean()), "se_6h": float(sub.std(ddof=1) / math.sqrt(len(sub)))}


# ---------------------------------------------------------------------------
# HTF zones (Layer B context)
# ---------------------------------------------------------------------------
def _contig(s, a, b):
    """True if bars a..b (inclusive) are consecutive with no missing candle and no closure inside."""
    return not (s.gap[a + 1:b + 1].any() or s.closure[a + 1:b + 1].any())


def build_zones(s):
    """Returns {zone_variant: dict of numpy arrays side, lo, hi, kidx, inval}. kidx = 1h bar whose close makes the zone known."""
    n = s.n
    col = np.sign(s.c - s.o).astype(int)
    prior = np.full(n, np.nan)
    prior[1:] = s.atr[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(prior > 0, (s.h - s.l) / prior, np.nan)

    def opposing(k, dk):
        for m in range(k - 1, max(k - 1 - ZONE_BACK, -1), -1):
            if col[m] == -dk and _contig(s, m, k):
                return m
        return None
    raw = {}                                                     # (construction, side, m) -> kidx (earliest wins)

    def add(cn, dk, m, kidx):
        if m is not None:
            key = (cn, dk, m)
            if key not in raw or kidx < raw[key]:
                raw[key] = kidx
    for k in range(1, n):
        if np.isfinite(ratio[k]) and ratio[k] >= ZA_K and col[k] != 0 and s.gap[k] == 0 and not s.closure[k]:
            add("ZA", int(col[k]), opposing(k, int(col[k])), k)
    for k in range(ZB_N + 1, n):
        if not _contig(s, k - ZB_N - 1, k):
            continue
        hi_now, hi_prev = s.h[k - ZB_N:k].max(), s.h[k - 1 - ZB_N:k - 1].max()
        lo_now, lo_prev = s.l[k - ZB_N:k].min(), s.l[k - 1 - ZB_N:k - 1].min()
        if s.c[k] > hi_now and s.c[k - 1] <= hi_prev:
            add("ZB", +1, opposing(k, +1), k)
        elif s.c[k] < lo_now and s.c[k - 1] >= lo_prev:
            add("ZB", -1, opposing(k, -1), k)
    for k in range(ZC_RANGE_BARS, n - ZC_BARS + 1):
        k2 = k + ZC_BARS - 1
        if not _contig(s, k - ZC_RANGE_BARS, k2):
            continue
        rngw = s.h[k - ZC_RANGE_BARS:k].max() - s.l[k - ZC_RANGE_BARS:k].min()
        net = s.c[k2] - s.o[k]
        if rngw > 0 and abs(net) >= ZC_FRAC * rngw:
            dk = 1 if net > 0 else -1
            add("ZC", dk, opposing(k, dk), k2)
    zones = {}
    for cn in ZONE_CONSTRUCTIONS:
        for ext in ZONE_EXTENTS:
            rows = []
            for (c_, dk, m), kidx in raw.items():
                if c_ != cn:
                    continue
                lo, hi = (s.l[m], s.h[m]) if ext == "full" else (min(s.o[m], s.c[m]), max(s.o[m], s.c[m]))
                beyond = np.nonzero(s.c[kidx + 1:] < lo)[0] if dk == 1 else np.nonzero(s.c[kidx + 1:] > hi)[0]
                inval = kidx + 1 + int(beyond[0]) if len(beyond) else n + 10
                rows.append((dk, lo, hi, kidx, inval, m))
            rows.sort(key=lambda r: r[3])
            arr = np.array(rows, float) if rows else np.zeros((0, 6))
            zones[f"{cn}-{ext}"] = {"side": arr[:, 0].astype(int), "lo": arr[:, 1], "hi": arr[:, 2], "kidx": arr[:, 3].astype(int),
                                    "inval": arr[:, 4].astype(int), "m": arr[:, 5].astype(int)}
    return zones


def zone_located(Z, ev):
    """Is the sweep extreme inside an ACTIVE, ALIGNED zone (+- tolerance) at the time of the event?"""
    if len(Z["side"]) == 0:
        return False
    j, tol, x = ev["j"], ZONE_TOL_ATR * ev["atr"], ev["stop"]
    ok = (Z["side"] == ev["d"]) & (Z["kidx"] <= j - 2) & (j - Z["kidx"] <= ZONE_MAX_AGE) & (Z["inval"] > j - 1) & (Z["lo"] - tol <= x) & (x <= Z["hi"] + tol)
    return bool(ok.any())


# ---------------------------------------------------------------------------
# CRT block: variants, hierarchy, layer-3 descriptives
# ---------------------------------------------------------------------------
def _summ_variant(meas, excl, n_eps):
    out = {"episodes": n_eps, "measured": len(meas), "excluded": dict(excl), "eligible_geometry": sum(1 for m in meas if m["eligible"])}
    out["horizons"] = {}
    for h in HORIZONS_H:
        t = _mean_test([m["exc"][h]["net"] for m in meas])
        out["horizons"][str(h)] = {"net": t, "mfe_mean": float(np.mean([m["exc"][h]["mfe"] for m in meas])) if meas else None,
                                  "mae_mean": float(np.mean([m["exc"][h]["mae"] for m in meas])) if meas else None}
    out["primary"] = out["horizons"][str(PRIMARY_H)]["net"]
    out["n_min"] = len(meas)
    out["sample_label"] = w1.sample_label(len(meas))
    if meas:
        out["reach"] = {k: (float(np.mean([m["reach"][k] for m in meas if m["reach"][k] is not None])) if any(m["reach"][k] is not None for m in meas) else None)
                        for k in ("mid", "far", "stop_level")}
        out["reach_mid_n"] = sum(1 for m in meas if m["reach"]["mid"] is not None)
        out["median_t_mfe_min"] = float(np.median([m["t_mfe_min"] for m in meas]))
        out["median_t_mae_min"] = float(np.median([m["t_mae_min"] for m in meas]))
    out["by_side"] = {sd: _mean_test([m["exc"][PRIMARY_H]["net"] for m in meas if m["side"] == sd]) for sd in ("high", "low")}
    # natural geometry (stop = sweep extreme, target = far side)
    nat = [m for m in meas if m["nat"] and m["nat"]["R"] is not None]
    resolved = [m for m in nat if m["nat"]["win"] is not None]
    out["natural"] = {"n": len(nat), "resolved": len(resolved), "mean_R": _mean_test([m["nat"]["R"] for m in nat]),
                      "win_rate": float(np.mean([m["nat"]["win"] for m in resolved])) if resolved else None,
                      "null_win_rate": float(np.mean([1 / (1 + m["rr"]) for m in resolved])) if resolved else None,
                      "mean_excess": _mean_test([m["excess"] for m in resolved]), "mean_RR": float(np.mean([m["rr"] for m in nat])) if nat else None,
                      "mean_R_net_spread": float(np.mean([m["nat_net"]["R"] for m in meas if m["nat_net"] and m["nat_net"]["R"] is not None])) if any(m["nat_net"] and m["nat_net"]["R"] is not None for m in meas) else None,
                      "mean_R_worst_ambiguity": float(np.mean([m["nat_worst"]["R"] for m in meas if m["nat_worst"] and m["nat_worst"]["R"] is not None])) if any(m["nat_worst"] and m["nat_worst"]["R"] is not None for m in meas) else None}
    bk = []
    for lo, hi in RR_BUCKETS:
        b = [m for m in nat if lo <= m["rr"] < hi]
        bk.append({"rr_from": lo, "rr_to": None if hi > 1e8 else hi, "n": len(b), "mean_R": float(np.mean([m["nat"]["R"] for m in b])) if b else None})
    out["rr_buckets"] = bk
    grid = {}
    for key in (f"{sn}|{tn}" for sn in STOPS for tn in TARGETS):
        g = _mean_test([m["cells"][key]["R"] for m in meas if m["cells"][key] and m["cells"][key]["R"] is not None])
        grid[key] = {"n": g["n"], "mean_R": g["mean"], "se": g["se"], "z": g["z"]}
    out["grid"] = grid
    return out


def _event_row(cfg, m, zone_flags):
    ev = m["ev"]
    row = {"config": cfg, "side": m["side"], "d": ev["d"], "t_known": str(ev["t_known"]), "j_1h": ev["j"], "entry": m["entry"], "atr1h": m["atr"],
           "stop_dist": m["stop_d"], "far_dist": m["far_d"], "mid_dist": m["mid_d"], "rr": m["rr"], "beyond_mid": bool(ev["beyond_mid"]),
           "exc": {str(h): v for h, v in m["exc"].items()}, "t_mfe_min": m["t_mfe_min"], "t_mae_min": m["t_mae_min"], "reach": m["reach"],
           "ob_confirm": m["ob_confirm"], "zones": zone_flags, "nat_status": m["nat"]["status"] if m["nat"] else None,
           "nat_R": m["nat"]["R"] if m["nat"] else None}
    if "path_fav" in m:
        row["path_fav_hundredths_atr"], row["path_adv_hundredths_atr"] = m["path_fav"], m["path_adv"]
    return row


def run_crt(s1h, s5, trig, events, zones, dev_end, keep_rows=False):
    """events: {config: [event dicts]} with configs A, D, E, ACC, B@5m, B@15m. Everything is measured on the 5m series."""
    res = {"variants": {}, "hier": {}}
    rows = []
    base_meas = None
    for cfg in ("A", "D", "E", "B@5m", "B@15m", "ACC"):
        eps = collapse([x for x in events[cfg] if x["t_known"] < dev_end])
        meas, excl = [], Counter()
        for x in eps:
            m = measure_event(x, s5, trig, keep_path=keep_rows)
            if m["ok"]:
                meas.append(m)
                if keep_rows:
                    rows.append(_event_row(cfg, m, {zv: zone_located(zones[zv], x) for zv in ZONE_VARIANTS}))
            else:
                excl[m["reason"]] += 1
        res["variants"][cfg] = _summ_variant(meas, excl, len(eps))
        if cfg == "A":
            base_meas = meas
    # Tier 2: does each added condition carry information AFTER CRT-A?  (same entries, same outcomes)
    A = base_meas or []
    flags = {"H1_beyond_mid": [bool(m["ev"]["beyond_mid"]) for m in A],
             "H2_outside_confirm@5m": [bool(m["ob_confirm"].get("5m")) for m in A],
             "H2_outside_confirm@15m": [bool(m["ob_confirm"].get("15m")) for m in A]}
    for zv in ZONE_VARIANTS:
        flags[f"H3_zone_{zv}"] = [zone_located(zones[zv], m["ev"]) for m in A]
    net = np.array([m["exc"][PRIMARY_H]["net"] for m in A], float)
    for name, fl in flags.items():
        f = np.array(fl, bool)
        t = _welch(net[f], net[~f]) if len(A) else _welch([], [])
        t["share_flagged"] = float(f.mean()) if len(A) else None
        t["mean_flagged"] = float(net[f].mean()) if f.any() else None
        t["mean_unflagged"] = float(net[~f].mean()) if (~f).any() else None
        res["hier"][name] = t
    res["rows"] = rows
    return res


def p1_descriptive(s, sweeps, dev_end):
    by_j = {x["j"]: x for x in sweeps}
    rows = []
    for j in range(1, s.n):
        if s.index[j] + s.step >= dev_end or not np.isfinite(s.atr[j - 1]) or s.atr[j - 1] <= 0 or s.gap[j] > 0 or s.closure[j]:
            continue
        sg = by_j.get(j)
        rows.append(((s.h[j - 1] - s.l[j - 1]) / s.atr[j - 1], float(sg is not None and sg["reclaim"]), float(sg is not None)))
    arr = np.array(rows, float)
    if len(arr) < 60:
        return None
    edges = np.quantile(arr[:, 0], [1 / 3, 2 / 3])
    grp = np.digitize(arr[:, 0], edges)
    return [{"c1_range_tercile": g + 1, "n": int((grp == g).sum()), "range_x_ATR": (round(float(arr[grp == g, 0].min()), 2), round(float(arr[grp == g, 0].max()), 2)),
             "p_sweep_any": round(float(arr[grp == g, 2].mean()), 4), "p_sweep_and_reclaim": round(float(arr[grp == g, 1].mean()), 4)} for g in range(3)]


# ---------------------------------------------------------------------------
# outside-bar block
# ---------------------------------------------------------------------------
def ob_masks(P):
    s, n = P.s, P.n
    contig = np.zeros(n, bool)
    contig[1:] = (s.gap[1:] == 0) & (~s.closure[1:])
    outside = np.zeros(n, bool)
    outside[1:] = (s.h[1:] > s.h[:-1]) & (s.l[1:] < s.l[:-1]) & contig[1:]
    up, dn = P.dirn > 0, P.dirn < 0
    ph, pl = np.r_[np.nan, s.h[:-1]], np.r_[np.nan, s.l[:-1]]
    pbt, pbb = np.r_[np.nan, np.maximum(s.o[:-1], s.c[:-1])], np.r_[np.nan, np.minimum(s.o[:-1], s.c[:-1])]
    with np.errstate(invalid="ignore"):
        B = outside & ((up & (s.c > ph)) | (dn & (s.c < pl)))
        C = outside & ((up & (s.c > pbt)) | (dn & (s.c < pbb)))
        D = outside & (P.ratio >= OB_D_MIN_RATIO)
    return {"A": outside, "B": B, "C": C, "D": D}, contig


def _bar_net(s, i, d):
    """(MFE - MAE) / ATR[i] over OB_HORIZON bars from the next open; None if the window is not clean."""
    last = i + OB_HORIZON
    if last >= s.n or s.gap[i + 1:last + 1].any() or not np.isfinite(s.atr[i]) or s.atr[i] <= 0:
        return None
    entry = s.o[i + 1]
    hi, lo = s.h[i + 1:last + 1], s.l[i + 1:last + 1]
    fav, adv = (hi - entry, entry - lo) if d == 1 else (entry - lo, hi - entry)
    return float((fav.max() - adv.max()) / s.atr[i])


def _ob_tally(P, idxs, rbin):
    strat, strat_w, net = {}, {}, {}
    counts = Counter()
    for i in idxs:
        st, _ = P.outcome(int(i), 0.0)
        counts[st] += 1
        key = (int(P.dirn[i]), int(P.session[i]), int(P.atrb[i]), int(rbin[i]))
        for dct, status in ((strat, st), (strat_w, rm.STOP_FIRST if st == rm.AMBIGUOUS else st)):
            if status in (rm.TARGET_FIRST, rm.STOP_FIRST):
                dct.setdefault(key, [0, 0])[0 if status == rm.TARGET_FIRST else 1] += 1
        v = _bar_net(P.s, int(i), int(P.dirn[i]))
        if v is not None:
            net.setdefault(key, []).append(v)
    return {k: tuple(v) for k, v in strat.items()}, {k: tuple(v) for k, v in strat_w.items()}, dict(counts), net


def run_ob_block(P):
    masks, contig = ob_masks(P)
    rbin = np.digitize(np.nan_to_num(P.ratio, nan=0.0), OB_RATIO_BINS)
    ct_idx = P.episodes((~masks["A"]) & contig, True)
    cs, cs_w, cc, cnet = _ob_tally(P, ct_idx, rbin)
    tc, nc = sum(v[0] for v in cs.values()), sum(sum(v) for v in cs.values())
    out = {}
    for v in OB_VARIANTS:
        ev_idx = P.episodes(masks[v], True)
        es, es_w, ec, enet = _ob_tally(P, ev_idx, rbin)
        sd = w1.stratified_diff(es, cs)
        te, ne = sum(x[0] for x in es.values()), sum(sum(x) for x in es.values())
        r = {"tf": P.tf, "variant": v, "episodes": int(len(ev_idx)), "control_episodes": int(len(ct_idx)), "counts": ec,
             "p_event": (te / ne) if ne else None, "p_event_ci": w1.wilson(te, ne), "n_event": ne,
             "p_control": (tc / nc) if nc else None, "n_control": nc, "n_min_resolved": min(ne, nc), **sd}
        r["sample_label"] = w1.sample_label(r["n_min_resolved"])
        if sd["diff"] is not None:
            r["diff_ci"] = (sd["diff"] - 1.96 * sd["se"], sd["diff"] + 1.96 * sd["se"])
            r["worst_case_diff"] = w1.stratified_diff(es_w, cs_w)["diff"]
        r["net_mfe_mae"] = _strat_mean_diff(enet, cnet)
        r["net_mfe_mae_event_mean"] = float(np.mean([x for vals in enet.values() for x in vals])) if enet else None
        by = {}
        for nm, dv in (("long (bullish bars)", 1), ("short (bearish bars)", -1)):
            e2 = [i for i in ev_idx if P.dirn[i] == dv]
            c2 = [i for i in ct_idx if P.dirn[i] == dv]
            a, _, _, _ = _ob_tally(P, e2, rbin)
            b, _, _, _ = _ob_tally(P, c2, rbin)
            q = w1.stratified_diff(a, b)
            by[nm] = {"episodes": len(e2), "diff": q["diff"], "diff_ci": (q["diff"] - 1.96 * q["se"], q["diff"] + 1.96 * q["se"]) if q["diff"] is not None else None}
        r["by_direction"] = by
        out[v] = r
    return out


# ---------------------------------------------------------------------------
# orchestration, FDR, verdicts
# ---------------------------------------------------------------------------
def analyze(s1h, series, P, dev_end, keep_rows=False):
    """series: {'5m': Series, '15m': Series}; P: {tf: Prepared}. Pure function of data; no holdout access."""
    sweeps = sweep_signals(s1h)
    base = crt_events_1h(s1h, sweeps)
    zones = build_zones(s1h)
    events = {k: base[k] for k in ("A", "D", "E", "ACC")}
    events["B@5m"] = crt_events_b(s1h, series["5m"])
    events["B@15m"] = crt_events_b(s1h, series["15m"])
    res = {"zone_counts": {z: int(len(v["side"])) for z, v in zones.items()}}
    res["crt"] = run_crt(s1h, series["5m"], series, events, zones, dev_end, keep_rows)
    res["ob"] = {tf: run_ob_block(P[tf]) for tf in LTFS}
    res["p1"] = p1_descriptive(s1h, sweeps, dev_end)
    res["baseline"] = baseline_net(s1h, series["5m"], dev_end)
    return res


def collect_tests(res):
    """[(family, container, p, effect_floor, key)] for every tested hypothesis."""
    t = []
    for cfg in CRT_CONFIGS:
        c = res["crt"]["variants"][cfg]["primary"]
        t.append((1, c, c["p"], MIN_EFFECT_ATR, ("crt", cfg)))
    for tf in LTFS:
        for var in OB_VARIANTS:
            c = res["ob"][tf][var]
            t.append((1, c, c["p"], MIN_EFFECT_PP, ("ob", tf, var)))
    for name in HIER_TESTS:
        c = res["crt"]["hier"][name]
        t.append((2, c, c["p"], MIN_EFFECT_ATR, ("hier", name)))
    return t


def apply_fdr(res, status):
    tests = collect_tests(res)
    for fam, st in ((1, status), (2, "EXPLORATORY_READY")):
        rows = [r for r in tests if r[0] == fam and r[2] is not None]
        reject, q = w1.bh_fdr([r[2] for r in rows]) if rows else ([], [])
        rq = {id(r[1]): (rj, qq) for r, rj, qq in zip(rows, reject, q)}
        for f, c, p, floor, key in tests:
            if f != fam:
                continue
            rj, qq = rq.get(id(c), (False, None))
            c["family"], c["fdr_reject"], c["q_value"] = fam, rj, qq
            if key[0] == "crt":
                val, ci, n_min = c["mean"], c["ci"], c["n"]
            elif key[0] == "ob":
                val, ci, n_min = c["diff"], c.get("diff_ci"), c["n_min_resolved"]
            else:
                val, ci, n_min = c["diff"], c["ci"], c["n_min"]
            if val is None or ci is None:
                c["verdict"] = "DATA-LIMITED"
            else:
                c["verdict"] = verdict(val, ci[0], ci[1], rj, n_min, st, floor)
                if key[0] == "ob":
                    c["verdict"] += fragile_suffix(c["verdict"], val, c.get("worst_case_diff"))
    return {1: sum(1 for r in tests if r[0] == 1 and r[2] is not None), 2: sum(1 for r in tests if r[0] == 2 and r[2] is not None)}


def run_wave4(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None, keep_rows=True):
    clean = {tf: (preloaded[tf] if preloaded else rc.clean_timeframe(tf, raw_dir, source)[0]) for tf in ("5m", "15m", "1h")}
    s1h, s5, s15 = rm.Series(clean["1h"], 60), rm.Series(clean["5m"], 5), rm.Series(clean["15m"], 15)
    P = {"5m": w1.Prepared("5m", clean["5m"]), "15m": w1.Prepared("15m", clean["15m"], s_lo=s5)}
    rep15 = rc.clean_timeframe("15m", raw_dir, source)[2] if not preloaded else {
        "first": None, "last": None, "raw_lines_parsed": 0, "unique_timestamps": 0, "duplicate_lines": 0, "conflicting_duplicates": 0,
        "dropped_market_closed": 0, "dropped_off_grid": 0, "dropped_invalid_ohlc": 0, "kept_bars": len(clean["15m"]),
        "unexplained_gaps": 0, "missing_open_bars": 0}
    a6 = rg.gate_a6({"15m": clean["15m"]}, {"15m": rep15}, {"15m": s15})
    status = rg.gate_a7({"15m": clean["15m"]}, {"15m": s15}, a6)["details"]["status"]
    dev_end = rc.HOLDOUT_START_UTC - pd.Timedelta(hours=MAX_H + 1)
    res = analyze(s1h, {"5m": s5, "15m": s15}, P, dev_end, keep_rows)
    res["event_rows"] = res["crt"].pop("rows")
    res["event_rows_n"] = len(res["event_rows"])
    n = apply_fdr(res, status)
    res.update({"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False, "n_tests": n})
    return res


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _pp(x):
    return "n/a" if x is None else f"{100 * x:+.1f}pp"


def _a(x, nd=3):
    return "n/a" if x is None else f"{x:+.{nd}f}"


def _ci(ci, f=_a):
    return "n/a" if not ci else f"[{f(ci[0])}, {f(ci[1])}]"


def render_markdown(r):
    L = ["# Observatory — Wave 4 report (CRT, outside bar, stops/targets, HTF zones as variants)\n",
         f"Contract `{r['contract']['version']}` hash `{r['contract_hash']}` · dataset **{r['dataset_status']}** · holdout opened: **False** · "
         f"Tier-1 tests {r['n_tests'][1]} (FDR family 1) · Tier-2 tests {r['n_tests'][2]} (FDR family 2, exploratory only)\n",
         "**Development data only. Nothing here is a trading rule. A null result means: these definitions, these timeframes, this data.**\n",
         "NET = (MFE − MAE) / ATR(1h) over 6 hours from entry. It is exactly 0 under a driftless market whatever the stop or target, so it "
         "measures what price does after the event before any risk construction. Practical threshold 0.10 ATR. Each line shows the confidence "
         "interval: the largest effect the data cannot rule out.\n"]
    c = r["crt"]
    L.append("## CRT variants (1h reference candle, outcomes measured on 5m candles)\n")
    bl = r.get("baseline") or {}
    if bl.get("mean_long") is not None:
        L.append(f"Reference (all hour boundaries, no event): NET of a long = {_a(bl['mean_long'])} ATR (s.e. {bl['se_6h']:.3f} from non-overlapping windows), "
                 f"of a short = {_a(bl['mean_short'])}; n = {bl['n']}. This is how far this data sits from the symmetric null the tests assume; "
                 f"a CRT result smaller than this is not distinguishable from drift.\n")
    L.append("| variant | episodes | measured | NET 6h | 95% CI | p | q | verdict |\n|---|---|---|---|---|---|---|---|")
    for var in CRT_CONFIGS:
        v = c["variants"][var]
        p = v["primary"]
        L.append(f"| {var} | {v['episodes']} | {v['measured']} | {_a(p['mean'])} | {_ci(p['ci'])} | {'n/a' if p['p'] is None else format(p['p'], '.4f')} | "
                 f"{'n/a' if p.get('q_value') is None else format(p['q_value'], '.4f')} | **{p.get('verdict')}** |")
    v = c["variants"]["ACC"]
    L.append(f"\nControl ACC (C2 closes beyond the swept side; descriptive): measured {v['measured']}, NET {_a(v['primary']['mean'])} {_ci(v['primary']['ci'])}\n")
    L.append("Raw outcome record (6h), per variant:\n")
    L.append("| variant | MFE | MAE | median min to MFE | reach 50% | reach far side | sweep extreme exceeded | NET 3h | NET 12h* | excluded |\n|---|---|---|---|---|---|---|---|---|---|")
    pc = lambda x: "n/a" if x is None else f"{x:.0%}"
    for var in CRT_CONFIGS:
        v = c["variants"][var]
        if not v["measured"]:
            L.append(f"| {var} | n/a |  |  |  |  |  |  |  | {v['excluded']} |")
            continue
        h6, h3, h12 = v["horizons"]["6"], v["horizons"]["3"], v["horizons"]["12"]
        L.append(f"| {var} | {h6['mfe_mean']:.2f} | {h6['mae_mean']:.2f} | {v['median_t_mfe_min']:.0f} | {pc(v['reach']['mid'])} (n {v['reach_mid_n']}) | {pc(v['reach']['far'])} | "
                 f"{pc(v['reach']['stop_level'])} | {_a(h3['net']['mean'])} | {_a(h12['net']['mean'])} | {v['excluded']} |")
    L.append("\n*12h windows overlap between episodes (K = 6h), so their intervals are anti-conservative; descriptive only. "
             "Reach flags and the two tables below use only episodes whose stop and far distances are both >= 0.5 ATR(1h) (eligible n shown in the table).\n")
    L.append("Natural geometry (stop = sweep extreme, target = far side), mean R with a 6h time exit (0 under no edge):\n")
    L.append("| variant | eligible n | mean RR | win rate | win rate expected with no edge | mean R | 95% CI | net of 1 pip | worst-case ambiguity |\n|---|---|---|---|---|---|---|---|---|")
    for var in CRT_CONFIGS:
        nt = c["variants"][var]["natural"]
        if not nt["n"]:
            continue
        m = nt["mean_R"]
        L.append(f"| {var} | {nt['n']} | {nt['mean_RR']:.2f} | {'n/a' if nt['win_rate'] is None else format(nt['win_rate'], '.3f')} | "
                 f"{'n/a' if nt['null_win_rate'] is None else format(nt['null_win_rate'], '.3f')} | {_a(m['mean'])} | {_ci(m['ci'])} | "
                 f"{_a(nt['mean_R_net_spread'])} | {_a(nt['mean_R_worst_ambiguity'])} |")
    L.append("")
    L.append("### Tier 2 — does the extra condition add information AFTER CRT-A? (exploratory family; NET difference, flagged minus unflagged)\n")
    L.append("| condition | flagged n | share | NET flagged | NET not | difference | 95% CI | p | q | verdict |\n|---|---|---|---|---|---|---|---|---|---|")
    for name in HIER_TESTS:
        t = c["hier"][name]
        L.append(f"| {name} | {t['n1']} | {'n/a' if t['share_flagged'] is None else format(t['share_flagged'], '.0%')} | {_a(t['mean_flagged'])} | {_a(t['mean_unflagged'])} | "
                 f"{_a(t['diff'])} | {_ci(t['ci'])} | {'n/a' if t['p'] is None else format(t['p'], '.4f')} | {'n/a' if t.get('q_value') is None else format(t['q_value'], '.4f')} | **{t.get('verdict')}** |")
    L.append("")
    L.append("### Layer 3 (descriptive, NOT tested) — stop × target grid, CRT-A and CRT-D, mean R with 6h time exit (z in brackets)\n")
    L.append("Expected |z| of the best of ~20 cells under no edge is about 2: the best cell in this table is NOT a finding.\n")
    L.append("| stop \\ target | " + " | ".join(TARGETS) + " |\n|---|" + "---|" * len(TARGETS))
    for var in ("A", "D"):
        for sn in STOPS:
            cells = []
            for tn in TARGETS:
                g = c["variants"][var]["grid"][f"{sn}|{tn}"]
                cells.append("n/a" if g["mean_R"] is None else f"{g['mean_R']:+.2f} (z {g['z']:+.1f}, n {g['n']})")
            L.append(f"| {var} · {sn} | " + " | ".join(cells) + " |")
    L.append("")
    L.append(f"Raw per-episode dataset with full 5m excursion paths: wave4_events.jsonl.gz ({r.get('event_rows_n', 0)} episodes).\n")
    L.append("## Outside bar variants (trigger timeframe; continuation of bar colour; P(+1R before −1R) vs matched ordinary bars)\n")
    L.append("OB-B ⊂ OB-C ⊂ OB-A (nested). D is A plus range ≥ 1.5 × prior ATR.\n")
    for tf in LTFS:
        L.append(f"### {tf}\n")
        L.append("| variant | episodes | p(event) | p(control) | difference | 95% CI | p | q | NET diff (descriptive) | verdict |\n|---|---|---|---|---|---|---|---|---|---|")
        for v in OB_VARIANTS:
            o = r["ob"][tf][v]
            if o.get("diff") is None:
                L.append(f"| {v} | {o['episodes']} | DATA-LIMITED |  |  |  |  |  |  | **{o.get('verdict')}** |")
                continue
            nd = o["net_mfe_mae"]
            L.append(f"| {v} | {o['episodes']} | {o['p_event']:.3f} | {o['p_control']:.3f} | {_pp(o['diff'])} | [{_pp(o['diff_ci'][0])}, {_pp(o['diff_ci'][1])}] | "
                     f"{o['p']:.4f} | {o['q_value']:.4f} | {_a(nd['diff'])} {_ci(nd['ci'])} | **{o['verdict']}** |")
        L.append("")
    if r.get("p1"):
        L.append("## P1 (descriptive) — how often does the next 1h candle sweep, by C1 range size?\n")
        for t in r["p1"]:
            L.append(f"- tercile {t['c1_range_tercile']} (C1 range {t['range_x_ATR'][0]}–{t['range_x_ATR'][1]}×ATR, n={t['n']}): any sweep {t['p_sweep_any']:.1%} · sweep and reclaim {t['p_sweep_and_reclaim']:.1%}")
    L.append("\n## HTF zones built (1h, whole development period)\n- " + " · ".join(f"{k}: {v}" for k, v in r["zone_counts"].items()))
    L.append("\n## Not run in this wave\n- Stop/target search with a verdict, zone proximity beyond the location flag, 4H, further combinations, the holdout (sealed).\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _mk(bars, step, start="2026-03-10 00:00"):
    t0 = pd.Timestamp(start, tz="UTC")
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex([t0 + pd.Timedelta(minutes=step * i) for i in range(len(bars))]))
    df["gap_before_missing"], df["closure_before"] = 0, False
    ser = rm.Series(df, step)
    ser.gap, ser.closure = ser.gap.copy(), ser.closure.copy()          # pandas hands back read-only views
    return ser


def _hour_5m(o, h, l, c, hi_bar=2, lo_bar=8, path=None):
    """12 five-minute bars whose aggregate is exactly (o, h, l, c). Default: flat at o, spike high at bar hi_bar, low at lo_bar, end at c."""
    bars = []
    cur = o
    for k in range(12):
        bh, bl = max(cur, c if k == 11 else cur), min(cur, c if k == 11 else cur)
        nxt = c if k == 11 else cur
        if path is not None and k < len(path):
            nxt = path[k]
            bh, bl = max(cur, nxt), min(cur, nxt)
        if k == hi_bar:
            bh = max(bh, h)
        if k == lo_bar:
            bl = min(bl, l)
        bars.append((cur, bh, bl, nxt))
        cur = nxt
    return bars


def selftest(null_series=12):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    base = [(100, 101, 99, 100)] * 20
    C1 = (105, 110, 100, 105)                                           # range [100, 110], mid 105

    def ev1h(c2, c3=None):
        s = _mk(base + [C1, c2] + ([c3] if c3 else []), 60)
        sw = [x for x in sweep_signals(s) if x["j"] == 21]
        return s, sw, crt_events_1h(s, sw)
    # ---- A / D / ACC / E definitions
    s, sw, ev = ev1h((105, 111, 101, 108))
    chk("A sweep-high reclaim", (sw[0]["side"], sw[0]["reclaim"], sw[0]["d"], sw[0]["stop"], sw[0]["far"], sw[0]["mid"]), ("high", True, -1, 111.0, 100.0, 105.0))
    chk("A present, D absent when close above the midpoint", (len(ev["A"]), len(ev["D"])), (1, 0))
    s, sw, ev = ev1h((105, 111, 101, 104))
    chk("D present when close beyond the midpoint", (len(ev["A"]), len(ev["D"])), (1, 1))
    s, sw, ev = ev1h((105, 111, 101, 112))
    chk("acceptance goes to ACC only", (len(ev["A"]), len(ev["ACC"])), (0, 1))
    s, sw, ev = ev1h((105, 108, 99, 106))
    chk("sweep-low reclaim -> long, D", (sw[0]["side"], sw[0]["d"], sw[0]["stop"], sw[0]["far"], len(ev["D"])), ("low", 1, 99.0, 110.0, 1))
    chk("both sides swept -> none", len(ev1h((105, 111, 99, 105))[1]), 0)
    chk("touching the high is not a sweep", len(ev1h((105, 110, 101, 106))[1]), 0)
    s, sw, ev = ev1h((105, 111, 101, 108), (108, 109, 100, 106))
    chk("E: next candle closes lower", (len(ev["E"]), ev["E"][0]["stop"], ev["E"][0]["jk"]), (1, 111.0, 22))
    s, sw, ev = ev1h((105, 111, 101, 108), (108, 112, 100, 107))
    chk("E: stop is the extreme since C1 closed", ev["E"][0]["stop"], 112.0)
    s, sw, ev = ev1h((105, 111, 101, 108), (108, 109, 100, 109))
    chk("E absent when C3 does not confirm", len(ev["E"]), 0)
    s_gap = _mk(base + [C1, (105, 111, 101, 108)], 60)
    s_gap.gap[s_gap.n - 1] = 2
    chk("gap before C2 blocks the signal", len([x for x in sweep_signals(s_gap) if x["j"] == 21]), 0)
    # ---- B on the trigger timeframe (built from 5m bars so the 1h aggregate is consistent)
    def build5(c2_bars):
        bars = []
        for _ in range(20):
            bars += _hour_5m(100, 101, 99, 100)
        bars += _hour_5m(105, 110, 100, 105)
        bars += c2_bars
        d5 = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex([pd.Timestamp("2026-03-10", tz="UTC") + pd.Timedelta(minutes=5 * i) for i in range(len(bars))]))
        d5["gap_before_missing"], d5["closure_before"] = 0, False
        h1 = d5.resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        h1["gap_before_missing"], h1["closure_before"] = 0, False
        return rm.Series(h1, 60), rm.Series(d5, 5)
    flat = (105, 106, 104, 105)
    c2 = [flat] * 4 + [(105, 111, 105, 108)] + [(108, 108.5, 107, 107.5)] * 7
    s1, s5 = build5(c2)
    b = crt_events_b(s1, s5)
    t_c2 = pd.Timestamp("2026-03-10", tz="UTC") + pd.Timedelta(hours=21)
    chk("B triggers on the first closed bar back inside", (len(b), b[0]["side"], b[0]["d"], b[0]["stop"], b[0]["t_known"]), (1, "high", -1, 111.0, t_c2 + pd.Timedelta(minutes=25)))
    c2 = [flat] * 4 + [(105, 111, 105, 111)] + [(111, 111.5, 109, 109.5)] + [(109.5, 109.8, 108.5, 109)] * 6
    s1, s5 = build5(c2)
    b = crt_events_b(s1, s5)
    chk("B waits while the close is still beyond; stop includes later extremes", (len(b), b[0]["stop"], b[0]["t_known"]), (1, 111.5, t_c2 + pd.Timedelta(minutes=30)))
    c2 = [flat] * 4 + [(105, 111, 105, 111)] + [(111, 111.2, 99, 99.5)] + [(99.5, 100, 99, 99.5)] * 6
    s1, s5 = build5(c2)
    b = crt_events_b(s1, s5)
    chk("B: sweep-high then low side breached before returning -> no high-side event", [x for x in b if x["side"] == "high"], [])
    c2 = [flat] * 4 + [(105, 109, 100.5, 105)] + [flat] * 7
    s1, s5 = build5(c2)
    chk("B: no sweep -> no event", len(crt_events_b(s1, s5)), 0)
    # ---- measure_event on a hand-built 5m path (short from 108, stop 111, far 100, mid 105)
    def meas(path, ev_over=None, bars_after=None):
        pre = [(100, 100.1, 99.9, 100)] * 30
        t_known = pd.Timestamp("2026-03-10", tz="UTC") + pd.Timedelta(minutes=5 * 31)
        bars = pre + [(108, 108.2, 107.8, 108)] + path + (bars_after or [(108, 108.2, 107.8, 108)] * 200)
        s_ = _mk(bars, 5)
        ev = {"d": -1, "stop": 111.0, "far": 100.0, "mid": 105.0, "atr": 2.0, "side": "high", "t_known": t_known, "beyond_mid": False}
        ev.update(ev_over or {})
        return measure_event(ev, s_, {})
    m = meas([(108, 108.5, 99.5, 100.5)])
    chk("target first: natural geometry", (m["ok"], m["nat"]["status"], round(m["nat"]["R"], 3), round(m["rr"], 3)), (True, rm.TARGET_FIRST, 2.667, 2.667))
    chk("target first: excess", round(m["excess"], 4), round(1 - 1 / (1 + 8 / 3), 4))
    chk("MFE and MAE exact (ATR units)", (round(m["exc"][6]["mfe"], 4), round(m["exc"][6]["mae"], 4)), (round((108 - 99.5) / 2, 4), round((108.5 - 108) / 2, 4)))
    chk("reach flags", (m["reach"]["mid"], m["reach"]["far"], m["reach"]["stop_level"]), (True, True, False))
    m = meas([(108, 111.5, 107.0, 111.0)])
    chk("stop first", (m["nat"]["status"], m["nat"]["R"]), (rm.STOP_FIRST, -1.0))
    chk("grid cell ATR stop 1.0 (2.0 price) with a 1R target is also stopped", m["cells"]["ATR1.0|1R"]["status"], rm.STOP_FIRST)
    m = meas([(108, 111.5, 99.5, 100.0)])
    chk("same bar both: natural excluded, excursions kept", (m["ok"], m["nat"]["R"], m["nat_worst"]["R"]), (True, None, -1.0))
    m = meas([(108, 108.5, 107.5, 108)] * 80)
    chk("neither: time exit marks at the horizon close", (m["nat"]["status"], round(m["nat"]["R"], 4)), (rm.NEITHER, round((108 - 108) / 3.0, 4)))
    m = meas([(108, 108.5, 107.5, 108)] * 4 + [(108, 108.4, 106.0, 106.4)] + [(106.4, 106.5, 106.3, 106.4)] * 75)
    chk("neither with an open profit: exit mark R", (m["nat"]["status"], round(m["nat"]["R"], 4)), (rm.NEITHER, round((108 - 106.4) / 3.0, 4)))
    m = meas([(108, 108.5, 107, 108)], {"stop": 108.4})
    chk("stop 0.4 < 0.5 ATR: kept for excursions, out of the stop/target layers", (m["ok"], m["eligible"], m["nat"], m["rr"], round(m["exc"][6]["mfe"], 3)), (True, False, None, None, 0.5))
    m = meas([(108, 108.5, 107, 108)], {"far": 108.2})
    chk("short already past the far side: kept for excursions, ineligible for geometry", (m["ok"], m["eligible"], m["reach"]["far"]), (True, False, None))
    # ---- zones
    zb = [(100, 101, 99, 100)] * 20 + [(100.5, 101, 99, 99.5)] + [(99.6, 104, 99.5, 103.5)]
    sz = _mk(zb, 60)
    Z = build_zones(sz)
    k = 21
    chk("ZA demand zone (full)", (Z["ZA-full"]["side"].tolist(), Z["ZA-full"]["lo"].tolist(), Z["ZA-full"]["hi"].tolist(), Z["ZA-full"]["kidx"].tolist(), Z["ZA-full"]["m"].tolist()), ([1], [99.0], [101.0], [k], [20]))
    chk("ZA demand zone (body)", (Z["ZA-body"]["lo"].tolist(), Z["ZA-body"]["hi"].tolist()), ([99.5], [100.5]))
    chk("zone invalidation index", Z["ZA-full"]["inval"].tolist(), [sz.n + 10])
    zb2 = zb + [(103.5, 103.6, 98.0, 98.5)]                                       # closes below the demand zone
    Z2 = build_zones(_mk(zb2, 60))
    chk("a 1h close below the demand zone invalidates it", Z2["ZA-full"]["inval"].tolist()[0], 22)
    ev_z = {"j": 26, "d": 1, "stop": 100.9, "atr": 2.0}
    Zf = {"side": np.array([1]), "lo": np.array([99.0]), "hi": np.array([101.0]), "kidx": np.array([21]), "inval": np.array([200]), "m": np.array([20])}
    chk("zone_located: inside and aligned", zone_located(Zf, ev_z), True)
    chk("zone_located: wrong side", zone_located(Zf, dict(ev_z, d=-1)), False)
    chk("zone_located: sweep extreme within the tolerance band", zone_located(Zf, dict(ev_z, stop=101.4)), True)
    chk("zone_located: beyond the tolerance band", zone_located(Zf, dict(ev_z, stop=101.6)), False)
    chk("zone_located: zone formed after C1 opened is not usable", zone_located(Zf, dict(ev_z, j=22)), False)
    chk("zone_located: too old", zone_located(Zf, dict(ev_z, j=21 + ZONE_MAX_AGE + 1)), False)
    chk("zone_located: invalidated before C2", zone_located(dict(Zf, inval=np.array([25])), ev_z), False)
    zbb = [(100, 101, 99, 100)] * 12 + [(100.2, 100.6, 99.2, 99.6)] + [(99.6, 102.5, 99.5, 102.2)]
    Zb = build_zones(_mk(zbb, 60))
    chk("ZB zone from the last bearish bar before a 10-bar break", (Zb["ZB-full"]["side"].tolist(), Zb["ZB-full"]["m"].tolist(), Zb["ZB-full"]["kidx"].tolist()), ([1], [12], [13]))
    zc = [(100, 100.4, 99.6, 100)] * 25 + [(100, 100.2, 99.8, 99.9)] + [(99.9, 101, 99.9, 100.9)] + [(100.9, 101.8, 100.8, 101.7)] + [(101.7, 102.6, 101.6, 102.5)]
    Zc = build_zones(_mk(zc, 60))
    chk("ZC zone: 3-bar move >= 50% of the 24-bar range", (Zc["ZC-full"]["side"].tolist(), Zc["ZC-full"]["m"].tolist(), Zc["ZC-full"]["kidx"].tolist()), ([1], [25], [28]))
    # ---- outside-bar nesting
    prev = (100.0, 102.0, 99.0, 101.0)
    def obv(bar):
        P_ = w1.Prepared("5m", _mk([(100, 101, 99, 100)] * 40 + [prev, bar] + [(100, 101, 99, 100)] * 40, 5).df)
        mk, _ = ob_masks(P_)
        return {v: bool(mk[v][41]) for v in "ABCD"}
    chk("OB bar above the previous high", obv((99.5, 103.0, 98.0, 102.5)), {"A": True, "B": True, "C": True, "D": True})
    chk("OB bar closes inside the range but above the body", obv((99.5, 103.0, 98.0, 101.5)), {"A": True, "B": False, "C": True, "D": True})
    chk("OB bar closes inside the previous body", obv((99.5, 103.0, 98.0, 100.5)), {"A": True, "B": False, "C": False, "D": True})
    chk("not outside", obv((100.0, 101.5, 99.5, 101.0)), {"A": False, "B": False, "C": False, "D": False})
    rng = np.random.default_rng(11)
    from . import gates as gg
    d5r = gg._random_walk_series(30000, rng, step=5).df
    Pr = w1.Prepared("5m", d5r)
    mk, _ = ob_masks(Pr)
    chk("nesting B within C within A, D within A", (bool((mk["B"] & ~mk["C"]).any()), bool((mk["C"] & ~mk["A"]).any()), bool((mk["D"] & ~mk["A"]).any())), (False, False, False))
    chk("variants are not all identical", (int(mk["A"].sum()) > int(mk["C"].sum()) > int(mk["B"].sum()) > 0), True)
    # ---- no lookahead: events known by T are unchanged when the future is removed
    d5 = gg._random_walk_series(9000, rng, step=5).df
    h1 = d5[["open", "high", "low", "close"]].resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    h1["gap_before_missing"], h1["closure_before"] = 0, False
    s1f, s5f = rm.Series(h1, 60), rm.Series(d5, 5)
    def keyset(s1_, s5_):
        e1 = crt_events_1h(s1_, sweep_signals(s1_))
        allv = {v: e1[v] for v in e1}
        allv["B"] = crt_events_b(s1_, s5_)
        return {(v, x["side"], x["t_known"], round(x["stop"], 6)) for v, lst in allv.items() for x in lst}
    full = keyset(s1f, s5f)
    cutT = h1.index[400] + pd.Timedelta(hours=1)
    s1c = rm.Series(h1[h1.index + pd.Timedelta(hours=1) <= cutT], 60)
    s5c = rm.Series(d5[d5.index + pd.Timedelta(minutes=5) <= cutT], 5)
    part = keyset(s1c, s5c)
    # E needs the NEXT candle (C3); an event is known at C3's close, so compare on t_known <= cutT only
    chk("events unchanged when the future is removed", {x for x in full if x[2] <= cutT} == {x for x in part if x[2] <= cutT}, True)
    zf, zc_ = build_zones(s1f), build_zones(rm.Series(h1[h1.index + pd.Timedelta(hours=1) <= cutT], 60))
    known = lambda Zd: {(v, int(a), int(b)) for v, d in Zd.items() for a, b in zip(d["m"], d["kidx"]) if b <= 399}
    chk("zones known by T are unchanged when the future is removed", known(zf) == known(zc_), True)
    # ---- null calibration: random walks, every test should look uninformative
    zs = {"crt": [], "ob": [], "hier": []}
    used = 0
    base_means, rows0 = [], []
    for _ in range(null_series):
        d5_ = gg._random_walk_series(45000, rng, step=5).df
        h1_ = d5_[["open", "high", "low", "close"]].resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        h1_["gap_before_missing"], h1_["closure_before"] = 0, False
        d15_ = d5_[["open", "high", "low", "close"]].resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        d15_["gap_before_missing"], d15_["closure_before"] = 0, False
        s1_, s5_, s15_ = rm.Series(h1_, 60), rm.Series(d5_, 5), rm.Series(d15_, 15)
        P_ = {"5m": w1.Prepared("5m", d5_), "15m": w1.Prepared("15m", d15_, s_lo=s5_)}
        r_ = analyze(s1_, {"5m": s5_, "15m": s15_}, P_, d5_.index[-1] + pd.Timedelta(days=1), keep_rows=(used == 0))
        if used == 0:
            rows0 = r_["crt"]["rows"]
        used += 1
        base_means.append(r_["baseline"]["mean_long"])
        for var in CRT_CONFIGS:
            z = r_["crt"]["variants"][var]["primary"]["z"]
            if z is not None:
                zs["crt"].append(z)
        for tf in LTFS:
            for var in OB_VARIANTS:
                z = r_["ob"][tf][var].get("z")
                if z is not None:
                    zs["ob"].append(z)
        for name in HIER_TESTS:
            z = r_["crt"]["hier"][name]["z"]
            if z is not None:
                zs["hier"].append(z)
    # ---- raw dataset: rows carry the path, and any stop/target can be rebuilt from it
    chk("verdict: small real effect is SMALL_EFFECT, not NO_INFORMATION", verdict(0.03, 0.01, 0.05, False, 100, "EXPLORATORY_READY", 0.10), "SMALL_EFFECT")
    chk("verdict: tight CI around zero is NO_INFORMATION", verdict(0.0, -0.05, 0.05, False, 100, "EXPLORATORY_READY", 0.10), "NO_INFORMATION")
    chk("verdict: wide CI is INCONCLUSIVE", verdict(0.0, -0.3, 0.3, False, 100, "EXPLORATORY_READY", 0.10), "INCONCLUSIVE")
    chk("event rows were produced", len(rows0) > 50, True)
    if rows0:
        r0 = next(x for x in rows0 if x["config"] == "A")
        chk("row path length = 12h of 5m bars", (len(r0["path_fav_hundredths_atr"]), len(r0["path_adv_hundredths_atr"])), (144, 144))
        chk("row MFE (6h) equals the path maximum", abs(max(r0["path_fav_hundredths_atr"][:72]) / 100 - r0["exc"]["6"]["mfe"]) < 0.006, True)
        chk("row MAE (6h) equals the path maximum", abs(max(r0["path_adv_hundredths_atr"][:72]) / 100 - r0["exc"]["6"]["mae"]) < 0.006, True)
        chk("rows are JSON serialisable", len(json.dumps(r0)) > 100, True)
    chk("A, D, E are run once (no per-timeframe copies)", all(c in ("A", "D", "E", "B@5m", "B@15m", "ACC") for c in {x["config"] for x in rows0}), True)
    chk("baseline NET of an unconditional long is ~0 on random walks (|mean| < 0.03)", abs(float(np.mean(base_means))) < 0.03, True)
    for fam, arr in zs.items():
        arr = np.asarray(arr, float)
        chk(f"null {fam}: enough tests ({len(arr)})", len(arr) >= 20, True)
        if len(arr) >= 20:
            chk(f"null {fam}: |mean z| < 0.5 (got {arr.mean():+.2f})", abs(arr.mean()) < 0.5, True)
            chk(f"null {fam}: sd(z) in [0.6, 1.5] (got {arr.std():.2f})", 0.6 <= arr.std() <= 1.5, True)
            chk(f"null {fam}: share |z|>1.96 <= 15% (got {(np.abs(arr) > 1.96).mean():.0%})", (np.abs(arr) > 1.96).mean() <= 0.15, True)
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": used, **{f"{k}_tests": len(v) for k, v in zs.items()},
                     **{f"{k}_mean_z": round(float(np.mean(v)), 3) if v else None for k, v in zs.items()},
                     **{f"{k}_sd_z": round(float(np.std(v)), 3) if v else None for k, v in zs.items()},
                     **{f"{k}_share_sig": round(float((np.abs(v) > 1.96).mean()), 3) if v else None for k, v in zs.items()}}}


def main():
    ap = argparse.ArgumentParser(description="Observatory Wave 4 (parameterised variants). Read-only on raw data; the holdout stays sealed.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    res = run_wave4(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    import gzip
    with gzip.open(os.path.join(a.out, "wave4_events.jsonl.gz"), "wt") as f:
        for row in res.pop("event_rows"):
            f.write(json.dumps(row) + "\n")
    with open(os.path.join(a.out, "wave4_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "wave4_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
