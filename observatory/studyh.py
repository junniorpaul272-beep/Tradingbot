"""
Observatory — Study H: "buy low, sell high" range definitions (GBPUSD, 5m, development data only).
Read-only on raw data. The holdout stays sealed.

The claim being tested (the friend's premium/discount idea, stated so it can fail):
    "Inside a dealing range, price in the UPPER part tends to fall and price in the LOWER part tends to rise (mean reversion toward the middle)."
The range itself is a hypothesis, so FOUR definitions are tested separately, and a fifth family uses distance from a rolling mean:
    prev   previous trading day's high/low (the Study F box)
    r24    high/low of the last 24h of trading (288 five-minute bars)
    r5d    high/low of the last 5 trading days (1440 bars)
    swing  the latest CONFIRMED 1h swing high and swing low (fractal, 2 bars each side, confirmed 2 hours after the swing bar; at most 72h old)
    m24 / m5d   close at least 1.5 ATR ABOVE (or, mirrored, BELOW) the mean close of the last 24h / 5 trading days (no range at all)

Position in the range: pos = (close - range low) / (range high - range low), measured at the CLOSE of the decision bar from bars up to and including it.
Observations are taken on a 4-hour grid (00, 04, 08, 12, 16, 20 UTC bar closes). Price must be INSIDE the range (0 <= pos <= 1); a close beyond the range is a breakout and is not
part of this study (Study F covers it).
    H.1  premium / discount : pos >= 0.5  (the upper half; the lower half is the mirror image)       claim: fade
    H.2  deep zone          : pos >= 0.8  (the upper fifth; the lower fifth is the mirror image)    claim: fade
    H.3  rolling-mean deviation: dev >= 1.5 ATR above (or below) the rolling mean                    claim: fade
    Control: the MIDDLE of the same range (0.4 <= pos <= 0.6), upper orientation only (the middle band is its own mirror image), same outcome; it should show nothing.
Outcome = +1 ATR through before -1 ATR away within 24h minus a placebo with the same weekday, hour and recent move (outcome 1 = up first; negative excess supports the
fade claim). The same placebo as Studies A-G: it removes the clock slot and the recent move, so what is left is what the POSITION itself adds.
Inference: two-sided, cluster-robust by 2-day UTC block; BH-FDR q=0.10 within each family; practical threshold 5 probability points.
Family 1: 10 tests (4 ranges x {half, deep} + 2 mean-deviation). Family 2: 8 (each range's half/deep zone minus its mid-range control).

HONEST LIMITS. The zones are nested (a deep event is also a half event), the four ranges overlap (the 24h range often IS the previous day's range), and observations are
4 hours apart inside the same swing: the effective sample is far smaller than the event counts. Study H overlaps Studies D, F and G (same price, different labels).
No stops, no costs, no order-flow, no volume (OHLC only). Nothing here is a trading rule.

Run:  python3 -m observatory.studyh [--raw-dir ...] [--out ...]   --selftest   --counts   --readiness
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
from . import studyd as sd
from . import studyf as sf
from . import studyg as sg
from .studyb import draw_placebo, NS_5, NS_D, NS_H, HMAX, FDR_Q, SEED
from .studya import verdict, _ns, _phi, _a, _p
from .studyc import cluster_test, MIN_EFFECT

GRID_NS = 4 * NS_H                    # observation grid: bars that CLOSE on a 4-hour boundary
RANGES = ("prev", "r24", "r5d", "swing")
ROLL = {"r24": (288, 26 * NS_H), "r5d": (1440, int(7.5 * NS_D))}     # (bars, maximum calendar span in ns: a weekend is allowed in 5d, not in 24h)
SWING_K, SWING_MAX_AGE = 2, 72 * NS_H
W_ATR = (1.5, 60.0)                   # a range narrower than 1.5 ATR or wider than 60 ATR is not a dealing range
ZONES = {"half": 0.5, "deep": 0.8}
MID = (0.4, 0.6)
DEV_ATR = 1.5
MEANS = {"m24": 288, "m5d": 1440}
TEST_KEYS = [f"H:{r}:{z}" for r in RANGES for z in ZONES] + [f"H:{m}" for m in MEANS]
CTRL_KEYS = [f"C:{r}:mid" for r in RANGES]
ALL_KEYS = TEST_KEYS + CTRL_KEYS
SPEC_KEYS = [f"X:{k}" for k in TEST_KEYS if k.split(":")[1] in RANGES]
CONTRACT = {
    "study": "H", "module": "studyh", "version": "v1", "instrument": "GBPUSD",
    "claim": "fade: price in the upper part of a range falls / in the lower part rises (mirror-symmetric)",
    "ranges": {"prev": "previous trading day high/low (Study F box)", "r24": "last 288 five-minute bars, span <= 26h", "r5d": "last 1440 bars, span <= 7.5 days",
               "swing": f"latest confirmed 1h fractal swing high and low (k={SWING_K}), at most 72h old"},
    "range_width_atr": list(W_ATR), "grid": "bars closing on a 4h boundary (UTC)", "position": "(close - low) / (high - low), inside the range only",
    "zones": {"half": ">= 0.5", "deep": ">= 0.8", "mid_control": list(MID)}, "mean_deviation": {"windows_bars": MEANS, "threshold_atr": DEV_ATR},
    "outcome": "+-1 ATR first passage within 24h minus placebo (weekday, hour +-1h, trailing 1h move)", "inference": "two-sided, cluster-robust by 2-day UTC block",
    "min_effect": MIN_EFFECT, "fdr_q": FDR_Q, "family1": TEST_KEYS, "family2": "zone minus mid-range control, 4 ranges x {half, deep}", "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# observations and ranges
# ---------------------------------------------------------------------------
def _swings(ns1, h1, l1):
    """Confirmed fractal swings on the 1h series (in this context's coordinates). Returns (high_idx, high_conf_ns, low_idx, low_conf_ns); conf = end of the confirming bar."""
    n, k = len(ns1), SWING_K
    hi, lo = [], []
    for j in range(k, n - k):
        if int(ns1[j + k] - ns1[j - k]) != 2 * k * NS_H:
            continue
        w_h, w_l = h1[j - k:j + k + 1], l1[j - k:j + k + 1]
        if h1[j] > np.max(np.delete(w_h, k)):
            hi.append(j)
        if l1[j] < np.min(np.delete(w_l, k)):
            lo.append(j)
    hi, lo = np.array(hi, int), np.array(lo, int)
    return hi, ns1[hi + k] + NS_H if len(hi) else np.zeros(0, np.int64), lo, ns1[lo + k] + NS_H if len(lo) else np.zeros(0, np.int64)


def build_obs(ctx):
    """Per-range positions and mean deviations at every grid bar. Uses only bars up to and including the decision bar. Returns (obs, counts)."""
    ns5, h, l, c, atr = ctx["ns5"], ctx["A"]["h5"], ctx["A"]["l5"], ctx["A"]["c5"], ctx["atr_t"]
    n = len(ns5)
    grid = np.flatnonzero(((ns5 + NS_5) % GRID_NS == 0) & np.isfinite(atr) & (atr > 0))
    obs = {r: {"p": [], "pos": []} for r in RANGES}
    obs.update({m: {"p": [], "pos": []} for m in MEANS})
    cnt = {r: {"grid": int(len(grid)), "no_range": 0, "width_out": 0, "outside_range": 0, "usable": 0} for r in RANGES}
    cnt.update({m: {"grid": int(len(grid)), "no_range": 0, "width_out": 0, "outside_range": 0, "usable": 0} for m in MEANS})
    # previous-day boxes (Study F definition)
    boxes, _ = sf.build_boxes(ctx)
    bs = np.array([b["s"] for b in boxes], int)
    be = np.array([b["e"] for b in boxes], int)
    # rolling extremes through cumulative arrays are not needed: slices are short (<= 1440 bars) and the grid is sparse
    cs = np.concatenate([[0.0], np.cumsum(c)])
    hi_i, hi_c, lo_i, lo_c = _swings(ctx["ns1"], ctx["A"]["h1"], ctx["A"]["l1"])
    for p in grid:
        a = float(atr[p])
        cp = float(c[p])
        # previous-day box
        k = int(np.searchsorted(bs, p, side="right")) - 1 if len(bs) else -1
        if k >= 0 and p < be[k]:
            b = boxes[k]
            W = b["pdh"] - b["pdl"]
            if not (W_ATR[0] <= W / a <= W_ATR[1]):
                cnt["prev"]["width_out"] += 1
            else:
                pos = (cp - b["pdl"]) / W
                if 0.0 <= pos <= 1.0:
                    obs["prev"]["p"].append(int(p)); obs["prev"]["pos"].append(float(pos)); cnt["prev"]["usable"] += 1
                else:
                    cnt["prev"]["outside_range"] += 1
        else:
            cnt["prev"]["no_range"] += 1
        # rolling windows
        for r, (nb, span) in ROLL.items():
            if p + 1 < nb or int(ns5[p] - ns5[p + 1 - nb]) > span:
                cnt[r]["no_range"] += 1
                continue
            H, L = float(h[p + 1 - nb:p + 1].max()), float(l[p + 1 - nb:p + 1].min())
            W = H - L
            if not (W_ATR[0] <= W / a <= W_ATR[1]):
                cnt[r]["width_out"] += 1
                continue
            pos = (cp - L) / W
            if 0.0 <= pos <= 1.0:
                obs[r]["p"].append(int(p)); obs[r]["pos"].append(float(pos)); cnt[r]["usable"] += 1
            else:
                cnt[r]["outside_range"] += 1
        # latest confirmed swing pair
        tc = int(ns5[p]) + NS_5
        ih, il = int(np.searchsorted(hi_c, tc, side="right")) - 1, int(np.searchsorted(lo_c, tc, side="right")) - 1
        if ih < 0 or il < 0 or tc - int(ctx["ns1"][hi_i[ih]]) > SWING_MAX_AGE or tc - int(ctx["ns1"][lo_i[il]]) > SWING_MAX_AGE:
            cnt["swing"]["no_range"] += 1
        else:
            SH, SL = float(ctx["A"]["h1"][hi_i[ih]]), float(ctx["A"]["l1"][lo_i[il]])
            W = SH - SL
            if W <= 0 or not (W_ATR[0] <= W / a <= W_ATR[1]):
                cnt["swing"]["width_out"] += 1
            else:
                pos = (cp - SL) / W
                if 0.0 <= pos <= 1.0:
                    obs["swing"]["p"].append(int(p)); obs["swing"]["pos"].append(float(pos)); cnt["swing"]["usable"] += 1
                else:
                    cnt["swing"]["outside_range"] += 1
        # mean deviation
        for m, nb in MEANS.items():
            span = ROLL["r24" if nb == 288 else "r5d"][1]
            if p + 1 < nb or int(ns5[p] - ns5[p + 1 - nb]) > span:
                cnt[m]["no_range"] += 1
                continue
            dev = (cp - (cs[p + 1] - cs[p + 1 - nb]) / nb) / a
            obs[m]["p"].append(int(p)); obs[m]["pos"].append(float(dev)); cnt[m]["usable"] += 1
    for v in obs.values():
        v["p"], v["pos"] = np.array(v["p"], int), np.array(v["pos"], float)
    return obs, cnt


def ctx_h(s5, s1h, sign, hold_ns):
    ctx = sb.make_ctx(s5, s1h, sign, hold_ns)
    ctx["obs"], ctx["obs_cnt"] = build_obs(ctx)
    return ctx


def _gate():
    return {"detected": 0, "outside_region": 0, "window": 0, "no_trail": 0}


def _event(ctx, p, rname, pos):
    return {"p": int(p), "t": int(ctx["ns5"][p]), "x": float(ctx["x"][p]), "side": ctx["sign"], "atr": float(ctx["atr_t"][p]), "f": float(pos), "sess": rname, "frac": float(pos)}


def events_for(ctx, key, region="dev"):
    cnt = _gate()
    parts = key.split(":")
    if parts[0] == "C" and ctx["sign"] < 0:
        return [], cnt                # the middle band is its own mirror image: counting both orientations would force exactly 50% by construction
    if parts[0] == "C":
        rname, sel = parts[1], lambda pos: (pos >= MID[0]) & (pos <= MID[1])
    elif parts[1] in RANGES:
        rname, thr = parts[1], ZONES[parts[2]]
        sel = lambda pos, thr=thr: pos >= thr
    else:
        rname, sel = parts[1], lambda pos: pos >= DEV_ATR
    o = ctx["obs"][rname]
    out = []
    for p, pos in zip(o["p"][sel(o["pos"])], o["pos"][sel(o["pos"])]):
        cnt["detected"] += 1
        if sf._admit(ctx, int(p), region, cnt):
            out.append(_event(ctx, int(p), rname, pos))
    return out, cnt


# ---------------------------------------------------------------------------
# scoring, summaries, inference
# ---------------------------------------------------------------------------
def collect(s5, s1h, hold_ns, keys=None):
    keys = ALL_KEYS if keys is None else keys
    ctxs = [ctx_h(s5, s1h, +1, hold_ns), ctx_h(s5, s1h, -1, hold_ns)]
    rows, audit = {}, {}
    for tag, key in enumerate(keys, 1):
        au = {"used": 0, "unresolved": 0, "ambiguous": 0, "no_placebo": 0}
        au.update(_gate())
        evs = []
        for ctx in ctxs:
            ev, cnt = events_for(ctx, key, "dev")
            for kk, v in cnt.items():
                au[kk] += v
            evs += [(ctx, e) for e in ev]
        rows[key] = sg._score(evs, tag, au)
        audit[key] = au
    return {"rows": rows, "audit": audit, "ranges": {r: {k: sum(ctx["obs_cnt"][r][k] for ctx in ctxs[:1]) for k in ctxs[0]["obs_cnt"][r]} for r in list(RANGES) + list(MEANS)}}


def contrasts(R):
    out = {}
    for k in TEST_KEYS:
        r = k.split(":")[1]
        if r in RANGES:
            out["X:" + k] = sg._diff(R[k], R[f"C:{r}:mid"])
    return out


def _apply_fdr(res, status):
    fam = {1: [("T", k) for k in TEST_KEYS], 2: [("X", k) for k in SPEC_KEYS]}
    counts = {}
    for f, tests in fam.items():
        live = [p for p in tests if res[p[0]][p[1]].get("p") is not None]
        rej, q = w1.bh_fdr([res[p[0]][p[1]]["p"] for p in live]) if live else ([], [])
        counts[f] = len(live)
        for p, rj, qq in zip(live, rej, q):
            r = res[p[0]][p[1]]
            est = "diff" if p[0] == "X" else "mean"
            nmin = r.get("n_min", 0) if p[0] == "X" else r.get("n", 0)
            r["family"], r["fdr_reject"], r["q_value"] = f, bool(rj), float(qq)
            r["verdict"] = verdict(r[est], r["ci"][0], r["ci"][1], bool(rj), nmin, status if f == 1 else "EXPLORATORY_READY", MIN_EFFECT)
        for p in tests:
            if res[p[0]][p[1]].get("p") is None:
                res[p[0]][p[1]]["verdict"] = "DATA-LIMITED"
    return counts


NAMES = {"half": "H.1 premium/discount (upper/lower half)", "deep": "H.2 deep zone (upper/lower fifth)"}
RN = {"prev": "previous-day range", "r24": "24h range", "r5d": "5-day range", "swing": "1h swing range", "m24": "24h mean", "m5d": "5-day mean"}


def label(key):
    p = key.split(":")
    if p[1] in MEANS:
        return f"H.3 deviation from the {RN[p[1]]} (>= 1.5 ATR)"
    return f"{NAMES[p[2]]} · {RN[p[1]]}"


def reading(res):
    s = [(k, res["T"][k]) for k in TEST_KEYS if res["T"][k].get("fdr_reject")]
    if not s:
        out = ["No premium/discount, deep-zone or mean-deviation test survives multiple-testing control: no demonstrated information about the next move beyond the clock slot and the recent move."]
    else:
        parts = []
        for k, v in s:
            side = "FADE side (price moved back toward the middle more than placebo)" if v["mean"] < 0 else "CONTINUATION side (the OPPOSITE of the buy-low/sell-high claim)"
            x = res["X"].get("X:" + k, {})
            sp = ("; the effect is larger than at the mid-range control (survives)" if x.get("fdr_reject") else "; NOT shown to differ from the mid-range control, so the position itself is not shown to be what carries it") if k.split(":")[1] in RANGES else ""
            parts.append(f"{label(k)}: excess {v['mean']:+.3f} ({side}; observed {100 * v['obs_rate']:.1f}% vs placebo {100 * v['placebo_rate']:.1f}%, q={v['q_value']:.3f}){sp}")
        out = ["Survives multiple-testing control: " + " | ".join(parts) + "."]
    x = [k for k in SPEC_KEYS if res["X"][k].get("fdr_reject")]
    out.append("Zone vs mid-range control (family 2): " + (", ".join(x) + " survive(s) (see the table)." if x else "no difference survives."))
    out.append("The four range definitions overlap and the zones are nested: agreement between rows is NOT independent confirmation. Also overlaps Studies D, F and G.")
    return " ".join(out)


def run_from_series(s5, s1h, status, hold_ns):
    col = collect(s5, s1h, hold_ns)
    R = col["rows"]
    res = {"T": {k: sg.summarize(R[k], True) for k in TEST_KEYS}, "ctrl": {k: sg.summarize(R[k], True) for k in CTRL_KEYS}, "X": contrasts(R)}
    res["n_tests"] = _apply_fdr(res, status)
    res["audit"], res["ranges"] = col["audit"], col["ranges"]
    res["reading"] = reading(res)
    res.update({"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False})
    return res


def run_studyh(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    res = run_from_series(s5, s1h, status, hold_ns)
    res["development"] = {"first_bar_utc": str(s5.index[0]), "boundary_utc": str(rc.HOLDOUT_START_UTC)}
    return res


def structure_counts(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    """Structure only: no outcome is computed."""
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    ctxs = [ctx_h(s5, s1h, +1, hold_ns), ctx_h(s5, s1h, -1, hold_ns)]
    out = {"dataset_status": status, "bars_5m": len(s5.index), "ranges": ctxs[0]["obs_cnt"], "tests": {}}
    for key in ALL_KEYS:
        row = {}
        for region in ("dev", "hold"):
            n, cn = 0, _gate()
            for ctx in ctxs:
                ev, cnt = events_for(ctx, key, region)
                n += len(ev)
                for kk, v in cnt.items():
                    cn[kk] += v
            row[region] = {"events": n, "gates": cn}
        out["tests"][key] = row
    return out


def holdout_readiness(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None, dev_res=None):
    if dev_res is None:
        dev_res = run_studyh(raw_dir, source, preloaded)
    cnt = structure_counts(raw_dir, source, preloaded)
    s5, _, _ = sa.load_series(raw_dir, source, preloaded)
    months = max((s5.index[-1] - rc.HOLDOUT_START_UTC).days / 30.44, 0.0)
    out = {"holdout_from": str(rc.HOLDOUT_START_UTC)[:10], "holdout_months": round(months, 2), "tests": {}}
    for key, t in dev_res["T"].items():
        nh = cnt["tests"][key]["hold"]["events"]
        if t.get("mean") is None:
            continue
        row = {"dev_effect": round(t["mean"], 4), "dev_q": t.get("q_value"), "survived_dev_fdr": bool(t.get("fdr_reject")), "holdout_events": nh}
        if t.get("se") and nh > 0 and t["n"] > 0 and months > 0:
            se_h = t["se"] * math.sqrt(t["n"] / nh)
            for nm, eff in (("full", abs(t["mean"])), ("half", abs(t["mean"]) / 2)):
                row[nm] = {"power_now": round(_phi(eff / se_h - 1.96), 2), "months_needed_for_80pct": round(months * (se_h / (eff / (1.96 + 0.842))) ** 2, 1) if eff > 0 else None}
            row["ready_to_open"] = bool(row["survived_dev_fdr"] and row["half"]["power_now"] >= 0.80)
        out["tests"][key] = row
    out["note"] = ("Only tests that survived the development FDR are candidates for the holdout; the rest are listed for power context (an estimate near zero gives huge 'months needed': "
                   "ignore those). The holdout stays sealed until a candidate's half-effect power is >= 0.80.")
    return out


def detectable(res):
    """Smallest true effect detectable with 80% power (two-sided 5%) given each test's cluster-robust standard error. No simulation: the event overlap makes planted-effect simulation misleading."""
    return {k: round(2.8 * t["se"], 4) for k, t in res["T"].items() if t.get("se")}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def render_markdown(r):
    n1, n2 = r["n_tests"][1], r["n_tests"][2]
    L = ["# Observatory — Study H: buy low / sell high range definitions (GBPUSD)\n",
         f"Contract `{r['contract_hash']}` · dataset **{r['dataset_status']}** · holdout opened: **False** · family 1: {n1} tests · family 2: {n2} tests\n",
         "**Development data only. Claim: price in the upper part of a range falls and in the lower part rises (the lower half/fifth is the mirror image). Four range definitions "
         "(previous day, 24h, 5 days, 1h swings) and two zone sizes (>= 0.5, >= 0.8 of the range) plus distance from a rolling mean (>= 1.5 ATR). 4-hour observation grid; price must be inside the range. "
         "Outcome = +1 ATR before -1 ATR within 24h minus a placebo with the same weekday, hour and recent move (negative excess = fade). Two-sided, 2-day-block clustered. Nothing here is a trading rule.**\n",
         "## Reading (pre-declared)\n", f"**{r['reading']}**\n"]
    hd = ("| test | events | observed | placebo | excess | 95% CI | z | q | pips/event | setup net (pips) | placebo net | verdict |\n|---|---|---|---|---|---|---|---|---|---|---|---|")

    def trow(lab, t):
        if t.get("mean") is None:
            return f"| {lab} | {t.get('n', 0)} | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | **{t.get('verdict', 'DATA-LIMITED')}** |"
        return (f"| {lab} | {t['n']} | {100 * t['obs_rate']:.1f}% | {100 * t['placebo_rate']:.1f}% | {t['mean']:+.3f} | [{t['ci'][0]:+.3f}, {t['ci'][1]:+.3f}] | {t['z']:+.2f} | "
                f"{_p(t.get('q_value'))} | {t['excess_pips']:+.2f} | {t['setup_net_pips']:+.2f} | {t['placebo_net_pips']:+.2f} | **{t.get('verdict', '')}** |")
    L += ["## H.1 / H.2 / H.3 (family 1)\n", hd] + [trow(label(k), r["T"][k]) for k in TEST_KEYS]
    L += ["\n## Controls: the MIDDLE of the same range (0.4-0.6, one orientation; not tested; for context)\n", hd] + [trow(f"mid-range · {RN[k.split(':')[1]]}", r["ctrl"][k]) for k in CTRL_KEYS]
    L += ["\n## Zone minus mid-range control (family 2)\n", "| test | n (zone / mid) | difference | 95% CI | z | q | verdict |\n|---|---|---|---|---|---|---|"]
    for k in SPEC_KEYS:
        t = r["X"][k]
        lab = label(k[2:]) + " − mid-range"
        if t.get("diff") is None:
            L.append(f"| {lab} | {t.get('n1', 0)} / {t.get('n0', 0)} | n/a | n/a | n/a | n/a | **{t.get('verdict', 'DATA-LIMITED')}** |")
        else:
            L.append(f"| {lab} | {t['n1']} / {t['n0']} | {t['diff']:+.3f} | [{t['ci'][0]:+.3f}, {t['ci'][1]:+.3f}] | {t['z']:+.2f} | {_p(t.get('q_value'))} | **{t.get('verdict', '')}** |")
    L += ["\n## Expansion and raw 24h path (descriptive; no stop, no target)\n",
          "Forward 24h range, maximum rise (toward/through the extreme) and maximum fall (away from it) in ATR, events versus matched placebo bars.\n",
          "| group | range (event) | range (placebo) | range excess | rise excess | fall excess |\n|---|---|---|---|---|---|"]
    for k in TEST_KEYS + CTRL_KEYS:
        t = (r["T"] if k in TEST_KEYS else r["ctrl"])[k]
        p = t.get("path")
        if not p:
            continue

        def e(x):
            return f"{_a(x.get('mean'))} [{_a(x['ci'][0]) if x.get('ci') else 'n/a'}, {_a(x['ci'][1]) if x.get('ci') else 'n/a'}]"
        L.append(f"| {k} | {p['event_range_atr']:.3f} | {p['placebo_range_atr']:.3f} | {e(p['range_excess'])} | {e(p['rise_excess'])} | {e(p['fall_excess'])} |")
    L += ["\n## Upper (premium) versus lower (discount) side, and halves of the sample (descriptive)\n", "| test | upper side excess (n) | lower side excess (n) | first half | second half |\n|---|---|---|---|---|"]
    for k in TEST_KEYS:
        t = r["T"][k]
        if t.get("mean") is None:
            continue
        s, h = t.get("sides", {}), t.get("halves", {})

        def c(x):
            return f"{x['mean']:+.3f} ({x['n']})" if x and x.get("mean") is not None else "n/a"
        L.append(f"| {k} | {c(s.get('up'))} | {c(s.get('down'))} | {c(h.get('first_half'))} | {c(h.get('second_half'))} |")
    dm = detectable(r)
    L += ["\n## Smallest effect detectable with 80% power (2.8 × cluster standard error; probability points)\n", "| test | detectable effect |\n|---|---|"] + [f"| {k} | {100 * v:.1f} |" for k, v in dm.items()]
    L += ["\n## Audit: range and observation gates (per orientation: the upper side; the lower side is symmetric)\n",
          "| range | grid bars | no range | width outside 1.5-60 ATR | close outside the range | usable |\n|---|---|---|---|---|---|"]
    for k, v in r["ranges"].items():
        L.append(f"| {k} | {v['grid']} | {v['no_range']} | {v['width_out']} | {v['outside_range']} | {v['usable']} |")
    L += ["\n| test | detected | outside region | window | no trail | ambiguous | no placebo | scored |\n|---|---|---|---|---|---|---|---|"]
    for k, a in r["audit"].items():
        L.append(f"| {k} | {a['detected']} | {a['outside_region']} | {a['window']} | {a['no_trail']} | {a['ambiguous']} | {a['no_placebo']} | {a['used']} |")
    L.append("\n## Not run\n- Holdout (sealed). Other zone thresholds, grid spacing and range lengths (fixed, not tuned). Order blocks / fair-value gaps / liquidity pools (not defined here). Stops, costs, order flow, volume. A planted-effect power simulation (overlapping events make it misleading; the detectable-effect table above uses the real standard errors).\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _synth_path(n5, rng, kappa, sigma=0.00035, mom=0.0):
    """A continuous 24/7 5m world. kappa > 0: price is an AR(1) around a drifting anchor (mean-reverting). mom > 0: persistent trends (a slow AR(0.99) drift added to every step)."""
    z = np.zeros(n5)
    anchor = 1.30 + np.cumsum(rng.normal(0, sigma * 0.15, n5))
    v = 0.0
    walk = np.zeros(n5)
    for i in range(1, n5):
        z[i] = z[i - 1] * (1 - kappa) + rng.normal(0, sigma)
        v = 0.99 * v + rng.normal(0, sigma * mom)
        walk[i] = walk[i - 1] + v
    close = anchor + z + walk
    o = np.concatenate([[1.30], close[:-1]])
    pad = np.abs(rng.normal(0, sigma * 0.5, n5))
    idx = pd.date_range(pd.Timestamp("2026-01-05 00:00", tz="UTC"), periods=n5, freq="5min")
    df = pd.DataFrame({"open": o, "high": np.maximum(o, close) + pad, "low": np.minimum(o, close) - pad, "close": close}, index=idx)
    df["gap_before_missing"], df["closure_before"] = 0, False
    return df


def _ctxs(df, hold=None):
    s5, s1h = sa._series_from_df(df)
    far = pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value if hold is None else hold
    return s5, s1h, [ctx_h(s5, s1h, +1, far), ctx_h(s5, s1h, -1, far)]


def selftest(null_series=5):
    fails, n = [], 0

    def chk(label_, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label_}: got {got!r}, want {want!r}")
    rng = np.random.default_rng(81)
    df = sb._synth_ohlc(90000, rng)
    s5, s1h = sa._series_from_df(df)
    ns5 = _ns(s5.index)
    far = pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value
    cp, cm = ctx_h(s5, s1h, +1, far), ctx_h(s5, s1h, -1, far)
    h, l, c = np.asarray(s5.h), np.asarray(s5.l), np.asarray(s5.c)
    # grid and independent re-derivation of the 24h / 5d range positions with pandas rolling
    chk("grid: every observation bar closes on a 4h boundary", bool(all((ns5[p] + NS_5) % GRID_NS == 0 for p in cp["obs"]["r24"]["p"][:200])), True)
    for r, nb in (("r24", 288), ("r5d", 1440)):
        hr = pd.Series(h).rolling(nb).max().to_numpy()
        lr = pd.Series(l).rolling(nb).min().to_numpy()
        o = cp["obs"][r]
        want = (c[o["p"]] - lr[o["p"]]) / (hr[o["p"]] - lr[o["p"]])
        chk(f"{r}: position equals pandas' rolling (close-low)/(high-low) on {len(o['p'])} observations (synthetic world is continuous, so the span rule never binds)", (len(o["p"]) > 300, bool(np.max(np.abs(want - o["pos"])) < 1e-9)), (True, True))
        chk(f"{r}: every position is inside [0, 1]", bool(((o["pos"] >= 0) & (o["pos"] <= 1)).all()), True)
    # mean deviation equals pandas rolling mean
    mu = pd.Series(c).rolling(288).mean().to_numpy()
    o = cp["obs"]["m24"]
    chk("m24: deviation equals (close - rolling 288-bar mean) / ATR", bool(np.max(np.abs(o["pos"] - (c[o["p"]] - mu[o["p"]]) / cp["atr_t"][o["p"]])) < 1e-9), True)
    # swing definition by hand: a fractal high on a hand-built 1h series is confirmed only after two more bars
    hh = np.array([1.0, 1.1, 1.3, 1.2, 1.15, 1.2, 1.1, 1.0, 1.0, 1.0])
    ll = hh - 0.1
    ll[4] = 0.8
    ns1 = (np.arange(len(hh)) * NS_H).astype(np.int64)
    hi_i, hi_c, lo_i, lo_c = _swings(ns1, hh, ll)
    chk("swing high at index 2 (higher than 2 before and 2 after), confirmed at the END of bar 4, no other high", (hi_i.tolist(), (hi_c // NS_H).tolist()), ([2], [5]))
    chk("swing low: bar 4 (1.05) is lower than 2 each side", lo_i.tolist(), [4])
    # mirror: the lower side is exactly the upper side of the negated series
    dn = df.copy()
    dn["open"], dn["close"], dn["high"], dn["low"] = -df["open"], -df["close"], -df["low"], -df["high"]
    s5n, s1n = sa._series_from_df(dn)
    cpn = ctx_h(s5n, s1n, +1, far)
    chk("mirror: positions in the lower context equal positions of the negated frame in the upper context",
        (cm["obs"]["r24"]["p"].tolist() == cpn["obs"]["r24"]["p"].tolist(), bool(np.max(np.abs(cm["obs"]["r24"]["pos"] - cpn["obs"]["r24"]["pos"])) < 1e-9)), (True, True))
    # no look-ahead: change every bar after p0; observations at or before p0 are unchanged (all ranges, including the swing)
    p0 = int(cp["obs"]["swing"]["p"][len(cp["obs"]["swing"]["p"]) // 2])
    df2 = df.copy()
    for col in ("open", "high", "low", "close"):
        df2.iloc[p0 + 1:, df2.columns.get_loc(col)] += 0.3
    s52, s1h2 = sa._series_from_df(df2)
    cp2 = ctx_h(s52, s1h2, +1, far)
    same = True
    for r in list(RANGES) + list(MEANS):
        a_, b_ = cp["obs"][r], cp2["obs"][r]
        ka, kb = a_["p"] <= p0, b_["p"] <= p0
        same &= a_["p"][ka].tolist() == b_["p"][kb].tolist() and bool(np.allclose(a_["pos"][ka], b_["pos"][kb]))
    chk("no look-ahead: positions of observations at or before an altered future are identical for every range", bool(same), True)
    # zone semantics
    ev_h, _ = events_for(cp, "H:r24:half")
    ev_d, _ = events_for(cp, "H:r24:deep")
    ev_c, _ = events_for(cp, "C:r24:mid")
    chk("zones: half events all have pos >= 0.5, deep >= 0.8 and are a subset of half, control inside 0.4-0.6",
        (all(e["f"] >= 0.5 for e in ev_h), all(e["f"] >= 0.8 for e in ev_d), {e["p"] for e in ev_d} <= {e["p"] for e in ev_h}, all(0.4 <= e["f"] <= 0.6 for e in ev_c), len(ev_d) > 50), (True, True, True, True, True))
    # region sealing
    mid_ns = int(ns5[45000])
    cpm = ctx_h(s5, s1h, +1, mid_ns)
    dev, _ = events_for(cpm, "H:r24:half", "dev")
    hold, _ = events_for(cpm, "H:r24:half", "hold")
    chk("development events end their outcome window before the boundary; holdout events start at or after it", (all(e["t"] + (HMAX + 1) * NS_5 <= mid_ns for e in dev), all(e["t"] >= mid_ns for e in hold), len(hold) > 20), (True, True, True))
    col = collect(s5, s1h, mid_ns)
    chk("no scored row touches the holdout", all(r["t"] + (HMAX + 1) * NS_5 <= mid_ns for rows in col["rows"].values() for r in rows), True)
    # null calibration of the full pipeline on random walks (with and without clustered volatility)
    ps, zs = [], []
    for kk in range(null_series):
        d_ = sb._synth_ohlc(110000, rng, garch=(kk % 2 == 1))
        s5_, s1_ = sa._series_from_df(d_)
        r_ = run_from_series(s5_, s1_, "PRIMARY_READY", far)
        for t_ in list(r_["T"].values()) + list(r_["X"].values()):
            if t_.get("p") is not None:
                ps.append(t_["p"])
                zs.append(t_["z"])
    ps = np.asarray(ps)
    chk(f"null: enough tests ({len(ps)})", len(ps) >= 60, True)
    if len(ps) >= 60:
        chk(f"null: share of two-sided p < 0.05 is at most 14% (got {(ps < 0.05).mean():.1%}); tests overlap, so the share is noisy", (ps < 0.05).mean() <= 0.14, True)
        chk(f"null: mean z is near zero (got {np.mean(zs):+.2f})", abs(np.mean(zs)) < 0.6, True)
    # planted worlds: mean-reverting prices must show the FADE sign, trending prices the CONTINUATION sign, with a strong z
    out_pl = {}
    for name, kappa, mom, want in (("mean-reverting", 0.02, 0.0, -1), ("trending", 0.0, 0.015, 1)):
        d_ = _synth_path(100000, np.random.default_rng(7 if want < 0 else 8), kappa, mom=mom)
        s5_, s1_ = sa._series_from_df(d_)
        r_ = run_from_series(s5_, s1_, "PRIMARY_READY", far)
        t_ = r_["T"]["H:r24:half"]
        out_pl[name] = {"mean": t_.get("mean"), "z": t_.get("z"), "n": t_.get("n")}
        chk(f"planted {name} world: H:r24:half has the {'FADE' if want < 0 else 'CONTINUATION'} sign with |z| >= 3 (got z={t_.get('z', float('nan')):+.2f}, excess {t_.get('mean', float('nan')):+.3f})",
            t_.get("z") is not None and want * t_["z"] >= 3.0, True)
    rP = run_from_series(s5, s1h, "PRIMARY_READY", far)
    md = render_markdown(rP)
    chk("report renders and JSON serialises", (len(md) > 3000, len(json.dumps(rP, default=str)) > 3000), (True, True))
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": null_series, "tests": int(len(ps)), "share_p_lt_0.05": round(float((ps < 0.05).mean()), 3) if len(ps) else None,
                     "share_p_lt_0.01": round(float((ps < 0.01).mean()), 3) if len(ps) else None, "mean_z": round(float(np.mean(zs)), 3) if zs else None}, "planted": out_pl}


def main():
    ap = argparse.ArgumentParser(description="Observatory Study H (buy low / sell high range definitions). Read-only on raw data; the holdout stays sealed.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--counts", action="store_true", help="range and event counts only (no outcome is computed)")
    ap.add_argument("--readiness", action="store_true", help="holdout event counts and power from development estimates; no holdout outcome is read")
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
    res = run_studyh(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "studyh_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "studyh_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
