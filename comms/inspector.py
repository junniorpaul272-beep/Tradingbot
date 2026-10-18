"""
inspector.py — an independent observer that audits the bot from OUTSIDE its decision flow.

THE IDEA (from the builder's own debugging habit): when I debug this code I rerun it,
look at what comes out, and notice something is OFF — without having been told which
kind of bug to look for — then trace it back to where it first went wrong. This module
tries to do that continuously, with no bug class written into it. It looks for GENERIC
signs that a system is not behaving like itself:

  PROBES (each knows nothing about SMC, MIL or any past bug)
    dwell         a state that has lasted far longer than it ever has before
    placeholder   a price-like field holding a literal zero
    directions    disagreement between INDEPENDENT sources of direction (one family = one
                  source; recorded copies are excluded; a faster timeframe differing is LOW).
                  It never says which side is right. A finding closes only when its condition
                  is gone, never because its data went missing (that stays open, UNEVALUABLE).
    time          a timestamp from the future, or newer than the moment the file was written
    identity      an id that embeds numbers which no longer match the live fields
    growth        a collection that only ever gets bigger across runs
    schema        keys appearing / disappearing / changing type between runs
  EXERCISE (reruns the real code)
    fuzz_mil      drives the DEPLOYED mil.py with seeded random observation streams and
                  checks properties any belief engine must satisfy: deterministic, does
                  not mutate its input, bounded memory, mirror-symmetric between bull and
                  bear, and — under constant input — converges to something consistent
                  with that input. (Its job is to find whole families of bug, e.g. a state
                  that can never be left.)

WHAT IT MAY NOT DO (the autoimmune rule): it never edits state, never "repairs", never
imports BUS or the catalog, and no engine module imports it. It DETECTS, LOCALISES and
REPORTS. Findings go to integrity_ledger.jsonl (append-only, hash-verified, single
writer) and bus_runner.py is the only thing that speaks about them.

FIRST DIVERGENCE: findings in the same cluster are ordered by LAYER_ORDER (where in the
pipeline the field is produced). The earliest layer's finding is flagged the suspected
origin; later ones are marked downstream, so one root cause is one alarm, not five.

This is a first version. It is honest about being heuristic: every finding carries its
evidence and a severity, and a probe that cannot decide says nothing rather than guess.
"""

import hashlib
import json
import os
import random
import re
import sys
from datetime import datetime, timezone, timedelta

import ledger

FINDING = "INTEGRITY_FINDING"
RESOLVED = "INTEGRITY_RESOLVED"
UNEVALUABLE = "INTEGRITY_UNEVALUABLE"      # a finding could not be re-judged (its data is missing): it stays OPEN
SEV_RANK = {"HIGH": 3, "MED": 2, "LOW": 1}

# Where a field is produced, earliest first. Used ONLY to order findings for "first
# divergence"; it never decides whether something is wrong. Prefix match on the top-level key.
LAYER_ORDER = [
    ("candle", 0), ("structure_digest", 1), ("macro_leg", 1), ("campaign", 1), ("leg15", 1),
    ("macro_bias", 2), ("market_phase", 3), ("market_thesis", 3), ("market_intent", 4),
    ("leg_owner", 4), ("mil_understanding", 5),
]
DIRECTIONS = ("BULLISH", "BEARISH")
# Must equal scanner_live.CODE_VERSION_FILES (a test compares them; the inspector may not import the live loop).
CODE_VERSION_FILES = ("scanner_live.py", "mil.py", "scanner_observation.py", "scanner_common.py",
                      "market_read.py", "brain.py", "min_scanner.py", "leg_age.py", "momentum_read.py")


def compute_code_version(base_dir):
    h = hashlib.sha256()
    for name in CODE_VERSION_FILES:
        try:
            with open(os.path.join(base_dir, name), "rb") as f:
                h.update(name.encode() + b":" + hashlib.sha256(f.read()).digest())
        except OSError:
            h.update(name.encode() + b":missing")
    return h.hexdigest()[:12]


def layer_of(path):
    top = path.split(".")[0].split("[")[0]
    for prefix, rank in LAYER_ORDER:
        if top.startswith(prefix):
            return rank
    return 9


# ---------------------------------------------------------------- tree helpers
def walk(obj, path=""):
    """Yield (path, leaf) for every scalar leaf; lists are indexed."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _ts(x):
    if isinstance(x, str) and re.match(r"^\d{4}-\d\d-\d\dT", x):
        try:
            return ledger.to_utc(x)
        except ledger.LedgerError:
            return None
    return None


def finding(probe, severity, summary, paths, evidence):
    fp = hashlib.sha256(json.dumps([probe, sorted(paths)], sort_keys=True).encode()).hexdigest()[:12]
    return {"fingerprint": fp, "probe": probe, "severity": severity, "summary": summary,
            "paths": sorted(paths), "evidence": evidence,
            "layer": min((layer_of(p) for p in paths), default=9)}


# ---------------------------------------------------------------- probes
def probe_dwell(state, now=None):
    """Any list of dated transitions (dicts carrying a time key and a destination key) gives
    dwell durations. The CURRENT dwell is compared with every earlier one."""
    out = []
    snap = _ts(state.get("timestamp")) or _ts(state.get("market_data_timestamp")) or now
    if snap is None:
        return out

    def lists(o, p=""):
        if isinstance(o, dict):
            for k, v in o.items():
                yield from lists(v, f"{p}.{k}" if p else k)
        elif isinstance(o, list) and o and all(isinstance(x, dict) for x in o):
            yield p, o

    for path, lst in lists(state):
        tkey = next((k for k in ("at", "timestamp", "changed_at") if all(k in x for x in lst)), None)
        dkey = next((k for k in ("to_state", "state", "to") if all(k in x for x in lst)), None)
        if not tkey or not dkey:
            continue
        times = [_ts(x[tkey]) for x in lst]
        if None in times or len(times) < 3:
            continue
        durs = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
        cur = (snap - times[-1]).total_seconds()
        prior_max, prior_med = max(durs), sorted(durs)[len(durs) // 2]
        # at least 2h and at least twice any earlier dwell (and well over the typical one). With only a
        # few transitions on record this can only say MED; a long history makes the ratio meaningful.
        if cur > max(2 * prior_max, 7200) and cur > 3 * prior_med:
            out.append(finding(
                "dwell", "HIGH" if cur > 4 * prior_max else "MED",
                f"'{lst[-1][dkey]}' has lasted {cur/3600:.1f}h; no earlier state in this history lasted longer than {prior_max/3600:.1f}h",
                [path], {"current_state": lst[-1][dkey], "since": lst[-1][tkey], "current_dwell_s": int(cur),
                         "prior_dwells_s": [int(d) for d in durs], "ratio_to_prior_max": round(cur / prior_max, 1) if prior_max else None}))
    return out


_PRICEY = re.compile(r"(level|price|origin|extreme|high|low|zone|entry|stop|target)", re.I)


def probe_placeholder(state):
    """A literal 0.0 in a field that is named like a price, in a state where the other
    price-like numbers sit in a normal range."""
    leaves = [(p, v) for p, v in walk(state) if _num(v)]
    prices = [v for p, v in leaves if _PRICEY.search(p.split(".")[-1]) and 0.5 < abs(v) < 5000]
    if len(prices) < 3:
        return []
    out = []
    for p, v in leaves:
        if isinstance(v, float) and v == 0.0 and _PRICEY.search(p.split(".")[-1]):
            out.append(finding("placeholder", "MED", f"{p} is exactly 0.0 while other price-like fields hold real levels — it looks like an unset placeholder",
                               [p], {"value": 0.0, "price_like_fields_seen": len(prices)}))
    return out


# ---- direction provenance. A field is only evidence if we know WHAT it is. Every direction leaf is
# classified as: EXCLUDED (a recording of the past, or a field whose job is to disagree), a CURRENT-STATE
# family (one family = one source, however many fields it has), a TIMEFRAME family (a faster/slower
# horizon, expected to diverge from the macro bias now and then), or UNMAPPED (not counted at all).
# Counting raw fields is what made 13 -> 14 meaningless: the 14th was a stored signal record.
_DIR_EXCLUDED = re.compile(r"(history|prev_snapshot|undelivered_signal_events|(^|\.)prior_|(^|\.)previous|counter|candidate|"
                           r"emerging|challenger|origin_direction)", re.I)
_DIR_FAMILIES = (                      # (family, kind, pattern) - first match wins
    ("htf_macro",  "state", re.compile(r"^(macro_bias_confirmed|macro_leg_direction|structure_digest\.regime\.macro_bias)$")),
    ("mil",        "state", re.compile(r"^mil_understanding\.primary_thesis\.direction$")),
    ("campaign",   "state", re.compile(r"^(campaign_direction|market_thesis_campaign\.direction)$")),
    ("shadow",     "variant", re.compile(r"^shadow_")),
    ("tf_5m",      "timeframe", re.compile(r"^market_thesis_mtf_5m\.")),
    ("tf_15m",     "timeframe", re.compile(r"^(leg15_direction|market_thesis_mtf_15m\.)")),
    ("structure_event", "timeframe", re.compile(r"^structure_digest\.structure\.")),
)
_FAMILY_LABEL = {"htf_macro": "the macro bias", "mil": "the MIL thesis", "campaign": "the campaign direction",
                 "shadow": "the shadow pipeline", "tf_5m": "the 5-minute direction", "tf_15m": "the 15-minute direction",
                 "structure_event": "the latest structure break"}


# Audited against the code on 2026-10-14 (see DIRECTION_PROVENANCE.md). "distinct sources" means distinct
# calculations, NOT proven statistical independence.
FAMILY_PROVENANCE = {
    "htf_macro": {"origin": "scanner_observation.compute_macro_bias (one writer; bias_confirmed and leg_direction are set together)",
                  "meaning": "held 1H macro direction", "authority": "compute_macro_bias",
                  "expected": "members always agree; a mismatch inside this family is a defect"},
    "mil": {"origin": "mil.py: primary_thesis.direction is copied from macro_bias_confirmed on bootstrap/adoption",
            "meaning": "the belief engine's primary thesis", "authority": "mil.py",
            "expected": "follows macro bias; may legitimately lag while status is TRANSITIONING"},
    "campaign": {"origin": "scanner_observation._campaign_updates, fed by compute_macro_bias; market_thesis_campaign.direction is a copy",
                 "meaning": "direction of the current structural campaign (observation only)", "authority": "_campaign_updates",
                 "expected": "follows macro bias but is only resynced on a NEW leg, so it can lag"},
    "shadow": {"origin": "compute_macro_bias_shadow_old_rule / _min_leg_gate: deliberately different rules",
               "meaning": "alternative macro-bias rules run for study", "authority": "shadow functions (never trade)",
               "expected": "is DESIGNED to differ from the live bias"},
    "tf_5m": {"origin": "scanner_observation mtf_5m (5-minute BOS)", "meaning": "entry-timeframe direction",
              "authority": "build mtf_5m", "expected": "may differ from macro bias (engine labels it countertrend)"},
    "tf_15m": {"origin": "leg15 (15-minute leg, also touched by the stale-bias promotion path)", "meaning": "15-minute leg direction",
               "authority": "scanner_observation", "expected": "may differ from macro bias"},
    "structure_event": {"origin": "MarketFacts.bos_15m() via build_structure_digest", "meaning": "latest 15-minute break of structure",
                        "authority": "MarketFacts", "expected": "an event, may differ from macro bias"},
}
# The relationship that FAILED decides the severity, not just who is involved.
_REL_SEVERITY = {("htf_macro", "htf_macro"): ("same_writer", "HIGH"),
                 ("htf_macro", "mil"): ("consumer_of_macro_bias", "HIGH"),
                 ("campaign", "htf_macro"): ("derived_and_may_lag", "MED"),
                 ("campaign", "mil"): ("derived_and_may_lag", "MED")}


REGIME_COPY = "structure_digest.regime.macro_bias"      # a per-scan COPY; only valid if stamped by this scan


# The MIL exception has a boundary. The engine converges a challenger in CHALLENGER_CONFIRM_SCANS (6) scans
# of ~5 minutes = ~30 minutes; twice that is the most a thesis may sit in TRANSITIONING and still be called
# "lagging by design". Past it, or when the entry time cannot be established, the conflict is HIGH.
MIL_TRANSITION_GRACE_S = 3600


def _mil_transition_age_s(state):
    """Seconds MIL has been in TRANSITIONING at this snapshot, or None if it cannot be established."""
    mil = state.get("mil_understanding")
    now = _ts(state.get("timestamp")) or _ts(state.get("market_data_timestamp"))
    if not isinstance(mil, dict) or now is None:
        return None
    entered = None
    for h in (mil.get("history") or []):
        if isinstance(h, dict) and h.get("to_state") == "TRANSITIONING" and _ts(h.get("at")):
            entered = _ts(h["at"])
    if entered is None or entered > now:
        return None
    return (now - entered).total_seconds()


def classify_direction(path):
    """-> (family, kind) or ('excluded', None) or (None, None) when unmapped."""
    if _DIR_EXCLUDED.search(path):
        return "excluded", None
    for fam, kind, pat in _DIR_FAMILIES:
        if pat.search(path):
            return fam, kind
    return None, None


def direction_groups(state):
    """{'state': {family: {path: value}}, 'timeframe': {...}, 'excluded': n, 'unmapped': [paths]}"""
    g = {"state": {}, "timeframe": {}, "variant": {}, "excluded": 0, "unmapped": []}
    regime = ((state.get("structure_digest") or {}).get("regime") or {})
    digest_scan, state_scan = regime.get("as_of_scan_id"), state.get("scan_id")
    stale_digest = digest_scan is not None and state_scan is not None and digest_scan != state_scan
    g["stale_copy"] = None
    for p, v in walk(state):
        if v not in DIRECTIONS:
            continue
        fam, kind = classify_direction(p)
        if p == REGIME_COPY and (stale_digest or digest_scan is None):
            # a copy not written by THIS scan is a recording of the past, not a current assertion
            g["excluded"] += 1
            if stale_digest:
                g["stale_copy"] = {"path": p, "value": v, "copy_from_scan": digest_scan, "state_scan": state_scan}
        elif fam == "excluded":
            g["excluded"] += 1
        elif fam is None:
            g["unmapped"].append(p)
        else:
            g[kind].setdefault(fam, {})[p] = v
    return g


def present_families(state):
    g = direction_groups(state)
    return sorted(set(g["state"]) | set(g["timeframe"]) | set(g["variant"]))


def probe_directions(state, learned=None):
    """Reports DISAGREEMENT between distinct sources (calculation families; not proven statistically
    independent). It never says which side is right and never counts fields as votes. Severity follows the
    RELATIONSHIP that failed (FAMILY_PROVENANCE / _REL_SEVERITY): same writer or a consumer of the macro bias
    -> HIGH; a derived field that is allowed to lag -> MED; a faster timeframe or a deliberately different
    shadow rule -> LOW (expected to differ)."""
    g = direction_groups(state)
    out = []
    fam_vals = {f: sorted(set(m.values())) for f, m in g["state"].items()}
    by_value = {}
    for f, m in g["state"].items():
        for v in set(m.values()):
            by_value.setdefault(v, set()).add(f)
    if len(by_value) == 2:                                   # both directions asserted by current-state sources
        involved = sorted(g["state"])
        sides = {v: {f: sorted(p for p, x in g["state"][f].items() if x == v) for f in sorted(fs)} for v, fs in by_value.items()}
        # the findings's identity is the SET OF SOURCES in dispute, so a copy appearing or going does not reopen it
        disputed = sorted(f for f in involved if len(fam_vals[f]) > 1 or any(f in fs for fs in by_value.values()) and
                          any(f not in fs for fs in by_value.values()))
        paths = sorted(min(g["state"][f]) for f in disputed)
        bull = ", ".join(_FAMILY_LABEL[f] for f in sorted(by_value["BULLISH"]))
        bear = ", ".join(_FAMILY_LABEL[f] for f in sorted(by_value["BEARISH"]))
        rels = []
        for f in involved:
            if len(fam_vals[f]) > 1:
                rels.append(_REL_SEVERITY.get((f, f), ("same_writer", "HIGH")))
        for i, x in enumerate(involved):
            for y in involved[i + 1:]:
                if len(fam_vals[x]) > 1 or len(fam_vals[y]) > 1:
                    continue                                 # a family at odds with itself is counted above
                if fam_vals[x] != fam_vals[y]:
                    rels.append(_REL_SEVERITY.get((x, y)) or _REL_SEVERITY.get((y, x)) or ("unclassified", "MED"))
        if "mil" in involved and (state.get("mil_understanding") or {}).get("thesis_status") == "TRANSITIONING":
            age = _mil_transition_age_s(state)
            within = age is not None and age <= MIL_TRANSITION_GRACE_S
            rels = [(("mil_in_transition", "MED") if within else ("mil_transition_overdue", "HIGH"))
                    if r[0] == "consumer_of_macro_bias" else r for r in rels]
        rank = {"HIGH": 2, "MED": 1}
        rel, sev = max(rels or [("unclassified", "MED")], key=lambda r: (rank[r[1]], r[0] != "derived_and_may_lag"))
        out.append(finding("direction_conflict", sev,
                           f"Sources that should agree on direction disagree: BULLISH from {bull}; BEARISH from {bear}. "
                           f"Relationship that failed: {rel}. Which is right is not decided here and the cause is not established.",
                           paths, {"families": involved, "sides": sides, "distinct_sources": len(involved), "relationship": rel,
                                   "all_failed_relationships": sorted({r[0] for r in rels}),
                                   "excluded_recorded_fields": g["excluded"]}))
    macro = g["state"].get("htf_macro")
    macro_vals = sorted(set(macro.values())) if macro else []
    if len(macro_vals) == 1:
        for fam, m in sorted(g["timeframe"].items()):
            for p, v in sorted(m.items()):
                if v != macro_vals[0]:
                    out.append(finding("timeframe_divergence", "LOW",
                                       f"{_FAMILY_LABEL[fam][0].upper() + _FAMILY_LABEL[fam][1:]} is {v} while the macro bias is {macro_vals[0]}. "
                                       f"This can be a normal pullback; no fault is implied.",
                                       [p, min(macro)], {"timeframe_path": p, "timeframe_value": v, "macro_value": macro_vals[0],
                                                          "family": fam, "families": ["htf_macro", fam]}))
        for fam, m in sorted(g["variant"].items()):
            for p, v in sorted(m.items()):
                if v != macro_vals[0]:
                    out.append(finding("variant_divergence", "LOW",
                                       f"{p} ({v}) differs from the live macro bias ({macro_vals[0]}). It runs a deliberately different rule, "
                                       f"so this is expected to happen; no fault is implied.",
                                       [p, min(macro)], {"variant_path": p, "variant_value": v, "macro_value": macro_vals[0],
                                                          "family": fam, "families": ["htf_macro", fam]}))
    if g["stale_copy"]:
        sc = g["stale_copy"]
        out.append(finding("direction_copy_stale", "LOW",
                           f"{sc['path']} was written by scan {sc['copy_from_scan']} but the state is scan {sc['state_scan']} "
                           f"(an early return skipped the refresh), so it was not used as evidence.",
                           [sc["path"]], dict(sc)))
    if g["unmapped"]:
        out.append(finding("direction_unmapped", "LOW",
                           f"{len(g['unmapped'])} direction field(s) have no provenance entry and were not counted as evidence: "
                           + ", ".join(sorted(g["unmapped"])[:5]),
                           sorted(g["unmapped"])[:12], {"unmapped": sorted(g["unmapped"])}))
    return out


def probe_time(state, now=None):
    persisted = _ts(state.get("timestamp"))
    out = []
    for p, v in walk(state):
        t = _ts(v)
        if t is None:
            continue
        if persisted and t > persisted + timedelta(seconds=5):
            out.append(finding("time", "MED", f"{p} ({v}) is later than the moment the file says it was written", [p],
                               {"value": v, "written_at": state.get("timestamp")}))
        elif now and t > now + timedelta(minutes=5):
            out.append(finding("time", "MED", f"{p} ({v}) is in the future", [p], {"value": v}))
    return out


_ID_FLOAT = re.compile(r"-?\d+\.\d{3,}")


def probe_identity(state):
    """A string id of the form 'A|1.33093|1.31933' embeds numbers. Each must still equal some
    live numeric field; an id describing a different leg than the fields next to it is stale."""
    nums = [v for _p, v in walk(state) if _num(v)]
    out = []
    for p, v in walk(state):
        if isinstance(v, str) and "|" in v:
            toks = [float(t) for t in _ID_FLOAT.findall(v)]
            missing = [t for t in toks if not any(abs(t - n) < 1e-9 for n in nums)]
            if toks and missing:
                out.append(finding("identity", "MED", f"{p} embeds {missing} which match no live field", [p],
                                   {"id": v, "unmatched": missing}))
    return out


def sizes(state):
    """Per-path collection sizes (len of lists/dicts/strings over 40 chars), for growth tracking."""
    out = {}

    def rec(o, p):
        if isinstance(o, dict):
            out[p or "$"] = len(o)
            for k, v in o.items():
                rec(v, f"{p}.{k}" if p else str(k))
        elif isinstance(o, list):
            out[p] = len(o)
    rec(state, "")
    return out


def probe_growth(cur_sizes, hist):
    """hist: {path: [sizes over the last runs]}. A collection that grew on (almost) every one
    of its last N observations, and is not tiny, is a leak candidate."""
    out = []
    for p, series in hist.items():
        s = series + [cur_sizes.get(p)] if cur_sizes.get(p) is not None else series
        if len(s) >= 12 and s[-1] >= 30:
            ups = sum(1 for a, b in zip(s, s[1:]) if b > a)
            if ups >= 0.9 * (len(s) - 1) and s[-1] > s[0]:
                out.append(finding("growth", "MED", f"{p} has grown on {ups} of its last {len(s)-1} observations (now {s[-1]}) and never shrinks",
                                   [p], {"series_tail": s[-12:]}))
    return out


def probe_schema(prev_schema, state):
    cur = {p: type(v).__name__ for p, v in walk(state) if not re.search(r"\[\d+\]", p)}
    if not prev_schema:
        return [], cur
    gone = sorted(set(prev_schema) - set(cur))
    new = sorted(set(cur) - set(prev_schema))
    changed = sorted(p for p in cur if p in prev_schema and cur[p] != prev_schema[p] and "NoneType" not in (cur[p], prev_schema[p]))
    out = []
    if gone or changed:
        out.append(finding("schema", "LOW", f"{len(gone)} key(s) vanished and {len(changed)} changed type since the last run",
                           (gone + changed)[:8] or ["$"], {"vanished": gone[:20], "type_changed": changed[:20], "new": new[:20]}))
    return out, cur


def probe_version(state, base_dir):
    """The state file says which code wrote it; compare with the code on disk now. A mismatch means
    the file was produced by different code than the code about to be trusted to read it — a failed
    scan, a half-deployed commit, or a stale artifact. Says nothing if either side is unknown."""
    stamped = state.get("code_version")
    if base_dir is None:
        return []
    if stamped is None:
        return [finding("version", "LOW", "state.json carries no code_version stamp (written by code older than the stamp)",
                        ["code_version"], {"stamped": None})]
    if stamped == "unknown":
        return []
    now_v = compute_code_version(base_dir)
    if stamped != now_v:
        return [finding("version", "MED", f"state.json was written by code {stamped} but the code on disk is {now_v}",
                        ["code_version"], {"stamped": stamped, "on_disk": now_v})]
    return []


# ---------------------------------------------------------------- exercise: rerun the real MIL
def _mirror_obs(o):
    import dataclasses
    swap = {"BULLISH": "BEARISH", "BEARISH": "BULLISH"}
    kw = dataclasses.asdict(o)
    for k, v in list(kw.items()):
        if isinstance(v, str) and v in swap:
            kw[k] = swap[v]
    C = 3.0                                              # mirror levels about a constant: p -> 2C - p
    for k in ("macro_leg_origin", "macro_leg_extreme"):
        if _num(kw.get(k)):
            kw[k] = 2 * C - kw[k]
    return type(o)(**kw)


def _mirror_understanding(u):
    import dataclasses
    swap = {"BULLISH": "BEARISH", "BEARISH": "BULLISH"}

    def sw(x):
        if isinstance(x, dict):
            return {k: sw(v) for k, v in x.items()}
        if isinstance(x, list):
            return [sw(v) for v in x]
        return swap.get(x, x) if isinstance(x, str) else x
    d = dataclasses.asdict(u)
    for k in ("primary_thesis", "counter_thesis"):
        d[k] = sw(d[k])
    return dataclasses.replace(u, primary_thesis=d["primary_thesis"], counter_thesis=d["counter_thesis"])


def fuzz_mil(mil, seed=0, runs=60, steps=14):
    """Seeded and deterministic. Returns findings; [] means every property held."""
    rng = random.Random(seed)
    bad = {}

    def fail(prop, detail):
        bad.setdefault(prop, detail)

    def mk_obs(bias, stale=False, promo=None):
        return mil.ObservedLegState(
            macro_bias_confirmed=bias, macro_bias_stale=stale, macro_leg_origin=1.33, macro_leg_extreme=1.32,
            macro_leg_direction=bias if bias in DIRECTIONS else None, macro_leg_was_choch=False,
            last_invalidation_origin_violated=None, last_invalidation_retrace_violated=None,
            macro_candidate_leg_direction=None, macro_candidate_leg_atr_ok=None, macro_candidate_leg_vs_prior_ok=None,
            macro_candidate_leg_range_pips=None, macro_candidate_leg_required_atr_pips=None,
            macro_candidate_leg_required_prior_pips=None, leg15_direction=bias if bias in DIRECTIONS else None,
            leg15_break_count=1, promotion_confirmed=promo)

    def fresh(direction):
        return mil.MarketUnderstanding(mil.ThesisStatus.SUPPORTED, {"direction": direction}, [], [],
                                       mil.FailureCondition(0.0, "above", 0.0), [], "Research only")

    biases = ["BULLISH", "BEARISH", "CONSOLIDATION"]
    for r in range(runs):
        start = rng.choice(DIRECTIONS)
        seq = [(rng.choice(biases), rng.random() < 0.12, (True if rng.random() < 0.08 else None)) for _ in range(steps)]
        u = fresh(start)
        u_m = _mirror_understanding(u)
        for i, (b, stale, promo) in enumerate(seq):
            o = mk_obs(b, stale, promo)
            now = f"2026-10-01T10:{i:02d}:00+00:00"
            before = json.dumps(mil.understanding_to_dict(u), sort_keys=True, default=str)
            try:
                a1, a2 = mil.reconcile(u, o, now), mil.reconcile(u, o, now)
            except Exception as e:
                fail("no_crash", f"reconcile raised {type(e).__name__}: {e}")
                break
            if json.dumps(mil.understanding_to_dict(u), sort_keys=True, default=str) != before:
                fail("no_mutation", "reconcile changed its input")
            if mil.understanding_to_dict(a1) != mil.understanding_to_dict(a2):
                fail("deterministic", "same input produced different output")
            if len(a1.unresolved_questions) > 200 or len(a1.history) > 500:
                fail("bounded", f"unresolved={len(a1.unresolved_questions)} history={len(a1.history)}")
            m1 = mil.reconcile(u_m, _mirror_obs(o), now)
            exp = _mirror_understanding(a1)
            if (m1.thesis_status != a1.thesis_status
                    or (m1.primary_thesis or {}).get("direction") != (exp.primary_thesis or {}).get("direction")):
                fail("mirror_symmetry", f"bull/bear branches differ: {a1.thesis_status.value}/{a1.primary_thesis} vs mirrored {m1.thesis_status.value}/{m1.primary_thesis}")
            u, u_m = a1, mil.reconcile(u_m, _mirror_obs(o), now)

    # convergence: constant, clean input must settle on something consistent with that input
    for start in DIRECTIONS:
        for target in DIRECTIONS:
            u = fresh(start)
            for i in range(40):
                u = mil.reconcile(u, mk_obs(target), f"2026-10-01T11:{i:02d}:00+00:00")
            if (u.primary_thesis or {}).get("direction") != target:
                fail("convergence", f"after 40 identical scans of a confirmed {target} bias, primary_thesis is still "
                                    f"{(u.primary_thesis or {}).get('direction')} with status {u.thesis_status.value} (started {start})")
    return [finding("exercise", "HIGH", f"mil.py property '{k}' failed: {v}", ["mil_understanding"],
                    {"property": k, "detail": v, "seed": seed}) for k, v in sorted(bad.items())]


# ---------------------------------------------------------------- orchestration
def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def localize(findings):
    """One root cause, one alarm: findings that share a path cluster are ordered by layer;
    the earliest layer is only the earliest AFFECTED layer, not a verdict, the rest are marked downstream."""
    out = [dict(f) for f in findings]
    ranked = sorted(out, key=lambda f: (f["layer"], -SEV_RANK[f["severity"]]))
    if ranked:
        origin_layer = ranked[0]["layer"]
        for f in out:
            f["first_divergence"] = (f["layer"] == origin_layer and f["severity"] in ("HIGH", "MED"))
    return out


def inspect_state(state, mem=None, now=None, mil_module=None, seed=0, base_dir=None):
    """Pure given its inputs. Returns (findings, new_mem)."""
    mem = mem or {}
    found = []
    found += probe_dwell(state, now)
    found += probe_placeholder(state)
    found += probe_directions(state, mem)
    found += probe_time(state, now)
    found += probe_identity(state)
    found += probe_version(state, base_dir)
    cur_sizes = sizes(state)
    hist = {p: list(v) for p, v in (mem.get("sizes") or {}).items()}
    found += probe_growth(cur_sizes, hist)
    sch, new_schema = probe_schema(mem.get("schema"), state)
    found += sch
    if mil_module is not None:
        found += fuzz_mil(mil_module, seed=seed)
    new_hist = {}
    for p in set(hist) | set(cur_sizes):
        if p in cur_sizes:
            new_hist[p] = (hist.get(p, []) + [cur_sizes[p]])[-30:]
    new_mem = {"sizes": new_hist, "schema": new_schema, "runs": int(mem.get("runs", 0)) + 1,
               "disagree_ok": mem.get("disagree_ok", {}), "present_families": present_families(state)}
    return localize(found), new_mem


def run(root=".", now=None, mil_module=None):
    now = ledger.to_utc(now) if now is not None else datetime.now(timezone.utc)
    state = _read_json(os.path.join(root, "state.json"))
    if state is None:
        print("[INSPECTOR] no readable state.json; nothing to inspect")
        return {"findings": 0, "new": 0, "resolved": 0}
    mem_path = os.path.join(root, "inspector_state.json")
    mem = _read_json(mem_path) or {}
    if mil_module is None:
        try:
            sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # mil.py lives one folder up
            import mil as mil_module                       # the DEPLOYED module, exercised read-only
        except Exception as e:
            print(f"[INSPECTOR] could not import mil for the exercise: {e}")
            mil_module = None
    seed = int(now.timestamp() // 3600)                    # a new deterministic seed every hour
    findings, new_mem = inspect_state(state, mem, now, mil_module, seed, base_dir=root)

    path = os.path.join(root, "integrity_ledger.jsonl")
    recs, _ = ledger.read(path)
    open_fp = {}
    for r in recs:
        p = r["payload"]
        if r["event_type"] == FINDING:
            open_fp[p["fingerprint"]] = r
        elif r["event_type"] == RESOLVED:
            open_fp.pop(p["fingerprint"], None)
    cur_fp = {f["fingerprint"]: f for f in findings}
    new = resolved = 0
    for fp, f in cur_fp.items():
        if fp not in open_fp:
            ledger.append(path, FINDING, dict(f, scan_ts=state.get("timestamp"), first_seen=now.isoformat()), now=now)
            new += 1
    present = set(new_mem.get("present_families") or [])
    held = 0
    for fp in list(open_fp):
        if fp not in cur_fp:
            need = set(((open_fp[fp]["payload"].get("evidence") or {}).get("families")) or [])
            if need - present:
                # the data needed to judge it is gone: that is NOT a resolution. Leave it open and say why, once.
                held += 1
                if not any(r["event_type"] == UNEVALUABLE and r["payload"].get("fingerprint") == fp
                           and r["recorded_at"] > open_fp[fp]["recorded_at"] for r in recs):
                    ledger.append(path, UNEVALUABLE, {"fingerprint": fp, "missing_families": sorted(need - present),
                                                      "noted_at": now.isoformat()}, now=now)
                continue
            ledger.append(path, RESOLVED, {"fingerprint": fp, "resolved_at": now.isoformat()}, now=now)
            resolved += 1
    tmp = mem_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(new_mem, f)
    os.replace(tmp, mem_path)
    for f in sorted(findings, key=lambda f: -SEV_RANK[f["severity"]]):
        print(f"[INSPECTOR] {f['severity']:4s} {f['probe']:11s} {'ORIGIN ' if f.get('first_divergence') else '       '}{f['summary']}")
    summary = {"findings": len(findings), "new": new, "resolved": resolved, "held_unevaluable": held}
    print("[INSPECTOR]", json.dumps(summary))
    return summary


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else ".")
