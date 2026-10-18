"""
Observatory — STUDY K, STAGE 1: ENTRY TIMING AND RETRACEMENT-AREA SELECTION, COUNTS ONLY (GBPUSD, development data only). Read-only. The holdout stays sealed.

QUESTION (from the pullback-entry discussion). A better entry price is not the same as a better trade: a resting order can miss the move, fill just before the setup fails, or fill and keep losing.
Studies B/C measured what price does AFTER a pullback of a given depth; they did not put the entry policies side by side on the SAME impulses, count the trades each policy never gets, and charge costs.
That comparison, plus whether a structural zone (a 1h fair value gap) adds anything beyond the generic pullback of the same depth, is this study. THIS FILE DOES THE COUNTS ONLY: no outcome code.

IMPULSES (the Study B population, so the overlap is disclosed by construction): an up-BOS on 1h (pivot 3 bars each side, break by a 1h CLOSE within 48 bars, leg height 1-6 ATR(1h)); the down case is the same code on negated prices,
pooled. Written for the long side. Leg: L = lowest low from the pivot to the break bar, M0 = high of the break bar, W = M0 - L. Decision = the close of the break bar; nothing after it is used to define the impulse.
ENTRY POLICIES (all start at the first 5m bar after the decision; a limit order is valid for 144 bars = 12h, and is cancelled if price trades below L first)
  P0     market at the open of the first bar after the decision (the baseline every other policy must beat)
  F25, F50, F618   limit at M0 - f*W for f = 0.25, 0.5, 0.618 (comparison depths, NOT an assumption that Fibonacci works)
  FVG    limit at the TOP of the highest unmitigated bullish 1h fair value gap formed inside the leg (a >= the bar of L, c <= the break bar, low_c > high_a, no later 1h low <= its top up to the break); impulses with no such gap have no FVG order
A bar that both reaches the level and trades below L is AMBIGUOUS (the order of events inside a 5m bar is unknown): counted, excluded from the fill statistics of that policy, never resolved in the policy's favour.
COMMON CLOCK. Every trade must be able to resolve within 24h of the decision (a gap-free 288-bar window: 12h to fill, the rest to play out), so the policies are judged on exactly the same impulses.
LATER OUTCOME (declared here, not computed): per policy, P(fill), P(+1 ATR before -1 ATR from the entry | fill), and expectancy PER IMPULSE = P(fill) * (2p - 1 - cost/ATR) with non-fills counted as zero and a flat 1 pip cost; paired differences between policies on the same impulses;
FVG vs a generic limit at the same depth. A driftless price makes EVERY policy's expectancy about -cost x P(fill): a policy that "wins" by trading less is not an edge, so every policy is also compared with a matched-time placebo entry.

Run:  python3 -m observatory.studyk --counts [--raw-dir ...] [--out ...] [--boundary 2024-10-03]      python3 -m observatory.studyk --selftest
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

from . import clean as rc
from . import studya as sa
from . import studyb as sb
from . import studyi as si
from . import standards as st
from .studyb import NS_5, NS_H, NS_D, find_bos

CODE_VERSION = "studyk-counts-v2"
VALID = 144                 # 5m bars an order stays valid (12h)
TOT = 288                   # common clock: bars after the decision that must exist gap-free (24h: a 48h clock would lose every Thursday decision as well as Friday)
FIBS = {"F25": 0.25, "F50": 0.5, "F618": 0.618}
POLICIES = ["P0", "F25", "F50", "F618", "FVG"]
DEPTH_BINS = [(0.0, 0.25), (0.25, 0.5), (0.5, 0.618), (0.618, 1.0)]
DEFAULT_BOUNDARY = "2024-10-03"


def orient1h(s1h, sign, nd1):
    h, l, c = s1h.h[:nd1], s1h.l[:nd1], s1h.c[:nd1]
    return (h, l, c) if sign > 0 else (-l, -h, -c)


def find_fvg(h1, l1, ns1, m, j):
    """Highest-top unmitigated bullish 1h FVG formed inside the leg [m, j] (a >= m, c <= j). Returns (bottom, top, c_index) or None."""
    best = None
    for cc in range(m + 2, j + 1):
        a = cc - 2
        if ns1[cc] - ns1[a] != 2 * NS_H or l1[cc] <= h1[a]:
            continue
        top, bot = float(l1[cc]), float(h1[a])
        if cc < j and np.any(l1[cc + 1:j + 1] <= top):
            continue                      # mitigated before the decision
        if best is None or top > best[1]:
            best = (bot, top, cc)
    return best


def fill_status(l5, o5, q, level, L):
    """Limit buy at `level` from bar q for VALID bars; cancelled if a low goes below L first. Returns (status, bar index of fill)."""
    seg = l5[q:q + VALID]
    fi = np.flatnonzero(seg <= level)
    ii = np.flatnonzero(seg < L)
    f = int(fi[0]) if fi.size else None
    i = int(ii[0]) if ii.size else None
    if f is None:
        return ("invalidated_first" if i is not None else "not_reached"), None
    if i is not None and i < f:
        return "invalidated_first", None
    if i is not None and i == f:
        return "ambiguous", None
    return "filled", q + f


def detect_impulses(A5, s1h_o, ns1, sign, hold_ns):
    """Impulses in one orientation with their policy fills. A5: oriented 5m arrays dict o,h,l,c,ns,atr. s1h_o: (h1,l1,c1,atr1). Returns (list, counters)."""
    h1, l1, c1, atr1 = s1h_o
    ns5, n = A5["ns"], len(A5["ns"])
    ev, cnt = find_bos(h1, l1, c1, atr1, ns1)
    cnt = dict(cnt)
    cnt.update(no_5m_at_break=0, window_short=0, no_atr=0)
    out = []
    for b in ev:
        j, i = b["j"], b["i"]
        tb = int(ns1[j] + NS_H)
        q = int(np.searchsorted(ns5, tb))
        if q >= n or q < 1 or ns5[q] != tb or ns5[q - 1] != tb - NS_5:
            cnt["no_5m_at_break"] += 1
            continue
        usable = q + TOT <= n and ns5[q + TOT - 1] - ns5[q] == (TOT - 1) * NS_5 and ns5[q + TOT - 1] + NS_5 <= hold_ns
        L = float(b["L"])
        M0 = float(h1[j])
        W = M0 - L
        m = i + 1 + int(np.argmin(l1[i + 1:j + 1]))
        rec = {"sign": sign, "q": q, "t": int(ns5[q]), "j": int(j), "W": W, "atr": float(b["atr"]), "range_atr": float(b["range_atr"]), "age": int(b["age"]), "usable": bool(usable), "fills": {}}
        if not usable:
            cnt["window_short"] += 1
            out.append(rec)
            continue
        rec["fills"]["P0"] = ("filled", q)
        for name, f in FIBS.items():
            rec["fills"][name] = fill_status(A5["l"], A5["o"], q, M0 - f * W, L)
        fv = find_fvg(h1, l1, ns1, m, j)
        if fv is not None:
            bot, top, cc = fv
            rec["fvg"] = {"top": top, "bottom": bot, "depth": float((M0 - top) / W), "height_atr": float((top - bot) / b["atr"])}
            if top > L:
                rec["fills"]["FVG"] = fill_status(A5["l"], A5["o"], q, top, L)
        out.append(rec)
    return out, cnt


def count_policies(imps, bnd_ns):
    res = {}
    use = [r for r in imps if r["usable"]]
    res["impulses"] = len(imps)
    res["usable"] = len(use)
    t = np.array([r["t"] for r in use], np.int64)
    res["recent"], res["older"] = int((t >= bnd_ns).sum()), int((t < bnd_ns).sum())
    res["blocks_2day"] = len({int(x // (2 * NS_D)) for x in t})
    res["weekday_Mon_Sun"] = si.wd_counts(pd.DatetimeIndex(t.astype("datetime64[ns]")).dayofweek.to_numpy()) if len(t) else [0] * 7
    res["range_atr_quantiles"] = si._quant([r["range_atr"] for r in use])
    pol = {}
    for p in POLICIES:
        rows = [r for r in use if p in r["fills"]]
        st_ = {}
        for r in rows:
            s = r["fills"][p][0]
            st_[s] = st_.get(s, 0) + 1
        filled = [r for r in rows if r["fills"][p][0] == "filled"]
        tf = np.array([r["t"] for r in filled], np.int64)
        nb = len({int(x // (2 * NS_D)) for x in tf})
        pol[p] = {"orders": len(rows), "status": st_, "fill_rate_of_orders": (len(filled) / len(rows)) if rows else None, "fill_rate_of_all_usable": len(filled) / len(use) if use else None,
                  "filled": len(filled), "recent": int((tf >= bnd_ns).sum()), "older": int((tf < bnd_ns).sum()), "blocks_2day": nb, "feasibility": st.feasibility(len(filled), nb),
                  "median_minutes_to_fill": float(np.median([(r["fills"][p][1] - r["q"]) * 5 for r in filled])) if filled else None}
    res["policies"] = pol
    fv = [r for r in use if "fvg" in r]
    res["fvg"] = {"impulses_with_fvg": len(fv), "share_of_usable": len(fv) / len(use) if use else None, "height_atr_quantiles": si._quant([r["fvg"]["height_atr"] for r in fv]),
                  "depth_quantiles": si._quant([r["fvg"]["depth"] for r in fv])}
    bins = {}
    for lo, hi in DEPTH_BINS:
        inb = [r for r in fv if lo <= r["fvg"]["depth"] < hi]
        f_filled = [r for r in inb if r["fills"].get("FVG", ("", 0))[0] == "filled"]
        bins[f"{lo}-{hi}"] = {"fvg_impulses": len(inb), "fvg_filled": len(f_filled)}
    res["fvg"]["by_depth_bin"] = bins
    # does the FVG sit on a fib level? (within 0.05 W of 0.5 or 0.618) and the supply of controls: impulses WITHOUT an FVG whose generic limit at the matched depth fills
    near = [r for r in fv if min(abs(r["fvg"]["depth"] - 0.5), abs(r["fvg"]["depth"] - 0.618)) <= 0.05]
    res["fvg"]["within_0.05W_of_a_50_or_618_level"] = {"n": len(near), "share": len(near) / len(fv) if fv else None}
    return res


def run_counts(raw_dir=None, source="observatory", boundary=DEFAULT_BOUNDARY, preloaded=None):
    raw_dir = raw_dir or rc.DEFAULT_RAW_DIR
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    bnd_ns = pd.Timestamp(boundary, tz="UTC").as_unit("ns").value
    ctx = sb.make_ctx(s5, s1h, +1, hold_ns)
    ns5 = ctx["ns5"]
    nd = int((ns5 < hold_ns).sum())
    ns1 = sa._ns(s1h.index)
    nd1 = int((ns1 < hold_ns).sum())
    allimp, counters = [], {}
    for sign in (+1, -1):
        base = (s5.o[:nd], s5.h[:nd], s5.l[:nd], s5.c[:nd])
        o, h, l, c = (base if sign > 0 else (-base[0], -base[2], -base[1], -base[3]))
        A5 = {"o": o, "h": h, "l": l, "c": c, "ns": ns5[:nd]}
        h1, l1, c1 = orient1h(s1h, sign, nd1)
        imps, cnt = detect_impulses(A5, (h1, l1, c1, s1h.atr[:nd1]), ns1[:nd1], sign, hold_ns)
        allimp += imps
        counters["up-BOS (long)" if sign > 0 else "down-BOS (short, mirrored)"] = cnt
    res = {"dataset_status": status, "boundary_utc": boundary, "holdout_opened": False, "code_version": CODE_VERSION, "development_first_bar": str(s5.index[0]), "detection_counters": counters,
           "counts": count_policies(allimp, bnd_ns)}
    return res


def _f(x, nd=3):
    return "n/a" if x is None else f"{x:.{nd}f}"


def render_markdown(r):
    c = r["counts"]
    L = ["# Observatory — Study K, stage 1: entry timing and retracement-area selection, counts only (GBPUSD)\n",
         f"Dataset **{r['dataset_status']}** · first bar {r['development_first_bar']} · block boundary {r['boundary_utc']} · holdout opened: **False** · code version **{r['code_version']}** · **no outcome has been computed in this report**\n",
         "## Impulses\n"] + [f"- {k}: {v}" for k, v in r["detection_counters"].items()]
    L += [f"\nImpulses {c['impulses']} → usable (gap-free 24h from the decision, inside development data) **{c['usable']}** · recent {c['recent']} / older {c['older']} · 2-day blocks {c['blocks_2day']} · weekday Mon..Sun {c['weekday_Mon_Sun']}",
          f"Leg height (ATR): {c['range_atr_quantiles']}\n", "## Policies on the same impulses\n",
          "| policy | orders | status of orders | filled | fill rate (of orders) | fill rate (of all usable) | median min to fill | recent / older (filled) | blocks | detectable (opt / cons) | tier |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in POLICIES:
        v = c["policies"][p]
        fz = v["feasibility"]
        L.append(f"| {p} | {v['orders']} | {v['status']} | {v['filled']} | {_f(v['fill_rate_of_orders'])} | {_f(v['fill_rate_of_all_usable'])} | {_f(v['median_minutes_to_fill'], 0)} | {v['recent']} / {v['older']} | {v['blocks_2day']} | {_f(fz.get('detectable_optimistic'))} / {_f(fz.get('detectable_conservative'))} | {fz['tier']} |")
    f = c["fvg"]
    L += ["\n## The 1h fair value gap as a zone\n", f"Impulses with an eligible unmitigated 1h FVG inside the leg: {f['impulses_with_fvg']} ({_f(f['share_of_usable'])} of usable) · FVG height (ATR) {f['height_atr_quantiles']} · depth of the FVG top below the leg high (fraction of the leg) {f['depth_quantiles']}",
          f"FVG top within 0.05 leg of a 50% or 61.8% level: {f['within_0.05W_of_a_50_or_618_level']}", "By depth bin: " + str(f["by_depth_bin"]), "\n## Not done here\n",
          "- Any outcome, estimate or test; costs; stops and targets; the placebo entry; the generic-limit-at-the-same-depth control (its supply follows from the fills above); continuation vs pullback within a trend filter; other zone types (OB, breaker, resistance-turned-support)."]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------
def selftest():
    checks = []

    def chk(name, got, want):
        checks.append({"check": name, "ok": bool(got == want), "got": got, "want": want})

    # fills
    l = np.array([10.0, 9.8, 9.5, 9.0, 9.9, 9.9])
    o = np.full(6, 10.0)
    chk("limit fills at the first low <= level", fill_status(l, o, 0, 9.6, 8.0), ("filled", 2))
    chk("never reached", fill_status(l, o, 0, 8.5, 8.0)[0], "not_reached")
    chk("a fill followed by a later break of L is still a fill (the stop is the outcome stage's job)", fill_status(np.array([10.0, 9.4, 8.9]), o, 0, 9.5, 9.0), ("filled", 1))
    chk("one bar reaching the level and L is ambiguous (not resolved in the policy's favour)", fill_status(np.array([10.0, 8.9]), o, 0, 9.5, 9.0)[0], "ambiguous")
    chk("level never reached and L never broken", fill_status(np.array([10.0, 9.9, 9.8]), o, 0, 9.0, 8.0)[0], "not_reached")
    # fvg
    h1 = np.array([10.0, 10.2, 10.6, 11.0, 11.4, 11.8])
    l1 = np.array([9.8, 10.0, 10.5, 10.8, 11.3, 11.6])
    ns1 = np.arange(6, dtype=np.int64) * NS_H
    fv = find_fvg(h1, l1, ns1, 0, 5)
    chk("FVG: highest-top unmitigated gap in the leg (formed by the break bar itself)", None if fv is None else (round(fv[0], 2), round(fv[1], 2)), (11.0, 11.6))
    h2 = np.array([10.0, 10.4, 10.9, 11.2, 11.5])
    l2 = np.array([9.9, 10.2, 10.6, 11.0, 11.4])
    ns2 = np.arange(5, dtype=np.int64) * NS_H
    fv2 = find_fvg(h2, l2, ns2, 0, 4)
    chk("FVG: a gap exists when low_c > high_a (bars 0,1,2: 10.6 > 10.0) and the highest unmitigated top is chosen", None if fv2 is None else round(fv2[1], 2), 11.4)
    l3 = l2.copy()
    l3[4] = 10.5            # price came back into the first gaps before the decision: those tops are mitigated
    fv3 = find_fvg(h2, l3, ns2, 0, 4)
    chk("FVG: gaps entered by later lows are mitigated (none left)", fv3, None)
    # mirror symmetry of impulse detection
    rng = np.random.default_rng(11)
    n = 12 * 400
    c5 = 100 + np.cumsum(rng.normal(0, 0.03, n))
    o5 = np.r_[c5[0], c5[:-1]]
    h5 = np.maximum(o5, c5) + np.abs(rng.normal(0, 0.02, n))
    l5 = np.minimum(o5, c5) - np.abs(rng.normal(0, 0.02, n))
    ns5 = pd.Timestamp("2025-03-03", tz="UTC").as_unit("ns").value + np.arange(n, dtype=np.int64) * NS_5
    k = n // 12
    ns1 = ns5[::12][:k]
    h1 = np.array([h5[i * 12:(i + 1) * 12].max() for i in range(k)])
    l1 = np.array([l5[i * 12:(i + 1) * 12].min() for i in range(k)])
    c1 = np.array([c5[(i + 1) * 12 - 1] for i in range(k)])
    atr1 = np.full(k, 0.25)
    hold = ns5[-1] + 10 * NS_H
    A = {"o": o5, "h": h5, "l": l5, "c": c5, "ns": ns5}
    imps, cnt = detect_impulses(A, (h1, l1, c1, atr1), ns1, +1, hold)
    An = {"o": -o5, "h": -l5, "l": -h5, "c": -c5, "ns": ns5}
    impn, _ = detect_impulses(An, (-l1, -h1, -c1, atr1), ns1, -1, hold)
    chk("the random-walk test has impulses (not vacuous)", len(imps) >= 3, True)
    # the mirrored series of the mirrored series is the original
    A2 = {"o": -An["o"], "h": -An["l"], "l": -An["h"], "c": -An["c"], "ns": ns5}
    imps2, _ = detect_impulses(A2, (h1, l1, c1, atr1), ns1, +1, hold)
    chk("double negation reproduces the same impulses and fills", [(r["q"], {k: v for k, v in r["fills"].items()}) for r in imps], [(r["q"], {k: v for k, v in r["fills"].items()}) for r in imps2])
    # past-only: truncating the future leaves impulses decided before the cut unchanged (fills may be unknown near the cut, so compare impulse keys)
    cut = n // 2
    Ac = {k_: v[:cut] for k_, v in A.items()}
    kc = cut // 12 - 6
    impc, _ = detect_impulses(Ac, (h1[:kc], l1[:kc], c1[:kc], atr1[:kc]), ns1[:kc], +1, hold)
    early = lambda xs: sorted((r["q"], round(r["W"], 6)) for r in xs if r["q"] < cut - TOT - 50)
    chk("past-only: impulses decided well before the cut are the same without the future", early(impc), early(imps))
    chk("policy order list", POLICIES, ["P0", "F25", "F50", "F618", "FVG"])
    bad = [x for x in checks if not x["ok"]]
    return {"checks": len(checks), "failures": bad, "passed": not bad}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--counts", action="store_true")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--out", default=".")
    ap.add_argument("--boundary", default=DEFAULT_BOUNDARY)
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2, default=str))
        sys.exit(0 if r["passed"] else 1)
    if a.counts:
        r = run_counts(a.raw_dir, boundary=a.boundary)
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "studyk_counts.json"), "w") as f:
            json.dump(r, f, indent=2, default=str)
        md = render_markdown(r)
        with open(os.path.join(a.out, "studyk_counts.md"), "w") as f:
            f.write(md)
        print(md)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
