"""
observatory/studyf.py
=====================
OBSERVATORY — STUDY F: the previous-day box (previous UTC day's high and low). ISOLATED from the live bot.

Tests inside this study are F.1, F.2, F.3. The honest history behind it: the trader's own early version was "buy at the bottom of yesterday's range, sell at the top",
entering at or very near the boundary, later adding a 1h bias, trend-line breaks and retests, and supply/demand zones. Only the parts that can be stated objectively are
tested here; trend lines and supply/demand zones have no measure and are NOT tested. Nothing is tuned: every definition below is fixed before any outcome is read.

THE BOX (previous trading day)
  Day = UTC calendar day (cutoff 00:00 UTC; the data are UTC, so there is no daylight-saving ambiguity). Box for day D = highest high and lowest low of the PREVIOUS TRADING DAY (the
  latest earlier day with >= 240 of its 288 5m bars and no gap over 1h, at most 3 calendar days back, so Monday uses Friday), known at 00:00 of day D. A UTC Sunday (only the last two
  hours of the Sunday-evening open) is not an event day. A box is used only if its width W is between 1.5 and 15 ATR(1h) at the start of day D. The low boundary is the exact mirror of the
  high boundary (prices negated) and both are pooled.

F.1  APPROACH TO THE BOUNDARY: fade or break?      (family 1: 3 tests, two-sided)
  Event = first 5m bar of day D whose high reaches level_f = PDH - f*W (f = 0 "touch", 0.10, 0.20: "within 10% / 20% of the box width"), where the previous close was below the level and
  the bar closes no more than 0.05*W above it (a bar that blows through the level in one go is counted as 'crossed through', not an approach). One event per day per side per f.
  Outcome (exactly as Studies B/C/D): +1 ATR (through the boundary) before -1 ATR (away from it) within 24h of the decision close; excess = outcome - placebo (30 random 5m closes
  with the same weekday, hour +-1h and trailing-1h move, >= 24h away). The same events answer both claims: excess < 0 is the FADE (reversal), excess > 0 is the BREAKOUT
  (continuation through). Specificity (family 2): the same event definition at the mid-box level (f = 0.5) is the control; boundary - control tests whether the boundary matters.

F.2  1h BIAS FILTER                                (family 2: 3 tests, two-sided)
  Bias = last completed 1h close above (bullish) or below (bearish) its 50-period EMA, fixed before any outcome is seen. For a high-boundary event the bias that SUPPORTS the fade is
  bearish; for a low-boundary event, bullish (the mirror handles this). Test = excess(bias supports the fade) - excess(bias opposes it), at each f, cluster SE. Filtered is compared
  with the SAME events unfiltered, so a filter that only removes trades cannot look like a filter that helps.

F.3  BREAK AND RETEST                              (family 1: 1 test, two-sided; control in family 2)
  Break = first 5m close >= PDH + 0.5 ATR on day D. Retest = first later bar (within 16h) whose low is <= PDH + 0.05 ATR, with no close <= PDH - 0.5 ATR in between and on the bar itself
  (structure intact). Decision = close of the retest bar. Claim = bounce (excess > 0). Control: the same construct at a level 0.75 ATR above PDH (an ordinary level at a comparable
  distance, not a prior extreme).

DESCRIPTIVE ONLY (not tested, no FDR): the raw 24h forward path of the events against the matched placebo bars: mean maximum rise and maximum fall in ATR (no stop, no target), the
up-side vs down-side split, halves. These say what the path looks like independent of any stop-loss or take-profit choice.

FAMILIES (BH-FDR q = 0.10 inside each)
  family 1 (primary): F.1 touch / 10% / 20%, F.3 retest (4 tests).   family 2: F.1 boundary vs mid-box control x3, F.3 vs control level, F.2 bias contrast x3 (7 tests).
Practical threshold: a difference of 0.05 in outcome probability (5 points), as in Studies B-D. The holdout (>= HOLDOUT_START_UTC) is sealed: no outcome is computed for it.
Overlap ledger: F.1 (f = 0) and F.3 are built from the same previous-day extremes and often fire on the same days; they are NOT independent confirmations of each other.
Reaching a barrier is not a profitable trade; 'setup net' pips are descriptive (flat spread, +-1 ATR symmetric exits) and no stop/target is being recommended.

Run:  python3 -m observatory.studyf [--raw-dir ...] [--out ...]   --selftest   --counts   --readiness   --power
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
from . import studyd as sd
from .studyb import barrier, window_ok, trail_ok, draw_placebo, NS_5, NS_D, NS_H, HMAX, PIP, SPREAD_PIPS, FDR_Q, SEED
from .studya import verdict, _ns, _phi, _a, _ci, _p, _pc, ATR_STALE
from .studyc import cluster_test, cluster_diff, MIN_EFFECT

LEVELS = (0.0, 0.10, 0.20)
CTRL_F = 0.5
CLOSE_TOL = 0.05
BOX_MIN_BARS, BOX_MAX_GAP = 240, 12
PREV_LOOKBACK_DAYS = 3
W_ATR = (1.5, 15.0)
EXC, RT_TOL, RT_WAIT, R_CTRL_OFF = 0.5, 0.05, 192, 0.75
EMA_SPAN, EMA_WARM = 50, 100
A_KEYS = {"A:0": 0.0, "A:0.1": 0.10, "A:0.2": 0.20, "A:mid": CTRL_F}
R_KEYS = {"R:real": 0.0, "R:ctrl": R_CTRL_OFF}
TEST_KEYS = ["A:0", "A:0.1", "A:0.2", "R:real"]
CTRL_KEYS = ["A:mid", "R:ctrl"]
ALL_KEYS = TEST_KEYS + CTRL_KEYS
SPEC_KEYS = ["X:A:0", "X:A:0.1", "X:A:0.2", "X:R:real", "Bias:A:0", "Bias:A:0.1", "Bias:A:0.2"]
CLAIM = {"A": "fade", "R": "bounce"}
CONTRACT = {
    "study": "F", "module": "studyf", "version": "v1", "instrument": "GBPUSD", "box": "previous trading day high/low (cutoff 00:00 UTC; Monday uses Friday)",
    "box_filters": {"prev_trading_day_lookback_days": PREV_LOOKBACK_DAYS, "skip_utc_sunday": True, "min_bars": BOX_MIN_BARS, "max_gap_bars": BOX_MAX_GAP, "width_atr": list(W_ATR)},
    "approach": {"levels_f": list(LEVELS), "control_f": CTRL_F, "close_tol_W": CLOSE_TOL, "events_per_day": "first approach per side"},
    "retest": {"break_atr": EXC, "retest_tol_atr": RT_TOL, "wait_bars": RT_WAIT, "control_offset_atr": R_CTRL_OFF},
    "bias": {"ema_span_1h": EMA_SPAN, "rule": "1h close vs EMA; supports fade = against the approach direction"},
    "outcome": "+-1 ATR first passage within 24h of the decision close, minus placebo (same weekday, hour +-1h, trailing 1h move)",
    "inference": "two-sided, cluster-robust by 2-day UTC block", "min_effect": MIN_EFFECT, "fdr_q": FDR_Q,
    "family1": ["F.1 touch", "F.1 10%", "F.1 20%", "F.3 retest"], "family2": ["F.1 boundary vs mid-box x3", "F.3 vs control level", "F.2 bias contrast x3"],
    "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# context, boxes, events
# ---------------------------------------------------------------------------
def build_boxes(ctx):
    """One box per usable UTC day: previous day's extremes (in this context's coordinates), width, and ATR at the start of the day. Uses only data before the day starts."""
    ns5, h, l = ctx["ns5"], ctx["A"]["h5"], ctx["A"]["l5"]
    cnt = {"days": 0, "sunday_open": 0, "prev_day_missing": 0, "no_atr": 0, "width_out": 0, "usable": 0}
    if len(ns5) < 2:
        return [], cnt
    day = ns5 // NS_D
    starts = np.flatnonzero(np.r_[True, np.diff(day) != 0])
    ends = np.r_[starts[1:], len(ns5)]
    hi = np.maximum.reduceat(h, starts)
    lo = np.minimum.reduceat(l, starts)
    nb = ends - starts
    gap = np.array([int(np.diff(ns5[s:e]).max()) if e - s > 1 else 0 for s, e in zip(starts, ends)])
    complete = (nb >= BOX_MIN_BARS) & (gap <= BOX_MAX_GAP * NS_5)
    boxes = []
    for k in range(1, len(starts)):
        cnt["days"] += 1
        if int(sb.weekday(np.array([ns5[starts[k]]]))[0]) == 6:      # a UTC Sunday holds only the last hours of the Sunday-evening open
            cnt["sunday_open"] += 1
            continue
        j = k - 1
        while j >= 0 and day[starts[k]] - day[starts[j]] <= PREV_LOOKBACK_DAYS and not complete[j]:
            j -= 1
        if j < 0 or day[starts[k]] - day[starts[j]] > PREV_LOOKBACK_DAYS:
            cnt["prev_day_missing"] += 1
            continue
        a0 = float(ctx["atr_t"][starts[k]])
        if not (np.isfinite(a0) and a0 > 0):
            cnt["no_atr"] += 1
            continue
        W = float(hi[j] - lo[j])
        if not (W_ATR[0] <= W / a0 <= W_ATR[1]):
            cnt["width_out"] += 1
            continue
        boxes.append({"day": int(day[starts[k]]), "s": int(starts[k]), "e": int(ends[k]), "pdh": float(hi[j]), "pdl": float(lo[j]), "W": W, "a0": a0, "box_day": int(day[starts[j]])})
        cnt["usable"] += 1
    return boxes, cnt


def ctx_f(s5, s1h, sign, hold_ns):
    ctx = sb.make_ctx(s5, s1h, sign, hold_ns)
    ema = pd.Series(np.asarray(ctx["A"]["c1"], float)).ewm(span=EMA_SPAN, adjust=False).mean().to_numpy().copy()
    ema[:EMA_WARM] = np.nan
    ctx["ema1"] = ema
    ctx["boxes"], ctx["box_cnt"] = build_boxes(ctx)
    return ctx


def bias_at(ctx, p):
    """+1 bullish / -1 bearish / 0 unknown, from the last COMPLETE 1h bar at the decision time (in this context's coordinates)."""
    t = int(ctx["ns5"][p]) + NS_5
    ns1 = ctx["ns1"]
    j = int(np.searchsorted(ns1, t - NS_H, side="right")) - 1
    if j < 0 or (t - (int(ns1[j]) + NS_H)) > ATR_STALE.value or not np.isfinite(ctx["ema1"][j]):
        return 0
    c = ctx["A"]["c1"][j]
    return 1 if c > ctx["ema1"][j] else (-1 if c < ctx["ema1"][j] else 0)


def _gate():
    return {"detected": 0, "no_touch": 0, "gap": 0, "started_beyond": 0, "crossed_through": 0, "no_break": 0, "no_retest": 0, "structure_dead": 0,
            "outside_region": 0, "window": 0, "no_trail": 0}


def _admit(ctx, p, region, cnt):
    t = int(ctx["ns5"][p])
    hold = ctx["hold_ns"]
    if region == "dev":
        if t + (HMAX + 1) * sb.NS_5 > hold:
            cnt["outside_region"] += 1
            return False
        if not window_ok(ctx["ns5"], p + 1, HMAX):
            cnt["window"] += 1
            return False
    elif t < hold:
        cnt["outside_region"] += 1
        return False
    if not (np.isfinite(ctx["x"][p]) and sb.trail_ok(ctx["ns5"], p) and np.isfinite(ctx["atr_t"][p]) and ctx["atr_t"][p] > 0):
        cnt["no_trail"] += 1
        return False
    return True


def _event(ctx, p, b, f):
    return {"p": int(p), "t": int(ctx["ns5"][p]), "x": float(ctx["x"][p]), "side": ctx["sign"], "atr": float(ctx["atr_t"][p]), "f": f, "W": b["W"], "day": b["day"],
            "bias": bias_at(ctx, p)}


def detect_approach(ctx, f, region, cnt):
    ns5, h, c = ctx["ns5"], ctx["A"]["h5"], ctx["A"]["c5"]
    out = []
    for b in ctx["boxes"]:
        s, e, W = b["s"], b["e"], b["W"]
        level = b["pdh"] - f * W
        hit = np.flatnonzero(h[s:e] >= level)
        if hit.size == 0:
            cnt["no_touch"] += 1
            continue
        p = s + int(hit[0])
        if p == 0 or ns5[p] - ns5[p - 1] != NS_5:
            cnt["gap"] += 1
            continue
        if c[p - 1] >= level:
            cnt["started_beyond"] += 1
            continue
        if c[p] > level + CLOSE_TOL * W:
            cnt["crossed_through"] += 1
            continue
        cnt["detected"] += 1
        if _admit(ctx, p, region, cnt):
            out.append(_event(ctx, p, b, f))
    return out


def detect_retest(ctx, off, region, cnt):
    ns5, l, c = ctx["ns5"], ctx["A"]["l5"], ctx["A"]["c5"]
    n = len(ns5)
    out = []
    for b in ctx["boxes"]:
        s, e, a0 = b["s"], b["e"], b["a0"]
        L0 = b["pdh"] + off * a0
        exc, tol = EXC * a0, RT_TOL * a0
        hit = np.flatnonzero(c[s:e] >= L0 + exc)
        if hit.size == 0:
            cnt["no_break"] += 1
            continue
        bi = s + int(hit[0])
        end = min(bi + 1 + RT_WAIT, n)
        r_idx = np.flatnonzero(l[bi + 1:end] <= L0 + tol)
        if r_idx.size == 0:
            cnt["no_retest"] += 1
            continue
        r = bi + 1 + int(r_idx[0])
        dead = np.flatnonzero(c[bi + 1:r + 1] <= L0 - exc)
        if dead.size:
            cnt["structure_dead"] += 1
            continue
        if ns5[r] - ns5[bi] != (r - bi) * NS_5:
            cnt["gap"] += 1
            continue
        cnt["detected"] += 1
        if _admit(ctx, r, region, cnt):
            out.append(_event(ctx, r, b, off))
    return out


def events_for(ctx, key, region="dev"):
    cnt = _gate()
    kind = key.split(":")[0]
    if kind == "A":
        return detect_approach(ctx, A_KEYS[key], region, cnt), cnt
    return detect_retest(ctx, R_KEYS[key], region, cnt), cnt


# ---------------------------------------------------------------------------
# scoring and summaries
# ---------------------------------------------------------------------------
def _excursions(A, p, atr):
    c0 = A["c5"][p]
    hh, ll = A["h5"][p + 1:p + 1 + HMAX], A["l5"][p + 1:p + 1 + HMAX]
    return float((hh.max() - c0) / atr), float((c0 - ll.min()) / atr)


def score_f(ctx, e, rng, rng_pl):
    row, why = sd.score_sym(ctx, e, rng)
    if row is None:
        return None, why
    A = ctx["A"]
    up, dn = _excursions(A, e["p"], e["atr"])
    row.update(bias=e["bias"], up=up, dn=dn, f=e["f"])
    pl = draw_placebo(ctx, e["t"], e["x"], rng_pl)
    if pl is not None:
        ex = [_excursions(A, int(q), float(a)) for q, a in zip(*pl)]
        row["pup"], row["pdn"] = float(np.mean([x[0] for x in ex])), float(np.mean([x[1] for x in ex]))
    return row, "ok"


def _score(evs, tag, au):
    rows = []
    for ctx, e in evs:
        seed = SEED + int(e["t"] % 2 ** 31) + 17 * tag + (1 if ctx["sign"] < 0 else 0)
        s, why = score_f(ctx, e, np.random.default_rng(seed), np.random.default_rng(seed + 7))
        if s is None:
            au[why] = au.get(why, 0) + 1
            continue
        au["used"] = au.get("used", 0) + 1
        rows.append(s)
    return rows


def collect(s5, s1h, hold_ns, keys=None):
    keys = ALL_KEYS if keys is None else keys
    ctxs = [ctx_f(s5, s1h, +1, hold_ns), ctx_f(s5, s1h, -1, hold_ns)]
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
        rows[key] = _score(evs, tag, au)
        audit[key] = au
    box = {k: ctxs[0]["box_cnt"][k] for k in ctxs[0]["box_cnt"]}
    return {"rows": rows, "audit": audit, "box": box}


def summarize(rows, fade):
    t = sd.summarize(rows, fade)
    if not rows or t.get("mean") is None:
        return t
    ex = [r for r in rows if "pup" in r]
    if len(ex) >= 30:
        ts = np.array([r["t"] for r in ex], np.int64)
        t["path"] = {"event_rise_atr": float(np.mean([r["up"] for r in ex])), "placebo_rise_atr": float(np.mean([r["pup"] for r in ex])),
                     "event_fall_atr": float(np.mean([r["dn"] for r in ex])), "placebo_fall_atr": float(np.mean([r["pdn"] for r in ex])),
                     "rise_excess": cluster_test([r["up"] - r["pup"] for r in ex], ts), "fall_excess": cluster_test([r["dn"] - r["pdn"] for r in ex], ts)}
    return t


def contrasts(R):
    out = {}

    def diff(g, c):
        t = cluster_diff([r["d"] for r in g], [r["t"] for r in g], [r["d"] for r in c], [r["t"] for r in c])
        t.update(mean_a=float(np.mean([r["d"] for r in g])) if g else None, mean_b=float(np.mean([r["d"] for r in c])) if c else None)
        return t
    for k in ("A:0", "A:0.1", "A:0.2"):
        out["X:" + k] = diff(R[k], R["A:mid"])
    out["X:R:real"] = diff(R["R:real"], R["R:ctrl"])
    for k in ("A:0", "A:0.1", "A:0.2"):
        sup = [r for r in R[k] if r["bias"] == -1]
        opp = [r for r in R[k] if r["bias"] == +1]
        out["Bias:" + k] = diff(sup, opp)
    return out


def apply_fdr(res, status):
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


def _dir_word(key, m):
    kind = key.split(":")[0]
    if kind == "A":
        return "FADE side (price reversed away from the boundary more than placebo)" if m < 0 else "BREAKOUT side (price pushed through more than placebo; the OPPOSITE of the fade claim)"
    return "bounce side (price continued away from the retested level, the claim)" if m > 0 else "OPPOSITE to the bounce claim (price went back through)"


def reading(res):
    out = []
    surv = [(k, res["T"][k]) for k in TEST_KEYS if res["T"][k].get("fdr_reject")]
    for grp, nm in (("A", "F.1 approach to the previous-day boundary"), ("R", "F.3 break and retest")):
        s = [(k, v) for k, v in surv if k.startswith(grp)]
        if not s:
            out.append(f"{nm}: no test survives multiple-testing control: no demonstrated information about reversal or continuation beyond the clock slot and the recent move.")
            continue
        parts = []
        for k, v in s:
            spec = res["X"].get("X:" + k) if k != "A:0.2" or True else None
            sp = ("; specificity vs the control level survives" if spec and spec.get("fdr_reject") else "; specificity vs the control level does NOT survive: the boundary itself is not shown to be what carries it")
            parts.append(f"{k.split(':', 1)[1] if grp == 'A' else 'retest'}: excess {v['mean']:+.3f} ({_dir_word(k, v['mean'])}; observed {100 * v['obs_rate']:.1f}% vs placebo "
                         f"{100 * v['placebo_rate']:.1f}%, q={v['q_value']:.3f}){sp}")
        out.append(f"{nm} survives: " + " | ".join(parts) + ".")
    bs = [k for k in ("Bias:A:0", "Bias:A:0.1", "Bias:A:0.2") if res["X"][k].get("fdr_reject")]
    if bs:
        out.append("F.2 bias filter: the filtered/unfiltered difference survives at " + ", ".join(b.split(":", 1)[1] for b in bs) +
                   " (descriptive: look at the sign: a negative difference means events where the bias supports the fade did better for the fade).")
    else:
        out.append("F.2 bias filter: no filtered-vs-unfiltered difference survives: the 1h bias is not shown to improve on the same events unfiltered.")
    out.append("F.1 and F.3 share previous-day extremes and often the same days: they are not independent confirmations.")
    return " ".join(out)


def run_from_series(s5, s1h, status, hold_ns):
    col = collect(s5, s1h, hold_ns)
    R = col["rows"]
    res = {"T": {k: summarize(R[k], k.startswith("A")) for k in TEST_KEYS}, "ctrl": {k: summarize(R[k], k.startswith("A")) for k in CTRL_KEYS}, "X": contrasts(R)}
    res["n_tests"] = apply_fdr(res, status)
    res["audit"], res["box"] = col["audit"], col["box"]
    res["bias_groups"] = {k: {"support_fade": int(sum(1 for r in R[k] if r["bias"] == -1)), "oppose_fade": int(sum(1 for r in R[k] if r["bias"] == 1)),
                              "unknown": int(sum(1 for r in R[k] if r["bias"] == 0))} for k in ("A:0", "A:0.1", "A:0.2")}
    res["reading"] = reading(res)
    res.update({"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False})
    return res


def run_studyf(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    res = run_from_series(s5, s1h, status, hold_ns)
    res["development"] = {"first_bar_utc": str(s5.index[0]), "boundary_utc": str(rc.HOLDOUT_START_UTC)}
    return res


def structure_counts(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    """Structure only: no outcome is computed. Boxes and events per test, development and holdout."""
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    ctxs = [ctx_f(s5, s1h, +1, hold_ns), ctx_f(s5, s1h, -1, hold_ns)]
    out = {"dataset_status": status, "bars_5m": len(s5.index), "boxes": ctxs[0]["box_cnt"], "tests": {}}
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
        dev_res = run_studyf(raw_dir, source, preloaded)
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
                row[nm] = {"power_now": round(_phi(eff / se_h - 1.96), 2),
                           "months_needed_for_80pct": round(months * (se_h / (eff / (1.96 + 0.842))) ** 2, 1) if eff > 0 else None}
            row["ready_to_open"] = bool(row["survived_dev_fdr"] and row["half"]["power_now"] >= 0.80)
        out["tests"][key] = row
    out["note"] = ("Only tests that survived the development FDR are candidates for the holdout; the rest are listed for power context (an estimate near zero gives huge 'months needed': "
                   "ignore those). The holdout stays sealed until a candidate's half-effect power is >= 0.80.")
    return out


# ---------------------------------------------------------------------------
# power simulation with the real event structure
# ---------------------------------------------------------------------------
def _plant_fade(df, ctxs, size, key="A:0.1", ramp=48):
    n5 = len(df)
    shift = np.zeros(n5)
    tt = np.arange(n5)
    for ctx in ctxs:
        ev, _ = events_for(ctx, key, "dev")
        for e in ev:
            shift += -ctx["sign"] * size * e["atr"] * np.clip(tt - e["p"], 0, ramp) / float(ramp)
    d2 = df.copy()
    so = np.concatenate([[0.0], shift[:-1]])
    d2["close"] = df["close"] + shift
    d2["open"] = df["open"] + so
    d2["high"] = np.maximum(df["high"] + shift, np.maximum(d2["open"], d2["close"]))
    d2["low"] = np.minimum(df["low"] + shift, np.minimum(d2["open"], d2["close"]))
    return d2


def power_cell(size, n_rep, seed, n5=130000, garch=True, key="A:0.1"):
    far = pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value
    rng = np.random.default_rng(seed)
    hits, zs, ex, ns = 0, [], [], []
    for _ in range(n_rep):
        df = sb._synth_ohlc(n5, rng, garch=garch)
        if size > 0:
            s5, s1h = sa._series_from_df(df)
            ctxs = [ctx_f(s5, s1h, +1, far), ctx_f(s5, s1h, -1, far)]
            df = _plant_fade(df, ctxs, size, key)
        s5, s1h = sa._series_from_df(df)
        rows = collect(s5, s1h, far, [key])["rows"][key]
        t = summarize(rows, True)
        if t.get("mean") is None:
            continue
        zs.append(t["z"])
        ex.append(t["mean"])
        ns.append(t["n"])
        hits += int(t["p"] < 0.05 and t["mean"] < 0)
    return {"key": key, "size": size, "reps": n_rep, "detect_rate": hits / n_rep, "mean_z": float(np.mean(zs)) if zs else None,
            "mean_excess": float(np.mean(ex)) if ex else None, "mean_events": float(np.mean(ns)) if ns else None}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def render_markdown(r):
    n1, n2 = r["n_tests"][1], r["n_tests"][2]
    L = ["# Observatory — Study F: the previous-day box (GBPUSD)\n",
         f"Contract `{r['contract_hash']}` · dataset **{r['dataset_status']}** · holdout opened: **False** · family 1: {n1} tests · family 2: {n2} tests\n",
         "**Development data only. Box = previous trading day high/low (cutoff 00:00 UTC; Monday uses Friday). Outcome = +1 ATR through the boundary before −1 ATR away from it within 24h, minus a placebo with the same weekday, "
         "hour and recent move. Excess < 0 is the FADE, excess > 0 is the BREAKOUT. Two-sided, 2-day-block clustered. Nothing here is a trading rule.**\n",
         "## Reading (pre-declared)\n", f"**{r['reading']}**\n"]
    hd = ("| test | events | observed | placebo | excess | 95% CI | z | q | pips/event | setup net (pips) | placebo net | verdict |\n|---|---|---|---|---|---|---|---|---|---|---|---|")

    def trow(lab, t):
        if t.get("mean") is None:
            return f"| {lab} | {t.get('n', 0)} | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | **{t.get('verdict', 'DATA-LIMITED')}** |"
        return (f"| {lab} | {t['n']} | {100 * t['obs_rate']:.1f}% | {100 * t['placebo_rate']:.1f}% | {t['mean']:+.3f} | [{t['ci'][0]:+.3f}, {t['ci'][1]:+.3f}] | {t['z']:+.2f} | "
                f"{_p(t.get('q_value'))} | {t['excess_pips']:+.2f} | {t['setup_net_pips']:+.2f} | {t['placebo_net_pips']:+.2f} | **{t.get('verdict', '')}** |")
    names = {"A:0": "F.1 touch the boundary", "A:0.1": "F.1 within 10% of the box width", "A:0.2": "F.1 within 20% of the box width", "R:real": "F.3 break and retest (bounce claim)"}
    L += ["## F.1 / F.3 (family 1)\n", hd] + [trow(names[k], r["T"][k]) for k in TEST_KEYS]
    L += ["\n## Controls (not tested; for context)\n", hd, trow("F.1 control: first approach to the mid-box level", r["ctrl"]["A:mid"]), trow("F.3 control: retest of a level 0.75 ATR above the high", r["ctrl"]["R:ctrl"])]
    xh = "| test | n (boundary / control) | difference | 95% CI | z | q | verdict |\n|---|---|---|---|---|---|---|"
    xn = {"X:A:0": "F.1 touch − mid-box control", "X:A:0.1": "F.1 10% − mid-box control", "X:A:0.2": "F.1 20% − mid-box control", "X:R:real": "F.3 retest − control level",
          "Bias:A:0": "F.2 touch: bias supports fade − opposes", "Bias:A:0.1": "F.2 10%: bias supports fade − opposes", "Bias:A:0.2": "F.2 20%: bias supports fade − opposes"}
    L += ["\n## Specificity and the 1h bias (family 2)\n", "Boundary − control asks whether the previous-day extreme matters beyond an ordinary level. Bias rows compare the SAME events split by the 1h EMA bias "
          "(negative = the fade did better when the bias supported it).\n", xh]
    for k in SPEC_KEYS:
        t = r["X"][k]
        if t.get("diff") is None:
            L.append(f"| {xn[k]} | {t.get('n1', 0)} / {t.get('n0', 0)} | n/a | n/a | n/a | n/a | **{t.get('verdict', 'DATA-LIMITED')}** |")
        else:
            L.append(f"| {xn[k]} | {t['n1']} / {t['n0']} | {t['diff']:+.3f} | [{t['ci'][0]:+.3f}, {t['ci'][1]:+.3f}] | {t['z']:+.2f} | {_p(t.get('q_value'))} | **{t.get('verdict', '')}** |")
    L += ["\nBias group sizes (events where the bias supports / opposes the fade / unknown): " +
          "; ".join(f"{k}: {v['support_fade']} / {v['oppose_fade']} / {v['unknown']}" for k, v in r["bias_groups"].items()) + "\n"]
    L += ["## Raw 24h path (descriptive; no stop, no target)\n",
          "Mean maximum rise and maximum fall over the next 24h, in ATR, for the events versus the matched placebo bars. Rise is toward/through the boundary, fall is away from it.\n",
          "| group | rise (event) | rise (placebo) | rise excess | fall (event) | fall (placebo) | fall excess |\n|---|---|---|---|---|---|---|"]
    for k in TEST_KEYS + CTRL_KEYS:
        t = (r["T"] if k in TEST_KEYS else r["ctrl"])[k]
        p = t.get("path")
        if not p:
            continue
        re_, fe = p["rise_excess"], p["fall_excess"]
        L.append(f"| {k} | {p['event_rise_atr']:.3f} | {p['placebo_rise_atr']:.3f} | {_a(re_.get('mean'))} [{_a(re_['ci'][0]) if re_.get('ci') else 'n/a'}, {_a(re_['ci'][1]) if re_.get('ci') else 'n/a'}] | "
                 f"{p['event_fall_atr']:.3f} | {p['placebo_fall_atr']:.3f} | {_a(fe.get('mean'))} [{_a(fe['ci'][0]) if fe.get('ci') else 'n/a'}, {_a(fe['ci'][1]) if fe.get('ci') else 'n/a'}] |")
    L += ["\n## Up/down boundary and halves (descriptive)\n", "| test | high boundary excess (n) | low boundary excess (n) | first half | second half |\n|---|---|---|---|---|"]
    for k in TEST_KEYS:
        t = r["T"][k]
        if t.get("mean") is None:
            continue
        s, h = t.get("sides", {}), t.get("halves", {})

        def c(x):
            return f"{x['mean']:+.3f} ({x['n']})" if x and x.get("mean") is not None else "n/a"
        L.append(f"| {k} | {c(s.get('up'))} | {c(s.get('down'))} | {c(h.get('first_half'))} | {c(h.get('second_half'))} |")
    L += ["\n## Audit: events at each gate\n", "| test | structure detected | crossed through in one bar | started beyond | no touch / no break | no retest / dead | outside region | window | no trail | unresolved | ambiguous | no placebo | scored |\n"
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, a in r["audit"].items():
        L.append(f"| {k} | {a['detected']} | {a['crossed_through']} | {a['started_beyond']} | {a['no_touch'] + a['no_break']} | {a['no_retest'] + a['structure_dead']} | {a['outside_region']} | "
                 f"{a['window']} | {a['no_trail']} | {a['unresolved']} | {a['ambiguous']} | {a['no_placebo']} | {a['used']} |")
    b = r["box"]
    L.append(f"\nBoxes: {b['days']} UTC days; usable {b['usable']}; UTC Sundays skipped {b['sunday_open']}; no usable previous trading day {b['prev_day_missing']}; "
             f"width outside 1.5–15 ATR {b['width_out']}; no ATR {b['no_atr']}.\n")
    L.append("## Not run\n- Holdout (sealed). Trend lines and supply/demand zones (no objective measure exists; any definition would be ours, not the original). Sweep-and-reclaim of the box. "
             "Session highs/lows (Study G). Range definitions other than the previous day (Study H). Tradeability with stops and costs.\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _hand_df(close, pad=0.0002, start="2026-01-05 00:00"):
    close = np.asarray(close, float)
    o = np.concatenate([[1.30], close[:-1]])
    idx = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=len(close), freq="5min")
    df = pd.DataFrame({"open": o, "high": np.maximum(o, close) + pad, "low": np.minimum(o, close) - pad, "close": close}, index=idx)
    df["gap_before_missing"], df["closure_before"] = 0, False
    return df


def _day0(k):
    return 1.30 + 0.003 * np.sin(2 * np.pi * k / 288.0)


def _ctxs(df, hold=None):
    s5, s1h = sa._series_from_df(df)
    far = pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value if hold is None else hold
    return s5, s1h, [ctx_f(s5, s1h, +1, far), ctx_f(s5, s1h, -1, far)]


def selftest(null_series=8):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    # ---- hand frame: day 0 is a sine (known extremes), day 1 climbs slowly to the top, day 2 jumps through in one bar
    k = np.arange(288)
    d0 = _day0(k)
    d1 = np.full(288, 1.30)
    d1[100:] = 1.30 + (np.arange(188)) * 0.00002
    d1[270:] = 1.30                                                   # back to the start before the day ends
    d2 = np.full(288, 1.30)                                           # day 2 starts away from the boundary, then jumps through it in a single bar
    d2[100:] = 1.30 + 0.0060
    close = np.concatenate([d0, d1, d2, np.full(288 * 4, 1.30)])
    s5, s1h, (cp, cm) = _ctxs(_hand_df(close))
    ns5 = _ns(s5.index)
    chk("box: day 1 and day 2 are usable (day 0 has no previous day; the flat days after are too narrow to be boxes)", [int(b["day"] - ns5[0] // NS_D) for b in cp["boxes"]], [1, 2])
    b1 = cp["boxes"][0]
    pdh, pdl = 1.303 + 0.0002, 1.297 - 0.0002
    chk("box: previous-day high and low equal the sine extremes plus the bar padding", (abs(b1["pdh"] - pdh) < 1e-9, abs(b1["pdl"] - pdl) < 1e-9, abs(b1["W"] - (pdh - pdl)) < 1e-9), (True, True, True))
    chk("mirror box: the low boundary becomes the negated high boundary", (abs(cm["boxes"][0]["pdh"] + pdl) < 1e-9, abs(cm["boxes"][0]["W"] - b1["W"]) < 1e-9), (True, True))
    W = b1["W"]
    # expected first approach to f = 0.10 on day 1: first bar with high >= level (high = close + 0.0002 on a rising path)
    lvl = pdh - 0.10 * W
    exp_k = 100 + int(math.ceil((lvl - 0.0002 - 1.30) / 0.00002 - 1e-9))
    ev, cn = events_for(cp, "A:0.1", "dev")
    d1_events = [e for e in ev if e["day"] - ns5[0] // NS_D == 1]
    chk("approach f=0.10: the event is the first bar whose high reaches the level (hand-computed index)", [e["p"] - 288 for e in d1_events], [exp_k])
    ev0, _ = events_for(cp, "A:0", "dev")
    exp0 = 100 + int(math.ceil((pdh - 0.0002 - 1.30) / 0.00002 - 1e-9))
    chk("approach f=0 (touch): the event is the first bar whose high reaches the previous-day high", [e["p"] - 288 for e in ev0 if e["day"] - ns5[0] // NS_D == 1], [exp0])
    chk("approach: the touch event comes after the 10% event on the same day", exp0 > exp_k, True)
    e2 = [e for e in ev0 if e["day"] - ns5[0] // NS_D == 2]
    cnt2 = events_for(cp, "A:0", "dev")[1]
    chk("a bar that blows through the level in one go is 'crossed through', not an approach", (e2, cnt2["crossed_through"] >= 1), ([], True))
    # weekend: Friday full day, no Saturday, a Sunday-evening stub, then Monday: Monday's box is FRIDAY's range; Sunday is not an event day
    fri = _hand_df(_day0(np.arange(288)), start="2026-01-09 00:00")
    stub = _hand_df(np.full(24, 1.30), start="2026-01-11 22:00")
    mon = _hand_df(np.full(288, 1.30), start="2026-01-12 00:00")
    tue = _hand_df(np.full(288, 1.30), start="2026-01-13 00:00")
    wk = pd.concat([fri, stub, mon, tue])
    s5w, s1w = sa._series_from_df(wk)
    cw = ctx_f(s5w, s1w, +1, pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value)
    mon_id = int(pd.Timestamp("2026-01-12", tz="UTC").value // NS_D)
    bm = [b for b in cw["boxes"] if b["day"] == mon_id]
    chk("weekend: Monday's box is Friday's range (the previous trading day), and the Sunday stub is skipped as an event day",
        (len(bm) == 1 and abs(bm[0]["pdh"] - (1.303 + 0.0002)) < 1e-9, cw["box_cnt"]["sunday_open"]), (True, 1))
    # mirror equivalence on the negated frame
    dfn = _hand_df(close)
    dn = dfn.copy()
    dn["open"], dn["close"], dn["high"], dn["low"] = -dfn["open"], -dfn["close"], -dfn["low"], -dfn["high"]
    s5n, s1n, (cpn, cmn) = _ctxs(dn)
    evn, _ = events_for(cmn, "A:0.1", "dev")
    chk("mirror: negated frame in the low context gives the same events", ([e["p"] for e in evn], [round(e["x"], 9) for e in evn]), ([e["p"] for e in ev], [round(e["x"], 9) for e in ev]))
    # no look-ahead: shifting every bar after the event bar leaves events at or before it unchanged
    p0 = 288 + exp_k
    df2 = _hand_df(close)
    df2.iloc[p0 + 1:, df2.columns.get_loc("close")] += 0.4
    df2.iloc[p0 + 1:, df2.columns.get_loc("open")] += 0.4
    df2.iloc[p0 + 1:, df2.columns.get_loc("high")] += 0.4
    df2.iloc[p0 + 1:, df2.columns.get_loc("low")] += 0.4
    _, _, (cp2, _) = _ctxs(df2)
    ev2, _ = events_for(cp2, "A:0.1", "dev")
    key = lambda E: [(e["p"], round(e["x"], 9), round(e["atr"], 9), e["bias"]) for e in E if e["p"] <= p0]
    chk("no look-ahead: events at or before an altered future are identical (position, trailing move, ATR, bias)", key(ev2), key(ev))
    # a missing previous day: with the 1h data stale (no recent bars) the box is dropped and counted; with none in reach the day is dropped as 'no previous trading day'
    keep = np.ones(len(close), bool)
    keep[288:288 + 288] = False
    s5m, s1m = sa._series_from_df(_hand_df(close)[keep])
    cm_ = ctx_f(s5m, s1m, +1, pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value)
    chk("a day whose ATR data are stale (a whole day missing before it) is dropped and counted", cm_["box_cnt"]["no_atr"] >= 1, True)
    keep2 = np.ones(len(close), bool)
    keep2[288:288 * 5] = False
    s5q, s1q = sa._series_from_df(_hand_df(close)[keep2])
    cq = ctx_f(s5q, s1q, +1, pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value)
    chk("with no trading day within 3 days the day is dropped and counted", cq["box_cnt"]["prev_day_missing"] >= 1, True)
    # ---- retest hand case
    a0 = b1["a0"]
    L0 = pdh
    exc, tol = EXC * a0, RT_TOL * a0

    def retest_frame(die):
        d1r = np.full(288, 1.30)
        d1r[60:70] = np.linspace(1.30, L0 + exc + 0.0002, 10)
        d1r[70:80] = L0 + exc + 0.0002
        d1r[80:90] = np.linspace(L0 + exc + 0.0002, L0 + 0.0004, 10)
        d1r[90:] = L0 + 0.0002
        if die:                                                        # crash straight through the level: the retest bar itself closes through it
            d1r[80:] = L0 - exc - 0.0005
        return np.concatenate([d0, d1r, np.full(288 * 5, 1.30)])
    cl = retest_frame(False)
    _, _, (cr, _) = _ctxs(_hand_df(cl))
    evr, cnr = events_for(cr, "R:real", "dev")
    d1r_ev = [e for e in evr if e["day"] - ns5[0] // NS_D == 1]
    low_hit = None
    dfr = _hand_df(cl)
    lo_arr = dfr["low"].to_numpy()
    cand = np.flatnonzero(lo_arr[288 + 70:] <= L0 + tol)
    chk("retest: one event, on the first bar whose low returns to the broken level (after the break)", ([e["p"] for e in d1r_ev] == [288 + 70 + int(cand[0])]) if cand.size else False, True)
    cld = retest_frame(True)
    _, _, (crd, _) = _ctxs(_hand_df(cld))
    evd, cnd = events_for(crd, "R:real", "dev")
    chk("retest: structure that closes through the level before the retest is dead (no event, counted)", ([e for e in evd if e["day"] - ns5[0] // NS_D == 1], cnd["structure_dead"] >= 1), ([], True))
    # ---- region and sealing on a synthetic world, with brute-force re-derivation of the boxes from pandas
    rng = np.random.default_rng(41)
    df = sb._synth_ohlc(80000, rng)
    s5, s1h = sa._series_from_df(df)
    ns5 = _ns(s5.index)
    far = pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value
    cps = ctx_f(s5, s1h, +1, far)
    g = df.groupby(df.index.floor("D")).agg(hi=("high", "max"), lo=("low", "min"), nb=("high", "size"))
    ok_boxes = True
    for b in cps["boxes"]:
        d = pd.Timestamp(b["day"] * NS_D, tz="UTC") - pd.Timedelta(days=1)
        ok_boxes &= abs(g.loc[d, "hi"] - b["pdh"]) < 1e-12 and abs(g.loc[d, "lo"] - b["pdl"]) < 1e-12
    chk(f"synthetic world: {len(cps['boxes'])} boxes (>= 200); every box equals pandas' previous-day extremes", (len(cps["boxes"]) >= 200, bool(ok_boxes)), (True, True))
    evs, _ = events_for(cps, "A:0.1", "dev")
    chk(f"synthetic world yields approach events (>= 60, got {len(evs)})", len(evs) >= 60, True)
    inv = True
    for e in evs:
        b = next(x for x in cps["boxes"] if x["day"] == e["day"])
        lvl = b["pdh"] - 0.1 * b["W"]
        h, c = np.asarray(s5.h), np.asarray(s5.c)
        inv &= h[e["p"]] >= lvl and c[e["p"] - 1] < lvl and c[e["p"]] <= lvl + CLOSE_TOL * b["W"] and bool(np.all(h[b["s"]:e["p"]] < lvl))
    chk("every approach event: high reaches the level, previous close below it, close within tolerance, no earlier bar of the day reached it (first approach)", bool(inv), True)
    mid_ns = int(ns5[40000])
    cpm = ctx_f(s5, s1h, +1, mid_ns)
    dev, _ = events_for(cpm, "A:0.1", "dev")
    hold, _ = events_for(cpm, "A:0.1", "hold")
    chk("development events end their outcome window before the boundary; holdout events start at or after it", (all(e["t"] + (HMAX + 1) * NS_5 <= mid_ns for e in dev), all(e["t"] >= mid_ns for e in hold), len(hold) > 20), (True, True, True))
    col = collect(s5, s1h, mid_ns)
    chk("no scored row touches the holdout", all(r["t"] + (HMAX + 1) * NS_5 <= mid_ns for rows in col["rows"].values() for r in rows), True)
    # ---- null calibration of the full pipeline (random walk and GARCH), two-sided
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
        chk(f"null: share of two-sided p < 0.05 is at most 13% (got {(ps < 0.05).mean():.1%})", (ps < 0.05).mean() <= 0.13, True)
        chk(f"null: share of p < 0.01 is at most 4% (got {(ps < 0.01).mean():.1%})", (ps < 0.01).mean() <= 0.04, True)
        chk(f"null: mean z is near zero (got {np.mean(zs):+.2f})", abs(np.mean(zs)) < 0.5, True)
    pc = power_cell(1.5, 1, 21)
    chk(f"planted boundary reversal (fade) is detected as a FADE (z={pc['mean_z']:+.2f}, excess {pc['mean_excess']:+.3f})", pc["detect_rate"] == 1.0, True)
    rP = run_from_series(s5, s1h, "PRIMARY_READY", far)
    md = render_markdown(rP)
    chk("report renders and JSON serialises", (len(md) > 2500, len(json.dumps(rP, default=str)) > 2500), (True, True))
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": null_series, "tests": int(len(ps)), "share_p_lt_0.05": round(float((ps < 0.05).mean()), 3) if len(ps) else None,
                     "share_p_lt_0.01": round(float((ps < 0.01).mean()), 3) if len(ps) else None, "mean_z": round(float(np.mean(zs)), 3) if zs else None},
            "planted": {k: pc[k] for k in ("size", "mean_z", "mean_excess", "mean_events")}}


def main():
    ap = argparse.ArgumentParser(description="Observatory Study F (previous-day box). Read-only on raw data; the holdout stays sealed.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--counts", action="store_true", help="boxes and event counts only (no outcome is computed)")
    ap.add_argument("--readiness", action="store_true", help="holdout event counts and power from development estimates; no holdout outcome is read")
    ap.add_argument("--power", action="store_true", help="simulation of detectable fade sizes with the real event structure (synthetic data only)")
    ap.add_argument("--power-reps", type=int, default=12)
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    if a.counts:
        print(json.dumps(structure_counts(a.raw_dir, a.source), indent=2, default=str))
        return
    if a.power:
        cells = []
        for s in (0.0, 0.1, 0.2, 0.3, 0.5):
            cells.append(power_cell(s, a.power_reps, 3000 + int(s * 100)))
            print(json.dumps(cells[-1]), flush=True)
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "studyf_power.json"), "w") as f:
            json.dump(cells, f, indent=2)
        return
    if a.readiness:
        print(json.dumps(holdout_readiness(a.raw_dir, a.source), indent=2, default=str))
        return
    res = run_studyf(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "studyf_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "studyf_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
