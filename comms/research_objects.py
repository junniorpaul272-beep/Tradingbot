"""
research_objects.py — PHASE 1 of the catalog's research engine (design: BLUEPRINT section 11).
Objects and rules only. NO RANDOMNESS, no simulation, no statistics: those come later, one null
model at a time. Imports ledger only. Everything is an append-only ledger event, so "what was known
when" is reconstructable and nothing here can rewrite a trade, the engine's history, or an earlier belief.

THE PIPELINE THIS ENFORCES
  question -> hypothesis -> what is claimed -> what must stay fixed (PRESERVE) -> what may be
  randomised (RANDOMISE) -> FREEZE -> [null distribution: later] -> compare -> robustness ->
  account for the search (research_family_id) -> unseen data -> limitations -> conclusion.

RULES (each enforced in code and covered by a test)
  1. A spec must DECLARE preserve and randomise lists, and they must match the null model's own
     contract. "You claimed to test direction but also randomised entry location" is rejected.
  2. A spec is CONTENT-ADDRESSED: spec_id = hash of its canonical content. A different hypothesis, population,
     seed or declaration is therefore a different experiment, by construction. Nothing can be "edited".
  3. FREEZE is a ledger event. Results can only be recorded against a frozen spec, and only if they cite the
     frozen spec_id and its population_id (a changed population invalidates the comparison).
  4. Data splits: development data may be read any time; validation only after a freeze; the final test only
     after a freeze and exactly once — a second read is recorded as a PEEK and travels with the result.
  5. Every experiment carries research_family_id. family_summary() says "one of N searches", permanently.
  6. Statuses describe an EXPERIMENT, not a verdict on the strategy. INCONCLUSIVE does not mean "doesn't work".
  7. A result must state its effective sample size (independent episodes), not just the row count.
"""

import hashlib
import json

import ledger
from ledger import LedgerError

HYPOTHESIS_DRAFTED = "HYPOTHESIS_DRAFTED"
SPEC_DRAFTED = "EXPERIMENT_SPEC_DRAFTED"
SPEC_FROZEN = "EXPERIMENT_FROZEN"
SPLIT_ACCESSED = "DATA_SPLIT_ACCESSED"
RESULT_RECORDED = "RESEARCH_RESULT_RECORDED"

STATUSES = ("SUPPORTED", "NOT_SUPPORTED", "INCONCLUSIVE", "DATA_INSUFFICIENT", "INVALID_EXPERIMENT",
            "CONFOUNDING_DETECTED", "MULTIPLE_TESTING_RISK", "OUT_OF_SAMPLE_PENDING", "OUT_OF_SAMPLE_FAILED")
SPLITS = ("development", "validation", "final_test")

# Each null documents the question it answers AND the one it does not. `implemented` is False until
# the simulation exists; the contract (what it may randomise, what it must hold fixed) is binding now.
NULL_MODELS = {
    "direction_randomization": dict(
        implemented=False, randomizes={"direction"},
        must_preserve={"instrument", "entry_price", "stop_distance", "target_distance", "costs", "session", "position_size", "max_holding_time"},
        answers="Does my directional choice matter, given these exact entries, stops, targets and costs?",
        does_not_answer="Whether the ENTRIES were good, or whether the market simply drifted my way (use a drift benchmark)."),
    "entry_timing": dict(
        implemented=False, randomizes={"entry_time"},
        must_preserve={"instrument", "direction", "stop_method", "target_method", "costs", "session", "position_size"},
        answers="Does my exact entry location/timing inside the eligible window matter?",
        does_not_answer="Whether trading this window at all is useful."),
    "opportunity_selection": dict(
        implemented=False, randomizes={"selected_opportunities"},
        must_preserve={"instrument", "eligible_population", "selection_count", "direction_rule", "stop_method", "target_method", "costs"},
        answers="Was selecting THESE opportunities, out of the eligible set, useful?",
        does_not_answer="Whether the direction or the stops were good."),
    "session_assignment": dict(
        implemented=False, randomizes={"session"},
        must_preserve={"instrument", "eligible_population", "direction", "stop_method", "target_method", "costs"},
        answers="Does my chosen session matter?",
        does_not_answer="Anything about entry quality; sessions differ in conditions, so this needs a carefully matched population."),
    "block_resampling": dict(
        implemented=False, randomizes={"block_order"},
        must_preserve={"instrument", "within_block_structure", "block_definition", "costs"},
        answers="How sensitive is the result to the temporal ORDER of outcomes (clustering of wins/losses)?",
        does_not_answer="Why — sensitivity to ordering is a finding to investigate, not proof of skill or its absence."),
    "component_ablation": dict(
        implemented=False, randomizes=set(),               # not randomness: remove one rule component at a time
        must_preserve={"instrument", "population", "costs", "all_other_components"},
        answers="Which component of the setup (bias, BOS, engulfing, fib, retest, ...) contributes anything?",
        does_not_answer="Whether the combined rule beats chance."),
}
BENCHMARKS = ("always_follow_htf", "always_buy_in_regime", "always_sell_in_regime", "buy_and_hold_exposure")

REQUIRED_SPEC_FIELDS = ("question", "claim_under_test", "instrument", "population", "decision_under_test", "preserve",
                        "randomize", "outcome_metric", "null_model", "research_family_id", "data_cutoff", "data_split")


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_no_default)


def _no_default(o):
    raise LedgerError(f"spec must be plain JSON, got {type(o).__name__}")


def content_id(prefix, obj):
    return prefix + hashlib.sha256(canonical(obj).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- validation
def validate_spec(spec):
    """Returns a list of problems ([] = valid). Does not raise: the caller decides what to do with a draft."""
    p = []
    if not isinstance(spec, dict):
        return ["spec must be a dict"]
    for f in REQUIRED_SPEC_FIELDS:
        if spec.get(f) in (None, "", [], {}):
            if f == "randomize" and spec.get("null_model") == "component_ablation":
                continue
            p.append(f"missing field: {f}")
    if p:
        return p
    nm = NULL_MODELS.get(spec["null_model"])
    if nm is None:
        return [f"unknown null_model {spec['null_model']!r}; known: {sorted(NULL_MODELS)}"]
    preserve, randomize = set(spec["preserve"]), set(spec["randomize"])
    if preserve & randomize:
        p.append(f"declared both preserved and randomised: {sorted(preserve & randomize)}")
    extra = randomize - nm["randomizes"]
    if extra:
        p.append(f"{spec['null_model']} may only randomise {sorted(nm['randomizes'])}, but the spec also randomises "
                 f"{sorted(extra)} — that would test something other than the claim")
    short = nm["randomizes"] - randomize
    if short:
        p.append(f"{spec['null_model']} must randomise {sorted(short)}, which the spec does not")
    unfixed = nm["must_preserve"] - preserve
    if unfixed:
        p.append(f"{spec['null_model']} requires these to be preserved but the spec does not freeze them: {sorted(unfixed)}")
    if spec["data_split"] not in SPLITS:
        p.append(f"data_split must be one of {SPLITS}")
    pop = spec["population"]
    if not isinstance(pop, dict) or not pop.get("population_id") or not pop.get("eligibility_rule"):
        p.append("population needs population_id (a frozen, reproducible set) and eligibility_rule")
    for b in spec.get("benchmarks", []):
        if b not in BENCHMARKS:
            p.append(f"unknown benchmark {b!r}")
    if "seed" in spec and (isinstance(spec["seed"], bool) or not isinstance(spec["seed"], int)):
        p.append("seed must be an integer (reproducibility)")
    try:
        canonical(spec)
    except LedgerError as e:
        p.append(str(e))
    return p


# ---------------------------------------------------------------- the API (path = a ledger file)
def _recs(path):
    return ledger.read(path)[0]


def draft_hypothesis(path, question, claim, family_id, now=None, note=""):
    """The user's idea, as stated. No spec yet; nothing is frozen. Returns hypothesis_id."""
    if not question or not claim or not family_id:
        raise LedgerError("a hypothesis needs a question, a claim and a research_family_id")
    hid = content_id("H-", {"question": question, "claim": claim, "family": family_id})
    if any(r["event_type"] == HYPOTHESIS_DRAFTED and r["payload"]["hypothesis_id"] == hid for r in _recs(path)):
        return hid                                              # the same idea stated again is the same idea
    ledger.append(path, HYPOTHESIS_DRAFTED, {"hypothesis_id": hid, "question": question, "claim": claim,
                                             "research_family_id": family_id, "note": note}, now=now)
    return hid


def draft_spec(path, hypothesis_id, spec, now=None):
    """Records a candidate spec (every VARIANT tried is kept: the search process is itself evidence).
    An invalid spec is recorded as a draft with its problems and can never be frozen."""
    if not any(r["event_type"] == HYPOTHESIS_DRAFTED and r["payload"]["hypothesis_id"] == hypothesis_id for r in _recs(path)):
        raise LedgerError(f"unknown hypothesis {hypothesis_id}")
    problems = validate_spec(spec)
    spec_id = content_id("X-", dict(spec, hypothesis_id=hypothesis_id)) if not _unhashable(spec) else None
    if spec_id is None:
        raise LedgerError("spec is not plain JSON")
    ledger.append(path, SPEC_DRAFTED, {"spec_id": spec_id, "hypothesis_id": hypothesis_id, "spec": spec,
                                       "problems": problems, "valid": not problems,
                                       "research_family_id": spec.get("research_family_id")}, now=now)
    return spec_id, problems


def _unhashable(spec):
    try:
        canonical(spec)
        return False
    except LedgerError:
        return True


def freeze(path, spec_id, now=None):
    """Freezing is a ledger event, not a flag on a mutable object. Refuses an unknown, invalid, or already
    frozen spec. The frozen content is whatever was drafted under this spec_id — which is its own hash."""
    recs = _recs(path)
    drafts = [r for r in recs if r["event_type"] == SPEC_DRAFTED and r["payload"]["spec_id"] == spec_id]
    if not drafts:
        raise LedgerError(f"unknown spec {spec_id}")
    d = drafts[-1]["payload"]
    if not d["valid"]:
        raise LedgerError(f"spec {spec_id} is invalid and cannot be frozen: {d['problems']}")
    if any(r["event_type"] == SPEC_FROZEN and r["payload"]["spec_id"] == spec_id for r in recs):
        raise LedgerError(f"spec {spec_id} is already frozen")
    ledger.append(path, SPEC_FROZEN, {"spec_id": spec_id, "hypothesis_id": d["hypothesis_id"],
                                      "population_id": d["spec"]["population"]["population_id"],
                                      "data_cutoff": d["spec"]["data_cutoff"], "data_split": d["spec"]["data_split"],
                                      "research_family_id": d["spec"]["research_family_id"]}, now=now)
    return spec_id


def is_frozen(path, spec_id):
    return any(r["event_type"] == SPEC_FROZEN and r["payload"]["spec_id"] == spec_id for r in _recs(path))


def access_split(path, spec_id, role, now=None):
    """Record that a data split was READ for an experiment. Returns {"first_access": bool, "peek": bool}.
    development: any time. validation: only once the spec is frozen. final_test: only once frozen, and a
    second read is a PEEK, recorded and carried onto the result."""
    if role not in SPLITS:
        raise LedgerError(f"role must be one of {SPLITS}")
    recs = _recs(path)
    if role != "development" and not is_frozen(path, spec_id):
        raise LedgerError(f"{role} data may only be read after the experiment is frozen")
    prior = [r for r in recs if r["event_type"] == SPLIT_ACCESSED and r["payload"]["spec_id"] == spec_id and r["payload"]["role"] == role]
    first = not prior
    peek = (role == "final_test" and not first)
    ledger.append(path, SPLIT_ACCESSED, {"spec_id": spec_id, "role": role, "first_access": first, "peek": peek}, now=now)
    return {"first_access": first, "peek": peek}


def record_result(path, spec_id, population_id, status, effective_n, summary=None, limitations=None, now=None):
    """A result must cite the FROZEN spec and its exact population, and state how many INDEPENDENT episodes
    it rests on. A population mismatch is not an error to hide: it is recorded as INVALID_EXPERIMENT."""
    if status not in STATUSES:
        raise LedgerError(f"status must be one of {STATUSES}")
    recs = _recs(path)
    fr = [r for r in recs if r["event_type"] == SPEC_FROZEN and r["payload"]["spec_id"] == spec_id]
    if not fr:
        raise LedgerError("a result can only be recorded against a frozen experiment")
    if isinstance(effective_n, bool) or not isinstance(effective_n, int) or effective_n < 0:
        raise LedgerError("effective_n (independent episodes) must be a non-negative integer")
    note = None
    if population_id != fr[0]["payload"]["population_id"]:
        status, note = "INVALID_EXPERIMENT", "result cites a population other than the frozen one; comparison invalid"
    peeked = any(r["event_type"] == SPLIT_ACCESSED and r["payload"]["spec_id"] == spec_id and r["payload"]["peek"] for r in recs)
    ledger.append(path, RESULT_RECORDED, {"spec_id": spec_id, "population_id": population_id, "status": status,
                                          "effective_n": effective_n, "summary": summary or {},
                                          "limitations": limitations or [], "invalidated_because": note,
                                          "final_test_peeked": peeked,
                                          "research_family_id": fr[0]["payload"]["research_family_id"]}, now=now)
    return status


def family_summary(path, family_id, as_of=None):
    """'This finding came from a family of N related searches.' Counts every variant ever drafted, not just
    the ones that were frozen or worked."""
    recs = _recs(path)
    if as_of is not None:
        recs = ledger.as_of(recs, as_of)
    drafted = {r["payload"]["spec_id"] for r in recs if r["event_type"] == SPEC_DRAFTED and r["payload"]["research_family_id"] == family_id}
    frozen = {r["payload"]["spec_id"] for r in recs if r["event_type"] == SPEC_FROZEN and r["payload"]["research_family_id"] == family_id}
    results = {}
    for r in recs:
        if r["event_type"] == RESULT_RECORDED and r["payload"]["research_family_id"] == family_id:
            results[r["payload"]["spec_id"]] = r["payload"]["status"]            # latest wins
    by_status = {}
    for s in results.values():
        by_status[s] = by_status.get(s, 0) + 1
    return {"family": family_id, "specs_drafted": len(drafted), "specs_frozen": len(frozen),
            "results": len(results), "by_status": dict(sorted(by_status.items())),
            "hypotheses": len({r["payload"]["hypothesis_id"] for r in recs if r["event_type"] == HYPOTHESIS_DRAFTED
                               and r["payload"]["research_family_id"] == family_id})}


def cases(path, as_of=None):
    """Every hypothesis with its experiments and latest result status — the Catalog's research index."""
    recs = _recs(path)
    if as_of is not None:
        recs = ledger.as_of(recs, as_of)
    hyp = {r["payload"]["hypothesis_id"]: dict(r["payload"], experiments=[]) for r in recs if r["event_type"] == HYPOTHESIS_DRAFTED}
    frozen = {r["payload"]["spec_id"] for r in recs if r["event_type"] == SPEC_FROZEN}
    status = {}
    for r in recs:
        if r["event_type"] == RESULT_RECORDED:
            status[r["payload"]["spec_id"]] = r["payload"]["status"]
    for r in recs:
        if r["event_type"] == SPEC_DRAFTED and r["payload"]["hypothesis_id"] in hyp:
            hyp[r["payload"]["hypothesis_id"]]["experiments"].append(
                {"spec_id": r["payload"]["spec_id"], "valid": r["payload"]["valid"], "frozen": r["payload"]["spec_id"] in frozen,
                 "status": status.get(r["payload"]["spec_id"], "NO_RESULT")})
    return list(hyp.values())
