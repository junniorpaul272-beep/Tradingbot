"""
Observatory — PROGRAM STANDARDS (GBPUSD). Read-only. The holdout stays sealed.

Every study from Study I on must pass these checks BEFORE its results are interpreted. They are the lessons of Studies A-H turned into code:

  S1  PAST-ONLY CONTEXT      conditioning on anything that happens AFTER the decision time is look-ahead (the Study G audit v1 flaw manufactured effects of -18 to -28 points).
                             `near_past` is the only allowed way to ask "was there an X recently".
  S2  EVENT-LOSS LEDGER      candidates = kept + dropped, every drop has a reason. `LossLedger` refuses a study whose reasons do not add up. Weekday coverage is always reported
                             (the 24h window crosses the weekend close: Friday events are lost, so findings are Monday-Thursday findings).
  S3  EXACT BLOCK SPLIT      recent vs older, split by DECISION time at a boundary declared BEFORE the run (default 2024-10-03), difference with a cluster-robust test, ONE declared family.
                             A pooled run that contains the older block is never presented as a replication of that block.
  S4  SPIKE EXPOSURE         single-bar spikes and round trips in the data are flagged; every test reports how many events sit within 24h of one and its estimate without them.
  S5  DEPENDENCE / OVERLAP   share of a study's events with an earlier study's event in the PAST 24h, estimate with and without. Two studies that share events are not two confirmations.
  S6  FEASIBILITY FIRST      counts and the detectable effect (2.8 x SE) are computed from event counts alone, BEFORE outcomes are read. A test that cannot detect the practical
                             effect is labelled in advance, not discovered afterwards.
  S7  REGISTRY               every study declares its concepts and what is new versus the studies already run; the registry blocks an accidental repeat.
  S8  PRE-REGISTRATION       a spec missing any required field is refused (claims, direction, family size, controls, block boundary, past-only attestation, holdout untouched).
  S9  UNIQUE EVENTS          within one test, the same (decision time, side) must not appear twice.
  S10 MATCHED CONTROLS       a placebo must be matched on the volatility state (trailing 24h range in ATR, PAST ONLY) as well as clock and trailing move: the expansion excess seen in
                             Study G appeared in controls too, so an unmatched placebo leaks day-level volatility into every "excess".

Run:  python3 -m observatory.standards --selftest
      python3 -m observatory.standards [--raw-dir ...] [--out ...] [--boundary 2024-10-03]      (exact H block split, spike exposure and overlap for G and H)
      python3 -m observatory.standards --check-spec spec.json                                    (S7 + S8 on a new study's spec; no data needed)
"""
import argparse
import json
import math
import os
import sys

import numpy as np
import pandas as pd

MIN_EFFECT = 0.05                    # practical size: 5 probability points
Z_POWER = 2.8                        # 80% power at alpha 0.05: detectable effect = 2.8 x SE
NS_H = 3600 * 10 ** 9
DEFAULT_BOUNDARY = "2024-10-03"
SPIKE_RET = 0.005                    # a 5m close-to-close move of at least 0.5% is a spike candidate
SPIKE_RETRACE = 0.70                 # ... and it is a spike if >= 70% of it is undone within 12 bars (either side of it)
SPIKE_BARS = 12
SPIKE_WINDOW_NS = 24 * NS_H
HOLIDAY_MD = ((12, 24), (12, 25), (12, 26), (12, 31), (1, 1))     # thin-liquidity days: a >= 0.5% move on these dates is flagged on its own


# ---------------------------------------------------------------------------
# S1  past-only context
# ---------------------------------------------------------------------------
def near_past(ft_sorted, t, window_ns):
    """True if an event time in ft_sorted lies in [t - window, t]. PAST ONLY. A later event selects on the future price path and manufactures effects."""
    i = int(np.searchsorted(ft_sorted, t, side="right"))
    return bool(i > 0 and t - int(ft_sorted[i - 1]) <= window_ns)


# ---------------------------------------------------------------------------
# S2  loss ledger and weekday coverage
# ---------------------------------------------------------------------------
class LossLedger:
    """Every candidate is either kept or dropped with a reason. check() raises if the books do not balance."""

    def __init__(self, name):
        self.name, self.candidates, self.kept, self.reasons = name, 0, 0, {}

    def add_candidate(self):
        self.candidates += 1

    def keep(self):
        self.kept += 1

    def drop(self, reason):
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def check(self):
        dropped = sum(self.reasons.values())
        if self.kept + dropped != self.candidates:
            raise AssertionError(f"{self.name}: candidates {self.candidates} != kept {self.kept} + dropped {dropped}")
        return {"name": self.name, "candidates": self.candidates, "kept": self.kept, "dropped": dropped, "reasons": dict(self.reasons),
                "kept_share": self.kept / self.candidates if self.candidates else None}


def weekday_coverage(times_ns):
    """Counts of events per UTC weekday, Mon..Sun."""
    if not len(times_ns):
        return [0] * 7
    wd = pd.DatetimeIndex(np.asarray(times_ns, np.int64).astype("datetime64[ns]"), tz=None).dayofweek
    return [int((wd == d).sum()) for d in range(7)]


# ---------------------------------------------------------------------------
# S3  exact block split
# ---------------------------------------------------------------------------
def block_review(rows, boundary_ns, fade, min_block=30):
    """rows: scored event rows (dicts with t, d, o, pm, atr, ...). Split by decision time. Returns recent / older / difference. Counts always add up (no event lost or duplicated)."""
    from . import studyg as sg
    a, b = [r for r in rows if r["t"] >= boundary_ns], [r for r in rows if r["t"] < boundary_ns]
    assert len(a) + len(b) == len(rows), "block split lost or duplicated events"
    sa_, sb_ = sg.summarize(a, fade), sg.summarize(b, fade)
    out = {"all": {k: sg.summarize(rows, fade).get(k) for k in ("n", "mean", "se", "z", "p")},
           "recent": {k: sa_.get(k) for k in ("n", "mean", "se", "z", "p")}, "older": {k: sb_.get(k) for k in ("n", "mean", "se", "z", "p")}}
    if len(a) >= min_block and len(b) >= min_block:
        d = sg._diff(a, b)
        out["diff"] = {k: d.get(k) for k in ("diff", "se", "ci", "z", "p")}
    return out


# ---------------------------------------------------------------------------
# S4  spike bars
# ---------------------------------------------------------------------------
def spike_flags(close, index_ns, skip=None):
    """close: 1-D array of 5m closes, index_ns: int64 ns timestamps of the same bars. Returns a sorted array of flagged bar timestamps (ns).
    A bar is flagged if |return| >= SPIKE_RET and (a) >= 70% of it is undone within the next 12 bars, or (b) it undoes >= 70% of an opposite move in the previous 12 bars,
    or (c) it falls on a thin-liquidity date (24-26 Dec, 31 Dec, 1 Jan)."""
    c = np.asarray(close, float)
    t = np.asarray(index_ns, np.int64)
    r = np.zeros(len(c))
    r[1:] = c[1:] / c[:-1] - 1.0
    if skip is not None:
        r[np.asarray(skip, bool)] = 0.0              # a return across a market closure (weekend gap) is not a spike
    big = np.where(np.abs(r) >= SPIKE_RET)[0]
    flag = np.zeros(len(c), bool)
    md = pd.DatetimeIndex(t.astype("datetime64[ns]"))
    for i in big:
        pre = c[i - 1]
        fwd = c[i + 1:i + 1 + SPIKE_BARS]
        if len(fwd):
            move = c[i] - pre
            back = (c[i] - fwd) * np.sign(move)             # positive = price returning toward pre
            if back.max() >= SPIKE_RETRACE * abs(move):
                flag[i] = True
        for j in big[(big >= i - SPIKE_BARS) & (big < i)]:           # this bar undoes an earlier big move in the opposite direction
            mv_j = c[j] - c[j - 1]
            if mv_j * (c[i] - c[i - 1]) < 0 and abs(c[i] - c[j - 1]) <= (1 - SPIKE_RETRACE) * abs(mv_j):
                flag[i] = flag[j] = True
        if (md[i].month, md[i].day) in HOLIDAY_MD:
            flag[i] = True
    return np.sort(t[flag])


def spike_exposure(rows, spike_ns, window_ns=SPIKE_WINDOW_NS):
    """Split rows into those with a flagged bar within [t - window, t + window] and those without."""
    if not len(spike_ns):
        return list(rows), []
    exposed, clean = [], []
    for r in rows:
        i = int(np.searchsorted(spike_ns, r["t"] - window_ns, side="left"))
        (exposed if i < len(spike_ns) and spike_ns[i] <= r["t"] + window_ns else clean).append(r)
    return exposed, clean


# ---------------------------------------------------------------------------
# S6  feasibility (counts only, no outcomes)
# ---------------------------------------------------------------------------
def feasibility(n_events, n_blocks, sd=0.5, min_effect=MIN_EFFECT):
    """Detectable effect from counts alone. n_blocks = number of distinct 2-day blocks containing events (the conservative effective n: one independent observation per block).
    Binary outcome sd is at most 0.5. tier: POWERED (can detect the practical effect), LIMITED (detectable <= 2x), UNDERPOWERED (> 2x)."""
    if n_events <= 0:
        return {"n": 0, "n_blocks": 0, "detectable_optimistic": None, "detectable_conservative": None, "tier": "UNDERPOWERED"}
    opt = Z_POWER * sd / math.sqrt(n_events)
    con = Z_POWER * sd / math.sqrt(max(1, n_blocks))
    mid = math.sqrt(opt * con)                      # geometric mean of the two bounds: the reading used for the tier
    tier = "POWERED" if mid <= min_effect else ("LIMITED" if mid <= 2 * min_effect else "UNDERPOWERED")
    return {"n": int(n_events), "n_blocks": int(n_blocks), "detectable_optimistic": opt, "detectable_conservative": con, "detectable_used": mid, "tier": tier}


def block_diff_detectable(n_recent, n_older, sd=0.5):
    """Detectable recent-minus-older difference (2.8 x SE, independent events)."""
    if n_recent <= 0 or n_older <= 0:
        return None
    return Z_POWER * sd * math.sqrt(1.0 / n_recent + 1.0 / n_older)


# ---------------------------------------------------------------------------
# S7  registry
# ---------------------------------------------------------------------------
REGISTRY = {
    "A": {"concepts": {"persistence", "efficiency", "volatility_state"}, "what": "directional persistence, efficiency (trendiness) and volatility state at fixed observation times"},
    "B": {"concepts": {"bos", "pullback", "swing"}, "what": "break of a 1h swing high and pullback depth vs a geometry-matched placebo"},
    "C": {"concepts": {"fib_retrace", "bos", "pullback", "swing"}, "what": "Fibonacci retracement zones; Study B at 15m with all events"},
    "D": {"concepts": {"fib_ext", "elliott", "hist_level", "polarity", "confluence"}, "what": "Fibonacci extensions, wave geometry, historical levels, polarity switch, confluence"},
    "E": {"concepts": {"path_progress", "path_cost", "time"}, "what": "path progress, path cost and time at hourly observation"},
    "F": {"concepts": {"prev_day_box", "approach", "range_zone"}, "what": "approach to the previous-day high/low boundary"},
    "G": {"concepts": {"session_extreme", "sweep", "reclaim", "acceptance"}, "what": "first push beyond the Asian/London session extreme: rejection vs acceptance"},
    "H": {"concepts": {"range_zone", "prev_day_box", "swing", "mean_dev", "premium_discount"}, "what": "premium/discount and deep zones of prev/24h/5d/swing ranges, deviation from rolling mean"},
    "wave1": {"concepts": {"displacement"}, "what": "displacement bar continuation"},
    "wave2": {"concepts": {"fvg"}, "what": "fair value gap information beyond the displacement"},
    "wave4/crt2/crt3": {"concepts": {"sweep", "reclaim", "acceptance", "crt", "outside_bar", "htf_zone", "crossing_vs_near"}, "what": "CRT sweep-and-reclaim variants, branches, crossing vs being near the level"},
}
CONCEPT_VOCAB = sorted({c for v in REGISTRY.values() for c in v["concepts"]} | {"order_block", "rejection_block", "boc_entry", "bos_after_sweep", "equal_highs_lows", "session_activity", "volatility_expansion"})


def check_duplicate(concepts, new_vs_prior):
    """Returns (hits, errors). hits = registry entries sharing concepts with the spec (an overlap is allowed but must be explained in new_vs_prior).
    errors: a spec whose concepts are ALL covered by one earlier study with no stated difference is refused."""
    c, hits, errors = set(concepts), [], []
    for k, v in REGISTRY.items():
        sh = c & v["concepts"]
        if sh:
            hits.append({"study": k, "shared": sorted(sh), "covered": c <= v["concepts"], "what": v["what"]})
    if not str(new_vs_prior or "").strip():
        errors.append("new_vs_prior is empty: state what this study adds beyond the registry hits")
    for h in hits:
        if h["covered"] and len(str(new_vs_prior or "").strip()) < 40:
            errors.append(f"every concept is already covered by Study {h['study']}: new_vs_prior must say precisely what differs (>= 40 characters)")
    unknown = sorted(c - set(CONCEPT_VOCAB))
    if unknown:
        errors.append(f"concepts outside the vocabulary (add them to CONCEPT_VOCAB deliberately): {unknown}")
    return hits, errors


# ---------------------------------------------------------------------------
# S8  pre-registration
# ---------------------------------------------------------------------------
REQUIRED = ("id", "question", "claims", "family_size", "controls", "block_boundary", "heterogeneity_family", "decision_time_rule", "uses_future_information",
            "horizon_bars", "min_effect", "concepts", "new_vs_prior", "holdout_touched", "volatility_matched_placebo", "feasibility")


def validate_prereg(spec):
    errs = [f"missing: {k}" for k in REQUIRED if k not in spec]
    if errs:
        return errs
    if not spec["claims"] or any(not all(k in c for k in ("key", "direction", "outcome")) for c in spec["claims"]):
        errs.append("every claim needs key, direction (fade|continue) and outcome")
    elif any(c["direction"] not in ("fade", "continue") for c in spec["claims"]):
        errs.append("claim direction must be fade or continue")
    if spec["family_size"] != len(spec["claims"]):
        errs.append(f"family_size {spec['family_size']} != number of claims {len(spec['claims'])}")
    if not spec["controls"]:
        errs.append("at least one control (a nearby non-event condition) is required")
    if spec["uses_future_information"] is not False:
        errs.append("uses_future_information must be False: decision-time context is past-only (S1)")
    if spec["holdout_touched"] is not False:
        errs.append("holdout_touched must be False")
    if spec["volatility_matched_placebo"] is not True:
        errs.append("the placebo must be matched on volatility state (S10)")
    try:
        if pd.Timestamp(spec["block_boundary"], tz="UTC") >= pd.Timestamp("2026-06-18", tz="UTC"):
            errs.append("block_boundary must be inside the development data (< 2026-06-18)")
    except Exception:
        errs.append("block_boundary is not a date")
    if spec["heterogeneity_family"] != spec["family_size"]:
        errs.append("the block-difference family must be declared and equal the claim family (one Bonferroni threshold)")
    fz = spec["feasibility"]
    if not isinstance(fz, dict) or not fz:
        errs.append("feasibility (counts of events per claim key from a counts-only run) is required before outcomes are read")
    elif any(c["key"] not in fz for c in spec["claims"]):
        errs.append("feasibility must cover every claim key")
    hits, e2 = check_duplicate(spec["concepts"], spec["new_vs_prior"])
    return errs + e2


# ---------------------------------------------------------------------------
# S9  unique events
# ---------------------------------------------------------------------------
def duplicate_events(rows):
    seen, dup = set(), 0
    for r in rows:
        k = (int(r["t"]), int(r.get("side", 0)))
        if k in seen:
            dup += 1
        seen.add(k)
    return dup


# ---------------------------------------------------------------------------
# S10  volatility state, PAST ONLY (to be used as an extra matching variable for placebos)
# ---------------------------------------------------------------------------
def vol_state(high, low, atr, bars=288):
    """Trailing range over the previous `bars` 5m bars (ending at the decision bar, inclusive) in ATR units. Array aligned with the input; NaN until `bars` bars exist."""
    h = pd.Series(np.asarray(high, float)).rolling(bars).max().to_numpy()
    l = pd.Series(np.asarray(low, float)).rolling(bars).min().to_numpy()
    return (h - l) / np.asarray(atr, float)


# ---------------------------------------------------------------------------
# run on data
# ---------------------------------------------------------------------------
def _fade_g(key):
    from . import studyg as sg
    return sg.CLAIM[key.split(":")[1]] == "fade"


def run_standards(raw_dir=None, source="observatory", boundary=DEFAULT_BOUNDARY, preloaded=None):
    from . import clean as rc
    from . import studya as sa
    from . import studyf as sf
    from . import studyg as sg
    from . import studyh as sh
    raw_dir = raw_dir or rc.DEFAULT_RAW_DIR
    s5, s1h, status = sa.load_series(raw_dir, source, preloaded)
    hold_ns = pd.Timestamp(rc.HOLDOUT_START_UTC).as_unit("ns").value
    bnd_ns = pd.Timestamp(boundary, tz="UTC").as_unit("ns").value
    ns = sa._ns(s5.index)
    dev = ns < hold_ns
    sp = spike_flags(s5.c[dev], ns[dev], s5.closure[dev] | (s5.gap[dev] > 0))
    res = {"dataset_status": status, "boundary_utc": boundary, "holdout_opened": False,
           "spikes": {"n": int(len(sp)), "times": [str(pd.Timestamp(int(x), tz="UTC")) for x in sp]}}
    fts = {1: [], -1: []}
    for ctx in (sf.ctx_f(s5, s1h, +1, hold_ns), sf.ctx_f(s5, s1h, -1, hold_ns)):
        for key in ("A:0", "A:0.1", "A:0.2"):
            ev, _ = sf.events_for(ctx, key, "dev")
            fts[ctx["sign"]] += [e["t"] for e in ev]
    fts = {s: np.sort(np.unique(np.array(v, np.int64))) for s, v in fts.items()}
    studies = {"G": (sg.collect(s5, s1h, hold_ns)["rows"], sg.TEST_KEYS, sg.CTRL_KEYS, _fade_g),
               "H": (sh.collect(s5, s1h, hold_ns)["rows"], sh.TEST_KEYS, sh.CTRL_KEYS, lambda k: True)}
    out = {}
    for name, (R, tkeys, ckeys, fade) in studies.items():
        fam = len(tkeys)
        for key in tkeys + ckeys:
            rows = R[key]
            exposed, clean = spike_exposure(rows, sp)
            sm = sg.summarize(rows, fade(key))
            smc = sg.summarize(clean, fade(key)) if len(clean) >= 30 else {}
            near = np.array([near_past(fts[int(r["side"])], r["t"], 24 * NS_H) for r in rows], bool) if rows else np.array([], bool)
            nof = [r for r, b in zip(rows, near) if not b]
            smn = sg.summarize(nof, fade(key)) if len(nof) >= 30 else {}
            nb = len({int(r["t"] // (2 * 24 * NS_H)) for r in rows})
            out[key] = {"study": name, "family": fam if key in tkeys else None, "control": key in ckeys,
                        "blocks": block_review(rows, bnd_ns, fade(key)),
                        "spike": {"exposed": len(exposed), "clean": len(clean), "n": len(rows), "clean_estimate": {k: smc.get(k) for k in ("n", "mean", "se", "z")}},
                        "overlap_f": {"share": float(near.mean()) if len(near) else None, "without": {k: smn.get(k) for k in ("n", "mean", "se", "z")}},
                        "duplicates": duplicate_events(rows), "weekday": weekday_coverage([r["t"] for r in rows]),
                        "feasibility": feasibility(len(rows), nb), "overall": {k: sm.get(k) for k in ("n", "mean", "se", "z", "p")},
                        "diff_detectable": block_diff_detectable(sum(r["t"] >= bnd_ns for r in rows), sum(r["t"] < bnd_ns for r in rows))}
    res["tests"] = out
    return res


def _f(x, nd=3):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.{nd}f}"


def _pp(x):
    return "n/a" if x is None else ("<0.0001" if x < 1e-4 else f"{x:.4f}")


def render_markdown(r):
    L = ["# Observatory — Program standards check on Studies G and H (GBPUSD)\n",
         f"Dataset **{r['dataset_status']}** · block boundary {r['boundary_utc']} · holdout opened: **False**\n",
         "## 1. Spike bars flagged in development data (S4)\n", f"{r['spikes']['n']} bars flagged (>= 0.5% move, >= 70% undone within 12 bars either side, or on a thin-liquidity date).\n"]
    L += [f"- {t}" for t in r["spikes"]["times"][:60]]
    for study, fam in (("H", 10), ("G", 4)):
        keys = [k for k, v in r["tests"].items() if v["study"] == study]
        L += [f"\n## Study {study}: exact recent-vs-older split (S3), declared family of {fam}; Bonferroni threshold p < {0.05 / fam:.4f}\n",
              "| test | all n | all excess (z) | recent n | recent excess (z) | older n | older excess (z) | difference | z | p | below threshold? |\n|---|---|---|---|---|---|---|---|---|---|---|"]
        for k in keys:
            v = r["tests"][k]
            b = v["blocks"]
            d = b.get("diff")
            g = lambda x: "n/a" if not x or x.get("mean") is None else f"{_f(x['mean'])} ({_f(x['z'], 2)})"
            flag = "(control)" if v["control"] else ("n/a" if not d else ("yes" if d["p"] < 0.05 / fam else "no"))
            L.append(f"| {k} | {b['all'].get('n')} | {g(b['all'])} | {b['recent'].get('n')} | {g(b['recent'])} | {b['older'].get('n')} | {g(b['older'])} | "
                     f"{_f(d['diff']) if d else 'n/a'} | {_f(d['z'], 2) if d else 'n/a'} | {_pp(d['p']) if d else 'n/a'} | {flag} |")
        L += [f"\n### Study {study}: spike exposure (S4), overlap with Study F in the PAST 24h (S5), duplicates (S9), feasibility from counts (S6)\n",
              "| test | events | within 24h of a spike | estimate without spike events (z) | share with a past-24h F event | estimate without F overlap (z) | duplicate events | weekday Mon..Sun | detectable (opt / cons) | tier | detectable block difference |\n|---|---|---|---|---|---|---|---|---|---|---|"]
        for k in keys:
            v = r["tests"][k]
            g = lambda x: "n/a" if not x or x.get("mean") is None else f"{_f(x['mean'])} ({_f(x['z'], 2)}, n={x['n']})"
            fz = v["feasibility"]
            dd = v["diff_detectable"]
            L.append(f"| {k} | {v['spike']['n']} | {v['spike']['exposed']} | {g(v['spike']['clean_estimate'])} | "
                     f"{'n/a' if v['overlap_f']['share'] is None else format(100 * v['overlap_f']['share'], '.1f') + '%'} | {g(v['overlap_f']['without'])} | {v['duplicates']} | {v['weekday']} | "
                     f"{_f(fz['detectable_optimistic'])} / {_f(fz['detectable_conservative'])} | {fz['tier']} | {_f(dd) if dd else 'n/a'} |")
    L += ["\n## Not checked\n", "- Anything on the holdout. Whether a flagged bar is wrong (this only measures exposure and sensitivity). Volatility-matched placebos (S10): required for Study I, not retrofitted here."]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------
def selftest():
    checks, fails = 0, []

    def chk(label, got, want):
        nonlocal checks
        checks += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    rng = np.random.default_rng(3)
    n = 3000
    c = 1.30 + np.cumsum(rng.normal(0, 0.00005, n))
    t = (pd.Timestamp("2025-03-03 00:00", tz="UTC").as_unit("ns").value + np.arange(n, dtype=np.int64) * 300 * 10 ** 9)
    base = spike_flags(c, t)
    chk("S4 quiet random walk: no flags", len(base), 0)
    c2 = c.copy()
    c2[1000] *= 1.012                                   # one-bar spike that reverts at once
    f2 = spike_flags(c2, t)
    chk("S4 one-bar spike flagged (the spike bar and its reversal)", set(int(x) for x in f2) >= {int(t[1000])}, True)
    c3 = c.copy()
    c3[1500:] *= 1.012                                  # genuine level shift that stays
    f3 = spike_flags(c3, t)
    chk("S4 a genuine step that stays is NOT flagged", int(t[1500]) in set(int(x) for x in f3), False)
    c4 = c.copy()
    c4[1200:1204] *= 1.007                              # round trip within 4 bars (the 2020-12-25 pattern)
    f4 = set(int(x) for x in spike_flags(c4, t))
    chk("S4 round trip flagged", int(t[1200]) in f4, True)
    hol_t = pd.Timestamp("2025-12-25 10:00", tz="UTC").as_unit("ns").value + np.arange(50, dtype=np.int64) * 300 * 10 ** 9
    hc = 1.30 + np.zeros(50)
    hc[20:] *= 1.006
    chk("S4 thin-liquidity date: a 0.6% move flagged even if it stays", int(hol_t[20]) in set(int(x) for x in spike_flags(hc, hol_t)), True)

    ft = np.array([100, 500], np.int64)
    chk("S1 past-only: later event does not count, earlier does, beyond window does not",
        (near_past(ft, 60, 50), near_past(ft, 540, 50), near_past(ft, 700, 50), near_past(ft, 100, 50)), (False, True, False, True))

    led = LossLedger("x")
    for i in range(10):
        led.add_candidate()
    for i in range(6):
        led.keep()
    led.drop("closure")
    led.drop("closure")
    led.drop("gap")
    led.drop("region")
    chk("S2 ledger balances", led.check()["dropped"], 4)
    led.drop("silent")
    try:
        led.check()
        ok = False
    except AssertionError:
        ok = True
    chk("S2 ledger refuses unbalanced books", ok, True)
    d0 = pd.Timestamp("2025-03-03", tz="UTC").as_unit("ns").value
    chk("S2 weekday coverage (Mon, Mon, Fri)", weekday_coverage([d0, d0 + NS_H, d0 + 4 * 24 * NS_H]), [2, 0, 0, 0, 1, 0, 0])

    chk("S6 feasibility: 2000 events in 400 blocks is limited-or-better, 40 events underpowered",
        (feasibility(2000, 400)["tier"] in ("POWERED", "LIMITED"), feasibility(40, 20)["tier"]), (True, "UNDERPOWERED"))
    chk("S6 detectable shrinks with n", feasibility(100, 50)["detectable_used"] > feasibility(1000, 500)["detectable_used"], True)
    chk("S6 block-difference detectable arithmetic", round(block_diff_detectable(100, 100), 4), round(2.8 * 0.5 * math.sqrt(0.02), 4))

    hits, errs = check_duplicate({"prev_day_box", "range_zone"}, "")
    chk("S7 empty new_vs_prior refused, F and H listed", (len(errs) > 0, {h["study"] for h in hits} >= {"F", "H"}), (True, True))
    hits, errs = check_duplicate({"prev_day_box", "range_zone"}, "tests whether a volatility-state filter changes the zone effect, which H did not condition on")
    chk("S7 covered concepts with an explained difference are allowed", errs, [])
    hits, errs = check_duplicate({"order_block", "boc_entry"}, "first test of order blocks and the break-of-candle trigger, never run")
    chk("S7 novel concepts pass with no hits", (errs, hits), ([], []))
    chk("S7 unknown concept refused", len(check_duplicate({"made_up"}, "x" * 50)[1]) > 0, True)

    good = {"id": "I", "question": "q", "claims": [{"key": "I:a", "direction": "fade", "outcome": "first passage"}], "family_size": 1, "controls": ["C:a"], "block_boundary": "2024-10-03",
            "heterogeneity_family": 1, "decision_time_rule": "close of bar t, context from bars <= t", "uses_future_information": False, "horizon_bars": 288, "min_effect": 0.05,
            "concepts": ["order_block"], "new_vs_prior": "first order-block study, never run before this one", "holdout_touched": False, "volatility_matched_placebo": True,
            "feasibility": {"I:a": {"n": 500}}}
    chk("S8 complete spec passes", validate_prereg(good), [])
    for k, bad in (("uses_future_information", True), ("holdout_touched", True), ("volatility_matched_placebo", False), ("block_boundary", "2026-07-01"), ("family_size", 2)):
        s = dict(good)
        s[k] = bad
        chk(f"S8 spec refused when {k} = {bad!r}", len(validate_prereg(s)) > 0, True)
    s = dict(good)
    del s["controls"]
    chk("S8 missing field refused", len(validate_prereg(s)) > 0, True)

    rows = [{"t": 10, "side": 1}, {"t": 20, "side": 1}, {"t": 10, "side": -1}, {"t": 10, "side": 1}]
    chk("S9 duplicate (t, side) counted", duplicate_events(rows), 1)

    ex, cl = spike_exposure([{"t": 100 * NS_H}, {"t": 200 * NS_H}], np.array([110 * NS_H], np.int64))
    chk("S4 exposure: event 10h from a spike exposed, event 90h away clean", (len(ex), len(cl)), (1, 1))

    h = np.array([1.3010, 1.3020, 1.3030, 1.3040])
    l = np.array([1.3000, 1.3005, 1.3010, 1.3020])
    v = vol_state(h, l, np.full(4, 0.001), bars=3)
    chk("S10 vol_state is NaN until enough history, then past-only range / ATR", (bool(np.isnan(v[1])), round(float(v[2]), 3), round(float(v[3]), 3)), (True, 3.0, 3.5))

    from . import studyg as sg
    rr = []
    for i in range(80):
        o = float(i % 2)
        rr.append({"t": int(i * 24 * 3600 * 10 ** 9), "d": o - 0.5, "o": o, "pm": 0.5, "atr": 0.001, "side": 1, "a": 1.0, "b": 1.0})
    r2 = block_review(rr, 40 * 24 * 3600 * 10 ** 9, True)
    chk("S3 block split counts add up", (r2["recent"]["n"], r2["older"]["n"]), (40, 40))
    return {"checks": checks, "failures": fails, "passed": not fails}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", default=None)
    ap.add_argument("--out", default="observatory_run")
    ap.add_argument("--boundary", default=DEFAULT_BOUNDARY)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check-spec", default=None)
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    if a.check_spec:
        spec = json.load(open(a.check_spec))
        errs = validate_prereg(spec)
        hits, _ = check_duplicate(spec.get("concepts", []), spec.get("new_vs_prior", ""))
        print(json.dumps({"errors": errs, "registry_hits": hits, "ok": not errs}, indent=2))
        sys.exit(0 if not errs else 1)
    os.makedirs(a.out, exist_ok=True)
    res = run_standards(a.raw_dir, boundary=a.boundary)
    json.dump(res, open(os.path.join(a.out, "standards_results.json"), "w"), indent=2, default=str)
    md = render_markdown(res)
    open(os.path.join(a.out, "standards_report.md"), "w").write(md)
    print(md)


if __name__ == "__main__":
    main()
