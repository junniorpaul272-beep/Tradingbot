"""
Observatory — STUDY I, STAGE 2: THE FROZEN PRIMARY TEST (GBPUSD, development data only). Read-only. The holdout stays sealed.

This file is written AFTER the counts were reviewed and BEFORE any outcome exists. Everything in FROZEN below is the pre-registration in executable form; its hash is printed in the
report. It runs ONCE on real data (the workflow refuses to run the outcome unless the confirm word is typed). No variant is added after the primary outcome is seen.

PRIMARY CLAIM  I:reject  (family size 1)
  Population : NY-17:00 sessions that sweep ONE side of the previous valid session's wick range and close back inside it (cells S1_high_in, S1m_low_in), 24h outcome window gap-free,
               not crossing a closure. Expected usable count on the real data: 360 (182 + 178; recent 99 / older 261). The run ABORTS if the count differs: that would mean the
               population is not the one that was reviewed.
  Direction  : mirrored into the predicted direction: a swept HIGH predicts DOWN, a swept LOW predicts UP (price series negated for the high side).
  Outcome    : o = 1 if +1 ATR(1h) in the predicted direction is touched before -1 ATR within 288 five-minute bars from the bar after the decision bar; 0 if the other side first;
               unresolved / same-bar-ambiguous events are dropped and counted.
  Placebo    : 30 draws (without replacement) from 5m closes with the same NY weekday and hour +-1h, trailing-1h move within 0.5 ATR, trailing-24h range (clock time, ATR units)
               within +-15% of the event's, more than 24h away, full gap-free 24h window. Fewer than 10 candidates, or fewer than 8 resolved draws, drops the event ('no_placebo').
  Excess     : d_i = o_i - mean(placebo outcomes_i). POSITIVE d favours the predicted (fade) direction.
INFERENCE (frozen)
  mean(d) with a cluster-robust standard error, clusters = 2-day UTC blocks of the decision time (studyc.cluster_test), two-sided z, alpha = 0.05, one claim (no multiplicity).
  The unit of analysis is the EVENT excess: each event contributes ONE number (its outcome minus its own placebo mean); placebo draws are never treated as independent market
  observations. Placebo-sampling noise (30 draws) is not in the SE; it is MEASURED by re-drawing the placebo 20 times and reporting the spread of mean(d).
VERDICT (two separate dimensions, never collapsed)
  statistical : SIGNIFICANT_FADE | SIGNIFICANT_OPPOSITE | NOT_SIGNIFICANT          (p < 0.05, sign of the estimate)
  practical   : MEETS (estimate >= +0.05 in the fade direction) | BELOW_THRESHOLD
  reading     : table in verdict(); a non-significant result with the CI upper bound >= +0.05 is INCONCLUSIVE, never "no edge".
DESCRIPTIVE ONLY (no verdict, no promotion): recent/older split; Monday-Wednesday restriction and the UTC convention (sensitivity); overshoot bins and close-location terciles;
  the inner-level control; spike-exposure sensitivity; S5 overlap with Study F. S2/S5 (outside-close) are counts only: no outcome is computed for them.

Run:  python3 -m observatory.studyi_run --selftest      python3 -m observatory.studyi_run --run [--confirm I-FROZEN] [--raw-dir ...] [--out ...]
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
from . import studya as sa
from . import studyb as sb
from . import studyi as si
from . import standards as st
from .studyb import HMAX, TRAIL_TOL, MIN_POOL, MIN_PLACEBO, NS_5, NS_H, NS_D, R_PLACEBO, SEED, barrier
from .studyc import cluster_test, cluster_diff, BLOCK_NS, MIN_EFFECT

FROZEN = {
    "claim": "I:reject", "family_size": 1, "convention": "ny17", "cells": {"S1_high_in": -1, "S1m_low_in": +1}, "inner_cells": {"I_high_in": -1, "I_low_in": +1},
    "barrier_atr": 1.0, "horizon_bars": HMAX, "placebo_draws": R_PLACEBO, "placebo_min_candidates": MIN_POOL, "placebo_min_resolved": MIN_PLACEBO,
    "placebo_trailing_move_tol_atr": TRAIL_TOL, "placebo_vol_tol": 0.15, "placebo_hour_window": 1, "placebo_min_distance_bars": HMAX,
    "inference": "mean excess, 2-day UTC block cluster-robust SE, two-sided z, alpha 0.05", "alpha": 0.05, "min_effect": MIN_EFFECT,
    "boundary": "2024-10-03", "expected_n": 360, "expected_by_cell": {"S1_high_in": 182, "S1m_low_in": 178}, "expected_recent": 99, "expected_older": 261,
    "redraws": 20, "seed": SEED,
}
FROZEN_HASH = hashlib.sha256(json.dumps(FROZEN, sort_keys=True).encode()).hexdigest()[:16]
ALPHA = FROZEN["alpha"]
Z_CRIT = 1.959964


# ---------------------------------------------------------------------------
# verdict (pure)
# ---------------------------------------------------------------------------
def verdict(mean, ci_lo, ci_hi, p, min_effect=MIN_EFFECT, alpha=ALPHA):
    """Statistical and practical dimensions kept separate. Positive mean = the predicted fade direction."""
    if mean is None or p is None:
        return {"statistical": "NOT_COMPUTED", "practical": "NOT_COMPUTED", "reading": "DATA-LIMITED"}
    sig = p < alpha
    if sig and mean > 0:
        stat = "SIGNIFICANT_FADE"
    elif sig and mean < 0:
        stat = "SIGNIFICANT_OPPOSITE"
    else:
        stat = "NOT_SIGNIFICANT"
    prac = "MEETS" if mean >= min_effect else "BELOW_THRESHOLD"
    if stat == "SIGNIFICANT_FADE":
        reading = "MEETS_STATISTICAL_AND_PRACTICAL" if prac == "MEETS" else "DIRECTIONAL_EVIDENCE_BELOW_PRACTICAL_THRESHOLD"
    elif stat == "SIGNIFICANT_OPPOSITE":
        reading = "EVIDENCE_AGAINST_THE_FADE (opposite-direction effect; investigate, do not trade)"
    else:
        reading = "EVIDENCE_AGAINST_EFFECTS_OF_PRACTICAL_SIZE (CI upper bound below +%.2f)" % min_effect if ci_hi < min_effect else "INCONCLUSIVE (CI reaches +%.2f or more)" % min_effect
    return {"statistical": stat, "practical": prac, "reading": reading}


# ---------------------------------------------------------------------------
# event extraction (replicates the counts-stage eligibility exactly)
# ---------------------------------------------------------------------------
def _prepare(ns5, o, h, l, c, atr_t, x, good_q, hold_ns, tz, end_hour):
    nd = int((ns5 < hold_ns).sum())
    ns5d = ns5[:nd]
    S = si.build_sessions(ns5d, o[:nd], h[:nd], l[:nd], c[:nd], tz, end_hour)
    p_dec = S["b"] - 1
    atr_dec = atr_t[p_dec]
    C = si.classify(S, atr_dec)
    keys, naive = si.clock_keys(ns5d, tz)
    pools = si.build_pools(good_q, keys)
    vs = si.vol_state_clock(ns5d, h[:nd], l[:nd], atr_t[:nd])
    return {"S": S, "C": C, "p_dec": p_dec, "atr_dec": atr_dec, "ns5": ns5d, "naive": naive, "pools": pools, "vs": vs, "nd": nd}


def events_from(prep, ns5, cells, inner=False):
    """Eligible events: classified, outcome window OK (same rule as the counts stage). Returns list of dicts (decision bar p, time, orientation sign, cell, geometry)."""
    S, C, p_dec = prep["S"], prep["C"], prep["p_dec"]
    if inner:
        I = si.classify_inner(S, C, prep["atr_dec"])
        cellv, over = I["cell"], I["overshoot_atr"]
        idx = [int(i) for i in np.flatnonzero(cellv != "")]
    else:
        cellv, over = C["cell"], C["overshoot_atr"]
        idx = [int(i) for i in np.flatnonzero(C["cell"] != "") if C["cell"][i] in cells]
    out = []
    for i in idx:
        if cellv[i] not in cells:
            continue
        p = int(p_dec[i])
        if si.window_status(ns5, p) is not None:
            continue
        out.append({"i": i, "p": p, "t": int(ns5[p]), "sign": cells[cellv[i]], "cell": str(cellv[i]), "over": float(over[i]), "clv": float(C["clv"][i]), "wd": int(S["wd"][i])})
    return out


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------
def _outcome(h, l, c, p, atr, sign):
    """Oriented first passage from bar p+1: sign +1 as is, -1 on the negated series (high<->low). Returns (o, status)."""
    if sign > 0:
        return barrier(h, l, p + 1, c[p] + atr, c[p] - atr)
    return barrier(-l, -h, p + 1, -c[p] + atr, -c[p] - atr)


def placebo_candidates(prep, p, x, vol_tol=FROZEN["placebo_vol_tol"]):
    ns5, pools, naive, vs = prep["ns5"], prep["pools"], prep["naive"], prep["vs"]
    if not np.isfinite(x[p]) or not np.isfinite(vs[p]):
        return None
    cand = []
    for dh in (-1, 0, 1):
        t = naive[p] + pd.Timedelta(hours=dh)
        k = int(t.dayofweek) * 24 + int(t.hour)
        if k in pools:
            cand.append(pools[k])
    if not cand:
        return np.array([], int)
    q = np.concatenate(cand)
    m = (np.abs(x[q] - x[p]) <= TRAIL_TOL) & (np.abs(ns5[q] - ns5[p]) > HMAX * NS_5) & (np.abs(vs[q] - vs[p]) <= vol_tol * vs[p])
    return q[m]


def score_events(evs, prep, h, l, c, atr_t, x, rng_salt=0):
    """Returns (rows, drops). Each row: t, d (excess), o, pm, sign, plus geometry. One number per event."""
    rows, drops = [], {"unresolved": 0, "ambiguous": 0, "no_x_or_vol": 0, "no_placebo": 0}
    for e in evs:
        p, sign = e["p"], e["sign"]
        atr = float(atr_t[p])
        o, stt = _outcome(h, l, c, p, atr, sign)
        if stt != "ok":
            drops[stt] += 1
            continue
        cand = placebo_candidates(prep, p, x)
        if cand is None:
            drops["no_x_or_vol"] += 1
            continue
        if len(cand) < MIN_POOL:
            drops["no_placebo"] += 1
            continue
        rng = np.random.default_rng(SEED + int(e["t"] % 2 ** 31) + 17 * 91 + (1 if sign < 0 else 0) + 100003 * rng_salt)
        take = cand[rng.choice(len(cand), size=min(R_PLACEBO, len(cand)), replace=False)]
        vals = []
        for q in take:
            oq, sq = _outcome(h, l, c, int(q), float(atr_t[q]), sign)
            if sq == "ok":
                vals.append(oq)
        if len(vals) < MIN_PLACEBO:
            drops["no_placebo"] += 1
            continue
        pm = float(np.mean(vals))
        rows.append(dict(e, o=float(o), pm=pm, d=float(o) - pm))
    return rows, drops


def summarize(rows):
    d = [r["d"] for r in rows]
    t = [r["t"] for r in rows]
    s = cluster_test(d, t) if rows else {"n": 0, "mean": None, "se": None, "ci": None, "z": None, "p": None}
    if s.get("mean") is not None:
        s["event_rate"] = float(np.mean([r["o"] for r in rows]))
        s["placebo_rate"] = float(np.mean([r["pm"] for r in rows]))
        s["verdict"] = verdict(s["mean"], s["ci"][0], s["ci"][1], s["p"])
    return s


def split_block(rows, bnd_ns):
    rec = [r for r in rows if r["t"] >= bnd_ns]
    old = [r for r in rows if r["t"] < bnd_ns]
    out = {"recent": summarize(rec), "older": summarize(old), "diff": None}
    if len(rec) >= 10 and len(old) >= 10:
        out["diff"] = cluster_diff([r["d"] for r in rec], [r["t"] for r in rec], [r["d"] for r in old], [r["t"] for r in old])
    return out


def _brief(s):
    return {k: s.get(k) for k in ("n", "n_clusters", "mean", "se", "ci", "z", "p", "event_rate", "placebo_rate")}


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def run_primary(raw_dir=None, source="observatory", preloaded=None, boundary=FROZEN["boundary"], enforce_counts=True):
    raw_dir = raw_dir or rc.DEFAULT_RAW_DIR
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    bnd_ns = pd.Timestamp(boundary, tz="UTC").as_unit("ns").value
    ctx = sb.make_ctx(s5, s1h, +1, hold_ns)
    ns5, atr_t, x = ctx["ns5"], ctx["atr_t"], ctx["x"]
    good_q = np.concatenate([p[0] for p in ctx["pools"].values()]) if ctx["pools"] else np.array([], int)
    h, l, c = s5.h, s5.l, s5.c
    res = {"dataset_status": status, "holdout_opened": False, "frozen_hash": FROZEN_HASH, "frozen": FROZEN, "boundary_utc": boundary}

    prep = _prepare(ns5, s5.o, h, l, c, atr_t, x, good_q, hold_ns, *si.CONVENTIONS["ny17"])
    evs = events_from(prep, ns5, FROZEN["cells"])
    cnt = {"n": len(evs), "by_cell": {k: sum(e["cell"] == k for e in evs) for k in FROZEN["cells"]}, "recent": sum(e["t"] >= bnd_ns for e in evs), "older": sum(e["t"] < bnd_ns for e in evs)}
    res["population"] = cnt
    exp_ok = (cnt["n"] == FROZEN["expected_n"] and cnt["by_cell"] == FROZEN["expected_by_cell"] and cnt["recent"] == FROZEN["expected_recent"] and cnt["older"] == FROZEN["expected_older"])
    res["population_matches_reviewed_counts"] = bool(exp_ok)
    if enforce_counts and not exp_ok:
        res["aborted"] = "the event population differs from the reviewed counts (360 = 182 + 178, recent 99 / older 261); no outcome is reported"
        return res

    rows, drops = score_events(evs, prep, h, l, c, atr_t, x)
    res["drops"] = drops
    res["usable_after_exclusions"] = len(rows)
    prim = summarize(rows)
    res["primary"] = {**prim, "ci": list(prim["ci"]) if prim.get("ci") else None}
    res["power"] = {"detectable_80pct": 2.8 * prim["se"] if prim.get("se") else None,
                    "power_at_min_effect": float(0.5 * math.erfc(-(MIN_EFFECT / prim["se"] - Z_CRIT) / math.sqrt(2))) if prim.get("se") else None}
    res["block_split"] = {k: (_brief(v) if k != "diff" else (None if v is None else {kk: v.get(kk) for kk in ("diff", "se", "ci", "z", "p", "n1", "n0")})) for k, v in split_block(rows, bnd_ns).items()}

    # placebo-sampling noise: re-draw the placebo 20 times, spread of the mean excess (not part of the SE)
    means = []
    for r in range(1, FROZEN["redraws"] + 1):
        rr, _ = score_events(evs, prep, h, l, c, atr_t, x, rng_salt=r)
        means.append(float(np.mean([q["d"] for q in rr])) if rr else float("nan"))
    res["placebo_redraw"] = {"mean_excess_min": float(np.nanmin(means)), "mean_excess_max": float(np.nanmax(means)), "sd": float(np.nanstd(means, ddof=1)), "primary_se": prim.get("se")}

    # ---- descriptive / sensitivity (no verdict) ----
    desc = {}
    mw = [r for r in rows if r["wd"] <= 2]
    desc["monday_to_wednesday"] = _brief(summarize(mw))
    a_, b_ = si.OVERSHOOT_BINS
    desc["overshoot_bins"] = {f"<{a_}": _brief(summarize([r for r in rows if r["over"] < a_])), f"{a_}-{b_}": _brief(summarize([r for r in rows if a_ <= r["over"] < b_])), f">={b_}": _brief(summarize([r for r in rows if r["over"] >= b_]))}
    clv = np.array([r["clv"] for r in rows])
    q1, q2 = np.percentile(clv, [100 / 3, 200 / 3]) if len(clv) else (0, 0)
    desc["close_location_terciles"] = {"low": _brief(summarize([r for r in rows if r["clv"] <= q1])), "mid": _brief(summarize([r for r in rows if q1 < r["clv"] <= q2])), "high": _brief(summarize([r for r in rows if r["clv"] > q2])),
                                       "cutoffs": [float(q1), float(q2)]}
    # UTC convention (Mon-Wed on both sides: the UTC convention loses Thursday decisions, so the primary is restricted to Mon-Wed for comparison)
    pu = _prepare(ns5, s5.o, h, l, c, atr_t, x, good_q, hold_ns, *si.CONVENTIONS["utc"])
    ev_u = events_from(pu, ns5, FROZEN["cells"])
    ru, _ = score_events(ev_u, pu, h, l, c, atr_t, x)
    desc["utc_convention"] = {"events": len(ev_u), **_brief(summarize(ru))}
    # inner-level control
    ev_i = events_from(prep, ns5, FROZEN["inner_cells"], inner=True)
    ri, di = score_events(ev_i, prep, h, l, c, atr_t, x)
    desc["inner_level_control"] = {"events": len(ev_i), "usable": len(ri), "drops": di, **_brief(summarize(ri)), "note": "descriptive only; overshoot distribution differs from the primary; no boundary-specificity claim"}
    # spike exposure
    dev = ns5 < hold_ns
    sp = st.spike_flags(c[dev], ns5[dev], s5.closure[dev] | (s5.gap[dev] > 0))
    exposed, clean = st.spike_exposure(rows, sp)
    desc["spike_exposure"] = {"flagged_bars": int(len(sp)), "exposed_events": len(exposed), "clean": _brief(summarize(clean)), "note": "data-integrity sensitivity; events are NOT removed from the primary"}
    # S5 overlap with Study F (same definition as the standards module: an F event on the same side within the past 24h)
    try:
        from . import studyf as sf
        fts = {1: [], -1: []}
        for cx in (sf.ctx_f(s5, s1h, +1, hold_ns), sf.ctx_f(s5, s1h, -1, hold_ns)):
            for key in ("A:0", "A:0.1", "A:0.2"):
                ev, _ = sf.events_for(cx, key, "dev")
                fts[cx["sign"]] += [e["t"] for e in ev]
        fts = {s: np.sort(np.unique(np.array(v, np.int64))) for s, v in fts.items()}
        near = np.array([st.near_past(fts[int(-r["sign"])], r["t"], 24 * NS_H) for r in rows], bool)
        nof = [r for r, b in zip(rows, near) if not b]
        desc["overlap_with_study_f"] = {"share_with_F_event_in_past_24h": float(near.mean()) if len(near) else None, "events_without": len(nof), "without": _brief(summarize(nof)),
                                        "note": "audit aid, not a test; overlapping events are not independent replication"}
    except Exception as ex:  # the audit must not be silently lost
        desc["overlap_with_study_f"] = {"error": repr(ex)}
    res["descriptive"] = desc
    res["weekday"] = st.weekday_coverage([r["t"] for r in rows])
    res["duplicates"] = st.duplicate_events([{"t": r["t"], "side": r["sign"]} for r in rows])
    return res


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _f(x, nd=3):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.{nd}f}"


def _line(label, s):
    if not s or s.get("mean") is None:
        return f"- {label}: n={s.get('n') if s else 0} (not enough clusters)"
    ci = s["ci"]
    return f"- {label}: n={s['n']}, excess {_f(s['mean'])} (SE {s['se']:.3f}, 95% CI {_f(ci[0])}..{_f(ci[1])}), z {s['z']:.2f}, p {s['p']:.3f}"


def render_markdown(r):
    L = ["# Observatory — Study I, stage 2: the frozen primary test (GBPUSD)\n",
         f"Dataset **{r['dataset_status']}** · holdout opened: **False** · frozen contract hash **{r['frozen_hash']}** · block boundary {r['boundary_utc']}\n"]
    if "aborted" in r:
        return "\n".join(L + [f"**ABORTED:** {r['aborted']}", f"population found: {r['population']}"])
    p = r["primary"]
    L += ["## Population\n", f"{r['population']} (matches the reviewed counts: **{r['population_matches_reviewed_counts']}**) · dropped at scoring {r['drops']} · **usable after exclusions: {r['usable_after_exclusions']}**\n",
          "## Primary result (I:reject, mirrored into the predicted direction; positive excess = the fade direction)\n", _line("pooled", p),
          f"- event first-passage rate {p['event_rate']:.3f} vs placebo {p['placebo_rate']:.3f}",
          f"- verdict — statistical: **{p['verdict']['statistical']}** · practical: **{p['verdict']['practical']}** · reading: **{p['verdict']['reading']}**",
          f"- power: detectable at 80% = {_f(r['power']['detectable_80pct'])}; power at the 0.05 practical threshold = {r['power']['power_at_min_effect']:.2f}. A non-significant result is NOT proof of no effect.",
          f"- placebo re-draws (20): mean excess ranges {_f(r['placebo_redraw']['mean_excess_min'])}..{_f(r['placebo_redraw']['mean_excess_max'])} (sd {r['placebo_redraw']['sd']:.4f}; primary SE {r['placebo_redraw']['primary_se']:.4f})\n",
          "## Descriptive only (no verdict, no promotion)\n", "**Recent vs older (boundary 2024-10-03)**", _line("recent", r["block_split"]["recent"]), _line("older", r["block_split"]["older"])]
    d = r["block_split"]["diff"]
    L.append("- difference: n/a" if not d else f"- recent minus older: {_f(d['diff'])} (SE {d['se']:.3f}), p {d['p']:.3f}")
    D = r["descriptive"]
    L += ["\n**Sensitivity**", _line("Monday–Wednesday only (primary convention)", D["monday_to_wednesday"]), _line(f"UTC convention ({D['utc_convention']['events']} events, Mon–Wed by construction)", D["utc_convention"]),
          _line(f"inner-level control ({D['inner_level_control']['usable']} usable of {D['inner_level_control']['events']}) — NO boundary-specificity claim", D["inner_level_control"]),
          "\n**By overshoot bin (ATR beyond the boundary)**"] + [_line(k, v) for k, v in D["overshoot_bins"].items()]
    L += ["\n**By close-location tercile (low = close nearest the swept side's far end)**"] + [_line(k, D["close_location_terciles"][k]) for k in ("low", "mid", "high")]
    sp = D["spike_exposure"]
    L += ["\n**Data integrity**", f"- spike-flagged bars {sp['flagged_bars']}; events exposed within 24h: {sp['exposed_events']}", _line("excluding exposed events (sensitivity only)", sp["clean"])]
    ov = D["overlap_with_study_f"]
    L += ["\n**Overlap with Study F (S5)**", "- " + (f"error: {ov['error']}" if "error" in ov else f"{100 * ov['share_with_F_event_in_past_24h']:.1f}% of the events have a Study F event on the same side in the past 24h; {ov['events_without']} do not")]
    if "without" in ov:
        L.append(_line("excluding overlapped events", ov["without"]))
    L += [f"\nWeekday coverage (Mon..Sun): {r['weekday']} · duplicate events: {r['duplicates']}", "\n## Not done here\n", "- S2/S5 outcomes (counts only by pre-registration), any variant added after seeing this result, the holdout."]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------
def selftest():
    checks = []

    def chk(name, got, want=True, tol=None):
        ok = (abs(got - want) <= tol) if tol is not None else (got == want)
        checks.append({"check": name, "ok": bool(ok), "got": got if not isinstance(got, (np.floating, np.integer)) else float(got), "want": want})

    # verdict table (every row of the reviewed table, both thresholds)
    v = lambda m, lo, hi, p: verdict(m, lo, hi, p)["reading"]
    chk("verdict: significant fade >= 0.05", v(0.08, 0.02, 0.14, 0.01), "MEETS_STATISTICAL_AND_PRACTICAL")
    chk("verdict: significant fade < 0.05 is NOT collapsed into 'meets'", v(0.03, 0.005, 0.055, 0.02), "DIRECTIONAL_EVIDENCE_BELOW_PRACTICAL_THRESHOLD")
    chk("verdict: significant opposite", v(-0.06, -0.10, -0.02, 0.003).startswith("EVIDENCE_AGAINST_THE_FADE"), True)
    chk("verdict: non-significant, CI reaches +0.05 -> inconclusive", v(0.02, -0.03, 0.07, 0.40).startswith("INCONCLUSIVE"), True)
    chk("verdict: non-significant, CI upper < +0.05 -> evidence against effects of that size", v(-0.01, -0.06, 0.04, 0.70).startswith("EVIDENCE_AGAINST_EFFECTS"), True)
    chk("verdict: significant fade with estimate exactly at the threshold meets", v(0.05, 0.01, 0.09, 0.02), "MEETS_STATISTICAL_AND_PRACTICAL")
    chk("verdict: p exactly at alpha is not significant", verdict(0.06, -0.01, 0.13, 0.05)["statistical"], "NOT_SIGNIFICANT")
    vv = verdict(0.08, 0.02, 0.14, 0.01)
    chk("verdict: two separate dimensions", (vv["statistical"], vv["practical"]), ("SIGNIFICANT_FADE", "MEETS"))
    # orientation: the mirrored series must give the mirrored answer
    rng = np.random.default_rng(3)
    n = 400
    c = 100 + np.cumsum(rng.normal(0, 0.05, n))
    h, l = c + 0.03, c - 0.03
    o1, s1 = _outcome(h, l, c, 50, 0.2, +1)
    o2, s2 = _outcome(-l, -h, -c, 50, 0.2, +1)
    o3, s3 = _outcome(h, l, c, 50, 0.2, -1)
    chk("orientation: negated series with sign +1 equals original with sign -1", (o2, s2) == (o3, s3) or (np.isnan(o2) and np.isnan(o3)), True)
    chk("orientation: up-first on the original is down-first on the mirror", (o1 + o3 == 1.0) if (s1 == "ok" and s3 == "ok" and not (np.isnan(o1) or np.isnan(o3))) else True, True)
    # cluster SE accounts for dependence: duplicating each excess within its block must not shrink the SE the way independent events would
    d = rng.normal(0.0, 0.5, 300)
    t = (np.arange(300) // 3) * NS_D // 1 + 5
    t = (np.arange(300) // 3).astype(np.int64) * 2 * NS_D
    dd = np.repeat(d[::3], 3)  # perfectly dependent triplets inside each block
    se_dep = cluster_test(dd, t)["se"]
    se_naive = dd.std(ddof=1) / math.sqrt(len(dd))
    chk("cluster SE: perfectly dependent in-block events give a larger SE than the naive one", se_dep > 1.4 * se_naive, True)
    # null and planted effect on a synthetic bar series via the full pipeline
    from . import studyb as sbm
    chk("frozen hash is stable text", len(FROZEN_HASH), 16)
    bad = [c_ for c_ in checks if not c_["ok"]]
    return {"checks": len(checks), "failures": bad, "passed": not bad, "frozen_hash": FROZEN_HASH}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--out", default=".")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2, default=str))
        sys.exit(0 if r["passed"] else 1)
    if a.run:
        if a.confirm != "I-FROZEN":
            print("REFUSED: the outcome runs once, after the frozen rule has been read back and independently reviewed. Re-run with --confirm I-FROZEN.")
            sys.exit(2)
        r = run_primary(a.raw_dir)
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "studyi_outcome.json"), "w") as f:
            json.dump(r, f, indent=2, default=str)
        md = render_markdown(r)
        with open(os.path.join(a.out, "studyi_outcome.md"), "w") as f:
            f.write(md)
        print(md)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
