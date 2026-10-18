"""
Observatory — STUDY J, STAGE 2: THE FROZEN LADDER TEST (GBPUSD, development data only). Read-only. The holdout stays sealed.

Written AFTER the stage-1 counts were reviewed and BEFORE any outcome exists. FROZEN below is the pre-registration in executable form (its hash is printed in the report). It runs ONCE.
The workflow refuses to run the outcome unless the confirm word is typed.

QUESTION. After a sweep of a confirmed 1h swing level that is reclaimed, does price reverse (a swept HIGH predicts DOWN, a swept LOW predicts UP, pooled and mirrored) over the next 24h more
than a volatility-matched placebo, at each rung of the ladder: the sweep itself, the confirmation (CISD or BOS), and the trigger (BOC after a retest of an OB or a breaker zone)?

FAMILY 1 (confirmatory, 7 tests, Benjamini-Hochberg q = 0.10): the excess at the decision bar of
     J:sweep                 the sweep reclaim close
     J:cisd, J:bos           the first close that confirms (change in state of delivery / break of structure)
     J:cisd:ob:boc, J:cisd:brk:boc, J:bos:ob:boc, J:bos:brk:boc      the candle-break trigger after a retest of an order block / a breaker zone
  Rejection-block chains (UNDERPOWERED: 79-141 usable events) are COUNTS ONLY: no outcome is computed for them.
  Retest nodes are not tested as claims; they are used as parents in family 2.
OUTCOME  (same as Study I)  o = 1 if -1 ATR(1h) (in the predicted direction: the reversal) is touched before +1 ATR within 288 five-minute bars from the bar after the decision bar; unresolved
  and same-bar-ambiguous events are dropped and counted. Excess d = o - mean of 30 placebo draws (same UTC weekday, hour +-1h, trailing-1h move within 0.5 ATR, trailing-24h range within +-15%,
  more than 24h away, full gap-free window). Fewer than 10 candidates or fewer than 8 resolved draws drops the event ('no_placebo'; reported by node).
INFERENCE  mean(d), cluster-robust SE with 2-day UTC block clusters of the decision time, two-sided z, BH-FDR q = 0.10 over the 7 tests. Practical threshold: estimate >= +0.05 in the reversal direction.
  Statistical and practical are separate verdict dimensions. A non-rejected test with a CI upper bound >= +0.05 is INCONCLUSIVE, never "no edge".
  Block stability: recent (from 2024-10-03) minus older per node, Bonferroni over the 7 nodes (0.05/7), descriptive.
FAMILY 2 (diagnostic, 10 tests, BH q = 0.10 within the family, NO promotion): the incremental information of each rung, measured at a common decision AGE.
  For a node event with elapsed time D bars since its parent node, one control is drawn at random from the parent events that have NOT produced the node by the same age D, are still alive
  (no high above the sweep extreme) and have a usable window; the control is evaluated at parent time + D with its own volatility-matched placebo. Contrast = mean excess(node) - mean excess(control).
  Contrasts: J:cisd, J:bos vs J:sweep; each of the four chains' retest vs its root, and each BOC vs its retest.
DESCRIPTIVE ONLY: overshoot bins (<0.1, 0.1-0.3, >=0.3 ATR); spike-exposure sensitivity; overlap with Study F events in the past 24h; nesting overlap between the 7 nodes; placebo-supply shortfall.
NOT DONE: continuation setups; FVG zones; any parameter variant; the holdout; costs and execution (excess in probability points only; no stops, no spread).

Run:  python3 -m observatory.studyj_run --selftest      python3 -m observatory.studyj_run --run --confirm J-FROZEN [--raw-dir ...] [--out ...]
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
from . import studyi_run as ir
from . import studyj as sj
from . import standards as st
from . import wave1 as w1
from .studyb import HMAX, NS_5, NS_H, NS_D, SEED
from .studyc import cluster_test, cluster_diff, MIN_EFFECT

FAMILY1 = ["sweep", "cisd", "bos", "cisd:ob:boc", "cisd:brk:boc", "bos:ob:boc", "bos:brk:boc"]
PARENT = {"cisd": "sweep", "bos": "sweep", "cisd:ob:retest": "cisd", "cisd:brk:retest": "cisd", "bos:ob:retest": "bos", "bos:brk:retest": "bos",
          "cisd:ob:boc": "cisd:ob:retest", "cisd:brk:boc": "cisd:brk:retest", "bos:ob:boc": "bos:ob:retest", "bos:brk:boc": "bos:brk:retest"}
FAMILY2 = list(PARENT)
EXPECTED_USABLE = {"sweep": 3815, "cisd": 1002, "bos": 765, "cisd:ob:retest": 701, "cisd:brk:retest": 633, "bos:ob:retest": 396, "bos:brk:retest": 391,
                   "cisd:ob:boc": 431, "cisd:brk:boc": 402, "bos:ob:boc": 260, "bos:brk:boc": 255}
FROZEN = {"study": "J", "family1": FAMILY1, "family2": FAMILY2, "fdr_q": 0.10, "alpha_block_diff": 0.05 / len(FAMILY1), "barrier_atr": 1.0, "horizon_bars": HMAX,
          "placebo_draws": sb.R_PLACEBO, "placebo_min_candidates": sb.MIN_POOL, "placebo_min_resolved": sb.MIN_PLACEBO, "placebo_trailing_tol": sb.TRAIL_TOL, "placebo_vol_tol": 0.15,
          "placebo_clock": "UTC weekday/hour +-1h", "min_effect": MIN_EFFECT, "boundary": "2024-10-03", "expected_usable": EXPECTED_USABLE, "overshoot_bins": [0.1, 0.3],
          "control": "parent events not yet at node, same age D, alive, usable window, own vol-matched placebo", "redraws": 8, "seed": SEED, "code": sj.CODE_VERSION,
          "geometry": {"K1H": sj.K1H, "K5": sj.K5, "R_RECLAIM": sj.R_RECLAIM, "W_CONF": sj.W_CONF, "SERIES_MIN": sj.SERIES_MIN, "RETEST_WIN": sj.RETEST_WIN, "BOC_WIN": sj.BOC_WIN, "RB_MIN_ATR": sj.RB_MIN_ATR}}
FROZEN_HASH = hashlib.sha256(json.dumps(FROZEN, sort_keys=True).encode()).hexdigest()[:16]
OVB = FROZEN["overshoot_bins"]


# ---------------------------------------------------------------------------
# verdict (BH-based, two dimensions)
# ---------------------------------------------------------------------------
def verdict(mean, ci_hi, reject, min_effect=MIN_EFFECT):
    if mean is None:
        return {"statistical": "NOT_COMPUTED", "practical": "NOT_COMPUTED", "reading": "DATA-LIMITED"}
    stat = "SIGNIFICANT_REVERSAL" if (reject and mean > 0) else ("SIGNIFICANT_OPPOSITE" if (reject and mean < 0) else "NOT_SIGNIFICANT")
    prac = "MEETS" if mean >= min_effect else "BELOW_THRESHOLD"
    if stat == "SIGNIFICANT_REVERSAL":
        reading = "MEETS_STATISTICAL_AND_PRACTICAL" if prac == "MEETS" else "DIRECTIONAL_EVIDENCE_BELOW_PRACTICAL_THRESHOLD"
    elif stat == "SIGNIFICANT_OPPOSITE":
        reading = "EVIDENCE_AGAINST_THE_REVERSAL (continuation-side effect; investigate, do not trade)"
    else:
        reading = ("EVIDENCE_AGAINST_EFFECTS_OF_PRACTICAL_SIZE (CI upper bound below +%.2f)" if ci_hi < min_effect else "INCONCLUSIVE (CI reaches +%.2f or more)") % min_effect
    return {"statistical": stat, "practical": prac, "reading": reading}


def _brief(s):
    return {k: s.get(k) for k in ("n", "n_clusters", "mean", "se", "ci", "z", "p", "event_rate", "placebo_rate")}


def summarize(rows):
    d = [r["d"] for r in rows]
    t = [r["t"] for r in rows]
    s = cluster_test(d, t) if rows else {"n": 0, "mean": None, "se": None, "ci": None, "z": None, "p": None}
    if s.get("mean") is not None:
        s["event_rate"] = float(np.mean([r["o"] for r in rows]))
        s["placebo_rate"] = float(np.mean([r["pm"] for r in rows]))
        s["ci"] = list(s["ci"])
    return s


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------
def build(raw_dir, source="observatory", preloaded=None, boundary=FROZEN["boundary"]):
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    bnd_ns = pd.Timestamp(boundary, tz="UTC").as_unit("ns").value
    ctx = sb.make_ctx(s5, s1h, +1, hold_ns)
    ns5, atr_t, x = ctx["ns5"], ctx["atr_t"], ctx["x"]
    nd = int((ns5 < hold_ns).sum())
    ns1 = sa._ns(s1h.index)
    nd1 = int((ns1 < hold_ns).sum())
    good_q = np.concatenate([p[0] for p in ctx["pools"].values()]) if ctx["pools"] else np.array([], int)
    keys, naive = si.clock_keys(ns5[:nd], "UTC")
    prep = {"ns5": ns5[:nd], "pools": si.build_pools(good_q, keys), "naive": naive, "vs": si.vol_state_clock(ns5[:nd], s5.h[:nd], s5.l[:nd], atr_t[:nd])}
    base = (s5.o[:nd], s5.h[:nd], s5.l[:nd], s5.c[:nd])
    evs = {}
    for sign in (+1, -1):
        o, h, l, c = sj.orient(base, sign)
        h1 = s1h.h[:nd1] if sign > 0 else -s1h.l[:nd1]
        A5 = {"o": o, "h": h, "l": l, "c": c, "ns": ns5[:nd], "atr": atr_t[:nd]}
        evs[sign], _ = sj.detect_sweeps(A5, h1, ns1[:nd1])
    return {"s5": s5, "s1h": s1h, "status": status, "hold_ns": hold_ns, "bnd_ns": bnd_ns, "ns5": ns5[:nd], "atr_t": atr_t, "x": x, "prep": prep, "evs": evs,
            "h": s5.h[:nd], "l": s5.l[:nd], "c": s5.c[:nd]}


def usable(B, p):
    return si.window_status(B["ns5"], p) is None and B["ns5"][p] + (HMAX + 1) * NS_5 <= B["hold_ns"]


def node_events(B, node):
    out = []
    for sign, es in B["evs"].items():
        for e in es:
            if node in e["nodes"]:
                p = e["nodes"][node]
                if usable(B, p):
                    out.append({"p": int(p), "t": int(B["ns5"][p]), "sign": -sign, "setup": (sign, e["q"]), "over": e["over_atr"], "ext": e["ext"], "esign": sign, "e": e})
    return out


def draw_controls(B, node, evs_node, rng_salt=0):
    """For each node event pick one alive parent event NOT yet at the node at the same age D. Returns control events (p, t, sign) and the number of node events without any valid control."""
    par = PARENT[node]
    pool = [(sign, e) for sign, es in B["evs"].items() for e in es if par in e["nodes"]]
    ns, h, l = B["ns5"], B["h"], B["l"]
    H = {+1: h, -1: -l}
    out, miss = [], 0
    for ev in evs_node:
        e = ev["e"]
        D = e["nodes"][node] - e["nodes"][par]
        rng = np.random.default_rng(SEED + int(ev["t"] % 2 ** 31) + 7 + 100003 * rng_salt)
        got = None
        for _ in range(60):
            sgn, c = pool[int(rng.integers(len(pool)))]
            if c is e:
                continue
            pp = c["nodes"][par]
            p2 = pp + D
            if p2 >= len(ns) or ns[p2] - ns[pp] != D * NS_5:
                continue
            nk = c["nodes"].get(node)
            if nk is not None and nk <= p2:
                continue
            if D > 0 and H[sgn][pp:p2 + 1].max() > c["ext"]:
                continue
            if not usable(B, p2):
                continue
            got = {"p": int(p2), "t": int(ns[p2]), "sign": -sgn, "setup": (sgn, c["q"])}
            break
        if got is None:
            miss += 1
        else:
            out.append(got)
    return out, miss


def score(B, evs, salt=0):
    return ir.score_events(evs, B["prep"], B["h"], B["l"], B["c"], B["atr_t"], B["x"], rng_salt=salt)


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def run(raw_dir=None, source="observatory", preloaded=None, enforce=True):
    raw_dir = raw_dir or rc.DEFAULT_RAW_DIR
    B = build(raw_dir, source, preloaded)
    res = {"dataset_status": B["status"], "holdout_opened": False, "frozen_hash": FROZEN_HASH, "frozen": FROZEN, "boundary_utc": FROZEN["boundary"]}
    nodes_needed = sorted(set(FAMILY1) | set(FAMILY2))
    NE = {n: node_events(B, n) for n in nodes_needed}
    res["usable_counts"] = {n: len(v) for n, v in NE.items()}
    ok = all(res["usable_counts"][n] == EXPECTED_USABLE[n] for n in EXPECTED_USABLE)
    res["population_matches_reviewed_counts"] = bool(ok)
    if enforce and not ok:
        res["aborted"] = "usable event counts differ from the reviewed stage-1 counts; no outcome is reported"
        res["expected"] = EXPECTED_USABLE
        return res
    rows, drops = {}, {}
    for n in nodes_needed:
        rows[n], drops[n] = score(B, NE[n])
    res["drops"] = drops
    # family 1
    fam1 = {n: summarize(rows[n]) for n in FAMILY1}
    live = [n for n in FAMILY1 if fam1[n].get("p") is not None]
    rej, q = w1.bh_fdr([fam1[n]["p"] for n in live], q=FROZEN["fdr_q"]) if live else ([], [])
    for n, rj, qq in zip(live, rej, q):
        fam1[n]["bh_reject"], fam1[n]["q_value"] = bool(rj), float(qq)
        fam1[n]["verdict"] = verdict(fam1[n]["mean"], fam1[n]["ci"][1], bool(rj))
    res["family1"] = fam1
    res["block_split"] = {}
    for n in FAMILY1:
        sp = ir.split_block(rows[n], B["bnd_ns"])
        d = sp["diff"]
        res["block_split"][n] = {"recent": _brief(sp["recent"]), "older": _brief(sp["older"]),
                                 "diff": None if d is None else {k: d.get(k) for k in ("diff", "se", "ci", "z", "p", "n1", "n0")},
                                 "bonferroni_flag": bool(d is not None and d.get("p") is not None and d["p"] < FROZEN["alpha_block_diff"])}
    # placebo-sampling noise (re-draws) for the confirmatory nodes except the largest
    rd = {}
    for n in FAMILY1[1:]:
        means = []
        for r in range(1, FROZEN["redraws"] + 1):
            rr, _ = score(B, NE[n], salt=r)
            means.append(float(np.mean([z["d"] for z in rr])) if rr else float("nan"))
        rd[n] = {"min": float(np.nanmin(means)), "max": float(np.nanmax(means)), "sd": float(np.nanstd(means, ddof=1)), "se": fam1[n].get("se")}
    res["placebo_redraw"] = rd
    # family 2: incremental contrasts at a common decision age
    fam2, misses = {}, {}
    for n in FAMILY2:
        ctrl, miss = draw_controls(B, n, NE[n])
        crow, cdrop = score(B, ctrl)
        misses[n] = {"node_events": len(NE[n]), "controls_drawn": len(ctrl), "node_events_without_control": miss, "control_drops": cdrop}
        nr = rows[n]
        if len(nr) >= 10 and len(crow) >= 10:
            dd = cluster_diff([r["d"] for r in nr], [r["t"] for r in nr], [r["d"] for r in crow], [r["t"] for r in crow])
        else:
            dd = {"diff": None, "p": None}
        fam2[n] = {"parent": PARENT[n], "node": _brief(summarize(nr)), "control": _brief(summarize(crow)),
                   "diff": {k: (list(dd[k]) if k == "ci" and dd.get(k) else dd.get(k)) for k in ("diff", "se", "ci", "z", "p", "n1", "n0")}}
    live2 = [n for n in FAMILY2 if fam2[n]["diff"].get("p") is not None]
    rej2, q2 = w1.bh_fdr([fam2[n]["diff"]["p"] for n in live2], q=FROZEN["fdr_q"]) if live2 else ([], [])
    for n, rj, qq in zip(live2, rej2, q2):
        fam2[n]["bh_reject"], fam2[n]["q_value"] = bool(rj), float(qq)
    res["family2"], res["family2_controls"] = fam2, misses
    # descriptive
    desc = {"overshoot_bins": {}, "spike_exposure": {}, "overlap_study_f": {}}
    ns5 = B["ns5"]
    dev = np.ones(len(ns5), bool)
    sp = st.spike_flags(B["c"], ns5, B["s5"].closure[:len(ns5)] | (B["s5"].gap[:len(ns5)] > 0))
    fts = {1: [], -1: []}
    try:
        from . import studyf as sf
        for cx in (sf.ctx_f(B["s5"], B["s1h"], +1, B["hold_ns"]), sf.ctx_f(B["s5"], B["s1h"], -1, B["hold_ns"])):
            for key in ("A:0", "A:0.1", "A:0.2"):
                ev, _ = sf.events_for(cx, key, "dev")
                fts[cx["sign"]] += [e["t"] for e in ev]
        fts = {s: np.sort(np.unique(np.array(v, np.int64))) for s, v in fts.items()}
        have_f = True
    except Exception as ex:
        have_f, desc["overlap_study_f"]["error"] = False, repr(ex)
    for n in FAMILY1:
        r = rows[n]
        desc["overshoot_bins"][n] = {f"<{OVB[0]}": _brief(summarize([z for z in r if z["over"] < OVB[0]])), f"{OVB[0]}-{OVB[1]}": _brief(summarize([z for z in r if OVB[0] <= z["over"] < OVB[1]])),
                                     f">={OVB[1]}": _brief(summarize([z for z in r if z["over"] >= OVB[1]]))}
        exposed, clean = st.spike_exposure(r, sp)
        desc["spike_exposure"][n] = {"exposed": len(exposed), "clean": _brief(summarize(clean))}
        if have_f and r:
            near = np.array([st.near_past(fts[int(-z["sign"])], z["t"], 24 * NS_H) for z in r], bool)
            desc["overlap_study_f"][n] = {"share": float(near.mean()), "without": _brief(summarize([z for z, b in zip(r, near) if not b]))}
    ids = {n: {z["setup"] for z in rows[n]} for n in FAMILY1}
    desc["nesting_overlap_share_of_row_in_column"] = {a: {b: (len(ids[a] & ids[b]) / len(ids[a]) if ids[a] else None) for b in FAMILY1} for a in FAMILY1}
    res["descriptive"] = desc
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
    return f"- {label}: n={s['n']}, excess {_f(s['mean'])} (SE {s['se']:.3f}, 95% CI {_f(ci[0])}..{_f(ci[1])}), p {s['p']:.3f}"


def render_markdown(r):
    L = ["# Observatory — Study J, stage 2: the frozen ladder test (GBPUSD)\n",
         f"Dataset **{r['dataset_status']}** · holdout opened: **False** · frozen contract hash **{r['frozen_hash']}** · block boundary {r['boundary_utc']}\n"]
    if "aborted" in r:
        return "\n".join(L + [f"**ABORTED:** {r['aborted']}", f"usable found: {r['usable_counts']}", f"expected: {r['expected']}"])
    L += ["## Usable events entering the test (match the reviewed stage-1 counts: **%s**)\n" % r["population_matches_reviewed_counts"], str(r["usable_counts"]), f"\nDropped at scoring: {r['drops']}\n",
          "## Family 1 — confirmatory (7 tests, BH q = 0.10); positive excess = price moved in the reversal direction more than the vol-matched placebo\n",
          "| node | n | excess | 95% CI | p | q | statistical | practical | reading |", "|---|---|---|---|---|---|---|---|---|"]
    for n in FAMILY1:
        s = r["family1"][n]
        if s.get("mean") is None:
            L.append(f"| {n} | {s.get('n')} | n/a | | | | | | DATA-LIMITED |")
            continue
        v = s["verdict"]
        L.append(f"| {n} | {s['n']} | {_f(s['mean'])} | {_f(s['ci'][0])}..{_f(s['ci'][1])} | {s['p']:.3f} | {s['q_value']:.3f} | {v['statistical']} | {v['practical']} | {v['reading']} |")
    L += ["\nA non-rejected test is not proof of no effect: see the detectable effects in the stage-1 counts.\n", "## Block stability (recent from 2024-10-03 minus older; Bonferroni 0.05/7 = %.4f)\n" % FROZEN["alpha_block_diff"]]
    for n in FAMILY1:
        b = r["block_split"][n]
        d = b["diff"]
        L.append(f"- {n}: recent {_f(b['recent'].get('mean'))} (n={b['recent'].get('n')}), older {_f(b['older'].get('mean'))} (n={b['older'].get('n')}), diff " + ("n/a" if not d or d.get("diff") is None else f"{_f(d['diff'])} (p {d['p']:.3f})") + (" **FLAG**" if b["bonferroni_flag"] else ""))
    L += ["\n## Placebo re-draw spread (8 re-draws; not in the SE)\n"] + [f"- {n}: mean excess {_f(v['min'])}..{_f(v['max'])} (sd {v['sd']:.4f}, SE {v['se']:.4f})" for n, v in r["placebo_redraw"].items()]
    L += ["\n## Family 2 — diagnostic, incremental information at a common decision age (10 contrasts, BH within family, NO promotion)\n", "| node vs parent | node excess (n) | control excess (n) | difference | p | q |", "|---|---|---|---|---|---|"]
    for n, v in r["family2"].items():
        d = v["diff"]
        L.append(f"| {n} vs {v['parent']} | {_f(v['node'].get('mean'))} ({v['node'].get('n')}) | {_f(v['control'].get('mean'))} ({v['control'].get('n')}) | "
                 + ("n/a | | |" if d.get("diff") is None else f"{_f(d['diff'])} | {d['p']:.3f} | {v.get('q_value', float('nan')):.3f} |"))
    L += ["\nControl draws: " + str({n: (m["controls_drawn"], m["node_events_without_control"]) for n, m in r["family2_controls"].items()}) + "  (drawn, node events without a valid control)"]
    D = r["descriptive"]
    L += ["\n## Descriptive only\n", "**Overshoot bins (ATR beyond the level: <0.1, 0.1-0.3, >=0.3)**"]
    for n in FAMILY1:
        L.append(f"- {n}: " + " · ".join(f"{k}: n={v.get('n')}, {_f(v.get('mean'))}" for k, v in D["overshoot_bins"][n].items()))
    L += ["\n**Spike exposure (events not removed; clean-sample estimates are sensitivity only)**"] + [f"- {n}: exposed {v['exposed']}; clean {_f(v['clean'].get('mean'))} (n={v['clean'].get('n')})" for n, v in D["spike_exposure"].items()]
    L += ["\n**Overlap with Study F events in the past 24h**"] + ([f"- {n}: {100 * v['share']:.1f}% overlapped; without them {_f(v['without'].get('mean'))} (n={v['without'].get('n')})" for n, v in D["overlap_study_f"].items() if n != "error"] if "error" not in D["overlap_study_f"] else ["- error: " + D["overlap_study_f"]["error"]])
    L += ["\n**Nesting (share of the row's setups that also appear in the column's node)**", "| | " + " | ".join(FAMILY1) + " |", "|---|" + "---|" * len(FAMILY1)]
    for a in FAMILY1:
        L.append(f"| {a} | " + " | ".join("n/a" if D["nesting_overlap_share_of_row_in_column"][a][b] is None else f"{D['nesting_overlap_share_of_row_in_column'][a][b]:.2f}" for b in FAMILY1) + " |")
    L += ["\n## Not done here\n", "- Rejection-block chains (counts only), continuation setups, FVG zones, costs/stops/execution, any variant added after seeing this result, the holdout."]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------
def selftest():
    checks = []

    def chk(name, got, want):
        checks.append({"check": name, "ok": bool(got == want), "got": got, "want": want})

    v = lambda m, hi, rj: verdict(m, hi, rj)["reading"]
    chk("verdict: rejected, reversal, >= 0.05 -> meets both", v(0.08, 0.14, True), "MEETS_STATISTICAL_AND_PRACTICAL")
    chk("verdict: rejected, reversal, < 0.05 -> directional only", v(0.03, 0.055, True), "DIRECTIONAL_EVIDENCE_BELOW_PRACTICAL_THRESHOLD")
    chk("verdict: rejected, opposite", v(-0.05, -0.01, True).startswith("EVIDENCE_AGAINST_THE_REVERSAL"), True)
    chk("verdict: not rejected, CI reaches +0.05 -> inconclusive", v(0.01, 0.06, False).startswith("INCONCLUSIVE"), True)
    chk("verdict: not rejected, CI upper < +0.05 -> evidence against effects of that size", v(-0.01, 0.04, False).startswith("EVIDENCE_AGAINST_EFFECTS"), True)
    chk("family and contrast bookkeeping", (len(FAMILY1), len(FAMILY2), set(FAMILY1) <= set(EXPECTED_USABLE)), (7, 10, True))
    chk("every parent is a known node", all(p in EXPECTED_USABLE for p in PARENT.values()), True)
    # control logic on hand-built events: not-yet, alive, usable
    A = sj._bars(sj.BASE_ROWS)
    chk("base sequence has the expected nodes (inherited from the stage-1 selftest)", sj.analyze_sweep(A, sj.run_end(A["ns"]), 4, 4, 11.0, sj.SWING_LOW)["nodes"].get("cisd"), 6)
    chk("frozen hash is stable text", len(FROZEN_HASH), 16)
    bad = [c for c in checks if not c["ok"]]
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
        if a.confirm != "J-FROZEN":
            print("REFUSED: the outcome runs once, after the frozen rule has been reviewed. Re-run with --confirm J-FROZEN.")
            sys.exit(2)
        r = run(a.raw_dir)
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "studyj_outcome.json"), "w") as f:
            json.dump(r, f, indent=2, default=str)
        md = render_markdown(r)
        with open(os.path.join(a.out, "studyj_outcome.md"), "w") as f:
            f.write(md)
        print(md)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
