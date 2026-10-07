"""
observatory/crt2.py
===================
OBSERVATORY — CRT follow-up ("CRT-2"): which PART of the CRT construction carries the result?  ISOLATED from the live bot.
(Not a numbered wave: the wave numbers are taken. Run it after wave4; it reuses wave4's event definitions and measurement.)

Question set (the branches from the Wave 4 review). Wave 4 showed that one CONSTRUCTION behaved badly. That is a result about
the construction. Why it behaved that way is a separate hypothesis, and each part of the construction is tested separately here:

  Branch A  EVENT      Does a one-sided sweep of the previous 1h candle contain information about the next 6 hours at all?
  Branch B  RECLAIM    Does closing back inside (reclaim) change what happens, compared with closing beyond (acceptance)?
  Branch D  STOP       Is the sweep extreme revisited more often than a move of the same size the other way?
                       ("revisit asymmetry". This measures WHAT PRICE DOES. It does not show WHY: stop-hunting is one candidate
                       explanation, momentum / volatility clustering / breakout continuation are others, and this test cannot
                       tell them apart.)
  Branch C  ENTRY      Is a RETEST entry (price leaves the swept level, then comes back to it) different from entering at the close?
  Branch E  TARGET     Descriptive only: the distribution of how far price goes for / against the fade (MFE / MAE percentiles),
                       so any target can be judged against what price actually did. No test.
  Branch F  CONTEXT    Does the fade result depend on volatility, sweep size, C1 range or higher-timeframe trend? (exploratory family)
  Branch G  COMBINATIONS  NOT run. A conjunction is only worth testing once its parts show something; the allowed list is fixed
                       below so combinations cannot be invented after the fact.

All outcomes are NET = (MFE - MAE) / ATR14(1h) in the FADE direction (against the sweep) over 6 hours, measured on 5m candles.
A negative NET means price went WITH the sweep (continuation). NET is exactly 0 in a symmetric market whatever stop/target is used.

FROZEN CONTRACT (hash printed in every report; frozen 2026-10-06 before any CRT-2 result was seen)
-------------------------------------------------------------------------------------------------
  Events         wave4 definitions: one-sided sweep of C1 by C2 (1h). RECLAIM group = C2 closes back inside C1 (wave4 CRT-A).
                 ACCEPTANCE group = C2 closes beyond the swept side. Known at C2's close; entry = open of the next 5m bar.
                 Episodes: anchored, K = 6h, per side. Reclaim and acceptance are each collapsed WITHIN their group (the wave4
                 CRT-A / ACC episodes) for B1, D1, C1 and F1-F4; A1 uses the two groups pooled and collapsed together so its
                 windows never overlap. (Cross-group overlap in B1 can only enlarge that difference's standard error: conservative.)
  ATR            ATR14(1h) at C1's close (known before C2 starts).
  Outcome        NET (above), 6h, from entry. Everything measured on 5m candles; weekend closure inside the window is allowed
                 (as everywhere in the Observatory), a missing-candle gap excludes the episode.
  PRIMARY (family 1, Benjamini-Hochberg q = 0.10, 4 tests)
    A1  mean NET of ALL sweep episodes (reclaim + acceptance), fade direction.           Threshold 0.10 ATR
    B1  mean NET(reclaim) - mean NET(acceptance), stratified by side.                    Threshold 0.10 ATR
    D1  revisit asymmetry on reclaim episodes whose stop distance >= 0.25 ATR:
        RUN = 1{MAE >= d} - 1{MFE >= d} with d = distance from entry to the sweep extreme. 0 in a symmetric market.  Threshold 5pp
    C1  mean NET of RETEST entries on reclaim episodes. Retest = after the entry bar, price first moves >= 0.25 ATR beyond the
        swept level in the fade direction (departure), then a LATER 5m bar touches the swept level again; entry = open of the
        bar after the touch bar; window 6h; if the touch bar also exceeds the sweep extreme the episode is void.   Threshold 0.10 ATR
  EXPLORATORY (family 2, own BH family, 4 tests; reclaim episodes; NET difference)
    F1  volatility: top tercile minus bottom tercile of ATR14(1h) (edges from the development episodes)
    F2  sweep size: top minus bottom tercile of (distance entry -> sweep extreme) / ATR
    F3  C1 range: top minus bottom tercile of (C1 high - low) / ATR
    F4  trend: fade WITH the 1h trend (close vs EMA50, adjust=False, read at C2's close) minus fade AGAINST it,
        stratified by sweep side (so the drift of the sample cannot masquerade as a trend effect)
  Descriptive    NET by session of C2's close (UTC <7 / 7-13 / 13-21 / >=21), reclaim and acceptance separately;
                 MFE / MAE percentiles; retest fill rate and the immediate-entry NET of the episodes the retest never filled.
  Not run        G (combinations), zone location (already in wave4 H3), 4H, the holdout (SEALED: this module cannot open it).
  Allowed future conjunctions (fixed now): reclaim AND (F-variable level that F1-F4 flag) ; reclaim AND outside-bar confirmation
                 (wave4 H2) ; reclaim AND zone-located (wave4 H3). Nothing else.

Run:  python3 -m observatory.crt2                (real data, development only)
      python3 -m observatory.crt2 --selftest     (hand-built paths, lookahead, null calibration)
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

_W4_NEEDED = ("locate", "measure_event", "sweep_signals", "crt_events_1h", "crt_events_b", "collapse", "baseline_net",
              "verdict", "_mk", "_mean_test", "_welch", "_strat_mean_diff", "PRIMARY_H", "MAX_H", "K_EP_HOURS", "CONTRACT_HASH")
_W4_MISSING = [n for n in _W4_NEEDED if not hasattr(w4, n)]
if _W4_MISSING:
    raise SystemExit("observatory/wave4.py is the WRONG VERSION (missing: " + ", ".join(_W4_MISSING) + "). "
                     "crt2 needs the v2.1 wave4.py (the one with `def locate`). Replace observatory/wave4.py with it and re-run.")

MIN_EFFECT_ATR = 0.10
MIN_EFFECT_PP = 0.05
FDR_Q = w1.FDR_Q
D1_MIN_STOP_ATR = 0.25
DEPARTURE_ATR = 0.25
RETEST_WINDOW_H = 6
EMA_SPAN = 50
SESSION_EDGES = w1.SESSION_EDGES
SESSION_NAMES = w1.SESSION_NAMES
CONTRACT = {
    "module": "crt2", "version": "v1", "instrument": "GBPUSD", "uses": "wave4 event definitions (contract %s)" % w4.CONTRACT_HASH,
    "outcome": "NET=(MFE-MAE)/ATR1h, fade direction, 6h, 5m path", "episode_hours": w4.K_EP_HOURS,
    "family1": ["A1", "B1", "D1", "C1"], "family2": ["F1", "F2", "F3", "F4"],
    "min_effect_atr": MIN_EFFECT_ATR, "min_effect_pp": MIN_EFFECT_PP, "d1_min_stop_atr": D1_MIN_STOP_ATR,
    "retest": {"departure_atr": DEPARTURE_ATR, "window_h": RETEST_WINDOW_H}, "ema_span": EMA_SPAN, "fdr_q": FDR_Q,
    "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# building blocks
# ---------------------------------------------------------------------------
def htf_trend(s1h):
    """+1 / -1 / 0: sign of (close - EMA50) of each 1h candle, read at THAT candle's close (0 for the first 50 candles)."""
    ema = pd.Series(s1h.c).ewm(span=EMA_SPAN, adjust=False).mean().to_numpy()
    sg = np.sign(s1h.c - ema).astype(int)
    sg[:EMA_SPAN] = 0
    return sg


def swept_level(s1h, ev):
    """The side of C1 that C2 swept (the level itself, not the sweep extreme)."""
    j = ev["j"]
    return float(s1h.h[j - 1] if ev["side"] == "high" else s1h.l[j - 1])


def revisit_run(m):
    """RUN = 1{MAE >= d} - 1{MFE >= d}, d = distance from entry to the sweep extreme, over the 6h window. None if d is too small."""
    atr, d = m["atr"], m["stop_d"]
    if not (d >= D1_MIN_STOP_ATR * atr):
        return None
    e6 = m["exc"][w4.PRIMARY_H]
    return float(e6["mae"] * atr >= d) - float(e6["mfe"] * atr >= d)


def retest_entry(ev, level, s5, window_bars=None):
    """Scan the 5m bars after the entry bar's open. Returns {'status': FILLED|MISSED|NO_DEPARTURE|VOID_TOUCH|EXCLUDED, 'k': touch bar}.
    Fade direction d: d=-1 (short, sweep-high): level is above price; departure = low <= level - 0.25 ATR; touch = high >= level.
    A touch bar that also trades beyond the sweep extreme voids the episode (the stop would already be gone)."""
    e = w4.locate(ev, s5)
    if e is None:
        return {"status": "EXCLUDED", "reason": "no_trigger_bar"}
    n = window_bars or int(RETEST_WINDOW_H * 60 / s5.step_minutes)
    last = e + n
    if last + 1 >= s5.n:
        return {"status": "EXCLUDED", "reason": "end_of_data"}
    if s5.gap[e + 1:last + 1].any():
        return {"status": "EXCLUDED", "reason": "data_gap"}
    d, dep = ev["d"], DEPARTURE_ATR * ev["atr"]
    departed = False
    for k in range(e + 1, last + 1):
        if departed:
            touched = (s5.h[k] >= level) if d == -1 else (s5.l[k] <= level)
            if touched:
                beyond = (s5.h[k] > ev["stop"]) if d == -1 else (s5.l[k] < ev["stop"])
                return {"status": "VOID_TOUCH"} if beyond else {"status": "FILLED", "k": int(k)}
        if (s5.l[k] <= level - dep) if d == -1 else (s5.h[k] >= level + dep):
            departed = True
    return {"status": "MISSED" if departed else "NO_DEPARTURE"}


def _tercile_test(values, nets):
    v, x = np.asarray(values, float), np.asarray(nets, float)
    ok = np.isfinite(v)
    v, x = v[ok], x[ok]
    if len(v) < 60:
        return dict(w4._welch([], []), edges=None)
    lo, hi = np.quantile(v, [1 / 3, 2 / 3])
    t = w4._welch(x[v >= hi], x[v <= lo])
    t["edges"] = (float(lo), float(hi))
    t["mean_top"] = float(x[v >= hi].mean()) if (v >= hi).any() else None
    t["mean_bottom"] = float(x[v <= lo].mean()) if (v <= lo).any() else None
    return t


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------
def analyze(s1h, s5, dev_end):
    sweeps = w4.sweep_signals(s1h)
    base = w4.crt_events_1h(s1h, sweeps)
    trend = htf_trend(s1h)
    excl = {}

    def measure_all(events):
        res_ = []
        for x in events:
            m = w4.measure_event(x, s5, {})
            if not m["ok"]:
                excl[m["reason"]] = excl.get(m["reason"], 0) + 1
                continue
            m["group"], m["level"] = x["group"], swept_level(s1h, x)
            m["session"] = int(np.digitize(x["t_known"].hour, SESSION_EDGES))
            m["trend_aligned"] = int(trend[x["j"]]) * x["d"]              # +1: fade goes WITH the 1h trend, -1 against, 0 unknown
            m["c1_range_atr"] = x["c1_range"] / x["atr"]
            res_.append(m)
        return res_
    dev = lambda evs, g: [dict(x, group=g) for x in evs if x["t_known"] < dev_end]
    rec = measure_all(w4.collapse(dev(base["A"], "reclaim")))                       # within-group episodes (same as wave4 CRT-A / ACC)
    acc = measure_all(w4.collapse(dev(base["ACC"], "accept")))
    pooled = measure_all(w4.collapse(dev(base["A"], "reclaim") + dev(base["ACC"], "accept")))   # A1 only: windows never overlap
    net = lambda ms: [m["exc"][w4.PRIMARY_H]["net"] for m in ms]
    out = {"episodes_pooled": len(pooled), "reclaim": len(rec), "accept": len(acc), "excluded": excl}
    # ---- primary
    out["A1"] = w4._mean_test(net(pooled))
    out["A1"]["by_side"] = {sd: w4._mean_test([m["exc"][w4.PRIMARY_H]["net"] for m in pooled if m["side"] == sd]) for sd in ("high", "low")}
    out["reclaim_NET"] = w4._mean_test(net(rec))
    out["accept_NET"] = w4._mean_test(net(acc))
    by = lambda ms: {sd: [m["exc"][w4.PRIMARY_H]["net"] for m in ms if m["side"] == sd] for sd in ("high", "low")}
    b1 = w4._strat_mean_diff(by(rec), by(acc))
    b1["n_min"] = min(len(rec), len(acc))
    out["B1"] = b1
    runs = [r for r in (revisit_run(m) for m in rec) if r is not None]
    d1 = w4._mean_test(runs)
    out["D1"] = d1
    if runs:
        out["D1"]["revisit_rate"] = float(np.mean([m["exc"][w4.PRIMARY_H]["mae"] * m["atr"] >= m["stop_d"] for m in rec if revisit_run(m) is not None]))
        out["D1"]["same_distance_favourable_rate"] = float(np.mean([m["exc"][w4.PRIMARY_H]["mfe"] * m["atr"] >= m["stop_d"] for m in rec if revisit_run(m) is not None]))
    # retest
    rt_net, status = [], {"FILLED": 0, "MISSED": 0, "NO_DEPARTURE": 0, "VOID_TOUCH": 0, "EXCLUDED": 0, "MEASURE_FAILED": 0}
    missed_imm = []
    for m in rec:
        r = retest_entry(m["ev"], m["level"], s5)
        if r["status"] != "FILLED":
            status[r["status"]] += 1
            if r["status"] in ("MISSED", "NO_DEPARTURE"):
                missed_imm.append(m["exc"][w4.PRIMARY_H]["net"])
            continue
        ev2 = dict(m["ev"], t_known=s5.index[r["k"]] + s5.step)
        m2 = w4.measure_event(ev2, s5, {})
        if not m2["ok"]:
            status["MEASURE_FAILED"] += 1
            continue
        status["FILLED"] += 1
        rt_net.append(m2["exc"][w4.PRIMARY_H]["net"])
    c1 = w4._mean_test(rt_net)
    c1["status_counts"] = status
    c1["fill_rate"] = status["FILLED"] / max(1, len(rec))
    c1["missed_or_no_departure_immediate_NET"] = w4._mean_test(missed_imm)
    out["C1"] = c1
    # ---- exploratory context (reclaim episodes)
    rn = net(rec)
    out["F1"] = _tercile_test([m["atr"] for m in rec], rn)
    out["F2"] = _tercile_test([m["stop_d"] / m["atr"] for m in rec], rn)
    out["F3"] = _tercile_test([m["c1_range_atr"] for m in rec], rn)
    al = [m["exc"][w4.PRIMARY_H]["net"] for m in rec if m["trend_aligned"] == 1]
    ag = [m["exc"][w4.PRIMARY_H]["net"] for m in rec if m["trend_aligned"] == -1]
    side_grp = lambda g: {sd: [m["exc"][w4.PRIMARY_H]["net"] for m in rec if m["side"] == sd and m["trend_aligned"] == g] for sd in ("high", "low")}
    out["F4"] = w4._strat_mean_diff(side_grp(1), side_grp(-1))                 # within each sweep side, so drift of the sample cannot masquerade as trend
    out["F4"]["n_min"] = min(len(al), len(ag))
    out["F4"]["n1"], out["F4"]["n0"] = len(al), len(ag)
    out["F4"]["mean_with"] = float(np.mean(al)) if al else None
    out["F4"]["mean_against"] = float(np.mean(ag)) if ag else None
    # ---- descriptive
    out["session"] = {nm: {"reclaim": w4._mean_test([m["exc"][w4.PRIMARY_H]["net"] for m in rec if m["session"] == i]),
                           "accept": w4._mean_test([m["exc"][w4.PRIMARY_H]["net"] for m in acc if m["session"] == i])}
                      for i, nm in enumerate(SESSION_NAMES)}
    q = [10, 25, 50, 75, 90]
    out["excursion_percentiles_atr"] = {
        "percentiles": q,
        "reclaim_MFE": [float(np.percentile([m["exc"][w4.PRIMARY_H]["mfe"] for m in rec], p)) for p in q] if rec else None,
        "reclaim_MAE": [float(np.percentile([m["exc"][w4.PRIMARY_H]["mae"] for m in rec], p)) for p in q] if rec else None}
    out["baseline"] = w4.baseline_net(s1h, s5, dev_end)
    return out


def apply_fdr(res, status):
    fam = {1: [("A1", "mean", MIN_EFFECT_ATR, res["A1"]["n"]), ("B1", "diff", MIN_EFFECT_ATR, res["B1"]["n_min"]),
               ("D1", "mean", MIN_EFFECT_PP, res["D1"]["n"]), ("C1", "mean", MIN_EFFECT_ATR, res["C1"]["n"])],
           2: [("F1", "diff", MIN_EFFECT_ATR, None), ("F2", "diff", MIN_EFFECT_ATR, None), ("F3", "diff", MIN_EFFECT_ATR, None), ("F4", "diff", MIN_EFFECT_ATR, None)]}
    counts = {}
    for f, tests in fam.items():
        live = [(k, v, fl, n) for k, v, fl, n in tests if res[k].get("p") is not None]
        rej, q = w1.bh_fdr([res[k]["p"] for k, _, _, _ in live]) if live else ([], [])
        counts[f] = len(live)
        st = status if f == 1 else "EXPLORATORY_READY"
        for (k, v, fl, n), rj, qq in zip(live, rej, q):
            r = res[k]
            r["family"], r["fdr_reject"], r["q_value"] = f, rj, qq
            n_min = n if n is not None else r.get("n_min", 0)
            r["verdict"] = w4.verdict(r[v], r["ci"][0], r["ci"][1], rj, n_min, st, fl)
        for k, v, fl, n in tests:
            if res[k].get("p") is None:
                res[k]["verdict"] = "DATA-LIMITED"
    return counts


def run_crt2(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    clean = {tf: (preloaded[tf] if preloaded else rc.clean_timeframe(tf, raw_dir, source)[0]) for tf in ("5m", "15m", "1h")}
    s1h, s5, s15 = rm.Series(clean["1h"], 60), rm.Series(clean["5m"], 5), rm.Series(clean["15m"], 15)
    rep15 = rc.clean_timeframe("15m", raw_dir, source)[2] if not preloaded else {
        "first": None, "last": None, "raw_lines_parsed": 0, "unique_timestamps": 0, "duplicate_lines": 0, "conflicting_duplicates": 0,
        "dropped_market_closed": 0, "dropped_off_grid": 0, "dropped_invalid_ohlc": 0, "kept_bars": len(clean["15m"]),
        "unexplained_gaps": 0, "missing_open_bars": 0}
    a6 = rg.gate_a6({"15m": clean["15m"]}, {"15m": rep15}, {"15m": s15})
    status = rg.gate_a7({"15m": clean["15m"]}, {"15m": s15}, a6)["details"]["status"]
    dev_end = rc.HOLDOUT_START_UTC - pd.Timedelta(hours=w4.MAX_H + w4.PRIMARY_H + 1)       # retest entries can start up to 6h late
    res = analyze(s1h, s5, dev_end)
    n = apply_fdr(res, status)
    res.update({"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False, "n_tests": n})
    return res


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _a(x, nd=3):
    return "n/a" if x is None else f"{x:+.{nd}f}"


def _ci(ci):
    return "n/a" if not ci else f"[{_a(ci[0])}, {_a(ci[1])}]"


def _p(x):
    return "n/a" if x is None else f"{x:.4f}"


def render_markdown(r):
    L = ["# Observatory — CRT follow-up (CRT-2): which part of the construction carries the result?\n",
         f"Contract `{r['contract_hash']}` · dataset **{r['dataset_status']}** · holdout opened: **False** · family 1: {r['n_tests'][1]} tests · family 2 (exploratory): {r['n_tests'][2]} tests\n",
         "**Development data only. Nothing here is a trading rule. A result describes WHAT PRICE DID; it does not show WHY.**\n",
         "NET = (MFE − MAE) / ATR(1h), 6h, **in the fade direction** (against the sweep). Positive = price reversed; negative = price continued "
         "with the sweep. Zero in a symmetric market whatever stop or target is used. Each line shows the 95% CI: the largest effect the data cannot rule out.\n",
         f"Episodes measured: reclaim {r['reclaim']}, acceptance {r['accept']} (each collapsed within its group, K = 6h); A1 uses the pooled, jointly collapsed set ({r['episodes_pooled']}) so its windows never overlap · excluded {r['excluded']}\n"]
    bl = r.get("baseline") or {}
    if bl.get("mean_long") is not None:
        L.append(f"Reference (every hour boundary, no event): NET of a long {_a(bl['mean_long'])} ATR (s.e. {bl['se_6h']:.3f}).\n")
    L.append("## Primary (family 1)\n")
    L.append("| test | n | estimate | 95% CI | p | q | verdict |\n|---|---|---|---|---|---|---|")
    def row(name, label, key, est, n, unit=""):
        t = r[key]
        L.append(f"| **{name}** {label} | {n} | {_a(t.get(est))}{unit} | {_ci(t.get('ci'))} | {_p(t.get('p'))} | {_p(t.get('q_value'))} | **{t.get('verdict')}** |")
    row("A1", "any sweep: NET of the fade", "A1", "mean", r["A1"]["n"])
    row("B1", "reclaim − acceptance NET", "B1", "diff", r["B1"].get("n_min"))
    row("D1", "revisit asymmetry (P(back through the sweep extreme) − P(same-size move the other way))", "D1", "mean", r["D1"]["n"])
    row("C1", "retest entry: NET of the fade", "C1", "mean", r["C1"]["n"])
    L.append("")
    a1 = r["A1"]
    L.append(f"- A1 by side: sweep-high {_a(a1['by_side']['high']['mean'])} {_ci(a1['by_side']['high']['ci'])} (n {a1['by_side']['high']['n']}) · sweep-low {_a(a1['by_side']['low']['mean'])} {_ci(a1['by_side']['low']['ci'])} (n {a1['by_side']['low']['n']})")
    L.append(f"- reclaim NET {_a(r['reclaim_NET']['mean'])} {_ci(r['reclaim_NET']['ci'])} (n {r['reclaim_NET']['n']}) · acceptance NET {_a(r['accept_NET']['mean'])} {_ci(r['accept_NET']['ci'])} (n {r['accept_NET']['n']})")
    d1 = r["D1"]
    if d1.get("revisit_rate") is not None:
        L.append(f"- D1: price traded back through the sweep extreme in {d1['revisit_rate']:.1%} of episodes; it moved the same distance in the fade direction in {d1['same_distance_favourable_rate']:.1%} (equal in a symmetric market). "
                 "Stop-hunting is only ONE candidate explanation for a gap here; continuation / volatility clustering produce the same numbers.")
    c1 = r["C1"]
    L.append(f"- C1 retest: fill rate {c1['fill_rate']:.0%} · status {c1['status_counts']} · immediate-entry NET of the episodes the retest never filled {_a(c1['missed_or_no_departure_immediate_NET']['mean'])} {_ci(c1['missed_or_no_departure_immediate_NET']['ci'])} (n {c1['missed_or_no_departure_immediate_NET']['n']})")
    L.append("\n## Context (family 2, exploratory; reclaim episodes; NET difference)\n")
    L.append("| test | top / with | bottom / against | difference | 95% CI | p | q | verdict |\n|---|---|---|---|---|---|---|---|")
    for k, lab, a, b in (("F1", "volatility ATR(1h) tercile", "mean_top", "mean_bottom"), ("F2", "sweep size tercile", "mean_top", "mean_bottom"),
                         ("F3", "C1 range tercile", "mean_top", "mean_bottom"), ("F4", "fade WITH vs AGAINST the 1h trend", "mean_with", "mean_against")):
        t = r[k]
        L.append(f"| **{k}** {lab} | {_a(t.get(a))} | {_a(t.get(b))} | {_a(t.get('diff'))} | {_ci(t.get('ci'))} | {_p(t.get('p'))} | {_p(t.get('q_value'))} | **{t.get('verdict')}** |")
    L.append("\n## Descriptive\n")
    L.append("NET by session of C2's close (UTC; no test):\n")
    L.append("| session | reclaim NET | n | acceptance NET | n |\n|---|---|---|---|---|")
    for nm, v in r["session"].items():
        L.append(f"| {nm} | {_a(v['reclaim']['mean'])} {_ci(v['reclaim']['ci'])} | {v['reclaim']['n']} | {_a(v['accept']['mean'])} {_ci(v['accept']['ci'])} | {v['accept']['n']} |")
    ex = r["excursion_percentiles_atr"]
    if ex["reclaim_MFE"]:
        L.append("\nHow far price went (reclaim episodes, 6h, ATR units): percentiles " + "/".join(str(p) for p in ex["percentiles"]))
        L.append("- with the fade (MFE): " + " / ".join(f"{x:.2f}" for x in ex["reclaim_MFE"]))
        L.append("- against the fade (MAE): " + " / ".join(f"{x:.2f}" for x in ex["reclaim_MAE"]))
    L.append("\n## Not run\n- G (combinations): only the fixed list in the contract, and only after the singles are read.\n- Zone location (wave4 H3), 4H, the holdout (sealed).\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def selftest(null_series=12):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    from . import gates as gg
    # ---- retest scan (short fade, sweep-high): level 105, atr 2 -> departure needs low <= 104.5; stop 111
    def rt(path, ev_over=None, tail=200, filler=(104, 104.2, 103.8, 104)):
        pre = [(100, 100.1, 99.9, 100)] * 30
        t_known = pd.Timestamp("2026-03-10", tz="UTC") + pd.Timedelta(minutes=5 * 31)
        s_ = w4._mk(pre + [(104, 104.2, 103.8, 104)] + path + [filler] * tail, 5)
        ev = {"d": -1, "stop": 111.0, "far": 100.0, "mid": 105.0, "atr": 2.0, "side": "high", "t_known": t_known}
        ev.update(ev_over or {})
        return retest_entry(ev, 105.0, s_), s_
    r, s_ = rt([(104, 104.4, 103.0, 103.5), (103.5, 104.0, 103.2, 103.8), (103.8, 105.2, 103.7, 105.0)])
    chk("departure then touch -> FILLED at the touch bar", (r["status"], r["k"]), ("FILLED", 33))
    r, _ = rt([(104.6, 104.9, 104.6, 104.7), (104.7, 105.3, 104.6, 105.1)], filler=(104.6, 104.8, 104.6, 104.7))
    chk("a touch without a prior departure is not a retest", r["status"], "NO_DEPARTURE")
    r, _ = rt([(104, 104.4, 103.0, 103.5)] + [(103.5, 104.0, 103.2, 103.8)] * 5)
    chk("departed but never returned -> MISSED", r["status"], "MISSED")
    r, _ = rt([(104, 104.4, 103.0, 103.5), (103.5, 112.0, 103.4, 111.5)])
    chk("touch bar that also exceeds the sweep extreme is void", r["status"], "VOID_TOUCH")
    r, _ = rt([(104, 104.4, 103.0, 103.5)] + [(103.5, 104.0, 103.2, 103.8)] * 80 + [(103.8, 105.5, 103.7, 105.2)])
    chk("a touch after the 6h window is not counted", r["status"], "MISSED")
    # long mirror: level 95, atr 2, sweep-low extreme 89, departure high >= 95.5
    pre = [(100, 100.1, 99.9, 100)] * 30
    t_known = pd.Timestamp("2026-03-10", tz="UTC") + pd.Timedelta(minutes=5 * 31)
    sl = w4._mk(pre + [(96, 96.2, 95.8, 96)] + [(96, 97.0, 95.9, 96.8), (96.8, 96.9, 94.9, 95.2)] + [(96, 96.2, 95.8, 96)] * 200, 5)
    r = retest_entry({"d": 1, "stop": 89.0, "far": 110.0, "mid": 100.0, "atr": 2.0, "side": "low", "t_known": t_known}, 95.0, sl)
    chk("long mirror: FILLED", r["status"], "FILLED")
    sg = w4._mk(pre + [(104, 104.2, 103.8, 104)] + [(104, 104.4, 103.0, 103.5)] * 3 + [(104, 104.2, 103.8, 104)] * 200, 5)
    sg.gap[33] = 2
    chk("missing candle inside the window excludes the episode", retest_entry({"d": -1, "stop": 111.0, "far": 100.0, "mid": 105.0, "atr": 2.0, "side": "high", "t_known": t_known}, 105.0, sg)["status"], "EXCLUDED")
    # ---- RUN: d = 3 (entry 108, stop 111): MAE 3.2 and MFE 1.0 -> +1 ; both reached -> 0 ; neither -> 0 ; MFE only -> -1
    mk = lambda mfe, mae, d=3.0, atr=2.0: {"atr": atr, "stop_d": d, "exc": {w4.PRIMARY_H: {"mfe": mfe / atr, "mae": mae / atr}}}
    chk("RUN: revisited, no equal favourable move", revisit_run(mk(1.0, 3.2)), 1.0)
    chk("RUN: both", revisit_run(mk(3.5, 3.2)), 0.0)
    chk("RUN: neither", revisit_run(mk(1.0, 1.0)), 0.0)
    chk("RUN: favourable only", revisit_run(mk(3.5, 1.0)), -1.0)
    chk("RUN: tiny stop distance excluded", revisit_run(mk(1.0, 3.2, d=0.4)), None)
    # ---- trend label uses only the candle's own and earlier closes
    rng = np.random.default_rng(5)
    d5 = gg._random_walk_series(9000, rng, step=5).df
    h1 = d5[["open", "high", "low", "close"]].resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    h1["gap_before_missing"], h1["closure_before"] = 0, False
    full = htf_trend(rm.Series(h1, 60))
    part = htf_trend(rm.Series(h1.iloc[:300], 60))
    chk("trend label unchanged when the future is removed", bool((full[:300] == part).all()), True)
    alt = h1.copy()
    alt.iloc[301:, alt.columns.get_loc("close")] *= 1.3
    chk("trend label unchanged when the future is altered", bool((htf_trend(rm.Series(alt, 60))[:300] == full[:300]).all()), True)
    chk("first 50 candles unknown", int(np.abs(full[:50]).sum()), 0)
    # ---- null calibration on random walks
    zs = {k: [] for k in ("A1", "B1", "D1", "C1", "F1", "F2", "F3", "F4")}
    base_means, used = [], 0
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
    chk(f"null: enough tests ({len(allz)})", len(allz) >= 60, True)
    if len(allz) >= 60:
        chk(f"null: |mean z| < 0.4 (got {allz.mean():+.2f})", abs(allz.mean()) < 0.4, True)
        chk(f"null: sd(z) in [0.6, 1.5] (got {allz.std():.2f})", 0.6 <= allz.std() <= 1.5, True)
        chk(f"null: share |z|>1.96 <= 12% (got {(np.abs(allz) > 1.96).mean():.0%})", (np.abs(allz) > 1.96).mean() <= 0.12, True)
    for k in ("A1", "B1", "D1", "C1"):
        chk(f"null {k}: usable in most series ({len(zs[k])}/{used})", len(zs[k]) >= used * 0.7, True)
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": used, **{f"{k}_mean_z": round(float(np.mean(v)), 3) if v else None for k, v in zs.items()},
                     **{f"{k}_sd_z": round(float(np.std(v)), 3) if v else None for k, v in zs.items()}}}


def main():
    ap = argparse.ArgumentParser(description="Observatory CRT follow-up (CRT-2). Read-only on raw data; the holdout stays sealed.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    res = run_crt2(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "crt2_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "crt2_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
