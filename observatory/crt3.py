"""
observatory/crt3.py
===================
OBSERVATORY — CRT-3: does CROSSING the level add anything beyond being NEAR it? ISOLATED from the live bot.

WHY THIS QUESTION
-----------------
CRT-2 found that from the close of C2, price drifts TOWARD the swept level: after a reclaim (C2 closed back inside) it
tends to go on to the level, after an acceptance (C2 closed beyond) it tends to come back to it. That is one effect
seen from two sides, about 0.11 ATR(1h) (~1.6 pips) of (MFE - MAE) over 6h. Two things are unknown:
  (1) is it the SWEEP, or would any recent extreme that price came close to be revisited just the same?
  (2) is the effect big enough to survive a spread, measured as a real, fixed-rule trade (not as MFE - MAE)?

THREE LAYERS (kept apart on purpose)
------------------------------------
  Layer 1  PHENOMENON   S1, S2, P   (family 1, Benjamini-Hochberg q = 0.10)
  Layer 2  MAGNITUDE    effect sizes in ATR and pips, stability over time and by side (descriptive)
  Layer 3  TRADEABILITY R1          (family 2)  + fixed stop variants (descriptive) + opportunity cost

EVENTS (all known at the CLOSE of C2, two consecutive clean 1h candles C1, C2; level = the C1 extreme on the side in question)
  RECLAIM   C2 pierced the level one-sidedly and closed back inside C1       (wave4 sweep_signals, reclaim = True)
  ACCEPT    C2 pierced the level one-sidedly and closed beyond it            (wave4 sweep_signals, reclaim = False)
  APPROACH  C2 stayed inside C1 (high <= C1 high and low >= C1 low) and its extreme came within 0.25 ATR of the level
            WITHOUT crossing it. If both sides qualify the candle is ambiguous and skipped (counted).
  Direction TOWARD the level = +1 if level > C2 close else -1. It was read off the CRT-2 signs; "follow" is the exact
            negative, so it is NOT a second test.
  Episodes  anchored 6h per side, within each group (pooled sweeps jointly for R1).

OUTCOMES (5m path, entry at the OPEN of the first 5m bar after the event is known, 72 bars = 6h)
  NET   = (MFE - MAE) / ATR14(1h at C1), toward the level (zero in a symmetric market whatever the stop/target)
  RET   = direction * (close of the 72nd bar - entry), in pips; NET-OF-COST = RET - 1.0 pip (wave1's SPREAD_PIPS, flat per trade)
  The window must be free of missing candles (6h only; CRT-2 used 12h, which dropped ~11% of events) and must NOT cross a
  weekend closure (CRT-2 did not check; the Friday->Sunday jump is not a 6h move). Every exclusion is counted and audited.

TESTS
  S1  RECLAIM vs APPROACH, NET toward the level, stratified by side x distance(close->level, ATR bins 0.15/0.3/0.5/0.8).
      Sensitivity (descriptive): the same also matched on session, and on C1 range; each reports its matched share.
      "Does crossing the level add information, holding distance to the level fixed?"  (stratified Welch-type difference)
  S2  the same with only THIN-margin reclaims (pierced by <= 0.25 ATR) vs approaches (missed by <= 0.25 ATR): crossing by a hair vs missing by a hair.
  P   APPROACH alone, NET toward the level vs 0: is proximity by itself enough?
  R1  pooled sweeps (reclaim + accept, jointly collapsed), RET in pips toward the level vs 0 (gross), and net of 1 pip.
Descriptive: NET/RET of each group, by side, first vs second half of development, balance of the groups, three fixed stops
(0.5 / 1.0 / 2.0 ATR + 6h time exit), trades per month, a missingness audit, an unconditional-drift reference.

PRE-DECLARED READING OF S1 (so the conclusion is not chosen after the numbers)
  NO_INFORMATION   -> crossing the level adds nothing detectable beyond proximity: this CRT branch is KILLED.
  INCONCLUSIVE     -> cannot be decided; more independent data needed (see --readiness).
  SMALL_EFFECT / EXPLORATORY_SIGNAL / DISCOVERY_PASS -> crossing adds something on development data; it is a hypothesis for the
                      holdout, which stays sealed until the readiness gate says it can discriminate.
  If P's CI excludes 0 and S1's does not, the drift is explained by proximity, not by the sweep.

DATA REUSE: the sweep groups were already examined in wave 4 and CRT-2 on this development data; the APPROACH group is new.
The holdout (>= HOLDOUT_START_UTC) is sealed here: this module has no way to open it. --readiness counts holdout EVENTS only
(structure flags, no price outcome) and computes power from DEVELOPMENT estimates.

Run:  python3 -m observatory.crt3 [--raw-dir ...] [--out ...]      python3 -m observatory.crt3 --selftest
      python3 -m observatory.crt3 --readiness
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
from . import wave4 as w4

_W4_NEEDED = ("locate", "sweep_signals", "collapse", "verdict", "_mean_test", "_welch", "_strat_mean_diff", "_mk", "CONTRACT_HASH", "K_EP_HOURS")
_W4_MISSING = [n for n in _W4_NEEDED if not hasattr(w4, n)]
if _W4_MISSING:
    raise SystemExit("observatory/wave4.py is the WRONG VERSION (missing: " + ", ".join(_W4_MISSING) + "). "
                     "crt3 needs the v2.1 wave4.py (the one with `def locate`). Replace observatory/wave4.py with it and re-run.")

HORIZON_H = 6
NB5 = HORIZON_H * 12                      # 5m bars in the window
APPROACH_ATR = 0.25                       # an approach must come within this of the level
THIN_ATR = 0.25                           # thin-margin reclaims pierced by at most this
DL_EDGES = (0.15, 0.3, 0.5, 0.8)          # close->level distance bins in ATR (fixed, chosen from event COUNTS only, before any outcome was seen)
C1_EDGES = (0.8, 1.3)                     # C1 range / ATR bins, sensitivity only
STOP_ATRS = (0.5, 1.0, 2.0)
SPREAD_PIPS = w1.SPREAD_PIPS              # flat cost per trade, in pips
PIP = 1e-4
MIN_EFFECT_ATR = 0.10
FDR_Q = w1.FDR_Q
SESSION_EDGES = w1.SESSION_EDGES
SESSION_NAMES = w1.SESSION_NAMES
CONTRACT = {
    "module": "crt3", "version": "v1", "instrument": "GBPUSD", "uses": f"wave4 events (contract {w4.CONTRACT_HASH})",
    "horizon_h": HORIZON_H, "episode_hours": w4.K_EP_HOURS, "approach_atr": APPROACH_ATR, "thin_atr": THIN_ATR,
    "distance_bins_atr": list(DL_EDGES), "strata": "side x distance bin", "sensitivity_strata": ["+ session", "+ C1 range"], "c1_range_bins_atr": list(C1_EDGES), "stop_atr_variants": list(STOP_ATRS),
    "spread_pips_flat": SPREAD_PIPS, "direction": "toward the swept level (sign of level - C2 close)",
    "window_rules": "6h only: no missing candle, no weekend closure inside the window",
    "family1": ["S1", "S2", "P"], "family2": ["R1"], "min_effect_atr": MIN_EFFECT_ATR, "fdr_q": FDR_Q,
    "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------
def approach_signals(s, a_max=APPROACH_ATR):
    """One-sided approaches: C2 inside C1 and its extreme within a_max*ATR of the C1 extreme but not beyond it.
    Returns (events, n_ambiguous). Known at C2's close; uses only C1, C2 and ATR through C1."""
    out, amb = [], 0
    for j in range(1, s.n):
        if s.gap[j] > 0 or s.closure[j] or not np.isfinite(s.atr[j - 1]) or s.atr[j - 1] <= 0:
            continue
        h1, l1, h2, l2 = s.h[j - 1], s.l[j - 1], s.h[j], s.l[j]
        atr = float(s.atr[j - 1])
        inside = (h2 <= h1) and (l2 >= l1)
        if not inside:
            continue
        hi_ap = (h1 - h2) <= a_max * atr
        lo_ap = (l2 - l1) <= a_max * atr
        if hi_ap and lo_ap:
            amb += 1
            continue
        if hi_ap:
            out.append({"j": j, "side": "high", "level": float(h1), "gap": float(h1 - h2), "atr": atr, "c2": float(s.c[j]),
                        "t_known": s.index[j] + s.step, "c1_range": float(h1 - l1), "c2_range": float(h2 - l2)})
        elif lo_ap:
            out.append({"j": j, "side": "low", "level": float(l1), "gap": float(l2 - l1), "atr": atr, "c2": float(s.c[j]),
                        "t_known": s.index[j] + s.step, "c1_range": float(h1 - l1), "c2_range": float(h2 - l2)})
    return out, amb


def _mk_event(j, side, grp, level, margin, atr, c2, t_known, c1_range, c2_range):
    if c2 == level:
        return None
    return {"j": j, "jk": j, "side": side, "group": grp, "level": float(level), "margin": float(margin) / atr, "atr": float(atr),
            "c2": float(c2), "t_known": t_known, "dir": 1 if level > c2 else -1, "dlev": abs(level - c2) / atr,
            "c1_range_atr": float(c1_range) / atr, "c2_range_atr": float(c2_range) / atr,
            "session": int(np.digitize(t_known.hour, SESSION_EDGES))}


def make_events(s1h, sweeps, approaches):
    """Groups reclaim / accept / approach as lists of event dicts (events with the close exactly on the level are dropped: no direction)."""
    ev = {"reclaim": [], "accept": [], "approach": []}
    for sg in sweeps:
        j, side = sg["j"], sg["side"]
        level = s1h.h[j - 1] if side == "high" else s1h.l[j - 1]
        c2r = float(s1h.h[j] - s1h.l[j])
        e = _mk_event(j, side, "reclaim" if sg["reclaim"] else "accept", level, abs(sg["stop"] - level), sg["atr"], sg["c2"], sg["t_known"], sg["c1_range"], c2r)
        if e is not None:
            ev[e["group"]].append(e)
    for ap in approaches:
        e = _mk_event(ap["j"], ap["side"], "approach", ap["level"], ap["gap"], ap["atr"], ap["c2"], ap["t_known"], ap["c1_range"], ap["c2_range"])
        if e is not None:
            ev["approach"].append(e)
    return ev


# ---------------------------------------------------------------------------
# measurement (5m path, 6h window, own validity rules)
# ---------------------------------------------------------------------------
def window_ok(ev, s5):
    """Structure-only check (no price read): returns (e, None) or (None, reason)."""
    e = w4.locate(ev, s5)
    if e is None:
        return None, "no_trigger_bar"
    last = e + NB5
    if last >= s5.n:
        return None, "end_of_data"
    if s5.gap[e + 1:last + 1].any():
        return None, "data_gap"
    if s5.closure[e + 1:last + 1].any():
        return None, "closure_in_window"
    return e, None


def measure6(ev, s5):
    """Everything recorded for one event. {'ok': False, 'reason': ...} when the window is not clean."""
    e, why = window_ok(ev, s5)
    if e is None:
        return {"ok": False, "reason": why}
    d, atr = ev["dir"], ev["atr"]
    last = e + NB5
    entry = float(s5.o[e + 1])
    hi, lo = s5.h[e + 1:last + 1], s5.l[e + 1:last + 1]
    fav, adv = (hi - entry, entry - lo) if d == 1 else (entry - lo, hi - entry)
    mfe, mae = float(fav.max()), float(adv.max())
    ret = float(d * (s5.c[last] - entry))
    stops, stopped = {}, {}
    for k in STOP_ATRS:
        sd = k * atr
        hit = np.nonzero(adv >= sd)[0]
        stopped[k] = bool(len(hit))
        stops[k] = (-sd if len(hit) else ret) / PIP - SPREAD_PIPS                       # net pips; the stop fills AT the stop price (no slippage)
    after_gap = bool(last + 144 - NB5 < s5.n and s5.gap[last + 1:last + 1 + (144 - NB5)].any())   # would CRT-2's 12h rule have dropped it?
    return {"ok": True, "e": e, "entry": entry, "net": (mfe - mae) / atr, "mfe": mfe / atr, "mae": mae / atr,
            "ret_atr": ret / atr, "ret_pips": ret / PIP, "stops": stops, "stopped": stopped, "crt2_12h_gap": after_gap}


# ---------------------------------------------------------------------------
# statistics helpers
# ---------------------------------------------------------------------------
def stratum(m):
    return (m["side"], int(np.digitize(m["dlev"], DL_EDGES)))


def stratum_session(m):
    return stratum(m) + (m["session"],)


def stratum_c1(m):
    return stratum(m) + (int(np.digitize(m["c1_range_atr"], C1_EDGES)),)


def _by(ms, key, keyf=stratum):
    out = {}
    for m in ms:
        out.setdefault(keyf(m), []).append(m[key])
    return out


def _vals(ms, key):
    return [m[key] for m in ms]


def trade_verdict(net_mean, lo, hi, n, status):
    if n < 30 or net_mean is None:
        return "DATA-LIMITED"
    tag = "" if status == "PRIMARY_READY" else " (dataset not PRIMARY_READY)"
    if lo > 0:
        return "NET_POSITIVE_CI_EXCLUDES_0 (needs the holdout)" + tag
    if hi < 0:
        return "NET_NEGATIVE"
    return "NET_POSITIVE_NOT_SIGNIFICANT" if net_mean > 0 else "NET_NOT_POSITIVE"


def _net_of_cost(t):
    if t.get("mean") is None:
        return dict(t, net_mean=None, net_ci=None)
    return dict(t, net_mean=t["mean"] - SPREAD_PIPS, net_ci=(t["ci"][0] - SPREAD_PIPS, t["ci"][1] - SPREAD_PIPS))


def _matched(a, b, keyf=stratum):
    r = w4._strat_mean_diff(_by(a, "net", keyf), _by(b, "net", keyf))
    r["n1"], r["n0"], r["n_min"] = len(a), len(b), min(len(a), len(b))
    r["matched_share"] = (r["n"] / len(a)) if (a and r.get("n")) else 0.0
    r["mean_a"] = float(np.mean(_vals(a, "net"))) if a else None
    r["mean_b"] = float(np.mean(_vals(b, "net"))) if b else None
    return r


def baseline_ret6(s1h, s5, dev_end):
    """Unconditional reference: 6h return in pips of a LONG entered at every clean hour boundary (a short is the exact negative)."""
    vals = []
    for k in range(1, s1h.n):
        T = s1h.index[k]
        if s1h.index[k - 1] + s1h.step != T or s1h.gap[k] > 0 or s1h.closure[k]:
            continue
        if T + pd.Timedelta(hours=HORIZON_H) >= dev_end:
            continue
        a = int(s5.index.searchsorted(T, side="left"))
        b = a + NB5 - 1
        if b >= s5.n or s5.index[a] != T or s5.gap[a + 1:b + 1].any() or s5.closure[a + 1:b + 1].any():
            continue
        vals.append((s5.c[b] - s5.o[a]) / PIP)
    v = np.asarray(vals, float)
    if len(v) < 60:
        return {"n": len(v), "mean_long_pips": None, "se_pips": None}
    sub = v[::HORIZON_H]
    return {"n": int(len(v)), "mean_long_pips": float(v.mean()), "se_pips": float(sub.std(ddof=1) / math.sqrt(len(sub)))}


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------
def analyze_groups(rec, acc, app, pool, months, extra=None):
    """All statistics from measured events (lists of dicts). Separated from measurement so it can be tested on planted data."""
    out = {"counts": {"reclaim": len(rec), "accept": len(acc), "approach": len(app), "pooled_sweeps": len(pool)}}
    thin = [m for m in rec if m["margin"] <= THIN_ATR]
    # ---- Layer 1
    out["S1"] = _matched(rec, app)
    out["S2"] = _matched(thin, app)
    out["S2"]["n_thin_reclaim"] = len(thin)
    out["S1_sensitivity"] = {"plus_session": _matched(rec, app, stratum_session), "plus_c1_range": _matched(rec, app, stratum_c1)}
    out["sd_net_atr"] = {"reclaim": float(np.std(_vals(rec, "net"), ddof=1)) if len(rec) > 2 else None,
                         "approach": float(np.std(_vals(app, "net"), ddof=1)) if len(app) > 2 else None}
    out["P"] = w4._mean_test(_vals(app, "net"))
    # ---- Layer 3 primary
    r1 = _net_of_cost(w4._mean_test(_vals(pool, "ret_pips")))
    out["R1"] = r1
    if r1.get("mean") is not None:
        r1["sd_pips"] = float(np.std(_vals(pool, "ret_pips"), ddof=1))
        r1["by_side"] = {sd: _net_of_cost(w4._mean_test([m["ret_pips"] for m in pool if m["side"] == sd])) for sd in ("high", "low")}
        r1["break_even_cost_pips"] = r1["mean"]
    # ---- Layer 2 / descriptive
    ref = {}
    for nm, ms in (("reclaim", rec), ("accept", acc), ("approach", app), ("pooled_sweeps", pool)):
        ref[nm] = {"NET_atr": w4._mean_test(_vals(ms, "net")), "RET_pips": _net_of_cost(w4._mean_test(_vals(ms, "ret_pips"))),
                   "RET_atr": w4._mean_test(_vals(ms, "ret_atr"))}
    out["reference"] = ref
    out["P_by_side"] = {sd: w4._mean_test([m["net"] for m in app if m["side"] == sd]) for sd in ("high", "low")}
    out["S1_by_side"] = {sd: _matched([m for m in rec if m["side"] == sd], [m for m in app if m["side"] == sd]) for sd in ("high", "low")}
    allt = sorted(m["t_known"] for m in rec + app + pool)
    if allt:
        tmid = allt[len(allt) // 2]
        h = {}
        for nm, sel in (("first_half", lambda m: m["t_known"] < tmid), ("second_half", lambda m: m["t_known"] >= tmid)):
            r_, a_, p_ = [m for m in rec if sel(m)], [m for m in app if sel(m)], [m for m in pool if sel(m)]
            h[nm] = {"S1": _matched(r_, a_), "R1": _net_of_cost(w4._mean_test(_vals(p_, "ret_pips"))), "P": w4._mean_test(_vals(a_, "net"))}
        out["halves"] = h
    stops = {}
    for k in STOP_ATRS:
        stops[str(k)] = {"net_pips": w4._mean_test([m["stops"][k] for m in pool]), "stopped_share": float(np.mean([m["stopped"][k] for m in pool])) if pool else None}
    out["stops"] = stops
    out["opportunity"] = {"months": months, "episodes_per_month": (len(pool) / months) if months else None,
                          "gross_pips_per_month": (len(pool) * r1["mean"] / months) if (months and r1.get("mean") is not None) else None,
                          "net_pips_per_month": (len(pool) * r1["net_mean"] / months) if (months and r1.get("mean") is not None) else None,
                          "hours_in_market_share": (len(pool) * HORIZON_H / (months * 30.44 * 24 * 5 / 7)) if months else None}
    bal = {}
    for k_ in ("dlev", "c2_range_atr", "c1_range_atr", "atr"):
        sc = 1e4 if k_ == "atr" else 1.0
        bal[k_ + ("_pips" if k_ == "atr" else "")] = {"reclaim": float(np.mean([m[k_] * sc for m in rec])) if rec else None,
                                                      "approach": float(np.mean([m[k_] * sc for m in app])) if app else None}
    out["balance"] = bal
    if extra:
        out.update(extra)
    return out


def missingness(included, excluded):
    """Counts and balance of what was dropped. Everything here is known at the event time (no outcome is used)."""
    out = {"by_reason": {}, "share_excluded": {}, "by_session": {}, "by_weekday": {}, "balance": {}, "flags": []}
    for g in ("reclaim", "accept", "approach"):
        inc = [m for m in included if m["group"] == g]
        exc = [m for m in excluded if m["group"] == g]
        out["by_reason"][g] = {}
        for m in exc:
            out["by_reason"][g][m["reason"]] = out["by_reason"][g].get(m["reason"], 0) + 1
        tot = len(inc) + len(exc)
        out["share_excluded"][g] = (len(exc) / tot) if tot else None
        for lab, fn, rng_ in (("by_session", lambda m: m["session"], range(4)), ("by_weekday", lambda m: m["t_known"].dayofweek, range(7))):
            out[lab][g] = {}
            for v in rng_:
                ni, ne = sum(1 for m in inc if fn(m) == v), sum(1 for m in exc if fn(m) == v)
                out[lab][g][str(v)] = {"n": ni + ne, "excluded": ne, "share": (ne / (ni + ne)) if (ni + ne) else None}
        if len(exc) >= 30 and len(inc) >= 30:
            for k_ in ("dlev", "c2_range_atr", "atr"):
                t = w4._welch([m[k_] for m in exc], [m[k_] for m in inc])
                out["balance"].setdefault(g, {})[k_] = {"excluded_mean": float(np.mean([m[k_] for m in exc])), "included_mean": float(np.mean([m[k_] for m in inc])), "z": t["z"]}
                if t["z"] is not None and abs(t["z"]) > 3:
                    out["flags"].append(f"{g}: excluded events differ from included ones in {k_} (z = {t['z']:+.1f})")
        for lab in ("by_session", "by_weekday"):
            sh = [(v, x["share"], x["n"]) for v, x in out[lab][g].items() if x["share"] is not None and x["n"] >= 30]
            if len(sh) >= 2 and out["share_excluded"][g]:
                top = max(sh, key=lambda t: t[1])
                if top[1] > 2.0 * out["share_excluded"][g] and top[1] > 0.10:
                    out["flags"].append(f"{g}: exclusions concentrate in {lab[3:]} {top[0]} ({top[1]:.0%} vs {out['share_excluded'][g]:.0%} overall)")
    return out


def analyze(s1h, s5, dev_end):
    sweeps = w4.sweep_signals(s1h)
    apps, amb = approach_signals(s1h)
    ev = make_events(s1h, sweeps, apps)
    raw_counts = {g: len(v) for g, v in ev.items()}
    dev = lambda evs: [x for x in evs if x["t_known"] < dev_end]
    sets = {"reclaim": w4.collapse(dev(ev["reclaim"])), "accept": w4.collapse(dev(ev["accept"])), "approach": w4.collapse(dev(ev["approach"])),
            "pooled": w4.collapse(dev(ev["reclaim"]) + dev(ev["accept"]))}
    done, excluded, recovered = {}, [], 0
    cache = {}
    for g, evs in sets.items():
        ms = []
        for x in evs:
            key = (x["group"], x["j"], x["side"])
            if key not in cache:
                m = measure6(x, s5)
                cache[key] = m
            m = cache[key]
            if not m["ok"]:
                if g != "pooled":
                    excluded.append(dict(x, reason=m["reason"]))
                continue
            if g != "pooled" and m["crt2_12h_gap"]:
                recovered += 1
            ms.append({**x, **m})
        done[g] = ms
    first = min((x["t_known"] for x in dev(ev["reclaim"]) + dev(ev["accept"]) + dev(ev["approach"])), default=None)
    months = ((dev_end - first).days / 30.44) if first is not None else 0.0
    out = analyze_groups(done["reclaim"], done["accept"], done["approach"], done["pooled"], months,
                         {"signals": raw_counts, "ambiguous_approach_skipped": amb, "episodes_before_exclusions": {g: len(v) for g, v in sets.items()}})
    inc_all = [m for g in ("reclaim", "accept", "approach") for m in done[g]]
    out["missingness"] = missingness(inc_all, excluded)
    out["missingness"]["recovered_vs_crt2_12h_rule"] = recovered
    out["baseline"] = baseline_ret6(s1h, s5, dev_end)
    return out


def apply_fdr(res, status):
    fam = {1: [("S1", "diff", res["S1"]["n_min"]), ("S2", "diff", res["S2"]["n_min"]), ("P", "mean", res["P"]["n"])], 2: [("R1", "mean", res["R1"]["n"])]}
    counts = {}
    for f, tests in fam.items():
        live = [(k, v, n) for k, v, n in tests if res[k].get("p") is not None]
        rej, q = w1.bh_fdr([res[k]["p"] for k, _, _ in live]) if live else ([], [])
        counts[f] = len(live)
        for (k, v, n), rj, qq in zip(live, rej, q):
            r = res[k]
            r["family"], r["fdr_reject"], r["q_value"] = f, rj, qq
            if f == 1:
                r["verdict"] = w4.verdict(r[v], r["ci"][0], r["ci"][1], rj, n, status, MIN_EFFECT_ATR)
            else:
                r["verdict"] = trade_verdict(r["net_mean"], r["net_ci"][0], r["net_ci"][1], n, status)
        for k, v, n in tests:
            if res[k].get("p") is None:
                res[k]["verdict"] = "DATA-LIMITED"
    return counts


def reading(res):
    """The pre-declared reading of S1 and P (see the module docstring)."""
    v1, vp = res["S1"].get("verdict", ""), res["P"].get("verdict", "")
    if v1.startswith("NO_INFORMATION"):
        s = "S1 = NO_INFORMATION: crossing the level adds nothing detectable beyond proximity. This CRT branch is KILLED; do not tune it."
    elif v1.startswith("INCONCLUSIVE") or v1.startswith("DATA-LIMITED"):
        s = "S1 = " + v1 + ": the data cannot decide whether crossing matters. Do not open the holdout; see the readiness gate for how much more data is needed."
    else:
        s = "S1 = " + v1 + ": on development data crossing the level adds information beyond proximity. It is a hypothesis for one future holdout test, not a result."
    ci = res["P"].get("ci")
    if ci and (ci[0] > 0 or ci[1] < 0) and not (v1.startswith(("SMALL_EFFECT", "EXPLORATORY", "DISCOVERY"))):
        s += " P's CI excludes 0 while S1 shows nothing: the drift toward the level is explained by proximity, not by the sweep."
    return s


def run_crt3(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    clean = {tf: (preloaded[tf] if preloaded else rc.clean_timeframe(tf, raw_dir, source)[0]) for tf in ("5m", "15m", "1h")}
    s1h, s5, s15 = rm.Series(clean["1h"], 60), rm.Series(clean["5m"], 5), rm.Series(clean["15m"], 15)
    rep15 = rc.clean_timeframe("15m", raw_dir, source)[2] if not preloaded else {
        "first": None, "last": None, "raw_lines_parsed": 0, "unique_timestamps": 0, "duplicate_lines": 0, "conflicting_duplicates": 0,
        "dropped_market_closed": 0, "dropped_off_grid": 0, "dropped_invalid_ohlc": 0, "kept_bars": len(clean["15m"]),
        "unexplained_gaps": 0, "missing_open_bars": 0}
    a6 = rg.gate_a6({"15m": clean["15m"]}, {"15m": rep15}, {"15m": s15})
    status = rg.gate_a7({"15m": clean["15m"]}, {"15m": s15}, a6)["details"]["status"]
    dev_end = rc.HOLDOUT_START_UTC - pd.Timedelta(hours=HORIZON_H + 1)
    res = analyze(s1h, s5, dev_end)
    n = apply_fdr(res, status)
    res["reading"] = reading(res)
    res.update({"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False, "n_tests": n})
    return res


# ---------------------------------------------------------------------------
# holdout readiness: counts EVENTS (structure flags only) + power from DEVELOPMENT estimates. No holdout outcome is read.
# ---------------------------------------------------------------------------
H_CRT3_DRAFT = {
    "status": "DRAFT: frozen only after the development report has been read",
    "H-S": "reclaim episodes show a larger NET toward the swept level than matched approach episodes (S1), one-sided, alpha 0.05",
    "H-R": "pooled sweep episodes: mean 6h return toward the level, net of the flat cost, is > 0 (R1), one-sided, alpha 0.05",
    "open_rule": "open the holdout ONCE per hypothesis, only when power at HALF the development effect is >= 0.80; INCONCLUSIVE counts as NOT CONFIRMED",
}


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _power(effect, se):
    return None if (se is None or se <= 0 or effect is None) else _phi(effect / se - 1.645)


def holdout_readiness(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None, dev_res=None):
    clean = {tf: (preloaded[tf] if preloaded else rc.clean_timeframe(tf, raw_dir, source)[0]) for tf in ("5m", "1h")}
    s1h, s5 = rm.Series(clean["1h"], 60), rm.Series(clean["5m"], 5)
    dev_end = rc.HOLDOUT_START_UTC - pd.Timedelta(hours=HORIZON_H + 1)
    if dev_res is None:
        dev_res = analyze(s1h, s5, dev_end)
    ev = make_events(s1h, w4.sweep_signals(s1h), approach_signals(s1h)[0])
    hold = lambda evs: [x for x in evs if x["t_known"] >= rc.HOLDOUT_START_UTC]
    sets = {"reclaim": w4.collapse(hold(ev["reclaim"])), "accept": w4.collapse(hold(ev["accept"])), "approach": w4.collapse(hold(ev["approach"])),
            "pooled": w4.collapse(hold(ev["reclaim"]) + hold(ev["accept"]))}
    n = {g: sum(1 for x in v if window_ok(x, s5)[0] is not None) for g, v in sets.items()}
    last = s5.index[-1]
    months = (last - rc.HOLDOUT_START_UTC).days / 30.44
    r1, s1 = dev_res["R1"], dev_res["S1"]
    out = {"draft_hypotheses": H_CRT3_DRAFT, "holdout_from": str(rc.HOLDOUT_START_UTC)[:10], "holdout_months": round(months, 2),
           "holdout_episodes_with_clean_window": n, "dev_estimates_used": {"R1_gross_pips": r1.get("mean"), "R1_sd_pips": r1.get("sd_pips"), "S1_diff_atr": s1.get("diff")}}

    def block(effect, se_now, months_now):
        if effect is None or se_now is None or effect <= 0:
            return {"effect": effect, "power_now": None, "additional_months": None, "note": "effect missing or not positive"}
        target_se = effect / (1.645 + 0.842)
        need = months_now * (se_now / target_se) ** 2 if months_now > 0 else None
        return {"effect": round(effect, 4), "power_now": None if _power(effect, se_now) is None else round(_power(effect, se_now), 2),
                "months_needed_for_80pct": None if need is None else round(need, 1),
                "additional_months": None if need is None else round(max(0.0, need - months_now), 1)}
    if r1.get("mean") is not None and n["pooled"] > 0:
        se_h = r1["sd_pips"] / math.sqrt(n["pooled"])
        out["R1"] = {"gross_full": block(r1["mean"], se_h, months), "gross_half": block(r1["mean"] / 2, se_h, months),
                     "net_full": block(r1["mean"] - SPREAD_PIPS, se_h, months), "net_half": block(r1["mean"] / 2 - SPREAD_PIPS, se_h, months)}
    if s1.get("diff") is not None and n["reclaim"] > 0 and n["approach"] > 0:
        sd_r, sd_a = dev_res["sd_net_atr"]["reclaim"], dev_res["sd_net_atr"]["approach"]
        if sd_r and sd_a:
            se_h = math.sqrt(sd_r ** 2 / n["reclaim"] + sd_a ** 2 / n["approach"])
            out["S1"] = {"full": block(abs(s1["diff"]), se_h, months), "half": block(abs(s1["diff"]) / 2, se_h, months)}
    gates = {}
    if "R1" in out:
        p = out["R1"]["gross_half"].get("power_now")
        gates["H-R (gross, half effect)"] = bool(p is not None and p >= 0.80)
    if "S1" in out:
        p = out["S1"]["half"].get("power_now")
        gates["H-S (half effect)"] = bool(p is not None and p >= 0.80)
    out["ready_to_open"] = gates
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
    return "n/a" if x is None else f"{x:.0%}"


def render_markdown(r):
    L = ["# Observatory — CRT-3: does crossing the level add anything beyond being near it?\n",
         f"Contract `{r['contract_hash']}` · dataset **{r['dataset_status']}** · holdout opened: **False** · family 1: {r['n_tests'][1]} tests · family 2: {r['n_tests'][2]} test\n",
         "**Development data only. Nothing here is a trading rule. A result describes WHAT PRICE DID; it does not show WHY.**\n",
         "NET = (MFE − MAE) / ATR(1h) over 6h **toward the swept level** (zero in a symmetric market). RET = 6h return toward the level in pips. "
         f"Net of cost = RET − {SPREAD_PIPS:g} pip flat per trade. Each line shows the 95% CI: the largest effect the data cannot rule out. "
         "Sweep groups were already seen in wave 4 / CRT-2; the APPROACH group is new.\n"]
    c = r["counts"]
    L.append(f"Episodes measured: reclaim {c['reclaim']}, accept {c['accept']}, approach {c['approach']}, pooled sweeps {c['pooled_sweeps']} (6h episodes per group) · "
             f"signals before collapse {r['signals']} · ambiguous approach candles skipped {r['ambiguous_approach_skipped']} · episodes before exclusions {r['episodes_before_exclusions']}\n")
    bl = r.get("baseline") or {}
    if bl.get("mean_long_pips") is not None:
        L.append(f"Drift reference (every clean hour boundary, no event): a LONG earns {_a(bl['mean_long_pips'], 2)} pips per 6h (s.e. {bl['se_pips']:.2f}).\n")
    L.append("## Reading (pre-declared, see the contract)\n")
    L.append(f"**{r['reading']}**\n")
    L.append("## Layer 1 — phenomenon (family 1)\n")
    L.append("| test | n (events / controls) | estimate | 95% CI | p | q | verdict |\n|---|---|---|---|---|---|---|")
    s1, s2, p_ = r["S1"], r["S2"], r["P"]
    L.append(f"| **S1** reclaim − approach, NET toward the level, matched on side × distance | {s1['n1']} / {s1['n0']} | {_a(s1.get('diff'))} | {_ci(s1.get('ci'))} | {_p(s1.get('p'))} | {_p(s1.get('q_value'))} | **{s1.get('verdict')}** |")
    L.append(f"| **S2** thin-margin reclaim (≤ {THIN_ATR} ATR) − approach, same matching | {s2['n1']} / {s2['n0']} | {_a(s2.get('diff'))} | {_ci(s2.get('ci'))} | {_p(s2.get('p'))} | {_p(s2.get('q_value'))} | **{s2.get('verdict')}** |")
    L.append(f"| **P** approach alone, NET toward the level vs 0 | {p_['n']} | {_a(p_.get('mean'))} | {_ci(p_.get('ci'))} | {_p(p_.get('p'))} | {_p(p_.get('q_value'))} | **{p_.get('verdict')}** |")
    L.append("")
    L.append(f"- S1 matched share {_pc(s1.get('matched_share'))} of reclaim episodes (S2: {_pc(s2.get('matched_share'))}); group means: reclaim {_a(s1.get('mean_a'))}, approach {_a(s1.get('mean_b'))}")
    sn = r["S1_sensitivity"]
    L.append("- S1 sensitivity (more matching, less data): " + " · ".join(f"{lab} {_a(v.get('diff'))} {_ci(v.get('ci'))} (matched {_pc(v.get('matched_share'))})" for lab, v in (("+ session", sn["plus_session"]), ("+ C1 range", sn["plus_c1_range"]))))
    L.append("- S1 by side: " + " · ".join(f"{sd} {_a(v.get('diff'))} {_ci(v.get('ci'))} (n {v['n1']}/{v['n0']})" for sd, v in r["S1_by_side"].items()))
    L.append("- P by side: " + " · ".join(f"{sd} {_a(v.get('mean'))} {_ci(v.get('ci'))} (n {v['n']})" for sd, v in r["P_by_side"].items()))
    L.append("\n## Layer 2 — magnitude and stability (descriptive)\n")
    L.append("| group | NET toward level (ATR) | RET toward level (pips, gross) | RET net of cost (pips) | n |\n|---|---|---|---|---|")
    for nm, v in r["reference"].items():
        rr = v["RET_pips"]
        L.append(f"| {nm} | {_a(v['NET_atr'].get('mean'))} {_ci(v['NET_atr'].get('ci'))} | {_a(rr.get('mean'), 2)} {_ci(rr.get('ci'), 2)} | {_a(rr.get('net_mean'), 2)} {_ci(rr.get('net_ci'), 2)} | {rr.get('n')} |")
    L.append("")
    for nm, h in r.get("halves", {}).items():
        L.append(f"- {nm}: S1 {_a(h['S1'].get('diff'))} {_ci(h['S1'].get('ci'))} (n {h['S1']['n1']}/{h['S1']['n0']}) · P {_a(h['P'].get('mean'))} · R1 gross pips {_a(h['R1'].get('mean'), 2)} {_ci(h['R1'].get('ci'), 2)} (n {h['R1'].get('n')})")
    b = r["balance"]
    L.append("- group balance (unmatched means, reclaim vs approach): " + " · ".join(f"{k}: {_a(v['reclaim'], 2)} vs {_a(v['approach'], 2)}" for k, v in b.items()))
    L.append("\n## Layer 3 — tradeability\n")
    R = r["R1"]
    L.append("| test | n | gross pips | 95% CI | net of cost | net 95% CI | p | verdict |\n|---|---|---|---|---|---|---|---|")
    L.append(f"| **R1** pooled sweeps, hold 6h toward the level | {R.get('n')} | {_a(R.get('mean'), 2)} | {_ci(R.get('ci'), 2)} | {_a(R.get('net_mean'), 2)} | {_ci(R.get('net_ci'), 2)} | {_p(R.get('p'))} | **{R.get('verdict')}** |")
    if R.get("mean") is not None:
        L.append(f"\n- the effect disappears at a cost of {_a(R['break_even_cost_pips'], 2)} pips per trade; per-trade SD {R['sd_pips']:.1f} pips")
        L.append("- by side: " + " · ".join(f"{sd} gross {_a(v.get('mean'), 2)} {_ci(v.get('ci'), 2)} (n {v['n']})" for sd, v in R["by_side"].items()))
    o = r["opportunity"]
    if o["episodes_per_month"] is not None:
        L.append(f"- opportunity (in-sample, not a forecast): {o['episodes_per_month']:.1f} episodes/month · gross {_a(o['gross_pips_per_month'], 1)} pips/month · net {_a(o['net_pips_per_month'], 1)} pips/month · "
                 f"{_pc(o['hours_in_market_share'])} of weekday hours in a trade")
    L.append("\nFixed stops (descriptive; entry at the next 5m open, time exit at 6h, stop fills at its price, cost deducted):\n")
    L.append("| stop | stopped out | net pips per trade | 95% CI |\n|---|---|---|---|")
    for k, v in r["stops"].items():
        t = v["net_pips"]
        L.append(f"| {k} ATR | {_pc(v['stopped_share'])} | {_a(t.get('mean'), 2)} | {_ci(t.get('ci'), 2)} |")
    m = r["missingness"]
    L.append("\n## Missingness audit\n")
    L.append(f"- excluded share by group: " + " · ".join(f"{g} {_pc(v)}" for g, v in m["share_excluded"].items()) + f" · reasons {m['by_reason']}")
    L.append(f"- events CRT-2's 12h gap rule would have dropped but the 6h rule keeps: {m['recovered_vs_crt2_12h_rule']}")
    L.append("- excluded share by session (reclaim): " + " · ".join(f"{SESSION_NAMES[int(k)]} {_pc(v['share'])} (n {v['n']})" for k, v in m["by_session"]["reclaim"].items()))
    L.append("- flags: " + ("; ".join(m["flags"]) if m["flags"] else "none"))
    L.append("\n## Not run\n- Holdout (sealed). Run `--readiness` to see the event counts and the power gate.\n- Other instruments, 4H, zones, any further filter: only after this question is answered.\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _h1_series(bars, gaps=None):
    s = w4._mk(bars, 60)
    return s


def selftest(null_series=10):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    from . import gates as gg
    # ---- approach events on hand-built candles. ATR is read back from the series, so thresholds are exact.
    pre = [(100, 101, 99, 100)] * 20

    def case(c1, c2, mod=None):
        s = w4._mk(pre + [c1, c2] + [(100, 101, 99, 100)] * 3, 60)
        if mod:
            mod(s)
        a, amb = approach_signals(s)
        sw = w4.sweep_signals(s)
        return s, [x for x in a if x["j"] == 21], [x for x in sw if x["j"] == 21], amb
    c1 = (100, 102, 99, 101)
    s0, _, _, _ = case(c1, (100, 101, 99.5, 100.5))
    atr = float(s0.atr[20])
    _, a, sw, _ = case(c1, (100, 102 - 0.1 * atr, 100.0, 100.5))
    chk("high approach within 0.25 ATR", [(x["side"], x["level"]) for x in a], [("high", 102.0)])
    chk("an approach is not a sweep", len(sw), 0)
    _, a, _, _ = case(c1, (100, 102 - 0.5 * atr, 100.0, 100.5))
    chk("too far from the level is not an approach", len(a), 0)
    _, a, sw, _ = case(c1, (100, 102.3, 99.5, 100.5))
    chk("crossing the level is a sweep, not an approach", (len(a), len(sw)), (0, 1))
    _, a, _, _ = case(c1, (100, 102.0, 100.0, 100.5))
    chk("touching exactly (no cross) counts as an approach", len(a), 1)
    _, a, _, _ = case((100, 100.2, 99.9, 100), (100, 100.1, 99.95, 100.0))
    s_tiny, a2, _, amb = case((100, 100.2, 99.9, 100), (100, 100.1, 99.95, 100.0))
    chk("both sides near -> ambiguous, skipped and counted", (len(a2), amb >= 1), (0, True))

    def gapit(s):
        s.gap[21] = 2
    _, a, _, _ = case(c1, (100, 102 - 0.1 * atr, 100.0, 100.5), gapit)
    chk("a missing candle before C2 removes the event", len(a), 0)

    def clos(s):
        s.closure[21] = True
    _, a, _, _ = case(c1, (100, 102 - 0.1 * atr, 100.0, 100.5), clos)
    chk("a closure before C2 removes the event", len(a), 0)
    _, a, _, _ = case(c1, (100, 101.0, 100.0, 100.5))
    chk("a low-side approach needs the low within 0.25 ATR", len(a), 0)
    lo_atr = atr
    _, a, _, _ = case(c1, (100, 101.0, 99 + 0.1 * lo_atr, 100.5))
    chk("low-side approach", [(x["side"], x["level"]) for x in a], [("low", 99.0)])
    # ---- direction toward the level
    s = w4._mk(pre + [c1, (101, 102.6, 100.8, 100.9), (102, 103.5, 101.5, 103.2), (100, 101.9, 99.5, 100.5)], 60)
    sw = {x["j"]: x for x in w4.sweep_signals(s)}
    ev = make_events(s, [sw[21], sw[22]], [])
    chk("reclaim of a high: level above the close -> toward = long", [(e["group"], e["dir"]) for e in ev["reclaim"]], [("reclaim", 1)])
    chk("acceptance beyond a high: level below the close -> toward = short", [(e["group"], e["dir"]) for e in ev["accept"]], [("accept", -1)])
    # ---- measurement on a hand-built 5m path. Prices ~1.30, ATR 10 pips.
    P0 = 1.3000

    def path(spec, n_after=72, tail=400):
        pre5 = [(P0, P0 + 0.0001, P0 - 0.0001, P0)] * 30
        bars = [(P0, P0 + 0.0001, P0 - 0.0001, P0)] * (n_after + 2)
        for k, b in spec.items():
            bars[k + 1] = b                                                  # k = bar number counted from the entry bar (0 = entry bar)
        return w4._mk(pre5 + bars + [(P0, P0 + 0.0001, P0 - 0.0001, P0)] * tail, 5)
    t_known = pd.Timestamp("2026-03-10", tz="UTC") + pd.Timedelta(minutes=5 * 31)
    evl = {"dir": 1, "atr": 0.0010, "t_known": t_known}
    # entry bar = index 31 (rel 0). MFE +20 pips at rel 9; MAE -15 pips at rel 19; last window bar = rel 71 closes +3 pips
    sp = {9: (P0, P0 + 0.0020, P0, P0 + 0.0018), 19: (P0, P0, P0 - 0.0015, P0 - 0.0010), 71: (P0, P0 + 0.0004, P0, P0 + 0.0003)}
    s5 = path(sp)
    m = measure6(evl, s5)
    chk("measure6 ok", m["ok"], True)
    chk("NET = (MFE - MAE)/ATR = (2.0 - 1.5)/1", round(m["net"], 6), 0.5)
    chk("RET in pips = close of the 72nd bar - entry = +3", round(m["ret_pips"], 4), 3.0)
    chk("stop 0.5 ATR (5 pips) was hit: loss 5 pips + 1 pip cost", round(m["stops"][0.5], 4), -6.0)
    chk("stop 1.0 ATR (10 pips) was hit", round(m["stops"][1.0], 4), -11.0)
    chk("stop 2.0 ATR (20 pips) not hit: time exit +3 - 1 cost", round(m["stops"][2.0], 4), 2.0)
    chk("stopped flags", m["stopped"], {0.5: True, 1.0: True, 2.0: False})
    ms = measure6(dict(evl, dir=-1), s5)
    chk("short mirror: NET = (1.5 - 2.0)/1", round(ms["net"], 6), -0.5)
    chk("short mirror: RET = -3", round(ms["ret_pips"], 4), -3.0)
    # nothing outside the window or before entry may matter
    sp2 = dict(sp)
    sp2[72] = (P0, P0 + 0.0100, P0 - 0.0100, P0 + 0.0090)                      # the bar AFTER the window
    s5b = path(sp2)
    mb = measure6(evl, s5b)
    chk("a bar after the 72nd cannot change the result", (round(mb["net"], 6), round(mb["ret_pips"], 4)), (0.5, 3.0))
    pre_alt = path(sp)
    pre_alt.h = pre_alt.h.copy()
    pre_alt.h[30] = P0 + 0.0500
    chk("the bar before entry cannot change the result", round(measure6(evl, pre_alt)["net"], 6), 0.5)
    chk("no_trigger_bar when t_known is off the grid", measure6(dict(evl, t_known=t_known + pd.Timedelta(minutes=2)), s5)["reason"], "no_trigger_bar")
    sg = path(sp)
    sg.gap[50] = 3
    chk("missing 5m candle inside the window excludes", measure6(evl, sg)["reason"], "data_gap")
    sg2 = path(sp)
    sg2.gap[31 + 75] = 3
    m2 = measure6(evl, sg2)
    chk("a gap AFTER the 6h window does not exclude (CRT-2's 12h rule would)", (m2["ok"], m2["crt2_12h_gap"]), (True, True))
    sc = path(sp)
    sc.closure[60] = True
    chk("weekend closure inside the window excludes", measure6(evl, sc)["reason"], "closure_in_window")
    short = w4._mk([(P0, P0 + 0.0001, P0 - 0.0001, P0)] * 80, 5)
    chk("end_of_data when the window does not fit", measure6(evl, short)["reason"], "end_of_data")
    # ---- events carry no future information: truncating or altering the future leaves earlier events unchanged
    rng = np.random.default_rng(11)
    d5 = gg._random_walk_series(30000, rng, step=5).df
    h1 = d5[["open", "high", "low", "close"]].resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    h1["gap_before_missing"], h1["closure_before"] = 0, False
    S = rm.Series(h1, 60)
    full_a, _ = approach_signals(S)
    cut = 1500
    part_a, _ = approach_signals(rm.Series(h1.iloc[:cut], 60))
    key = lambda L: [(x["j"], x["side"], round(x["level"], 8)) for x in L if x["j"] < cut]
    chk("approach events unchanged when the future is removed", key(full_a), key(part_a))
    alt = h1.copy()
    alt.iloc[cut:, alt.columns.get_loc("high")] *= 1.2
    alt_a, _ = approach_signals(rm.Series(alt, 60))
    chk("approach events unchanged when the future is altered", key(full_a), key(alt_a))
    chk("random-walk 1h series produces approaches and sweeps", (len(full_a) > 100, len(w4.sweep_signals(S)) > 100), (True, True))

    # ---- statistics on planted data (no market involved)
    def fake(grp, net, k, side, dl, sess, margin=0.1, ret=0.0):
        return {"group": grp, "net": net, "ret_pips": ret, "ret_atr": ret / 10, "side": side, "dlev": dl, "session": sess, "margin": margin,
                "t_known": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(hours=7 * k), "atr": 0.001, "c2_range_atr": 1.0, "c1_range_atr": 1.0,
                "stops": {a: ret - 1.0 for a in STOP_ATRS}, "stopped": {a: False for a in STOP_ATRS}}
    rr = np.random.default_rng(3)

    def build(shift, n_=1500):
        rec_ = [fake("reclaim", float(rr.normal(shift, 2.4)), i, ("high", "low")[i % 2], float(rr.uniform(0.05, 1.5)), int(rr.integers(0, 4)), float(rr.uniform(0.01, 0.6))) for i in range(n_)]
        app_ = [fake("approach", float(rr.normal(0.0, 2.4)), i, ("high", "low")[i % 2], float(rr.uniform(0.05, 1.5)), int(rr.integers(0, 4)), 0.1) for i in range(n_)]
        return rec_, app_
    rec_, app_ = build(0.35)
    res = analyze_groups(rec_, [], app_, rec_, 12.0)
    st = apply_fdr(res, "PRIMARY_READY")
    chk("planted +0.35 ATR crossing effect is detected by S1", (res["S1"]["z"] > 3.0, res["S1"]["verdict"] in ("DISCOVERY_PASS", "EXPLORATORY_SIGNAL")), (True, True))
    chk("planted effect: approach alone shows nothing", abs(res["P"]["z"]) < 3.0, True)
    chk("S1 matched share is high on planted data", res["S1"]["matched_share"] > 0.9, True)
    rec0, app0 = build(0.0)
    res0 = analyze_groups(rec0, [], app0, rec0, 12.0)
    apply_fdr(res0, "PRIMARY_READY")
    chk("no planted effect: S1 not significant", abs(res0["S1"]["z"]) < 2.6, True)
    chk("S1 is NO_INFORMATION or INCONCLUSIVE with no effect", res0["S1"]["verdict"] in ("NO_INFORMATION", "INCONCLUSIVE"), True)
    chk("the reading is a kill when S1 is NO_INFORMATION", reading(dict(res0, S1=dict(res0["S1"], verdict="NO_INFORMATION"))).startswith("S1 = NO_INFORMATION"), True)
    # cost handling: gross +1.6 pips with a 1 pip cost leaves +0.6; CI shifts by exactly the cost
    t = _net_of_cost({"mean": 1.6, "ci": (0.4, 2.8), "n": 100})
    chk("net of cost shifts mean and CI by the flat cost", (round(t["net_mean"], 6), tuple(round(x, 6) for x in t["net_ci"])), (0.6, (-0.6, 1.8)))
    chk("trade verdict: CI includes 0 -> not significant", trade_verdict(0.6, -0.6, 1.8, 500, "PRIMARY_READY"), "NET_POSITIVE_NOT_SIGNIFICANT")
    chk("trade verdict: CI above 0", trade_verdict(0.6, 0.1, 1.1, 500, "PRIMARY_READY").startswith("NET_POSITIVE_CI_EXCLUDES_0"), True)
    chk("trade verdict: CI below 0", trade_verdict(-1.0, -2.0, -0.1, 500, "PRIMARY_READY"), "NET_NEGATIVE")
    # missingness flags a concentrated exclusion
    inc = [dict(fake("reclaim", 0, i, "high", 0.5, i % 4), group="reclaim") for i in range(400)]
    exc = [dict(fake("reclaim", 0, i, "high", 0.5, 0), group="reclaim", reason="data_gap") for i in range(120)]
    mm = missingness(inc, exc)
    chk("missingness flags exclusions concentrated in one session", any("session" in f for f in mm["flags"]), True)
    # power helper
    chk("power of an effect equal to 2.486 se is 0.80", round(_power(2.486, 1.0), 2), 0.80)
    # ---- random-walk null: every test should be about nominal
    zs = {k: [] for k in ("S1", "S2", "P", "R1")}
    used = 0
    for _ in range(null_series):
        d5_ = gg._random_walk_series(45000, rng, step=5).df
        h1_ = d5_[["open", "high", "low", "close"]].resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        h1_["gap_before_missing"], h1_["closure_before"] = 0, False
        r_ = analyze(rm.Series(h1_, 60), rm.Series(d5_, 5), d5_.index[-1] + pd.Timedelta(days=1))
        used += 1
        for k in zs:
            z = r_[k].get("z")
            if z is not None:
                zs[k].append(z)
    allz = np.asarray([z for v in zs.values() for z in v], float)
    chk(f"null: enough tests ({len(allz)})", len(allz) >= 20, True)
    if len(allz) >= 20:
        chk(f"null: |mean z| < 0.5 (got {allz.mean():+.2f})", abs(allz.mean()) < 0.5, True)
        chk(f"null: sd(z) in [0.6, 1.6] (got {allz.std():.2f})", 0.6 <= allz.std() <= 1.6, True)
        chk(f"null: share |z|>1.96 <= 15% (got {(np.abs(allz) > 1.96).mean():.0%})", (np.abs(allz) > 1.96).mean() <= 0.15, True)
    for k in ("S1", "P", "R1"):
        chk(f"null {k}: usable in most series ({len(zs[k])}/{used})", len(zs[k]) >= used * 0.7, True)
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": used, **{f"{k}_mean_z": round(float(np.mean(v)), 3) if v else None for k, v in zs.items()},
                     **{f"{k}_sd_z": round(float(np.std(v)), 3) if v else None for k, v in zs.items()}}}


def main():
    ap = argparse.ArgumentParser(description="Observatory CRT-3. Read-only on raw data; the holdout stays sealed.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--readiness", action="store_true", help="count holdout EVENTS and compute power from development estimates; no holdout outcome is read")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    if a.readiness:
        print(json.dumps(holdout_readiness(a.raw_dir, a.source), indent=2, default=str))
        return
    res = run_crt3(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "crt3_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "crt3_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
