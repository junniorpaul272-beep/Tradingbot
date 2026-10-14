"""
Observatory — STUDY I, STAGE 1: COUNTS-ONLY GATE (GBPUSD, development data only). Read-only. The holdout stays sealed.

THE CLAIM (a trading video, "find the daily bias"; the five drawn scenarios, wick-to-wick previous daily candle as the range)
  S1  one side swept (high), close back INSIDE the range ............ bearish   (a fade)
  S2  BOTH sides swept, close ABOVE the high ........................ bullish   (continuation)
  S5  BOTH sides swept, close BELOW the low ......................... bearish   (continuation)
  S3  both sides swept, close inside ................................ avoid
  S4  neither side swept (inside day) ............................... avoid
  The mirror of S1 (low swept, close inside -> bullish) is implied, not drawn. Two single-sided cells close BEYOND a boundary and are not in the video (X cells).
  The eight cells below are mutually exclusive and exhaustive: whatever the video meant, one cell covers it.

WHAT THIS FILE DOES (and does not do)
  It builds the daily sessions, classifies each one against the previous valid session, and COUNTS: cell sizes, exclusions and their reasons, recent/older blocks, prior-signal
  context, geometry (overshoot, close location, range width), cross-convention overlap, the placebo-candidate pool with and without a volatility-state match, and the
  detectable effect that follows from the counts. THERE IS NO OUTCOME CODE IN THIS FILE: no barrier, no forward return, no win rate, no estimate, no p-value. The
  pre-registration is frozen only after these counts are reviewed; the outcome stage is written after that.

CONVENTIONS (a session ends at the stated LOCAL wall-clock hour; DST handled by wall-clock arithmetic in the named timezone, never a fixed offset)
  ny17  PRIMARY.     America/New_York 17:00 (the FX trading day shown on the usual charts; the Sunday evening open belongs to Monday's session)
  utc   SENSITIVITY. 00:00 UTC (the day boundary of Study F). Reported alongside, not a second chance to pick the better boundary.
  ny05  CONTROL.     America/New_York 05:00: the same rules on a day shifted by 12 hours (does the conventional boundary matter, or any 24h range?)

RULES (fixed before any outcome exists)
  Session valid: >= 240 of 288 five-minute bars, no gap over 1h, and it reaches its boundary (or the market closes right after it).
  Reference: the latest EARLIER valid session at most 3 calendar days back (so Monday uses Friday). Decision time: the close of the last 5m bar of the classified session.
  Everything is known at that close: classification uses bars up to it only (PAST ONLY).
  Outcome window (for the later stage): the next 24h = 288 five-minute bars, gap-free including the step from the decision bar. A session whose window crosses the weekend
  closure, a missing run or the end of development data is excluded with its reason (Friday decisions are always lost: findings are Monday-Thursday findings).
  Placebo candidates (for the later stage): 5m closes with the same LOCAL weekday and hour +-1h (local clock of the convention), trailing-1h move within 0.5 ATR, more than 24h
  away; and, new here, a trailing 24h range (clock time) within a relative tolerance of the event's. Counted here at three tolerances.

Run:  python3 -m observatory.studyi --counts [--raw-dir ...] [--out ...] [--boundary 2024-10-03]      python3 -m observatory.studyi --selftest
"""
import argparse
import json
import math
import os
import sys

import numpy as np
import pandas as pd

from . import clean as rc
from . import studya as sa
from . import studyb as sb
from . import standards as st
from .studyb import HMAX, TRAIL, TRAIL_TOL, MIN_POOL, NS_5, NS_H, NS_D, R_PLACEBO

NY = "America/New_York"
CONVENTIONS = {"ny17": (NY, 17), "utc": ("UTC", 0), "ny05": (NY, 5)}
PRIMARY, SENSITIVITY, CONTROL = "ny17", "utc", "ny05"
MIN_BARS = 240
MAX_GAP_NS = NS_H
REF_MAX_DAYS = 3
W_ATR = (1.5, 15.0)                         # range width window used by Study F; counted here, applied at the freeze
VOL_TOLS = (0.15, 0.25, 0.40)               # relative tolerance on the trailing-24h range
VOL_MIN_BARS = 259                          # 90% of 288 bars present in the trailing 24h clock window
OVERSHOOT_BINS = (0.5, 1.5)                 # provisional, in ATR(1h); may be MERGED after seeing counts, never tuned on outcomes
CELLS = ("S4_none", "S1_high_in", "X_high_above", "S1m_low_in", "X_low_below", "S3_both_in", "S2_both_above", "S5_both_below")
CLAIM_DIR = {"S1_high_in": -1, "S1m_low_in": +1, "S2_both_above": +1, "S5_both_below": -1}
GROUPS = {"reject": ("S1_high_in", "S1m_low_in"), "outside_close": ("S2_both_above", "S5_both_below")}
VIDEO = {"S1_high_in": "S1 bearish", "S1m_low_in": "mirror of S1 (implied, bullish)", "S2_both_above": "S2 bullish", "S5_both_below": "S5 bearish",
         "S3_both_in": "S3 avoid", "S4_none": "S4 avoid", "X_high_above": "not in the video", "X_low_below": "not in the video"}
DEFAULT_BOUNDARY = "2024-10-03"


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------
def wd_counts(wd):
    wd = np.asarray(wd, int)
    return [int((wd == d).sum()) for d in range(7)]


def build_sessions(ns5, o, h, l, c, tz, end_hour):
    """Group 5m bars into sessions that END at local wall-clock `end_hour` in `tz`. Returns a dict of arrays, one entry per session in time order."""
    ns5 = np.asarray(ns5, np.int64)
    utc = pd.DatetimeIndex(pd.to_datetime(ns5, unit="ns", utc=True))
    loc = utc.tz_convert(tz)
    naive = loc.tz_localize(None)
    key = (naive + pd.Timedelta(hours=24 - end_hour)).floor("D")          # label = the local date on which the session ENDS
    key_ns = key.values.astype("datetime64[ns]").astype(np.int64)
    cut = np.flatnonzero(np.diff(key_ns)) + 1
    a = np.concatenate([[0], cut])
    b = np.concatenate([cut, [len(ns5)]])
    n_s = len(a)
    out = {k: [] for k in ("a", "b", "key_ns", "n", "maxgap", "high", "low", "open", "close", "end_ns", "tail_ns", "after_gap", "wd")}
    end_local = pd.DatetimeIndex(key[a] + pd.Timedelta(hours=end_hour))
    end_utc = end_local.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward").tz_convert("UTC")
    end_ns = end_utc.values.astype("datetime64[ns]").astype(np.int64)
    wd = np.asarray(loc.dayofweek)
    for i in range(n_s):
        x, y = int(a[i]), int(b[i])
        out["a"].append(x), out["b"].append(y)
        out["key_ns"].append(int(key_ns[x]))
        out["n"].append(y - x)
        out["maxgap"].append(int(np.diff(ns5[x:y]).max()) if y - x > 1 else 0)
        out["high"].append(float(h[x:y].max())), out["low"].append(float(l[x:y].min()))
        out["open"].append(float(o[x])), out["close"].append(float(c[y - 1]))
        out["end_ns"].append(int(end_ns[i]))
        out["tail_ns"].append(int(end_ns[i] - (ns5[y - 1] + NS_5)))          # how long before the boundary the last bar closed
        out["after_gap"].append(int(ns5[y] - (ns5[y - 1] + NS_5)) if y < len(ns5) else 0)
        out["wd"].append(int(wd[y - 1]))
    S = {k: np.array(v) for k, v in out.items()}
    reaches = (S["tail_ns"] <= 2 * NS_5) | (S["after_gap"] >= 20 * NS_H)           # reaches its boundary, or the market closes right after it
    S["valid"] = (S["n"] >= MIN_BARS) & (S["maxgap"] <= MAX_GAP_NS) & reaches
    return S


def classify(S, atr_at_decision):
    """Classify every valid session against the latest earlier valid session at most REF_MAX_DAYS back. PAST/closing information only.
    Returns per-session arrays: ref index (-1 none), cell, overshoot (ATR), CLV, ref width (ATR)."""
    n = len(S["a"])
    ref = np.full(n, -1)
    cell = np.array([""] * n, dtype=object)
    over, clv, width = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    valid_idx = np.flatnonzero(S["valid"])
    last = -1
    for i in valid_idx:
        if last >= 0 and (S["key_ns"][i] - S["key_ns"][last]) <= REF_MAX_DAYS * NS_D:
            ref[i] = last
        last = i
    for i in valid_idx:
        j = ref[i]
        if j < 0:
            continue
        H, L = S["high"][j], S["low"][j]
        hi, lo, cl = S["high"][i], S["low"][i], S["close"][i]
        up, dn = hi > H, lo < L
        zone = "above" if cl > H else ("below" if cl < L else "inside")
        if not up and not dn:
            cell[i] = "S4_none"
        elif up and not dn:
            cell[i] = "S1_high_in" if zone == "inside" else "X_high_above"
        elif dn and not up:
            cell[i] = "S1m_low_in" if zone == "inside" else "X_low_below"
        else:
            cell[i] = {"inside": "S3_both_in", "above": "S2_both_above", "below": "S5_both_below"}[zone]
        at = atr_at_decision[i]
        if np.isfinite(at) and at > 0:
            over[i] = max(hi - H, 0.0, L - lo) / at if cell[i] != "S4_none" else 0.0
            width[i] = (H - L) / at
        clv[i] = (cl - L) / (H - L) if H > L else np.nan
    return {"ref": ref, "cell": cell, "overshoot_atr": over, "clv": clv, "width_atr": width}


def window_status(ns5, p):
    """None if the 24h outcome window after decision bar p is gap-free (including the step from p), else the reason."""
    n = len(ns5)
    if p + HMAX >= n:
        return "data_end"
    mx = int(np.diff(ns5[p:p + HMAX + 1]).max())
    if mx == NS_5:
        return None
    if mx >= 20 * NS_H:
        return "closure_>=20h"
    if mx >= NS_H:
        return "closure_1-20h"
    return "missing_candles_<1h"


def vol_state_clock(ns5, h, l, atr_t):
    """Trailing 24h CLOCK-time range of the 5m highs/lows ending at each bar (inclusive), in ATR(1h); NaN unless >= VOL_MIN_BARS bars are present. PAST ONLY."""
    idx = pd.DatetimeIndex(pd.to_datetime(ns5, unit="ns", utc=True))
    hs, ls = pd.Series(h, index=idx), pd.Series(l, index=idx)
    mx = hs.rolling("24h").max().to_numpy()
    mn = ls.rolling("24h").min().to_numpy()
    cnt = pd.Series(np.ones(len(ns5)), index=idx).rolling("24h").sum().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        v = (mx - mn) / atr_t
    v[cnt < VOL_MIN_BARS] = np.nan
    return v


# ---------------------------------------------------------------------------
# placebo-candidate counting (no outcomes: only whether candidates exist)
# ---------------------------------------------------------------------------
def clock_keys(ns5, tz):
    loc = pd.DatetimeIndex(pd.to_datetime(ns5, unit="ns", utc=True)).tz_convert(tz)
    return np.asarray(loc.dayofweek) * 24 + np.asarray(loc.hour), loc.tz_localize(None)


def build_pools(good_q, keys):
    order = np.argsort(keys[good_q], kind="stable")
    q, k = good_q[order], keys[good_q][order]
    cut = np.flatnonzero(np.diff(k)) + 1
    pools = {}
    for chunk in np.split(np.arange(len(q)), cut):
        if len(chunk):
            pools[int(k[chunk[0]])] = q[chunk]
    return pools


def placebo_counts(p, ns5, naive_local, pools, x, vs, tols=VOL_TOLS):
    """Number of placebo candidates for the event at decision bar p: base (clock, hour +-1, trailing move within TRAIL_TOL, > 24h away) and, for each tolerance,
    additionally trailing-24h range within tol x the event's. Returns dict or None if the event has no usable x / vs."""
    if not np.isfinite(x[p]):
        return None
    cand = []
    for dh in (-1, 0, 1):
        t = naive_local[p] + pd.Timedelta(hours=dh)
        k = int(t.dayofweek) * 24 + int(t.hour)
        if k in pools:
            cand.append(pools[k])
    if not cand:
        return {"base": 0, **{f"vol{t}": 0 for t in tols}, "vs_ok": bool(np.isfinite(vs[p]))}
    q = np.concatenate(cand)
    m = (np.abs(x[q] - x[p]) <= TRAIL_TOL) & (np.abs(ns5[q] - ns5[p]) > HMAX * NS_5)
    q = q[m]
    out = {"base": int(len(q)), "vs_ok": bool(np.isfinite(vs[p]))}
    for t in tols:
        out[f"vol{t}"] = int((np.abs(vs[q] - vs[p]) <= t * vs[p]).sum()) if np.isfinite(vs[p]) else 0
    return out


# ---------------------------------------------------------------------------
# counting pass
# ---------------------------------------------------------------------------
def _quant(a, qs=(10, 25, 50, 75, 90)):
    a = np.asarray([v for v in a if np.isfinite(v)], float)
    return {f"p{q}": float(np.percentile(a, q)) for q in qs} if len(a) else {}


def count_convention(name, tz, end_hour, ns5, o, h, l, c, atr_t, x, good_q, bnd_ns, hold_ns):
    dev = ns5 < hold_ns
    nd = int(dev.sum())
    S = build_sessions(ns5[:nd], o[:nd], h[:nd], l[:nd], c[:nd], tz, end_hour)
    p_dec = S["b"] - 1
    atr_dec = atr_t[p_dec]
    C = classify(S, atr_dec)
    ns5d = ns5[:nd]
    keys, naive = clock_keys(ns5d, tz)
    pools = build_pools(good_q, keys)
    vs = vol_state_clock(ns5d, h[:nd], l[:nd], atr_t[:nd])
    res = {"convention": name, "timezone": tz, "end_hour": end_hour, "sessions": int(len(S["a"])), "valid": int(S["valid"].sum()),
           "invalid_reasons": {"short_or_gappy": int(((S["n"] < MIN_BARS) | (S["maxgap"] > MAX_GAP_NS)).sum()), "does_not_reach_boundary": int((~((S["tail_ns"] <= 2 * NS_5) | (S["after_gap"] >= 20 * NS_H))).sum())},
           "no_reference": int((S["valid"] & (C["ref"] < 0)).sum())}
    classified = np.flatnonzero(C["cell"] != "")
    res["classified"] = int(len(classified))
    res["cells"] = {k: int((C["cell"] == k).sum()) for k in CELLS}
    res["cells_by_decision_weekday"] = {k: wd_counts(S["wd"][C["cell"] == k]) for k in CELLS}
    # eligibility of the 24h outcome window, per cell, with reasons (ledger must balance)
    elig = {}
    status = {}
    for i in classified:
        status[int(i)] = window_status(ns5, int(p_dec[i]))
    for k in CELLS:
        led = st.LossLedger(k)
        for i in classified[C["cell"][classified] == k]:
            led.add_candidate()
            s = status[int(i)]
            if s is None:
                led.keep()
            else:
                led.drop("window:" + s)
        elig[k] = led.check()
    res["window_ledger"] = elig
    # groups: the claims that carry a direction
    res["groups"] = {}
    sig_dir = {int(i): CLAIM_DIR.get(C["cell"][i], 0) for i in classified}
    order = [int(i) for i in classified]
    pos = {i: n for n, i in enumerate(order)}
    for gname, cells in GROUPS.items():
        idx = [int(i) for i in classified if C["cell"][i] in cells and status[int(i)] is None]
        t_dec = np.array([ns5[p_dec[i]] for i in idx], np.int64)
        rec = [i for i in idx if ns5[p_dec[i]] >= bnd_ns]
        old = [i for i in idx if ns5[p_dec[i]] < bnd_ns]
        nblocks = len({int(t // (2 * NS_D)) for t in t_dec})
        width_ok = [i for i in idx if np.isfinite(C["width_atr"][i]) and W_ATR[0] <= C["width_atr"][i] <= W_ATR[1]]
        # prior-signal context (past only: the previous three classified sessions)
        ctx = {"agree": 0, "conflict": 0, "mixed": 0, "none": 0}
        for i in idx:
            prev = [sig_dir[order[m]] for m in range(max(0, pos[i] - 3), pos[i]) if sig_dir[order[m]] != 0]
            d = CLAIM_DIR[C["cell"][i]]
            ctx["none" if not prev else ("agree" if all(v == d for v in prev) else ("conflict" if all(v == -d for v in prev) else "mixed"))] += 1
        # placebo-candidate feasibility
        pc = [placebo_counts(int(p_dec[i]), ns5d, naive, pools, x, vs) for i in idx]
        pc = [r for r in pc if r is not None]
        pl = {"events_with_x": len(pc), "vs_missing": int(sum(not r["vs_ok"] for r in pc)),
              "median_candidates": {k: float(np.median([r[k] for r in pc])) for k in (["base"] + [f"vol{t}" for t in VOL_TOLS])} if pc else {},
              "share_below_min_pool": {k: float(np.mean([r[k] < MIN_POOL for r in pc])) for k in (["base"] + [f"vol{t}" for t in VOL_TOLS])} if pc else {},
              "share_below_30": {k: float(np.mean([r[k] < R_PLACEBO for r in pc])) for k in (["base"] + [f"vol{t}" for t in VOL_TOLS])} if pc else {}}
        over = [C["overshoot_atr"][i] for i in idx]
        a_, b_ = OVERSHOOT_BINS
        res["groups"][gname] = {
            "cells": list(cells), "classified": int(sum(C["cell"][i] in cells for i in classified)), "eligible": len(idx), "by_cell": {k: int(sum(C["cell"][i] == k for i in idx)) for k in cells},
            "recent": len(rec), "older": len(old), "blocks_2day": nblocks, "width_in_1.5_15_atr": len(width_ok),
            "weekday": wd_counts(S["wd"][idx]), "feasibility": st.feasibility(len(idx), nblocks),
            "feasibility_recent_vs_older_detectable_diff": st.block_diff_detectable(len(rec), len(old)),
            "overshoot_quantiles_atr": _quant(over), "overshoot_bins": {f"<{a_}": int(sum(v < a_ for v in over)), f"{a_}-{b_}": int(sum(a_ <= v < b_ for v in over)), f">={b_}": int(sum(v >= b_ for v in over))},
            "clv_quantiles": _quant([C["clv"][i] for i in idx]), "prior_signal_context": ctx, "placebo": pl,
            "decision_times": [int(t) for t in t_dec]}
    res["width_atr_quantiles_all_classified"] = _quant([C["width_atr"][i] for i in classified])
    res["_end_ns"] = {int(i): int(S["end_ns"][i]) for i in classified}
    res["_cell"] = {int(i): str(C["cell"][i]) for i in classified}
    res["_status"] = {int(i): status[int(i)] for i in classified}
    return res


def cross_overlap(a, b):
    """Primary events (convention a) matched to the nearest session END in convention b (within 12h): agreement of cells. Counts only."""
    ends_b = np.array(sorted(b["_end_ns"].items(), key=lambda kv: kv[1]))
    out = {}
    for gname, cells in GROUPS.items():
        tot = same = other_claim = no_match = not_eligible = 0
        for i, e in a["_end_ns"].items():
            if a["_cell"][i] not in cells or a["_status"][i] is not None:
                continue
            tot += 1
            if not len(ends_b):
                no_match += 1
                continue
            k = int(np.argmin(np.abs(ends_b[:, 1] - e)))
            j, eb = int(ends_b[k, 0]), int(ends_b[k, 1])
            if abs(eb - e) > 12 * NS_H:
                no_match += 1
            elif b["_status"][j] is not None:
                not_eligible += 1
            elif b["_cell"][j] == a["_cell"][i]:
                same += 1
            else:
                other_claim += 1
        out[gname] = {"primary_events": tot, "same_cell_in_other": same, "different_cell_in_other": other_claim, "other_not_eligible": not_eligible, "no_matching_session": no_match}
    return out


def run_counts(raw_dir=None, source="observatory", boundary=DEFAULT_BOUNDARY, preloaded=None):
    raw_dir = raw_dir or rc.DEFAULT_RAW_DIR
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    bnd_ns = pd.Timestamp(boundary, tz="UTC").as_unit("ns").value
    ctx = sb.make_ctx(s5, s1h, +1, hold_ns)
    ns5 = ctx["ns5"]
    good_q = np.concatenate([p[0] for p in ctx["pools"].values()]) if ctx["pools"] else np.array([], int)
    res = {"dataset_status": status, "boundary_utc": boundary, "holdout_opened": False, "development_first_bar": str(s5.index[0]), "conventions": {}}
    for name, (tz, hr) in CONVENTIONS.items():
        res["conventions"][name] = count_convention(name, tz, hr, ns5, s5.o, s5.h, s5.l, s5.c, ctx["atr_t"], ctx["x"], good_q, bnd_ns, hold_ns)
    res["overlap_primary_vs_utc"] = cross_overlap(res["conventions"][PRIMARY], res["conventions"][SENSITIVITY])
    for v in res["conventions"].values():
        for k in ("_end_ns", "_cell", "_status"):
            v.pop(k, None)
        for g in v["groups"].values():
            g.pop("decision_times", None)
    return res


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _f3(x):
    return "n/a" if x is None else f"{x:.3f}"


def _pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def render_markdown(r):
    L = ["# Observatory — Study I, stage 1: counts-only gate (GBPUSD)\n",
         f"Dataset **{r['dataset_status']}** · first bar {r['development_first_bar']} · block boundary {r['boundary_utc']} · holdout opened: **False** · **no outcome has been computed in this report**\n"]
    for name, v in r["conventions"].items():
        role = {"ny17": "PRIMARY", "utc": "SENSITIVITY", "ny05": "CONTROL (12h-shifted day)"}[name]
        L += [f"\n## {name} — {role} ({v['timezone']}, session ends {v['end_hour']:02d}:00 local)\n",
              f"Sessions {v['sessions']} · valid {v['valid']} · classified {v['classified']} · no reference {v['no_reference']} · invalid because short/gappy {v['invalid_reasons']['short_or_gappy']}, not reaching the boundary {v['invalid_reasons']['does_not_reach_boundary']}\n",
              "| cell | in the video | classified | outcome window OK | dropped (reasons) | kept share | decision weekday Mon..Fri |\n|---|---|---|---|---|---|---|"]
        for k in CELLS:
            led = v["window_ledger"][k]
            L.append(f"| {k} | {VIDEO[k]} | {v['cells'][k]} | {led['kept']} | {led['reasons']} | {_pct(led['kept_share'])} | {v['cells_by_decision_weekday'][k]} |")
        L.append(f"\nReference-range width in ATR(1h), all classified sessions: {v['width_atr_quantiles_all_classified']}\n")
        for g, d in v["groups"].items():
            fz = d["feasibility"]
            L += [f"### {name} / group `{g}` ({', '.join(d['cells'])})\n",
                  f"classified {d['classified']} → eligible {d['eligible']} (by cell {d['by_cell']}) · recent {d['recent']} / older {d['older']} · distinct 2-day blocks {d['blocks_2day']} · width within 1.5–15 ATR: {d['width_in_1.5_15_atr']} · weekday Mon..Sun {d['weekday']}",
                  f"- detectable effect from counts: optimistic {fz['detectable_optimistic']:.3f}, conservative {fz['detectable_conservative']:.3f}, tier **{fz['tier']}**; detectable recent-vs-older difference {_f3(d['feasibility_recent_vs_older_detectable_diff'])}" if fz.get("detectable_optimistic") is not None else "- no eligible events",
                  f"- overshoot beyond the boundary (ATR): {d['overshoot_quantiles_atr']} · provisional bins {d['overshoot_bins']}",
                  f"- close location (CLV) quantiles: {d['clv_quantiles']}",
                  f"- prior-signal context (previous 3 classified sessions, past only): {d['prior_signal_context']}"]
            pl = d["placebo"]
            if pl.get("median_candidates"):
                L += [f"- placebo candidates per event (median): {pl['median_candidates']} · share below {MIN_POOL}: {pl['share_below_min_pool']} · share below {R_PLACEBO}: {pl['share_below_30']} · events without a volatility state: {pl['vs_missing']} of {pl['events_with_x']}\n"]
    L += ["\n## Overlap of the primary (ny17) events with the UTC convention (nearest session end within 12h)\n", "| group | primary events | same cell in UTC | different cell in UTC | UTC not outcome-eligible | no matching session |\n|---|---|---|---|---|---|"]
    for g, d in r["overlap_primary_vs_utc"].items():
        L.append(f"| {g} | {d['primary_events']} | {d['same_cell_in_other']} | {d['different_cell_in_other']} | {d['other_not_eligible']} | {d['no_matching_session']} |")
    L += ["\n## Not done here\n", "- Any outcome, estimate or test. The freeze of definitions, bins, matching tolerance, families and multiplicity correction. The volatility-matched placebo itself (only its candidate supply is counted)."]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# selftest (synthetic, no data needed)
# ---------------------------------------------------------------------------
def _synth_week(start_utc, days_spec, step=5):
    """Gap-free 5m bars from start_utc. Each block (n_bars, open, high, low, close) runs open -> high (at n//3) -> low (at 2n//3) -> close, so the extremes are exact."""
    ns, o, h, l, c = [], [], [], [], []
    t = pd.Timestamp(start_utc, tz="UTC").as_unit("ns").value
    for (n, o0, hi, lo, cl, _order) in days_spec:
        i1, i2 = n // 3, 2 * n // 3
        path = np.interp(np.arange(n + 1), [0, i1, i2, n], [o0, hi, lo, cl])
        for i in range(n):
            a, b = path[i], path[i + 1]
            ns.append(t)
            o.append(a), c.append(b), h.append(max(a, b)), l.append(min(a, b))
            t += step * 60 * 10 ** 9
    return np.array(ns, np.int64), np.array(o), np.array(h), np.array(l), np.array(c)


def selftest():
    checks, fails = 0, []

    def chk(label, got, want):
        nonlocal checks
        checks += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    # eight consecutive NY-17:00 sessions, Mon 2025-03-03 .. : session k ends 17:00 NY = 22:00 UTC (winter), starts previous day 22:00 UTC
    start = "2025-03-02 22:00"                                  # Sunday 17:00 NY: the Sunday-evening open belongs to Monday's session
    base = 1.2500
    spec = [(288, base, base + 0.0100, base - 0.0100, base, "hl")]          # Monday: the reference, range [1.2400, 1.2600]
    plan = [("S4_none", (1.2550, 1.2450, 1.2500)), ("S1_high_in", (1.2640, 1.2480, 1.2540)), ("S3_both_in", (1.2650, 1.2350, 1.2500)),
            ("S2_both_above", (1.2700, 1.2340, 1.2660))]
    # each day is classified against the PREVIOUS day, so the reference changes: build reference-aware days one at a time
    ref_h, ref_l = base + 0.0100, base - 0.0100
    cells_expected = []
    for name, (hi, lo, cl) in plan:
        spec.append((288, ref_l + 0.005, hi, lo, cl, "hl"))
        cells_expected.append((name, ref_h, ref_l))
        ref_h, ref_l = hi, lo
    ns, o, h, l, c = _synth_week(start, spec)
    S = build_sessions(ns, o, h, l, c, NY, 17)
    chk("sessions: Sunday 17:00 NY open starts Monday's session (5 full sessions)", (len(S["a"]), list(S["n"])), (5, [288] * 5))
    chk("sessions: all valid and reach their boundary", bool(S["valid"].all()), True)
    atr = np.full(len(ns), 0.0010)
    C = classify(S, atr[S["b"] - 1])
    chk("classify: reference is the previous valid session; first session has none", (int(C["ref"][0]), list(C["ref"][1:])), (-1, [0, 1, 2, 3]))
    chk("classify: hand-built days map to the intended cells", list(C["cell"][1:5]), [e[0] for e in cells_expected])
    chk("classify: S1 CLV inside [0,1]", bool(0 <= C["clv"][2] <= 1), True)

    # mirror symmetry: negating prices swaps high/low cells
    S2 = build_sessions(ns, -o, -l, -h, -c, NY, 17)
    C2 = classify(S2, atr[S2["b"] - 1])
    swap = {"S1_high_in": "S1m_low_in", "S1m_low_in": "S1_high_in", "S2_both_above": "S5_both_below", "S5_both_below": "S2_both_above", "X_high_above": "X_low_below", "X_low_below": "X_high_above"}
    chk("mirror: negated prices swap the high and low cells", [swap.get(x, x) for x in C["cell"][1:5]], list(C2["cell"][1:5]))

    # past-only: changing bars AFTER a session's end leaves its classification unchanged
    k = int(S["b"][2])
    o3, h3, l3, c3 = o.copy(), h.copy(), l.copy(), c.copy()
    h3[k:] += 0.05
    l3[k:] -= 0.05
    c3[k:] += 0.03
    S3 = build_sessions(ns, o3, h3, l3, c3, NY, 17)
    C3 = classify(S3, atr[S3["b"] - 1])
    chk("past-only: sessions 0-2 classify identically when later bars change", (list(C3["cell"][:3]), list(C["cell"][:3])), (list(C["cell"][:3]), list(C["cell"][:3])))

    # DST: the same wall-clock 17:00 NY is 21:00 UTC in summer
    ns_s, o_s, h_s, l_s, c_s = _synth_week("2025-07-06 21:00", [(288, 1.3, 1.31, 1.29, 1.3, "hl")] * 2)       # Sunday 17:00 EDT
    S_s = build_sessions(ns_s, o_s, h_s, l_s, c_s, NY, 17)
    chk("DST: summer Sunday 21:00 UTC (17:00 EDT) opens Monday's session", (len(S_s["a"]), list(S_s["n"])), (2, [288, 288]))
    chk("DST: session end is 21:00 UTC in summer, 22:00 UTC in winter",
        (int(S_s["end_ns"][0] // NS_H % 24), int(S["end_ns"][0] // NS_H % 24)), (21, 22))

    # window status
    chk("window: gap-free window ok, data end flagged", (window_status(ns, 0), window_status(ns, len(ns) - 2)), (None, "data_end"))
    ns_g = ns.copy()
    ns_g[600:] += 48 * NS_H                                      # a long closure inside the window of bar 400
    chk("window: closure reason", window_status(ns_g, 400), "closure_>=20h")
    ns_m = ns.copy()
    ns_m[500:] += 6 * NS_5                                       # 30 minutes missing
    chk("window: short missing run reason", window_status(ns_m, 400), "missing_candles_<1h")
    ns_f = ns.copy()
    ns_f[450:] += 100 * NS_H                                     # the weekend gap sits exactly between the decision bar and the next
    chk("window: gap right after the decision bar is not 'ok' (Friday decision)", window_status(ns_f, 449), "closure_>=20h")

    # session validity: a session with a 2h hole is invalid
    ns_h = ns[:288 * 2].copy()
    ns_h[100:] += 24 * NS_5
    Sh = build_sessions(ns_h, o[:288 * 2], h[:288 * 2], l[:288 * 2], c[:288 * 2], NY, 17)
    chk("validity: a 2h hole makes the session invalid", bool(Sh["valid"][0]), False)

    # vol state: needs enough bars, and only past bars
    v = vol_state_clock(ns, h, l, atr)
    chk("vol state: NaN in the first 24h minus tolerance, finite after", (bool(np.isnan(v[10])), bool(np.isfinite(v[400]))), (True, True))
    h4, l4 = h.copy(), l.copy()
    h4[600:] += 1.0
    v4 = vol_state_clock(ns, h4, l4, atr)
    chk("vol state: changing FUTURE bars leaves earlier values unchanged", bool(np.allclose(v[:600], v4[:600], equal_nan=True)), True)

    # placebo counting returns the pool size and shrinks with a tighter volatility tolerance
    rng = np.random.default_rng(1)
    n = 40000
    ns_p = 1_700_000_000_000_000_000 + np.arange(n, dtype=np.int64) * NS_5
    x_p = rng.normal(0, 0.3, n)
    vs_p = np.abs(rng.normal(5, 1.5, n))
    keys, naive = clock_keys(ns_p, "UTC")
    gq = np.arange(300, n - 300)
    pools = build_pools(gq, keys)
    r = placebo_counts(20000, ns_p, naive, pools, x_p, vs_p)
    chk("placebo counts: base >= vol0.4 >= vol0.25 >= vol0.15 and base > 0", (r["base"] >= r["vol0.4"] >= r["vol0.25"] >= r["vol0.15"], r["base"] > 0), (True, True))

    # standards-module pieces used here
    chk("weekday_coverage_from_wd", wd_counts(np.array([0, 0, 4, 6])), [2, 0, 0, 0, 1, 0, 1])
    return {"checks": checks, "failures": fails, "passed": not fails}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", default=None)
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--boundary", default=DEFAULT_BOUNDARY)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--counts", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    if not a.counts:
        ap.error("stage 1 supports --counts and --selftest only (no outcome code exists yet)")
    os.makedirs(a.out, exist_ok=True)
    res = run_counts(a.raw_dir, boundary=a.boundary)
    json.dump(res, open(os.path.join(a.out, "studyi_counts.json"), "w"), indent=2, default=str)
    md = render_markdown(res)
    open(os.path.join(a.out, "studyi_counts.md"), "w").write(md)
    print(md)


if __name__ == "__main__":
    main()
