"""
test_mil_mutations.py
======================
Regression tests for the four mutation-isolation fixes made 2026-09-13
(SYSTEM INTEGRITY AUDIT, Pass 2): maintain_thesis(), evaluate_scenarios(),
generate_expectations(), track_counter_candidate() — all in mil.py.

Each test proves the THREE-PART isolation guardrail established during
the audit, not just object identity:

  1. OUTER IDENTITY   — before is not after
  2. INPUT PRESERVATION — before == snapshot taken prior to the call
  3. NESTED ISOLATION — for every mutable nested field the function
                         touches, before.<field> is not after.<field>

Proving (1) alone is exactly what would have let this bug class slip
through before — you can "fix" the outer dataclass while still sharing
a nested dict or list by reference. See reconcile()'s own 2026-09-12
fix comment in mil.py for the original incident this guards against.

Framework-free by design, same house style as regression_sweep.py
(check() + FAILURES + SystemExit(1)) — intended to become part of a
future test_mil.py in a subsystem-organized suite, not a permanent
standalone script.
"""

import copy
import mil

FAILURES = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"{status}: {label}")
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

def make_understanding(**overrides):
    """A minimal, valid MarketUnderstanding to mutate/replace() against.
    Defaults are deliberately boring — SUPPORTED, no counter case — so
    each test only has to override what it actually needs."""
    base = dict(
        thesis_status=mil.ThesisStatus.SUPPORTED,
        primary_thesis={"direction": "BULLISH"},
        supporting_evidence=["orig evidence line"],
        conflicting_evidence=["orig weakness line"],
        failure_condition=mil.FailureCondition(1.30, "below", 1.28),
        expectations=[{"type": "expected_next_event", "text": "orig"}],
        confidence="Research only",
        counter_thesis=None,
        unresolved_questions=[],
        history=[],
        last_reconciled_at="2026-09-13T00:00:00+00:00",
        worldstate_version=1,
        emerging_counter_case=None,
        counter_candidate_history=[],
    )
    base.update(overrides)
    return mil.MarketUnderstanding(**base)


def make_observed(**overrides):
    base = dict(
        macro_bias_confirmed="BULLISH",
        macro_bias_stale=False,
        macro_leg_origin=1.25,
        macro_leg_extreme=1.32,
        macro_leg_direction="BULLISH",
        macro_leg_was_choch=False,
    )
    base.update(overrides)
    return mil.ObservedLegState(**base)


# ---------------------------------------------------------------------
# maintain_thesis() — outer identity + input preservation
# (no nested mutable field is touched by this function itself)
# ---------------------------------------------------------------------

def test_maintain_thesis():
    before = make_understanding()
    before_snapshot = copy.deepcopy(before)

    after = mil.maintain_thesis(before, ["new evidence"], ["new weakness"])

    check("maintain_thesis: outer identity isolation", before is not after)
    check("maintain_thesis: input preserved", before == before_snapshot)
    check("maintain_thesis: output actually updated",
          after.supporting_evidence == ["new evidence"]
          and after.conflicting_evidence == ["new weakness"])


# ---------------------------------------------------------------------
# evaluate_scenarios() — outer identity + input preservation +
# NESTED isolation on counter_thesis (the dict this function edits)
# ---------------------------------------------------------------------

def test_evaluate_scenarios_mutating_path():
    before = make_understanding(
        counter_thesis={"direction": "BEARISH", "status": "awaiting_confirmation"}
    )
    before_snapshot = copy.deepcopy(before)
    watching_for = [{"role": "LOCATION", "zone_low": 1.26, "zone_high": 1.27}]

    after = mil.evaluate_scenarios(before, watching_for)

    check("evaluate_scenarios: outer identity isolation", before is not after)
    check("evaluate_scenarios: input preserved", before == before_snapshot)
    check("evaluate_scenarios: nested counter_thesis isolation",
          before.counter_thesis is not after.counter_thesis)
    check("evaluate_scenarios: output actually updated",
          after.counter_thesis.get("watching_for") == watching_for)


def test_evaluate_scenarios_noop_path():
    # No counter_thesis at all -> legitimately a no-op. Nothing was
    # mutated, so returning the same object here is correct, not a bug.
    before = make_understanding(counter_thesis=None)
    after = mil.evaluate_scenarios(before, [{"role": "LOCATION"}])
    check("evaluate_scenarios: true no-op returns same object (expected)",
          before is after)


# ---------------------------------------------------------------------
# generate_expectations() — outer identity + input preservation
# ---------------------------------------------------------------------

def test_generate_expectations():
    before = make_understanding()
    before_snapshot = copy.deepcopy(before)

    after = mil.generate_expectations(before, "CHoCH expected", [
        {"role": "CONFIRMATION", "code": "X1"},
        {"role": "LOCATION", "code": "X2"},  # must be filtered out
    ])

    check("generate_expectations: outer identity isolation", before is not after)
    check("generate_expectations: input preserved", before == before_snapshot)
    check("generate_expectations: only CONFIRMATION entries kept",
          after.expectations == [
              {"type": "expected_next_event", "text": "CHoCH expected"},
              {"type": "confirmation", "role": "CONFIRMATION", "code": "X1"},
          ])


# ---------------------------------------------------------------------
# track_counter_candidate() — the sharpest of the four: outer identity +
# input preservation + NESTED isolation on BOTH counter_candidate_history
# (a list, mutated via .append() before the fix) AND emerging_counter_case
# (a dict, edited in place before the fix)
# ---------------------------------------------------------------------

def test_track_counter_candidate_fresh_episode():
    before = make_understanding(primary_thesis={"direction": "BULLISH"})
    before_snapshot = copy.deepcopy(before)
    observed = make_observed(
        macro_candidate_leg_direction="BEARISH",
        macro_candidate_leg_atr_ok=True,
        macro_candidate_leg_vs_prior_ok=False,
    )

    after = mil.track_counter_candidate(before, observed, "2026-09-13T01:00:00+00:00")

    check("track_counter_candidate/fresh: outer identity isolation", before is not after)
    check("track_counter_candidate/fresh: input preserved", before == before_snapshot)
    check("track_counter_candidate/fresh: emerging_counter_case created",
          after.emerging_counter_case is not None
          and after.emerging_counter_case["direction"] == "BEARISH"
          and after.emerging_counter_case["maturity"] == "building")


def test_track_counter_candidate_continuing_episode_nested_isolation():
    """The critical case: an episode already exists (a dict) and this
    scan updates it. Before the fix, `current["maturity"] = ...` mutated
    that exact dict in place. This test proves the OLD dict a caller
    might still be holding is untouched after the call."""
    existing_case = {
        "direction": "BEARISH", "maturity": "early",
        "atr_ok": False, "vs_prior_ok": False,
        "range_pips": 10.0, "required_atr_pips": 30.0, "required_prior_pips": 40.0,
        "first_observed_at": "2026-09-13T00:30:00+00:00", "peak_maturity": "early",
    }
    before = make_understanding(
        primary_thesis={"direction": "BULLISH"},
        emerging_counter_case=existing_case,
    )
    before_snapshot = copy.deepcopy(before)
    observed = make_observed(
        macro_candidate_leg_direction="BEARISH",
        macro_candidate_leg_atr_ok=True,
        macro_candidate_leg_vs_prior_ok=True,   # now qualifies -> maturity ratchets up
    )

    after = mil.track_counter_candidate(before, observed, "2026-09-13T01:00:00+00:00")

    check("track_counter_candidate/continuing: outer identity isolation", before is not after)
    check("track_counter_candidate/continuing: input preserved", before == before_snapshot)
    check("track_counter_candidate/continuing: NESTED dict isolation "
          "(the old dict a caller might still hold is untouched)",
          before.emerging_counter_case is not after.emerging_counter_case
          and before.emerging_counter_case["maturity"] == "early"
          and before.emerging_counter_case["peak_maturity"] == "early")
    check("track_counter_candidate/continuing: new dict actually updated",
          after.emerging_counter_case["maturity"] == "qualified_awaiting_promotion"
          and after.emerging_counter_case["peak_maturity"] == "qualified_awaiting_promotion")


def test_track_counter_candidate_fizzle_history_isolation():
    """Proves counter_candidate_history is a NEW list, not the same list
    object .append()'d in place — a caller holding the pre-call list
    reference must still see it at its original length."""
    existing_case = {
        "direction": "BEARISH", "maturity": "building",
        "atr_ok": True, "vs_prior_ok": False,
        "range_pips": 20.0, "required_atr_pips": 30.0, "required_prior_pips": 40.0,
        "first_observed_at": "2026-09-13T00:30:00+00:00", "peak_maturity": "building",
    }
    old_history_ref = []
    before = make_understanding(
        primary_thesis={"direction": "BULLISH"},
        emerging_counter_case=existing_case,
        counter_candidate_history=old_history_ref,
    )
    before_snapshot = copy.deepcopy(before)
    # No candidate this scan (agrees with primary / is None) -> fizzles.
    observed = make_observed(macro_candidate_leg_direction=None, leg15_direction=None)

    after = mil.track_counter_candidate(before, observed, "2026-09-13T02:00:00+00:00")

    check("track_counter_candidate/fizzle: outer identity isolation", before is not after)
    check("track_counter_candidate/fizzle: input preserved", before == before_snapshot)
    check("track_counter_candidate/fizzle: history LIST isolation "
          "(pre-call list reference untouched)",
          old_history_ref == [] and after.counter_candidate_history is not old_history_ref)
    check("track_counter_candidate/fizzle: episode resolved and cleared",
          after.emerging_counter_case is None
          and len(after.counter_candidate_history) == 1
          and after.counter_candidate_history[0].resolution == "fizzled")


def test_track_counter_candidate_history_cap():
    """COUNTER_CANDIDATE_HISTORY_MAX_LEN truncation still returns a new
    list, not a mutated-in-place slice reassignment."""
    full_history = [
        mil.CounterCandidateRecord("BEARISH", "t0", "t1", "fizzled", "early")
        for _ in range(mil.COUNTER_CANDIDATE_HISTORY_MAX_LEN)
    ]
    old_history_ref = full_history
    existing_case = {
        "direction": "BEARISH", "maturity": "early",
        "atr_ok": False, "vs_prior_ok": False,
        "range_pips": 5.0, "required_atr_pips": 30.0, "required_prior_pips": 40.0,
        "first_observed_at": "2026-09-13T00:00:00+00:00", "peak_maturity": "early",
    }
    before = make_understanding(
        primary_thesis={"direction": "BULLISH"},
        emerging_counter_case=existing_case,
        counter_candidate_history=list(old_history_ref),
    )
    before_ref = before.counter_candidate_history
    observed = make_observed(macro_candidate_leg_direction=None, leg15_direction=None)

    after = mil.track_counter_candidate(before, observed, "2026-09-13T03:00:00+00:00")

    check("track_counter_candidate/cap: history capped at max length",
          len(after.counter_candidate_history) == mil.COUNTER_CANDIDATE_HISTORY_MAX_LEN)
    check("track_counter_candidate/cap: pre-call list untouched by truncation",
          len(before_ref) == mil.COUNTER_CANDIDATE_HISTORY_MAX_LEN
          and before.counter_candidate_history is not after.counter_candidate_history)


if __name__ == "__main__":
    test_maintain_thesis()
    test_evaluate_scenarios_mutating_path()
    test_evaluate_scenarios_noop_path()
    test_generate_expectations()
    test_track_counter_candidate_fresh_episode()
    test_track_counter_candidate_continuing_episode_nested_isolation()
    test_track_counter_candidate_fizzle_history_isolation()
    test_track_counter_candidate_history_cap()

    print()
    print("TOTAL FAILURES:", len(FAILURES))
    if FAILURES:
        raise SystemExit(1)
