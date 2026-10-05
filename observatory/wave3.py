"""
observatory/wave3.py
====================
OBSERVATORY — Wave 3: timeframe combinations (higher-timeframe context). ISOLATED from the live bot.

Question: does a displacement that agrees with the higher-timeframe direction continue differently from
one that goes against it?   (Catalog I2 / I3 / I5, with the I4 control built in.)

Wave 1 already covered the single-timeframe configurations (T1 = 1h, T2 = 15m, T3 = 5m, no context).
This wave covers the four combinations. In each, the event is detected on the LOWEST timeframe and the
higher timeframe(s) only supply a direction label:

  T4  events 15m, context 1h
  T5  events 5m,  context 15m
  T6  events 5m,  context 1h        (15m skipped)
  T7  events 5m,  context 1h AND 15m (both must agree; if they disagree the event is "mixed")

FROZEN CONTRACT (hash printed in every report; frozen 2026-10-05, before any Wave 3 result was seen)
-------------------------------------------------------------------------------------------------
  Context label  PRIMARY: +1 if the higher-timeframe CLOSE is above its EMA(50) (adjust=False), -1 if below.
                 It is read from the latest higher-timeframe bar that is fully CLOSED at the event's
                 confirmation time (bar open + step); the first 50 bars give 0 (unknown).
                 DESCRIPTIVE alternative (not in the FDR family): sign of close - close[20 bars earlier].
  Events         Wave 1 displacements: range/ATR14(prior) >= k, k in {1.0, 1.5, 2.0}; PRIMARY k = 1.5.
                 Trade = continuation of the displacement direction, next-open entry, +-1R (ATR14 at the
                 event bar), 24 bars, first touch; ambiguous 15m/1h bars settled with 5m where possible.
  Aligned/Counter  aligned = displacement direction == context; counter = displacement direction == -context.
  PRIMARY test   per configuration at k = 1.5: stratified (direction x session x ATR tercile) difference in
                 P(+1R first), ALIGNED minus COUNTER, among displacement events. Two-sided z-test.
                 4 primary tests, Benjamini-Hochberg q = 0.10. Minimum effect 5 percentage points.
  Control (I4)   ordinary bars (range/ATR < 1.0), split aligned/counter the same way. Shown beside the
                 primary: if ordinary bars show the same aligned-minus-counter gap, the context (not the
                 displacement) is carrying it. The difference-in-differences is reported.
  Episodes       K = 12, anchored, per direction AND per aligned/counter group.
  Split          the frozen absolute holdout date (clean.HOLDOUT_START_UTC); development events are purged.
                 Holdout stays SEALED; this module has no way to open it.
  Not primary    k = 1.0 and 2.0, the EMA-vs-net-change alternative, the T7 "mixed" group, worst-case ambiguity.

Run:  python3 -m observatory.wave3                (real data)
      python3 -m observatory.wave3 --selftest     (lookahead, alignment, null checks)
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

K_PRIMARY = 1.5
K_ALL = w1.DISPLACEMENT_K
MIN_EFFECT = w1.MIN_EFFECT
FDR_Q = w1.FDR_Q
EMA_SPAN = 50
NET_BARS = 20
CONFIGS = {"T4": ("15m", ("1h",)), "T5": ("5m", ("15m",)), "T6": ("5m", ("1h",)), "T7": ("5m", ("1h", "15m"))}
CONTRACT = {
    "wave": 3, "instrument": "GBPUSD", "configs": {k: {"events": v[0], "context": list(v[1])} for k, v in CONFIGS.items()},
    "context_primary": f"HTF close vs EMA({EMA_SPAN}), closed bars only", "context_descriptive": f"sign(close - close[{NET_BARS}])",
    "k_primary": K_PRIMARY, "k_all": list(K_ALL), "K_episode": w1.K_EPISODE, "horizon_bars": w1.HORIZON,
    "min_effect": MIN_EFFECT, "fdr_q": FDR_Q, "n_primary_tests": len(CONFIGS),
    "holdout_start_utc": str(rc.HOLDOUT_START_UTC), "purge_bars": w1.HORIZON + 1,
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# context labels (closed higher-timeframe bars only)
# ---------------------------------------------------------------------------
def htf_labels(htf_df, htf_step_min, event_index, event_step_min, method="ema"):
    """
    Returns an int array (len(event_index)) of +1 / -1 / 0 (unknown). Each event gets the label of the
    latest HTF bar whose CLOSE time (open + step) is <= the event's confirmation time (open + step).
    """
    close = htf_df["close"]
    if method == "ema":
        raw = np.sign((close - close.ewm(span=EMA_SPAN, adjust=False).mean()).to_numpy())
        raw[:EMA_SPAN] = 0
    elif method == "net":
        raw = np.sign((close - close.shift(NET_BARS)).to_numpy())
        raw = np.nan_to_num(raw, nan=0.0)
    else:
        raise ValueError(method)
    raw = raw.astype(int)
    htf_close_time = htf_df.index + pd.Timedelta(minutes=htf_step_min)
    ev_time = event_index + pd.Timedelta(minutes=event_step_min)
    pos = htf_close_time.searchsorted(ev_time, side="right") - 1
    out = np.zeros(len(event_index), dtype=int)
    ok = pos >= 0
    out[ok] = raw[pos[ok]]
    return out


def config_context(name, clean, method="ema"):
    ev_tf, ctx_tfs = CONFIGS[name]
    ev_df = clean[ev_tf]
    labs = [htf_labels(clean[t], rc.STEP_MINUTES[t], ev_df.index, rc.STEP_MINUTES[ev_tf], method) for t in ctx_tfs]
    if len(labs) == 1:
        return labs[0]
    agree = (labs[0] == labs[1]) & (labs[0] != 0)
    return np.where(agree, labs[0], 0)


# ---------------------------------------------------------------------------
# experiment
# ---------------------------------------------------------------------------
def _episodes(P, mask, ctx, group, dev=True):
    """group: 'aligned' | 'counter' | 'mixed'. Returns collapsed event indices."""
    idx = np.where(mask & P.valid)[0]
    idx = idx[idx < P.cut] if dev else idx[idx >= P.hold_start]
    if group == "aligned":
        idx = idx[(ctx[idx] != 0) & (P.dirn[idx] == ctx[idx])]
    elif group == "counter":
        idx = idx[(ctx[idx] != 0) & (P.dirn[idx] == -ctx[idx])]
    elif group == "mixed":
        idx = idx[ctx[idx] == 0]
    if len(idx) == 0:
        return idx
    _, kept = rm.collapse_episodes(idx, P.dirn[idx], w1.K_EPISODE)
    return idx[kept]


def _arm(P, idxs, worst=False):
    strata, counts = w1.tally(P, idxs, 0.0, worst)
    t = sum(v[0] for v in strata.values())
    n = sum(sum(v) for v in strata.values())
    return {"strata": strata, "counts": counts, "p": (t / n) if n else None, "ci": w1.wilson(t, n), "n": n, "episodes": int(len(idxs))}


def _gap(arm_a, arm_c):
    sd = w1.stratified_diff(arm_a["strata"], arm_c["strata"])
    if sd["diff"] is not None:
        sd["ci"] = (sd["diff"] - 1.96 * sd["se"], sd["diff"] + 1.96 * sd["se"])
    return sd


def run_config(P, ctx, k, status, dev=True, worst_case=True):
    ev_mask = P.ratio >= k
    ct_mask = P.ratio < 1.0
    out = {"k": k}
    for part, mask in (("events", ev_mask), ("controls", ct_mask)):
        a = _arm(P, _episodes(P, mask, ctx, "aligned", dev))
        c = _arm(P, _episodes(P, mask, ctx, "counter", dev))
        out[part] = {"aligned": {kk: v for kk, v in a.items() if kk != "strata"},
                     "counter": {kk: v for kk, v in c.items() if kk != "strata"}, "gap": _gap(a, c), "_a": a, "_c": c}
    mixed = _arm(P, _episodes(P, ev_mask, ctx, "mixed", dev))
    out["events_mixed"] = {kk: v for kk, v in mixed.items() if kk != "strata"}
    g_ev, g_ct = out["events"]["gap"], out["controls"]["gap"]
    if g_ev["diff"] is not None and g_ct["diff"] is not None:
        se = math.sqrt(g_ev["se"] ** 2 + g_ct["se"] ** 2)
        did = g_ev["diff"] - g_ct["diff"]
        out["did"] = {"diff": did, "ci": (did - 1.96 * se, did + 1.96 * se), "z": did / se if se > 0 else 0.0}
    if worst_case:
        a = _arm(P, _episodes(P, ev_mask, ctx, "aligned", dev), worst=True)
        c = _arm(P, _episodes(P, ev_mask, ctx, "counter", dev), worst=True)
        out["worst_case_gap"] = _gap(a, c)["diff"]
    n_min = min(out["events"]["aligned"]["n"], out["events"]["counter"]["n"])
    out["n_min_resolved"] = n_min
    out["sample_label"] = w1.sample_label(n_min)
    tot = out["events"]["aligned"]["episodes"] + out["events"]["counter"]["episodes"]
    out["share_aligned_of_known"] = (out["events"]["aligned"]["episodes"] / tot) if tot else None
    for part in ("events", "controls"):
        out[part].pop("_a")
        out[part].pop("_c")
    return out


def run_wave3(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    clean = {tf: (preloaded[tf] if preloaded else rc.clean_timeframe(tf, raw_dir, source)[0]) for tf in ("5m", "15m", "1h")}
    s5 = rm.Series(clean["5m"], 5)
    P = {tf: w1.Prepared(tf, clean[tf], s_lo=s5 if tf != "5m" else None) for tf in clean}
    rep15 = rc.clean_timeframe("15m", raw_dir, source)[2] if not preloaded else {
        "first": None, "last": None, "raw_lines_parsed": 0, "unique_timestamps": 0, "duplicate_lines": 0, "conflicting_duplicates": 0,
        "dropped_market_closed": 0, "dropped_off_grid": 0, "dropped_invalid_ohlc": 0, "kept_bars": len(clean["15m"]),
        "unexplained_gaps": 0, "missing_open_bars": 0}
    a6 = rg.gate_a6({"15m": clean["15m"]}, {"15m": rep15}, {"15m": P["15m"].s})
    status = rg.gate_a7({"15m": clean["15m"]}, {"15m": P["15m"].s}, a6)["details"]["status"]
    res = {"configs": {}, "alt_context_net20": {}}
    for name, (ev_tf, _) in CONFIGS.items():
        ctx = config_context(name, clean, "ema")
        res["configs"][name] = {"events_tf": ev_tf, "context_tfs": list(CONFIGS[name][1]),
                                "known_context_share": float((ctx[: P[ev_tf].cut] != 0).mean()),
                                "by_k": {str(k): run_config(P[ev_tf], ctx, k, status) for k in K_ALL}}
        ctx2 = config_context(name, clean, "net")
        res["alt_context_net20"][name] = run_config(P[ev_tf], ctx2, K_PRIMARY, status, worst_case=False)
    tests = []
    for name in CONFIGS:
        g = res["configs"][name]["by_k"][str(K_PRIMARY)]["events"]["gap"]
        if g["p"] is not None:
            tests.append((name, g["p"]))
    reject, qv = w1.bh_fdr([p for _, p in tests])
    fdr = {n: (rj, q) for (n, _), rj, q in zip(tests, reject, qv)}
    for name in CONFIGS:
        r = res["configs"][name]["by_k"][str(K_PRIMARY)]
        g = r["events"]["gap"]
        rj, q = fdr.get(name, (False, None))
        r["fdr_reject"], r["q_value"] = rj, q
        if g["diff"] is None:
            r["verdict"] = "DATA-LIMITED"
        else:
            r["verdict"] = w1.verdict(g["diff"], g["ci"][0], g["ci"][1], rj, r["n_min_resolved"], status, MIN_EFFECT)
            wc = r.get("worst_case_gap")
            if wc is not None and g["diff"] * wc <= 0:
                r["verdict"] += " [FRAGILE: sign flips under worst-case ambiguity]"
            did = r.get("did")
            if did and (did["ci"][0] <= 0 <= did["ci"][1]) and r["verdict"].startswith(("SMALL_EFFECT", "EXPLORATORY", "DISCOVERY")):
                r["verdict"] += " [context gap not clearly specific to displacement]"
    return {"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False,
            "n_primary_tests": len(tests), **res}


def _pp(x):
    return "n/a" if x is None else f"{100 * x:+.1f}pp"


def render_markdown(r):
    L = ["# Observatory — Wave 3 report (timeframe combinations)\n",
         f"Contract hash `{r['contract_hash']}` · dataset status **{r['dataset_status']}** · holdout opened: **False** · primary tests (FDR family): {r['n_primary_tests']}\n",
         "**Development data only. The higher-timeframe label is read from CLOSED bars only. Nothing here is a trading rule.**\n",
         "Aligned = the displacement goes WITH the higher-timeframe direction (close above/below its EMA50); counter = against it. "
         "p = P(+1R before −1R) continuing the displacement. Primary = aligned − counter among displacements (k ≥ 1.5), stratified. "
         "The same gap among ordinary bars is shown as the control; DiD = displacement gap minus ordinary-bar gap.\n"]
    desc = {"T4": "events 15m · context 1h", "T5": "events 5m · context 15m", "T6": "events 5m · context 1h", "T7": "events 5m · context 1h and 15m agree"}
    for name, c in r["configs"].items():
        L.append(f"## {name} — {desc[name]}  (context known for {c['known_context_share']:.0%} of dev bars)\n")
        for kk, v in c["by_k"].items():
            ev, ct = v["events"], v["controls"]
            g = ev["gap"]
            L.append(f"### {name} · displacement ≥ {kk} ATR" + ("  **(primary)**" if float(kk) == K_PRIMARY else "  (descriptive)"))
            if g["diff"] is None:
                L.append(f"- DATA-LIMITED (aligned {ev['aligned']['episodes']}, counter {ev['counter']['episodes']})\n")
                continue
            L.append(f"- episodes: aligned {ev['aligned']['episodes']}, counter {ev['counter']['episodes']}, mixed/unknown {v['events_mixed']['episodes']} · aligned share {v['share_aligned_of_known']:.0%} · sample: {v['sample_label']}")
            L.append(f"- p(aligned) {ev['aligned']['p']:.3f} {ev['aligned']['ci']} · p(counter) {ev['counter']['p']:.3f} {ev['counter']['ci']}")
            L.append(f"- **aligned − counter {_pp(g['diff'])}** (CI {_pp(g['ci'][0])} to {_pp(g['ci'][1])}) · p={g['p']:.4f}" +
                     (f" · q={v['q_value']:.4f}" if "q_value" in v and v["q_value"] is not None else ""))
            cg = ct["gap"]
            if cg["diff"] is not None:
                L.append(f"- same gap among ORDINARY bars: {_pp(cg['diff'])} (CI {_pp(cg['ci'][0])} to {_pp(cg['ci'][1])})")
            if "did" in v:
                L.append(f"- difference-in-differences (displacement gap − ordinary gap): {_pp(v['did']['diff'])} (CI {_pp(v['did']['ci'][0])} to {_pp(v['did']['ci'][1])})")
            if v.get("worst_case_gap") is not None:
                L.append(f"- worst-case ambiguity gap {_pp(v['worst_case_gap'])}")
            if v["events_mixed"]["p"] is not None:
                L.append(f"- mixed/unknown context: p={v['events_mixed']['p']:.3f} (n={v['events_mixed']['n']})")
            if "verdict" in v:
                L.append(f"- **verdict: {v['verdict']}**")
            L.append("")
    L.append("## Descriptive alternative context (sign of the 20-bar net change; not in the FDR family; k ≥ 1.5)\n")
    for name, v in r["alt_context_net20"].items():
        g = v["events"]["gap"]
        L.append(f"- {name}: aligned − counter {_pp(g['diff'])}" + ("" if g["diff"] is None else f" (CI {_pp(g['ci'][0])} to {_pp(g['ci'][1])})") +
                 f" · episodes {v['events']['aligned']['episodes']} / {v['events']['counter']['episodes']}")
    L.append("\n## Not run in this wave\n- Holdout: sealed.\n- FVG × context, other context definitions, session/extension conditioning (later waves).\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _aggregate(df5, minutes):
    d = df5[["open", "high", "low", "close"]].resample(f"{minutes}min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    d["gap_before_missing"], d["closure_before"] = 0, False
    return d


def selftest(null_series=20):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    # 1. closed-bar alignment on a hand-built hourly series whose EMA sign is +1 until 10:00 bar, then -1
    t0 = pd.Timestamp("2026-03-09 00:00", tz="UTC")
    hours = pd.DatetimeIndex([t0 + pd.Timedelta(hours=i) for i in range(120)])
    close = np.concatenate([np.linspace(100, 110, 60), np.linspace(110, 90, 60)])
    h = pd.DataFrame({"open": close, "high": close + 0.1, "low": close - 0.1, "close": close}, index=hours)
    ema_sign = np.sign(close - pd.Series(close).ewm(span=EMA_SPAN, adjust=False).mean().to_numpy())
    ev_idx = pd.DatetimeIndex([pd.Timestamp("2026-03-11 12:55", tz="UTC"), pd.Timestamp("2026-03-11 13:00", tz="UTC"), pd.Timestamp("2026-03-11 12:50", tz="UTC")])
    labs = htf_labels(h, 60, ev_idx, 5)
    # event bar opening 12:55 closes 13:00 -> the 12:00 HTF bar (closes 13:00) is usable; 13:00 event closes 13:05 -> still the 12:00 bar
    pos12 = list(hours).index(pd.Timestamp("2026-03-11 12:00", tz="UTC"))
    pos11 = pos12 - 1
    chk("event closing exactly at the HTF close uses that bar", int(labs[0]), int(ema_sign[pos12]))
    chk("next 5m event still uses the 12:00 bar", int(labs[1]), int(ema_sign[pos12]))
    chk("event closing before the HTF close uses the previous bar", int(labs[2]), int(ema_sign[pos11]))
    chk("warm-up gives unknown", int(htf_labels(h, 60, pd.DatetimeIndex([hours[10] + pd.Timedelta(minutes=5)]), 5)[0]), 0)
    # 2. no lookahead: labels for early events are identical when the future HTF data is removed or altered
    rng = np.random.default_rng(3)
    from . import gates as g
    ser = g._random_walk_series(9000, rng, step=5)
    d5 = ser.df
    d15, d60 = _aggregate(d5, 15), _aggregate(d5, 60)
    full = htf_labels(d60, 60, d5.index, 5)
    cut_t = d5.index[6000]
    trunc60 = d60[d60.index + pd.Timedelta(minutes=60) <= cut_t + pd.Timedelta(minutes=5)]
    part = htf_labels(trunc60, 60, d5.index[:6001], 5)
    chk("labels unchanged when the future is removed", bool((full[:6001] == part).all()), True)
    alt = d60.copy()
    alt.loc[alt.index > cut_t, "close"] = alt.loc[alt.index > cut_t, "close"] * 1.5
    chk("labels unchanged when the future is altered", bool((htf_labels(alt, 60, d5.index[:6001], 5) == full[:6001]).all()), True)
    # 3. T7 rule: both must agree
    cl = {"5m": d5, "15m": d15, "1h": d60}
    c7 = config_context("T7", cl)
    l1, l2 = htf_labels(d60, 60, d5.index, 5), htf_labels(d15, 15, d5.index, 5)
    chk("T7 = agreement of 1h and 15m", bool((c7 == np.where((l1 == l2) & (l1 != 0), l1, 0)).all()), True)
    chk("T7 has mixed events", bool((c7 == 0).any()), True)
    # 4. aligned/counter definition
    P = w1.Prepared("5m", d5)
    fake_ctx = np.ones(P.n, dtype=int)
    ia = _episodes(P, P.ratio >= 1.0, fake_ctx, "aligned")
    ic = _episodes(P, P.ratio >= 1.0, fake_ctx, "counter")
    chk("aligned events all go with the context", bool((P.dirn[ia] == 1).all()), True)
    chk("counter events all go against the context", bool((P.dirn[ic] == -1).all()), True)
    chk("aligned and counter are disjoint", len(set(ia) & set(ic)), 0)
    # 5. null: on random walks (context unrelated to the future) the primary gap should not look informative
    fp, used, diffs = 0, 0, []
    for _ in range(null_series):
        s_ = g._random_walk_series(14000, rng, step=5)
        d5_ = s_.df
        cl_ = {"5m": d5_, "15m": _aggregate(d5_, 15), "1h": _aggregate(d5_, 60)}
        P_ = w1.Prepared("5m", d5_)
        r = run_config(P_, config_context("T6", cl_), 1.0, "EXPLORATORY_READY", worst_case=False)
        gp = r["events"]["gap"]
        if gp["p"] is None:
            continue
        used += 1
        diffs.append(gp["diff"])
        fp += 1 if gp["p"] < 0.05 else 0
    chk("null series usable", used >= 12, True)
    chk("null false-positive rate <= 20%", used > 0 and fp / used <= 0.20, True)
    chk("null mean gap near zero", abs(float(np.mean(diffs))) < 0.02, True)
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": used, "false_positive_rate": round(fp / used, 3) if used else None,
                     "mean_gap": round(float(np.mean(diffs)), 4) if diffs else None}}


def main():
    ap = argparse.ArgumentParser(description="Observatory Wave 3 (timeframe combinations). Read-only on raw data.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    res = run_wave3(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "wave3_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "wave3_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
