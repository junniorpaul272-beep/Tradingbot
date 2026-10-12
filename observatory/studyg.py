"""
observatory/studyg.py
=====================
OBSERVATORY — STUDY G: session highs and lows (sweep or break?). ISOLATED from the live bot.

Tests inside this study are G.1 (rejected sweep) and G.2 (accepted break), by session. The claim behind the idea: price crosses a session's high or low, then EITHER reverses back into
the range (a "liquidity sweep") OR continues beyond it (a breakout). Several different claims hide in that sentence and are tested separately; nothing here assumes stop orders,
liquidity or "smart money" exist: only price behaviour is measured.

SESSIONS (fixed UTC clock, no daylight-saving ambiguity; each level is a FINAL level known at the session end, and only later bars are eligible)
  Asian    session 00:00-07:00 UTC, level known at 07:00, events searched 07:00-16:00 UTC (London session and the overlap).
  London   session 07:00-12:00 UTC (the London morning range), level known at 12:00, events searched 12:00-21:00 UTC (New York).
  A session is used only if >= 80% of its bars exist with no gap over 1h, the following window is >= 80% present, and its range is 0.5-12 ATR(1h) at the window start.
  High and low are the same event in mirror (prices negated) and are pooled.

THE FIRST PUSH BEYOND THE LEVEL (a three-way, exhaustive classification, decided by the bar that FIRST trades 0.05 ATR beyond the level, coming from below)
  rejected  that bar CLOSES back at or below the level                         -> G.1 event ("sweep and reject"), decision = that close. Claim: REVERSAL (excess < 0).
  accepted  it closes above the level AND the next two closes also stay above it, the third by >= 0.10 ATR (15 minutes of holding)
                                                                                -> G.2 event ("break and hold"), decision = the third close. Claim: CONTINUATION (excess > 0).
  neither   it closes above the level but does not hold (falls back inside within 15 minutes, or holds by less than 0.10 ATR) -> counted, not used.
  One event per session level per type. Windows that start with price already beyond the level are counted and excluded.

OUTCOMES (separate, as asked)
  Direction: +1 ATR (through the level) before -1 ATR (away) within 24h of the decision close, minus a placebo (30 random 5m closes, same weekday, hour +-1h, trailing-1h move,
  >= 24h away). Expansion (descriptive, with cluster CI, not in a family): the 24h forward range in ATR minus the same placebo. Raw path (descriptive): mean maximum rise and fall.

FAMILIES (BH-FDR q = 0.10 inside each; two-sided; cluster-robust by 2-day block)
  family 1 (primary, 4 tests): G.1 Asian, G.1 London, G.2 Asian, G.2 London.
  family 2 (8 tests): level-specificity (event at the session extreme minus the same event definition at the MID-RANGE level, an ordinary level at a comparable distance) x4;
                      session-specificity (Asian minus London) x2 (G.1, G.2); timing (early half minus late half of the search window, both sessions pooled) x2.
Practical threshold: 0.05 in outcome probability (5 points), as in Studies B-F. The holdout (>= HOLDOUT_START_UTC) is sealed: no outcome is computed for it.
Overlap ledger: the Asian high/low is often near the previous-day extreme tested in Study F, and the London range sits inside the same day. Agreement between G and F is NOT independent
confirmation. Reaching a barrier is not a profitable trade; 'setup net' pips are descriptive (flat spread, +-1 ATR symmetric exits).

Run:  python3 -m observatory.studyg [--raw-dir ...] [--out ...]   --selftest   --counts   --readiness   --power
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
from .studyb import window_ok, draw_placebo, NS_5, NS_D, NS_H, HMAX, PIP, FDR_Q, SEED
from .studya import verdict, _ns, _phi, _a, _ci, _p
from .studyc import cluster_test, cluster_diff, MIN_EFFECT

SESSIONS = {"asia": (0, 7, 16), "london": (7, 12, 21)}      # (session start hour, session end hour = level known, search-window end hour), UTC
EPS_ATR, ACC_ATR, ACC_BARS = 0.05, 0.10, 3
SES_MIN_FRAC, SES_MAX_GAP = 0.80, 12
R_ATR = (0.5, 12.0)
CTRL_F = 0.5
KINDS = ("rej", "acc")
TEST_KEYS = [f"G:{k}:{s}" for k in KINDS for s in SESSIONS]
CTRL_KEYS = [f"C:{k}:{s}" for k in KINDS for s in SESSIONS]
ALL_KEYS = TEST_KEYS + CTRL_KEYS
SPEC_KEYS = [f"X:{k}" for k in TEST_KEYS] + ["S:rej", "S:acc", "T:rej", "T:acc"]
CLAIM = {"rej": "fade", "acc": "continue"}
CONTRACT = {
    "study": "G", "module": "studyg", "version": "v1", "instrument": "GBPUSD",
    "sessions_utc": {k: {"session": [v[0], v[1]], "search_until": v[2]} for k, v in SESSIONS.items()},
    "session_filters": {"min_frac": SES_MIN_FRAC, "max_gap_bars": SES_MAX_GAP, "range_atr": list(R_ATR)},
    "first_push": {"eps_atr": EPS_ATR, "accept_atr": ACC_ATR, "rejected": "first push closes at or below the level", "accepted": "first push closes above and the next two closes stay above, the third >= accept_atr beyond (decision = third close)", "accept_bars": ACC_BARS},
    "control": {"level": "mid-range", "f": CTRL_F}, "outcome": "+-1 ATR first passage within 24h minus placebo (weekday, hour +-1h, trailing 1h move)",
    "inference": "two-sided, cluster-robust by 2-day UTC block", "min_effect": MIN_EFFECT, "fdr_q": FDR_Q,
    "family1": TEST_KEYS, "family2": ["boundary vs mid-range x4", "Asian vs London x2", "early vs late x2"], "holdout_start_utc": str(rc.HOLDOUT_START_UTC),
}
CONTRACT_HASH = hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# levels and events
# ---------------------------------------------------------------------------
def build_levels(ctx):
    """Final session levels (in this context's coordinates) with the search window that follows them. Uses only bars before the window starts."""
    ns5, h, l = ctx["ns5"], ctx["A"]["h5"], ctx["A"]["l5"]
    n = len(ns5)
    out = {s: [] for s in SESSIONS}
    cnt = {s: {"sessions": 0, "incomplete": 0, "no_window": 0, "no_atr": 0, "range_out": 0, "usable": 0} for s in SESSIONS}
    if n < 2:
        return out, cnt
    for d in range(int(ns5[0] // NS_D), int(ns5[-1] // NS_D) + 1):
        d0 = d * NS_D
        for name, (sh, eh, wh) in SESSIONS.items():
            c = cnt[name]
            ts, te, we = d0 + sh * NS_H, d0 + eh * NS_H, d0 + wh * NS_H
            i0, i1, i2 = (int(np.searchsorted(ns5, x, side="left")) for x in (ts, te, we))
            c["sessions"] += 1
            if i1 - i0 < SES_MIN_FRAC * (eh - sh) * 12 or (i1 - i0 > 1 and int(np.diff(ns5[i0:i1]).max()) > SES_MAX_GAP * NS_5):
                c["incomplete"] += 1
                continue
            if i1 >= n or int(ns5[i1]) - te > 2 * NS_5 or i2 - i1 < SES_MIN_FRAC * (wh - eh) * 12:
                c["no_window"] += 1
                continue
            a = float(ctx["atr_t"][i1])
            if not (np.isfinite(a) and a > 0):
                c["no_atr"] += 1
                continue
            H = float(h[i0:i1].max())
            R = H - float(l[i0:i1].min())
            if not (R_ATR[0] <= R / a <= R_ATR[1]):
                c["range_out"] += 1
                continue
            out[name].append({"day": d, "s": i1, "e": i2, "H": H, "R": R, "a": a})
            c["usable"] += 1
    return out, cnt


def ctx_g(s5, s1h, sign, hold_ns):
    ctx = sb.make_ctx(s5, s1h, sign, hold_ns)
    ctx["levels"], ctx["level_cnt"] = build_levels(ctx)
    return ctx


def _gate():
    return {"detected": 0, "no_exceed": 0, "gap": 0, "started_beyond": 0, "other_type": 0, "neither": 0, "outside_region": 0, "window": 0, "no_trail": 0}


def detect(ctx, sess, kind, f, region, cnt):
    ns5, h, c = ctx["ns5"], ctx["A"]["h5"], ctx["A"]["c5"]
    out = []
    for lv in ctx["levels"][sess]:
        s, e, a = lv["s"], lv["e"], lv["a"]
        level = lv["H"] - f * lv["R"]
        hit = np.flatnonzero(h[s:e] >= level + EPS_ATR * a)
        if hit.size == 0:
            cnt["no_exceed"] += 1
            continue
        p = s + int(hit[0])
        if p == 0 or ns5[p] - ns5[p - 1] != NS_5:
            cnt["gap"] += 1
            continue
        if c[p - 1] >= level:
            cnt["started_beyond"] += 1
            continue
        if c[p] <= level:
            typ, pdec = "rej", p
        else:
            q = p + ACC_BARS - 1
            if q >= e or ns5[q] - ns5[p] != (ACC_BARS - 1) * NS_5:
                cnt["neither"] += 1
                continue
            cs = c[p:q + 1]
            if np.all(cs > level) and cs[-1] >= level + ACC_ATR * a:
                typ, pdec = "acc", q
            else:
                cnt["neither"] += 1
                continue
        if typ != kind:
            cnt["other_type"] += 1
            continue
        p = pdec
        cnt["detected"] += 1
        if sf._admit(ctx, p, region, cnt):
            out.append({"p": int(p), "t": int(ns5[p]), "x": float(ctx["x"][p]), "side": ctx["sign"], "atr": float(ctx["atr_t"][p]), "f": f, "sess": sess,
                        "frac": (p - s) / float(e - s), "day": lv["day"]})
    return out


def events_for(ctx, key, region="dev"):
    cnt = _gate()
    kind_pfx, kind, sess = key.split(":")
    return detect(ctx, sess, kind, 0.0 if kind_pfx == "G" else CTRL_F, region, cnt), cnt


# ---------------------------------------------------------------------------
# scoring and summaries
# ---------------------------------------------------------------------------
def _path(A, p, atr):
    c0 = A["c5"][p]
    hh, ll = A["h5"][p + 1:p + 1 + HMAX], A["l5"][p + 1:p + 1 + HMAX]
    up, dn = float((hh.max() - c0) / atr), float((c0 - ll.min()) / atr)
    return up, dn, float((hh.max() - ll.min()) / atr)


def score_g(ctx, e, rng, rng_pl):
    row, why = sd.score_sym(ctx, e, rng)
    if row is None:
        return None, why
    A = ctx["A"]
    row["up"], row["dn"], row["rng"] = _path(A, e["p"], e["atr"])
    row.update(sess=e["sess"], frac=e["frac"], f=e["f"])
    pl = draw_placebo(ctx, e["t"], e["x"], rng_pl)
    if pl is not None:
        ex = [_path(A, int(q), float(a)) for q, a in zip(*pl)]
        row["pup"], row["pdn"], row["prng"] = (float(np.mean([x[i] for x in ex])) for i in range(3))
    return row, "ok"


def _score(evs, tag, au):
    rows = []
    for ctx, e in evs:
        seed = SEED + int(e["t"] % 2 ** 31) + 17 * tag + (1 if ctx["sign"] < 0 else 0)
        s, why = score_g(ctx, e, np.random.default_rng(seed), np.random.default_rng(seed + 7))
        if s is None:
            au[why] = au.get(why, 0) + 1
            continue
        au["used"] = au.get("used", 0) + 1
        rows.append(s)
    return rows


def collect(s5, s1h, hold_ns, keys=None):
    keys = ALL_KEYS if keys is None else keys
    ctxs = [ctx_g(s5, s1h, +1, hold_ns), ctx_g(s5, s1h, -1, hold_ns)]
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
    lev = {s: {k: ctxs[0]["level_cnt"][s][k] for k in ctxs[0]["level_cnt"][s]} for s in SESSIONS}
    return {"rows": rows, "audit": audit, "levels": lev}


def summarize(rows, fade):
    t = sd.summarize(rows, fade)
    if not rows or t.get("mean") is None:
        return t
    ex = [r for r in rows if "pup" in r]
    if len(ex) >= 30:
        ts = np.array([r["t"] for r in ex], np.int64)
        t["path"] = {"event_rise_atr": float(np.mean([r["up"] for r in ex])), "placebo_rise_atr": float(np.mean([r["pup"] for r in ex])),
                     "event_fall_atr": float(np.mean([r["dn"] for r in ex])), "placebo_fall_atr": float(np.mean([r["pdn"] for r in ex])),
                     "event_range_atr": float(np.mean([r["rng"] for r in ex])), "placebo_range_atr": float(np.mean([r["prng"] for r in ex])),
                     "rise_excess": cluster_test([r["up"] - r["pup"] for r in ex], ts), "fall_excess": cluster_test([r["dn"] - r["pdn"] for r in ex], ts),
                     "range_excess": cluster_test([r["rng"] - r["prng"] for r in ex], ts)}
    return t


def _diff(g, c):
    t = cluster_diff([r["d"] for r in g], [r["t"] for r in g], [r["d"] for r in c], [r["t"] for r in c])
    t.update(mean_a=float(np.mean([r["d"] for r in g])) if g else None, mean_b=float(np.mean([r["d"] for r in c])) if c else None)
    return t


def contrasts(R):
    out = {}
    for k in TEST_KEYS:
        out["X:" + k] = _diff(R[k], R["C" + k[1:]])
    for kind in KINDS:
        out["S:" + kind] = _diff(R[f"G:{kind}:asia"], R[f"G:{kind}:london"])
        rows = R[f"G:{kind}:asia"] + R[f"G:{kind}:london"]
        out["T:" + kind] = _diff([r for r in rows if r["frac"] < 0.5], [r for r in rows if r["frac"] >= 0.5])
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


def _word(key, m):
    if key.split(":")[1] == "rej":
        return "REVERSAL side (price moved away from the level more than placebo)" if m < 0 else "CONTINUATION side (the OPPOSITE of the sweep claim)"
    return "CONTINUATION side (price kept going through the level more than placebo)" if m > 0 else "REVERSAL side (the OPPOSITE of the break-and-hold claim)"


def reading(res):
    out = []
    names = {"rej": "G.1 sweep and reject", "acc": "G.2 break and hold"}
    for kind in KINDS:
        s = [(k, res["T"][k]) for k in TEST_KEYS if k.split(":")[1] == kind and res["T"][k].get("fdr_reject")]
        if not s:
            out.append(f"{names[kind]}: no session survives multiple-testing control: no demonstrated information beyond the clock slot and the recent move.")
            continue
        parts = []
        for k, v in s:
            x = res["X"].get("X:" + k, {})
            sp = "; specificity vs the mid-range level survives" if x.get("fdr_reject") else "; specificity vs the mid-range level does NOT survive: the session extreme is not shown to be what carries it"
            parts.append(f"{k.split(':')[2]}: excess {v['mean']:+.3f} ({_word(k, v['mean'])}; observed {100 * v['obs_rate']:.1f}% vs placebo {100 * v['placebo_rate']:.1f}%, q={v['q_value']:.3f}){sp}")
        out.append(f"{names[kind]} survives: " + " | ".join(parts) + ".")
    ss = [k for k in ("S:rej", "S:acc") if res["X"][k].get("fdr_reject")]
    tt = [k for k in ("T:rej", "T:acc") if res["X"][k].get("fdr_reject")]
    out.append("Session-specificity (Asian vs London): " + (", ".join(ss) + " survives (see the table)." if ss else "no difference survives."))
    out.append("Timing (early vs late in the search window): " + (", ".join(tt) + " survives (see the table)." if tt else "no difference survives."))
    out.append("Expansion is reported separately in the path table (descriptive). G overlaps Study F (the Asian range often sits near the previous-day extremes): they are not independent confirmations.")
    return " ".join(out)


def run_from_series(s5, s1h, status, hold_ns):
    col = collect(s5, s1h, hold_ns)
    R = col["rows"]
    res = {"T": {k: summarize(R[k], CLAIM[k.split(":")[1]] == "fade") for k in TEST_KEYS}, "ctrl": {k: summarize(R[k], CLAIM[k.split(":")[1]] == "fade") for k in CTRL_KEYS}, "X": contrasts(R)}
    res["n_tests"] = apply_fdr(res, status)
    res["audit"], res["levels"] = col["audit"], col["levels"]
    res["reading"] = reading(res)
    res.update({"contract": CONTRACT, "contract_hash": CONTRACT_HASH, "dataset_status": status, "holdout_opened": False})
    return res


def run_studyg(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    res = run_from_series(s5, s1h, status, hold_ns)
    res["development"] = {"first_bar_utc": str(s5.index[0]), "boundary_utc": str(rc.HOLDOUT_START_UTC)}
    return res


def structure_counts(raw_dir=rc.DEFAULT_RAW_DIR, source="observatory", preloaded=None):
    """Structure only: no outcome is computed. Session levels and events per test, development and holdout."""
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    ctxs = [ctx_g(s5, s1h, +1, hold_ns), ctx_g(s5, s1h, -1, hold_ns)]
    out = {"dataset_status": status, "bars_5m": len(s5.index), "levels": ctxs[0]["level_cnt"], "tests": {}}
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
        dev_res = run_studyg(raw_dir, source, preloaded)
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


# ---------------------------------------------------------------------------
# power simulation with the real event structure
# ---------------------------------------------------------------------------
def _plant(df, ctxs, key, size, direction, ramp=48):
    """direction -1: a reversal (fade) after each event of `key`; +1: continuation. size in ATR over `ramp` bars, permanent afterwards."""
    n5 = len(df)
    shift = np.zeros(n5)
    tt = np.arange(n5)
    for ctx in ctxs:
        ev, _ = events_for(ctx, key, "dev")
        for e in ev:
            shift += direction * ctx["sign"] * size * e["atr"] * np.clip(tt - e["p"], 0, ramp) / float(ramp)
    d2 = df.copy()
    so = np.concatenate([[0.0], shift[:-1]])
    d2["close"] = df["close"] + shift
    d2["open"] = df["open"] + so
    d2["high"] = np.maximum(df["high"] + shift, np.maximum(d2["open"], d2["close"]))
    d2["low"] = np.minimum(df["low"] + shift, np.minimum(d2["open"], d2["close"]))
    return d2


def power_cell(key, size, n_rep, seed, n5=130000, garch=True):
    far = pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value
    rng = np.random.default_rng(seed)
    direction = -1.0 if key.split(":")[1] == "rej" else 1.0
    hits, zs, ex, ns = 0, [], [], []
    for _ in range(n_rep):
        df = sb._synth_ohlc(n5, rng, garch=garch)
        if size > 0:
            s5, s1h = sa._series_from_df(df)
            df = _plant(df, [ctx_g(s5, s1h, +1, far), ctx_g(s5, s1h, -1, far)], key, size, direction)
        s5, s1h = sa._series_from_df(df)
        rows = collect(s5, s1h, far, [key])["rows"][key]
        t = summarize(rows, direction < 0)
        if t.get("mean") is None:
            continue
        zs.append(t["z"])
        ex.append(t["mean"])
        ns.append(t["n"])
        hits += int(t["p"] < 0.05 and (t["mean"] * direction) > 0)
    return {"key": key, "size": size, "reps": n_rep, "detect_rate": hits / n_rep, "mean_z": float(np.mean(zs)) if zs else None,
            "mean_excess": float(np.mean(ex)) if ex else None, "mean_events": float(np.mean(ns)) if ns else None}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def render_markdown(r):
    n1, n2 = r["n_tests"][1], r["n_tests"][2]
    L = ["# Observatory — Study G: session highs and lows (GBPUSD)\n",
         f"Contract `{r['contract_hash']}` · dataset **{r['dataset_status']}** · holdout opened: **False** · family 1: {n1} tests · family 2: {n2} tests\n",
         "**Development data only. Sessions (UTC): Asian 00-07 (events 07-16), London 00-07 → 07-12 (events 12-21). The first push 0.05 ATR beyond a FINAL session level is classed as REJECTED "
         "(closes back at/below the level: G.1, claim reversal) or ACCEPTED (holds above for 15 minutes: G.2, claim continuation). Outcome = +1 ATR through the level before −1 ATR away within 24h, minus a "
         "placebo with the same weekday, hour and recent move. Two-sided, 2-day-block clustered. Nothing here is a trading rule.**\n",
         "## Reading (pre-declared)\n", f"**{r['reading']}**\n"]
    hd = ("| test | events | observed | placebo | excess | 95% CI | z | q | pips/event | setup net (pips) | placebo net | verdict |\n|---|---|---|---|---|---|---|---|---|---|---|---|")

    def trow(lab, t):
        if t.get("mean") is None:
            return f"| {lab} | {t.get('n', 0)} | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | **{t.get('verdict', 'DATA-LIMITED')}** |"
        return (f"| {lab} | {t['n']} | {100 * t['obs_rate']:.1f}% | {100 * t['placebo_rate']:.1f}% | {t['mean']:+.3f} | [{t['ci'][0]:+.3f}, {t['ci'][1]:+.3f}] | {t['z']:+.2f} | "
                f"{_p(t.get('q_value'))} | {t['excess_pips']:+.2f} | {t['setup_net_pips']:+.2f} | {t['placebo_net_pips']:+.2f} | **{t.get('verdict', '')}** |")
    nm = {"rej": "G.1 sweep and reject", "acc": "G.2 break and hold"}
    sn = {"asia": "Asian high/low", "london": "London high/low"}
    L += ["## G.1 / G.2 by session (family 1)\n", hd] + [trow(f"{nm[k.split(':')[1]]} · {sn[k.split(':')[2]]}", r["T"][k]) for k in TEST_KEYS]
    L += ["\n## Controls: the same event definition at the MID-RANGE level (not tested; for context)\n", hd] + [trow(f"{nm[k.split(':')[1]]} · mid-range of {sn[k.split(':')[2]]}", r["ctrl"][k]) for k in CTRL_KEYS]
    xn = {}
    for k in TEST_KEYS:
        xn["X:" + k] = f"level-specificity: {nm[k.split(':')[1]]} · {sn[k.split(':')[2]]} − mid-range"
    xn.update({"S:rej": "session: G.1 Asian − London", "S:acc": "session: G.2 Asian − London", "T:rej": "timing: G.1 early − late half of the window",
               "T:acc": "timing: G.2 early − late half of the window"})
    L += ["\n## Specificity, session and timing (family 2)\n", "| test | n (A / B) | difference | 95% CI | z | q | verdict |\n|---|---|---|---|---|---|---|"]
    for k in SPEC_KEYS:
        t = r["X"][k]
        if t.get("diff") is None:
            L.append(f"| {xn[k]} | {t.get('n1', 0)} / {t.get('n0', 0)} | n/a | n/a | n/a | n/a | **{t.get('verdict', 'DATA-LIMITED')}** |")
        else:
            L.append(f"| {xn[k]} | {t['n1']} / {t['n0']} | {t['diff']:+.3f} | [{t['ci'][0]:+.3f}, {t['ci'][1]:+.3f}] | {t['z']:+.2f} | {_p(t.get('q_value'))} | **{t.get('verdict', '')}** |")
    L += ["\n## Expansion and raw 24h path (descriptive; no stop, no target)\n",
          "Forward 24h range, maximum rise and maximum fall in ATR, events versus matched placebo bars. Rise is toward/through the level, fall is away from it. These answer 'does a sweep or break "
          "predict a bigger move', separately from which barrier comes first.\n",
          "| group | range (event) | range (placebo) | range excess | rise excess | fall excess |\n|---|---|---|---|---|---|"]
    for k in TEST_KEYS + CTRL_KEYS:
        t = (r["T"] if k in TEST_KEYS else r["ctrl"])[k]
        p = t.get("path")
        if not p:
            continue

        def e(x):
            return f"{_a(x.get('mean'))} [{_a(x['ci'][0]) if x.get('ci') else 'n/a'}, {_a(x['ci'][1]) if x.get('ci') else 'n/a'}]"
        L.append(f"| {k} | {p['event_range_atr']:.3f} | {p['placebo_range_atr']:.3f} | {e(p['range_excess'])} | {e(p['rise_excess'])} | {e(p['fall_excess'])} |")
    L += ["\n## High/low boundary and halves (descriptive)\n", "| test | high-level excess (n) | low-level excess (n) | first half | second half |\n|---|---|---|---|---|"]
    for k in TEST_KEYS:
        t = r["T"][k]
        if t.get("mean") is None:
            continue
        s, h = t.get("sides", {}), t.get("halves", {})

        def c(x):
            return f"{x['mean']:+.3f} ({x['n']})" if x and x.get("mean") is not None else "n/a"
        L.append(f"| {k} | {c(s.get('up'))} | {c(s.get('down'))} | {c(h.get('first_half'))} | {c(h.get('second_half'))} |")
    L += ["\n## Audit: levels and events at each gate\n", "Session levels: " + "; ".join(
        f"{s}: {v['sessions']} sessions, usable {v['usable']}, incomplete {v['incomplete']}, no window {v['no_window']}, no ATR {v['no_atr']}, range outside 0.5–12 ATR {v['range_out']}"
        for s, v in r["levels"].items()) + "\n",
          "| test | classified | no push beyond | started beyond | other type | neither | outside region | window | no trail | ambiguous | no placebo | scored |\n|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, a in r["audit"].items():
        L.append(f"| {k} | {a['detected']} | {a['no_exceed']} | {a['started_beyond']} | {a['other_type']} | {a['neither']} | {a['outside_region']} | {a['window']} | {a['no_trail']} | "
                 f"{a['ambiguous']} | {a['no_placebo']} | {a['used']} |")
    L.append("\n## Not run\n- Holdout (sealed). Other session definitions (the clock windows and the 15-minute hold are fixed, not tuned). Stop-hunt or order-flow mechanisms (OHLC only). Tradeability with stops and costs. Higher-timeframe levels (Study H).\n")
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


def _ctxs(df, hold=None):
    s5, s1h = sa._series_from_df(df)
    far = pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value if hold is None else hold
    return s5, s1h, [ctx_g(s5, s1h, +1, far), ctx_g(s5, s1h, -1, far)]


def selftest(null_series=6):
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    # ---- hand frame: a flat warm-up day (so ATR exists), then four days whose Asian session (84 bars) is a sine with a known high; the search window (bars 84-191) is scripted per day
    k84 = np.arange(84)
    sine = 1.30 + 0.002 * np.sin(2 * np.pi * k84 / 84.0)            # high 1.302 (+pad) at bar 21, low 1.298 at bar 63
    H = 1.302 + 0.0002

    def day(script):
        d = np.full(288, 1.30)
        d[:84] = sine
        for k, v in script.items():
            d[k] = v
        return d
    # day A: a spike beyond the level that closes back below it (REJECTED); day B: the first push closes beyond and HOLDS for three bars (ACCEPTED);
    # day C: the first push closes just above the level and falls back (NEITHER); day D: price jumps beyond at the very first window bar and stays (ACCEPTED, decision on the 3rd bar)
    dA, dB, dC = day({110: 1.3010}), day({110: 1.3060, 111: 1.3060, 112: 1.3060}), day({110: H + 0.0001, 111: 1.30})
    dD = day({k: 1.3050 for k in range(84, 120)})
    close = np.concatenate([np.full(288, 1.30), dA, dB, dC, dD, np.full(288 * 3, 1.30)])
    df = _hand_df(close)
    df.iloc[288 + 110, df.columns.get_loc("high")] = 1.3030         # day A: the spike's high is well beyond the level, its close is back inside
    s5, s1h, (cp, cm) = _ctxs(df)
    ns5 = _ns(s5.index)
    lv = cp["levels"]["asia"]
    chk("levels: the four scripted Asian sessions are usable (days 1-4 after the warm-up day)", [int(x["day"] - ns5[0] // NS_D) for x in lv][:4], [1, 2, 3, 4])
    chk("level = session high (sine peak + pad), window starts at the session end (07:00 = bar 84 of the day)", (abs(lv[0]["H"] - H) < 1e-9, lv[0]["s"] - 288, lv[0]["e"] - 288), (True, 84, 192))
    ev_r, c_r = events_for(cp, "G:rej:asia", "dev")
    ev_a, c_a = events_for(cp, "G:acc:asia", "dev")
    chk("rejected sweep: exactly one event, on the spike bar of day A", [(e["p"] - 288, round(e["frac"], 4)) for e in ev_r], [(110, round((110 - 84) / 108.0, 4))])
    chk("accepted break: two events, decided on the THIRD close (day B bar 112; day D bar 86)", [e["p"] - 288 * 2 for e in ev_a][:1] + [e["p"] - 288 * 4 for e in ev_a][1:], [112, 86])
    chk("classification is exhaustive and exclusive (neither: day C; other type: rej sees 2 accepted days, acc sees 1 rejected day)",
        (c_r["neither"], c_a["neither"], c_r["other_type"], c_a["other_type"]), (1, 1, 2, 1))
    # mirror: the negated frame in the LOW context gives the same events
    dn = df.copy()
    dn["open"], dn["close"], dn["high"], dn["low"] = -df["open"], -df["close"], -df["low"], -df["high"]
    s5n, s1n, (cpn, cmn) = _ctxs(dn)
    evn, _ = events_for(cmn, "G:rej:asia", "dev")
    chk("mirror: the negated frame in the low context gives the same rejected-sweep events", [(e["p"], round(e["x"], 9)) for e in evn], [(e["p"], round(e["x"], 9)) for e in ev_r])
    # no look-ahead: shift every bar after the event bar; events at or before it are unchanged
    p0 = 288 + 110
    df2 = df.copy()
    for col in ("open", "high", "low", "close"):
        df2.iloc[p0 + 1:, df2.columns.get_loc(col)] += 0.4
    _, _, (cp2, _) = _ctxs(df2)
    chk("no look-ahead: the event at or before an altered future is identical (position, trailing move, ATR)",
        [(e["p"], round(e["x"], 9), round(e["atr"], 9)) for e in events_for(cp2, "G:rej:asia", "dev")[0] if e["p"] <= p0], [(e["p"], round(e["x"], 9), round(e["atr"], 9)) for e in ev_r])
    # the level is only used after its session ends: events are never inside the session
    chk("every event bar is at or after the session end (07:00 for Asian)", all(((e["p"] - 288) % 288) >= 84 for e in ev_r + ev_a), True)
    # ---- synthetic world: independent re-derivation of the levels from pandas, event invariants, region sealing
    rng = np.random.default_rng(51)
    df = sb._synth_ohlc(80000, rng)
    s5, s1h = sa._series_from_df(df)
    ns5 = _ns(s5.index)
    far = pd.Timestamp("2099-01-01", tz="UTC").as_unit("ns").value
    cps = ctx_g(s5, s1h, +1, far)
    ok = True
    for name, (sh, eh, wh) in SESSIONS.items():
        for lv_ in cps["levels"][name]:
            d0_ = pd.Timestamp(lv_["day"] * NS_D, tz="UTC")
            seg = df[(df.index >= d0_ + pd.Timedelta(hours=sh)) & (df.index < d0_ + pd.Timedelta(hours=eh))]
            ok &= abs(seg["high"].max() - lv_["H"]) < 1e-12 and abs((seg["high"].max() - seg["low"].min()) - lv_["R"]) < 1e-12
    chk(f"synthetic world: every session level equals pandas' session high and range ({len(cps['levels']['asia'])} Asian, {len(cps['levels']['london'])} London)", bool(ok), True)
    h, c = np.asarray(s5.h), np.asarray(s5.c)
    inv, tot = True, 0
    for key in TEST_KEYS:
        ev, _ = events_for(cps, key, "dev")
        kind, sess = key.split(":")[1], key.split(":")[2]
        for e in ev:
            lv_ = next(x for x in cps["levels"][sess] if x["day"] == e["day"])
            level, p = lv_["H"], e["p"]
            first = int(np.flatnonzero(h[lv_["s"]:lv_["e"]] >= level + EPS_ATR * lv_["a"])[0]) + lv_["s"]
            if kind == "rej":
                inv &= (first == p) and (c[first - 1] < level) and (c[p] <= level)
            else:
                inv &= (first == p - (ACC_BARS - 1)) and (c[first - 1] < level) and bool(np.all(c[first:p + 1] > level)) and (c[p] >= level + ACC_ATR * lv_["a"])
            tot += 1
    chk(f"synthetic world: every event is the FIRST push beyond its level, from below, and closes as classified ({tot} events, >= 40)", (bool(inv), tot >= 40), (True, True))
    mid_ns = int(ns5[40000])
    cpm = ctx_g(s5, s1h, +1, mid_ns)
    dev, _ = events_for(cpm, "G:rej:london", "dev")
    hold, _ = events_for(cpm, "G:rej:london", "hold")
    chk("development events end their outcome window before the boundary; holdout events start at or after it", (all(e["t"] + (HMAX + 1) * NS_5 <= mid_ns for e in dev), all(e["t"] >= mid_ns for e in hold), len(hold) > 5), (True, True, True))
    col = collect(s5, s1h, mid_ns)
    chk("no scored row touches the holdout", all(r["t"] + (HMAX + 1) * NS_5 <= mid_ns for rows in col["rows"].values() for r in rows), True)
    # ---- null calibration of the full pipeline
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
    pr = power_cell("G:rej:asia", 1.5, 1, 21)
    pa = power_cell("G:acc:asia", 1.5, 1, 22)
    chk(f"planted reversal after rejected sweeps is detected as a REVERSAL (z={pr['mean_z']:+.2f}, excess {pr['mean_excess']:+.3f})", pr["detect_rate"] == 1.0, True)
    chk(f"planted continuation after accepted breaks is detected as CONTINUATION (z={pa['mean_z']:+.2f}, excess {pa['mean_excess']:+.3f})", pa["detect_rate"] == 1.0, True)
    rP = run_from_series(s5, s1h, "PRIMARY_READY", far)
    md = render_markdown(rP)
    chk("report renders and JSON serialises", (len(md) > 3000, len(json.dumps(rP, default=str)) > 3000), (True, True))
    return {"checks": n, "failures": fails, "passed": not fails,
            "null": {"series": null_series, "tests": int(len(ps)), "share_p_lt_0.05": round(float((ps < 0.05).mean()), 3) if len(ps) else None,
                     "share_p_lt_0.01": round(float((ps < 0.01).mean()), 3) if len(ps) else None, "mean_z": round(float(np.mean(zs)), 3) if zs else None},
            "planted": {"reversal": {k: pr[k] for k in ("size", "mean_z", "mean_excess", "mean_events")}, "continuation": {k: pa[k] for k in ("size", "mean_z", "mean_excess", "mean_events")}}}


def main():
    ap = argparse.ArgumentParser(description="Observatory Study G (session highs and lows). Read-only on raw data; the holdout stays sealed.")
    ap.add_argument("--raw-dir", default=rc.DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--counts", action="store_true", help="session levels and event counts only (no outcome is computed)")
    ap.add_argument("--readiness", action="store_true", help="holdout event counts and power from development estimates; no holdout outcome is read")
    ap.add_argument("--power", action="store_true", help="detectable effect sizes with the real event structure (synthetic data only)")
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
        for key in ("G:rej:asia", "G:acc:asia"):
            for s in (0.0, 0.1, 0.2, 0.3, 0.5):
                cells.append(power_cell(key, s, a.power_reps, 4000 + int(s * 100)))
                print(json.dumps(cells[-1]), flush=True)
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "studyg_power.json"), "w") as f:
            json.dump(cells, f, indent=2)
        return
    if a.readiness:
        print(json.dumps(holdout_readiness(a.raw_dir, a.source), indent=2, default=str))
        return
    res = run_studyg(a.raw_dir, a.source)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "studyg_results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    md = render_markdown(res)
    with open(os.path.join(a.out, "studyg_report.md"), "w") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
