"""
scanner_integrity.py
=====================
System Integrity Observer. Same tier as min_scanner.py / brain.py: a
read-only consumer of state.json and the pipeline's own log files.
Imports scanner_common for paths/persistence helpers only. Never
imports scanner_live.py. Never writes anything scanner_live.py reads.
Nothing here is a live gate — this answers a different question than
every other module in the codebase: not "what is the market doing" but
"did the system itself behave the way it claims to behave."

Per chat, 2026-08-26 — this module deliberately does NOT contain a list
of the bugs this project has already found (EXP7/EXP4 tag drop, the
break_count write gap, the continuation-snapshot gap, silent state
resets on ephemeral runners, etc). Encoding those directly would just
be a bug-specific detector wearing a generic-sounding name. Instead,
each layer below checks a PROPERTY a healthy system must have; the fact
that those properties, when checked, would have caught every bug in
that list is a validation of the approach, not the design itself.

THREE LAYERS, deliberately ordered cheapest/most-certain first:

  1. SCHEMA INTEGRITY       Is a persisted object shaped the way its
                            own type already declares? Fully generic —
                            derived from dataclass field definitions
                            that already exist in scanner_observation.py,
                            and from structural signatures learned at
                            runtime for plain-dict outputs. Nothing here
                            is hand-listed per object.

  2. RELATIONSHIP INTEGRITY  Do a small number of EXPLICITLY DECLARED
                            cross-field facts still hold? This is real
                            domain knowledge and is NOT generic — kept
                            in exactly one table (RELATIONSHIP_RULES),
                            reviewed deliberately, added to rarely and
                            only once a rule is well understood. Do not
                            let this table grow into "if <bug I saw>."

  3. CONTINUITY INTEGRITY    Does this scan's set of completed pipeline
                            stages match the pattern every previous scan
                            established? Inferred entirely from the
                            pipeline's OWN execution history (see
                            PipelineCheckpoint / continuity_report) —
                            no stage order, no stage list, is hardcoded
                            here. This is the closest thing in this file
                            to "catch a failure nobody thought to name,"
                            without resorting to statistical baselining
                            on TRADING behavior — deliberately rejected
                            (see chat) because a legitimate rare market
                            event looks identical to a bug under that
                            kind of baseline. Stage co-occurrence has no
                            such ambiguity: it's deterministic code
                            structure, not market behavior.
"""

import os
import json
import dataclasses
import typing
from datetime import datetime, timezone

from scanner_common import BASE_DIR, atomic_write_json

INTEGRITY_BASELINE_FILE = os.path.join(BASE_DIR, "integrity_baseline.json")
PIPELINE_LOG_FILE       = os.path.join(BASE_DIR, "pipeline_checkpoints.jsonl")

MIN_OCCURRENCES_FOR_CONTINUITY_RULE = 20
CONTINUITY_CONFIDENCE_THRESHOLD     = 0.97
PIPELINE_LOG_MAX_RECORDS_READ       = 500


# =========================================================================
# LAYER 1 — SCHEMA INTEGRITY
# =========================================================================

def _field_is_required(f):
    """A dataclass field counts as 'required' if it has no default and
    no default_factory — e.g. MarketThesis.current_state, which every
    real constructor call in this codebase always supplies a concrete
    value for. Optional[...] typing is NOT what makes a field optional
    here: MarketPhase/MarketThesis/MarketIntent all pair Optional[...]
    typing WITH an explicit `= None` default for genuinely-optional
    fields, so the default is the honest signal, not the type hint."""
    return (f.default is dataclasses.MISSING and
            f.default_factory is dataclasses.MISSING)


def _json_permissive_family(type_hint):
    """Maps a dataclass field's declared type to the family of Python
    types it could legitimately be AFTER a JSON round-trip through
    state.json (tuples collapse to lists, Enums are persisted as
    `.value` elsewhere in this codebase — see enum_fields param below).
    Returns None for anything too ambiguous to check safely; skipping a
    check is always preferable to fabricating a false violation."""
    origin = typing.get_origin(type_hint)
    if origin is typing.Union:
        args = [a for a in typing.get_args(type_hint) if a is not type(None)]
        if len(args) == 1:
            type_hint = args[0]
            origin = typing.get_origin(type_hint)

    mapping = {
        str: (str,), int: (int,), float: (float, int), bool: (bool,),
        list: (list,), tuple: (list, tuple), dict: (dict,),
    }
    if type_hint in mapping:
        return mapping[type_hint]
    if origin in (list, tuple, dict):
        return mapping.get(origin)
    return None


def dataclass_schema_report(dataclass_type, state, prefix, enum_fields=None,
                             field_name_overrides=None):
    """
    GENERIC — no knowledge of what MarketThesis/MarketIntent MEAN, only
    what fields the class itself declares. Checks state.json (flattened
    under `prefix + field.name`, per this codebase's own persistence
    convention — e.g. MarketThesis.current_state is persisted as
    "market_thesis_current_state") for:

      - required fields: present in state at all. "Present" means the
        key exists, not "not None" — many optional-typed fields are
        legitimately None; a field with NO default, however, is one
        every real caller in this codebase always supplies a concrete
        value for, so its total absence is a real signal.
      - stored value round-trips through a permissive type family
        (best-effort only — see _json_permissive_family).

    `enum_fields`: optional {field_name: EnumClass}. This codebase
    always persists Enum fields as `.value` (transition_cause.value,
    phase.value — see scanner_observation.py/scanner_live.py
    throughout), so a plain type check on those fields would always
    "fail" against the class unless told the field is enum-backed.

    `field_name_overrides`: optional {field_name: state_key_suffix}.
    NOT every dataclass in this codebase persists as prefix+field.name
    verbatim — confirmed by testing against the actual apply_state_
    updates() call sites, not assumed:
      - MarketIntent.watching_for is persisted as "market_intent_watch_codes"
      - MarketIntent.not_interested_in is persisted as "market_intent_caution_codes"
    Without this override, this function would report both as
    permanently missing — a false positive on every single scan, which
    is worse than not checking them at all. This override table is the
    same kind of small, explicit, testable fact as RELATIONSHIP_RULES
    below, not an escape hatch — add to it only once you've confirmed
    the real persisted key name, the way MarketIntent's two entries here
    were confirmed.

    MarketPhase is deliberately NOT covered by this function at all: its
    fields are split across TWO different key prefixes ("market_phase_"
    for phase/age_bars/history, "market_thesis_" for break_count/
    transition_cause/aging_reason/dist_in_atr/swept_boundary/
    volatility_hint — see compute_market_phase()'s own `updates` dict).
    It doesn't follow a single prefix+field.name convention at all, so a
    generic checker can't safely cover it without becoming a hand-built,
    MarketPhase-specific schema — which defeats the point of this being
    generic. Confirmed by reading compute_market_phase() directly, not
    assumed from the class definition.

    Returns a list of violation dicts. Empty list = schema-clean this
    scan. Never raises — a schema-checker that can crash the thing it's
    checking is worse than not having one.
    """
    violations = []
    enum_fields = enum_fields or {}
    field_name_overrides = field_name_overrides or {}
    try:
        fields = dataclasses.fields(dataclass_type)
    except TypeError:
        return [{"field": None, "state_key": None,
                 "issue": f"{dataclass_type} is not a dataclass — cannot introspect"}]

    for f in fields:
        state_key = prefix + field_name_overrides.get(f.name, f.name)
        present = state_key in state

        if _field_is_required(f) and not present:
            violations.append({
                "field": f.name, "state_key": state_key,
                "issue": "required field missing from state entirely",
            })
            continue
        if not present:
            continue  # optional and absent — legitimate

        value = state[state_key]
        if value is None:
            continue  # None is always legitimate once the key is present

        if f.name in enum_fields:
            enum_cls = enum_fields[f.name]
            valid_values = {m.value for m in enum_cls}
            if value not in valid_values:
                violations.append({
                    "field": f.name, "state_key": state_key,
                    "issue": f"value {value!r} is not a valid {enum_cls.__name__} member",
                })
            continue

        expected = _json_permissive_family(f.type)
        if expected is not None and not isinstance(value, expected):
            violations.append({
                "field": f.name, "state_key": state_key,
                "issue": f"expected type family {expected}, got {type(value).__name__}",
            })

    return violations


# ---- structural signature (for plain-dict outputs, not dataclasses) ----

def _signature(obj, path=""):
    """Recursively walks a JSON-shaped object into a flat {path: type_name}
    fingerprint — order-independent (dict keys sorted), CONTENT-
    independent (only shape/type is recorded, never a value). This is
    what lets the same function fingerprint structure_digest,
    live_tier_digest, or anything else this codebase later adds as a
    plain-dict output, with no per-object schema ever written by hand.

    Lists in this codebase are homogeneous by construction (every
    watch_codes entry has the same {code, role, zone_low, zone_high,
    sentence} shape) — recorded as "list" plus the first element's own
    signature as representative, not one entry per item."""
    sig = {}
    if isinstance(obj, dict):
        for k in sorted(obj.keys()):
            sig.update(_signature(obj[k], f"{path}.{k}" if path else k))
    elif isinstance(obj, list):
        sig[path] = "list"
        if obj:
            sig.update(_signature(obj[0], f"{path}[]"))
    else:
        sig[path] = type(obj).__name__ if obj is not None else "NoneType"
    return sig


def _load_baseline():
    try:
        with open(INTEGRITY_BASELINE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_baseline(baseline):
    try:
        atomic_write_json(INTEGRITY_BASELINE_FILE, baseline)
    except Exception as e:
        print("[INTEGRITY BASELINE SAVE ERROR] " + str(e))


def structural_signature_check(obj, label, baseline=None):
    """
    GENERIC — works on any JSON-shaped dict this codebase produces
    (structure_digest, live_tier_digest, future additions). No hand-
    written schema for any of them.

    First time `label` is seen: records its signature as the baseline,
    returns no violations (nothing to compare against yet).

    Every time after, compares against the recorded baseline:
      - a path present in the baseline but MISSING now -> violation.
        The generic version of "something that used to exist silently
        stopped existing," with no knowledge of what the field was.
      - a path present with a DIFFERENT type than the baseline ->
        violation. "A field has the wrong value/type but remains
        syntactically valid," also caught with no knowledge of the field.
      - a NEW path not in the baseline -> NOT a violation. Recorded as
        schema evolution and folded into the baseline. Treating every
        addition as a violation would punish every future feature this
        bot ships — that's friction, not integrity.

    Returns (violations, updated_baseline). Caller persists the
    baseline (via _save_baseline) only if it changed. Never raises.
    """
    current_sig = _signature(obj)
    baseline = dict(baseline) if baseline is not None else _load_baseline()
    prior_sig = baseline.get(label)

    if prior_sig is None:
        baseline[label] = current_sig
        return [], baseline

    violations = []
    for path, expected_type in prior_sig.items():
        if path not in current_sig:
            violations.append({
                "label": label, "path": path,
                "issue": "field present in the recorded baseline is missing this scan",
            })
        elif current_sig[path] != expected_type:
            violations.append({
                "label": label, "path": path,
                "issue": f"type changed: baseline={expected_type}, now={current_sig[path]}",
            })

    new_paths = {p: t for p, t in current_sig.items() if p not in prior_sig}
    if new_paths:
        merged = dict(prior_sig)
        merged.update(new_paths)
        baseline[label] = merged

    return violations, baseline


# =========================================================================
# LAYER 2 — RELATIONSHIP INTEGRITY (explicit, small, curated)
# =========================================================================
# Each rule below targets a real, well-understood fact about this
# codebase's own architecture — most of them the exact invariant a past
# bug violated, expressed as the STANDING rule that would catch it (and
# its recurrence in a different field), not as a copy of the bug itself.
# Keep this list short. A rule belongs here only once you understand it
# this precisely — if you're not sure a rule is always true, it doesn't
# belong here yet.

def _rule_continuation_snapshot_presence(state):
    """capture_prior_continuation_snapshot() (scanner_observation.py)
    only returns {} (no-op) on the FIRST leg this codebase has ever
    tracked — see its own docstring. By campaign_continuation_count >= 2,
    a real previous continuation must exist, so prior_continuation_leg_*
    must be populated. This checks the invariant the function's
    docstring already states, so it stays correct even if the function's
    internals change later."""
    count = state.get("campaign_continuation_count")
    if count is None or count < 2:
        return None
    missing = [k for k in ("prior_continuation_leg_direction",
                           "prior_continuation_leg_origin",
                           "prior_continuation_leg_extreme")
               if state.get(k) is None]
    if missing:
        return {
            "rule": "continuation_snapshot_presence",
            "issue": f"campaign_continuation_count={count} but {missing} "
                     f"are missing/null — a continuation this deep must "
                     f"have a captured prior leg",
        }
    return None


def _rule_thesis_breakcount_copresence(state):
    """Targets the exact SHAPE of the bug fixed 2026-08-12 (a field read
    every scan by prev_thesis_snapshot but never actually written by the
    block responsible for writing it) as a standing co-presence
    invariant, not a copy of that specific bug: if the thesis ran this
    scan (current_state is set), its own break_count must also be
    present. Would catch this class recurring in any other thesis field
    the same way."""
    if state.get("market_thesis_current_state") is None:
        return None
    if state.get("market_thesis_break_count") is None:
        return {
            "rule": "thesis_breakcount_copresence",
            "issue": "market_thesis_current_state is set but "
                     "market_thesis_break_count is missing — thesis ran "
                     "but didn't persist a field it's supposed to",
        }
    return None


def _rule_thesis_freshness(state, max_gap_minutes=30):
    """Targets 'a subsystem silently stops participating' without ever
    raising — thesis fields could sit stale for days while the rest of
    state.json keeps moving every scan. Compares market_thesis_updated_at
    to the overall save timestamp; more than a few scan-intervals apart
    means thesis stopped updating even though scans kept running."""
    updated = state.get("market_thesis_updated_at")
    overall = state.get("timestamp")
    if not updated or not overall:
        return None
    try:
        gap_minutes = abs((datetime.fromisoformat(overall) -
                            datetime.fromisoformat(updated)).total_seconds()) / 60
    except Exception:
        return None
    if gap_minutes > max_gap_minutes:
        return {
            "rule": "thesis_freshness",
            "issue": f"market_thesis_updated_at is {gap_minutes:.0f} "
                     f"minutes older than the current state snapshot — "
                     f"thesis appears to have stopped updating",
        }
    return None


RELATIONSHIP_RULES = [
    _rule_continuation_snapshot_presence,
    _rule_thesis_breakcount_copresence,
    _rule_thesis_freshness,
]


def run_relationship_checks(state):
    violations = []
    for rule in RELATIONSHIP_RULES:
        try:
            v = rule(state)
        except Exception as e:
            v = {"rule": rule.__name__, "issue": f"rule itself raised: {e}"}
        if v:
            violations.append(v)
    return violations


# =========================================================================
# LAYER 3 — CONTINUITY INTEGRITY (inferred, not declared)
# =========================================================================

class PipelineCheckpoint:
    """Accumulates the names of pipeline stages that completed
    successfully THIS scan; .flush() appends one line to
    PIPELINE_LOG_FILE. This is the only new instrumentation this layer
    needs — it records THAT a stage completed, never WHAT it computed,
    so it carries no domain knowledge and needs no changes as the
    pipeline's own stages evolve.

    Intended call sites: alongside each try/except block already in
    _scan_once() (scanner_live.py) that wraps one additive, best-effort
    stage — structure_digest, phase, thesis, intent, markov, etc. See
    the proposed integration diff (not applied here) for exact call
    sites; this class has zero dependency on scanner_live.py itself."""

    def __init__(self):
        self._stages = []

    def mark(self, stage_name):
        self._stages.append(stage_name)

    def flush(self, now_utc):
        try:
            with open(PIPELINE_LOG_FILE, "a") as f:
                f.write(json.dumps({
                    "ts": now_utc.isoformat(),
                    "stages": list(self._stages),
                }) + "\n")
        except Exception as e:
            print("[PIPELINE CHECKPOINT LOG ERROR] " + str(e))


def _read_pipeline_log(limit=PIPELINE_LOG_MAX_RECORDS_READ):
    if not os.path.exists(PIPELINE_LOG_FILE):
        return []
    records = []
    with open(PIPELINE_LOG_FILE, "r") as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records[-limit:]


def continuity_report(min_occurrences=MIN_OCCURRENCES_FOR_CONTINUITY_RULE,
                       confidence=CONTINUITY_CONFIDENCE_THRESHOLD,
                       records=None):
    """
    GENERIC — infers "stage A implies stage B" rules purely from how
    often B has historically followed A across past scans, then checks
    only the MOST RECENT scan against rules the history itself
    established. No stage name, no stage order, no pipeline shape is
    hardcoded anywhere in this function.

    Two guardrails, deliberately mirroring the "don't let a bug become
    the new normal" objection raised against learned baselines
    generally (see chat, 2026-08-26):
      - a rule is never asserted from fewer than `min_occurrences` prior
        scans where A occurred — not enough history to trust yet.
      - a rule is only asserted when B followed A at least `confidence`
        of the time historically — a stage that's ALREADY flaky
        (sometimes present, sometimes not) never becomes a rule that
        flags its own normal absence.

    This is the closest thing in this codebase to detecting a failure
    nobody thought to name in advance, without the "rare legitimate
    event looks like a bug" problem statistical baselines on TRADE
    behavior have — stage co-occurrence is deterministic code
    structure, not market behavior, so there's no legitimate reason for
    it to vary scan to scan the way trade frequency legitimately does.
    """
    records = records if records is not None else _read_pipeline_log()
    if len(records) < min_occurrences + 1:
        return {"status": "insufficient history",
                "have": len(records), "need": min_occurrences + 1,
                "violations": []}

    history = records[:-1]
    latest = records[-1]
    all_stages = sorted({s for r in history for s in r["stages"]})

    # Collect every (a -> b) rule the history supports, then dedupe by
    # `b`: several antecedents can all imply the same missing stage, and
    # reporting "b is missing" once (citing the strongest antecedent) is
    # what a human needs — not one line per antecedent that happens to
    # also predict it.
    best_support = {}  # b -> (rate, count, a)
    for a in all_stages:
        occurrences_with_a = [r for r in history if a in r["stages"]]
        if len(occurrences_with_a) < min_occurrences or a not in latest["stages"]:
            continue
        for b in all_stages:
            if b == a or b in latest["stages"]:
                continue
            co_occur = sum(1 for r in occurrences_with_a if b in r["stages"])
            rate = co_occur / len(occurrences_with_a)
            if rate < confidence:
                continue
            prior = best_support.get(b)
            if prior is None or len(occurrences_with_a) > prior[1]:
                best_support[b] = (rate, len(occurrences_with_a), a)

    violations = [
        {
            "at_ts": latest["ts"],
            "issue": f"'{b}' has followed '{a}' in {rate:.0%} of {count} "
                     f"prior scans, but did not occur this scan",
        }
        for b, (rate, count, a) in sorted(best_support.items())
    ]
    return {"status": "ok" if not violations else "violations found",
            "violations": violations}


# =========================================================================
# ORCHESTRATION
# =========================================================================

def _default_dataclass_checks():
    """Only MarketThesis and MarketIntent are included — both confirmed,
    by reading their actual apply_state_updates() call sites in
    scanner_live.py, to follow a checkable persistence convention
    (MarketIntent needs field_name_overrides for two renamed fields; see
    dataclass_schema_report's docstring). MarketPhase is deliberately
    excluded — confirmed its fields split across two unrelated prefixes,
    see the same docstring. Imported lazily inside the function that
    uses this, not at module load, so scanner_integrity.py has no
    load-time dependency on scanner_observation.py for callers that only
    want Layers 2/3."""
    from scanner_observation import MarketThesis, MarketIntent
    return [
        (MarketThesis, "market_thesis_", None, None),
        (MarketIntent, "market_intent_", None,
         {"watching_for": "watch_codes", "not_interested_in": "caution_codes"}),
    ]


def run_integrity_pass(state, dataclass_checks=None, digest_keys=None):
    """
    dataclass_checks: list of (dataclass_type, prefix, enum_fields,
        field_name_overrides) for Layer 1. Defaults to
        _default_dataclass_checks() — the two objects confirmed safe to
        check generically. Pass an explicit [] to skip Layer 1 entirely.
    digest_keys: list of (label, state_key) plain-dict outputs to run
        Layer-1b structural signature checks against. Defaults to the
        two that already exist in state.json today.
    """
    digest_keys = digest_keys or [
        ("structure_digest", "structure_digest"),
        ("live_tier_digest", "live_tier_digest"),
    ]
    if dataclass_checks is None:
        dataclass_checks = _default_dataclass_checks()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema": [], "structure": [], "relationships": [], "continuity": None,
    }

    for dc_type, prefix, enum_fields, overrides in dataclass_checks:
        report["schema"].extend(
            dataclass_schema_report(dc_type, state, prefix, enum_fields, overrides))

    baseline = _load_baseline()
    baseline_changed = False
    for label, key in digest_keys:
        obj = state.get(key)
        if obj is None:
            continue
        violations, baseline = structural_signature_check(obj, label, baseline)
        report["structure"].extend(violations)
        baseline_changed = True
    if baseline_changed:
        _save_baseline(baseline)

    report["relationships"] = run_relationship_checks(state)
    report["continuity"] = continuity_report()

    return report


def format_integrity_report(report):
    lines = [f"🔎 *Integrity Report* — {report['generated_at']}"]

    cont = report["continuity"]
    cont_clean = cont["status"] != "violations found"
    total_hard_violations = (len(report["schema"]) + len(report["structure"]) +
                              len(report["relationships"]) +
                              (0 if cont_clean else len(cont["violations"])))
    if total_hard_violations == 0:
        lines.append("✅ No integrity violations at any layer checked.")
    for v in report["schema"]:
        lines.append(f"🧬 SCHEMA — {v['state_key']}: {v['issue']}")
    for v in report["structure"]:
        lines.append(f"🧩 STRUCTURE — {v['label']}.{v['path']}: {v['issue']}")
    for v in report["relationships"]:
        lines.append(f"🔗 RELATIONSHIP — {v['rule']}: {v['issue']}")
    if cont["status"] == "insufficient history":
        lines.append(f"⏳ CONTINUITY — not enough history yet "
                      f"({cont['have']}/{cont['need']} scans)")
    else:
        for v in cont["violations"]:
            lines.append(f"⛓ CONTINUITY — {v['issue']}")

    return "\n".join(lines)
