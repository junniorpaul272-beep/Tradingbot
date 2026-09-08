"""
mil.py — Market Intelligence Layer (MIL)

Purpose: maintain the system's ONE coherent, persistent interpretation
of the market. This is the cognition/integration organ — per chat,
the "nervous system" connecting the other organs, not a replacement
for any of them.

MIL does NOT:
    - fetch market data
    - calculate raw market facts (candles, fractals, OB/FVG, ATR)
    - recompute macro_bias / market_phase — those are LIVE's authoritative
      output (scanner_observation.py, called from scanner_live.py). MIL
      reads them, never re-derives them. (This is the exact bug already
      found twice in the current codebase — MIN independently re-running
      compute_macro_bias() instead of reading LIVE's committed value —
      and MIL must not repeat it.)
    - run experiments or maintain experiment capital (MIN's job)
    - generate trading signals or size positions (LIVE/SIGNAL's job)

MIL DOES:
    - reconcile new observations against what it previously believed
    - maintain ONE persistent MarketUnderstanding object across scans
      (not a fresh synthesis every scan from nothing)
    - track thesis_status through a fixed, enumerated transition table
    - require an explicit, written reconciliation whenever direction
      changes WITHOUT the stated failure_condition having fired —
      this is the specific gap that motivated MIL: a bearish thesis
      silently became a bullish one without its own stated invalidation
      level ever being touched, and nothing in the system could explain
      why. See /areas/mil-architecture and /areas/gbpusd-smc-scanner.

Authority hierarchy (per chat — the rule that keeps MIL from becoming
a god-organ):
    FACT AUTHORITY          -> LIVE / underlying factual organs
    INTERPRETATION AUTHORITY -> existing interpretation machinery
                                (MarketThesis, MarketIntent, MarketPhase)
    INTEGRATION AUTHORITY    -> MIL (this file)
    EMPIRICAL AUTHORITY      -> MIN
MIL may say "the current LIVE bias is bullish, but my persistent
understanding is contested because the prior bearish thesis was never
mechanically invalidated." It may NOT say "LIVE is wrong, therefore
I'm overriding the market bias."

Status: ingest() and reconcile() are now real, wired against the exact
state fields compute_macro_bias()/check_leg_anchor_survival() produce
(see scanner_observation.py). maintain_thesis() / evaluate_scenarios()
/ generate_expectations() remain stubs — assembly work over existing
MarketThesis/MarketIntent fields, not decision logic, so lower priority
than the state machine getting reconcile() right.

---------------------------------------------------------------------
THE ORGANISM MODEL (per chat) — read this before adding any new organ,
not just MIL:

    REALITY
       |
       v
      LIVE            <- perception: turns candles into observations
       |                 (structure, phase, bias, setups)
       v
   WORLDSTATE          <- shared circulation, NOT an intelligence organ.
       |                  "This is what the organism currently knows,"
       |                  never "this is what it means." Bloodstream,
       |                  not the heart — it doesn't decide anything,
   ----+----            it just makes observations available in common.
   |   |   |
   v   v   v
  MIL  MIN OTHER       <- organs interpret/specialize/measure/remember,
   |    |               each reading the SAME circulating reality
   |    |               instead of privately recomputing their own copy
   +----+
       v
  shared organism

THE ONE RULE LOCKED IN AT THIS LEVEL (deliberately above any single
module's design — applies to every organ, present and future):

    No organ may maintain a private version of reality that the
    organism already has authoritative shared state for.

This is the same disease at every scale:
    - MIN independently re-running compute_macro_bias() instead of
      reading LIVE's committed value (found, being fixed).
    - A MarketUnderstanding that only mil.py's direct importers can
      see, instead of re-entering WorldState, is the SAME violation
      one layer up — a private channel instead of a private
      computation. Whatever persists reconcile()'s output must flow
      back through WorldState like every other organism-level fact,
      not live in a side file only MIL-aware code knows to check.

INSERTION POINT (traced against the real scanner_live.py _scan_once(),
not guessed): NOT one call site — two, because the file itself splits
this data into an early and a late half.
    1. reconcile() itself -> call right after compute_macro_bias()/
       bias_stale become authoritative (~line 2557 in the traced file),
       UNCONDITIONALLY, every scan -- same discipline compute_macro_bias
       itself already follows. This alone gets thesis_status/direction/
       ThesisTransition right even on scans that hit the CONSOLIDATION
       early-return (~2655) or the no-swing-points early-return (~2669)
       -- both exit BEFORE facts/phase_result/thesis/intent ever get
       built, so a single late insertion point would silently skip MIL
       on both, which is exactly the amnesia this file exists to fix
       (a thesis collapsing into "no edge" IS a real transition).
    2. maintain_thesis()/evaluate_scenarios() -> call later (~line 2904
       in the traced file, after thesis+intent are built and
       persisted), ONLY on scans that reach it, to ENRICH the same
       MarketUnderstanding object #1 already produced/updated this
       scan with evidence/counter_thesis -- not a second reconciliation.
Both calls stay strictly before evaluate_rule_of_law() (~line 2960) --
MIL sees what LIVE decided FROM, never what LIVE did about it.

Either call site: MIL never opens state.json/WorldState and writes
into it itself -- that's the "database administrator" pattern we're
explicitly avoiding (see persist_reasoning_state()'s docstring). LIVE
calls reconcile()/maintain_thesis(), gets a payload back, and commits
it via apply_state_updates()+save_state() -- the exact same commit
pattern every other block in _scan_once() already uses. build_world_state()
then picks it up next build, same as every other fact.

THREE STREAMS OF CIRCULATING INFORMATION (per chat — vocabulary for
WHY future WorldState namespacing should exist, not a schema to build
yet):
    OBSERVATION  — "what happened / what exists?"        -> LIVE
    INTELLIGENCE — "what do we currently believe?"        -> MIL
    KNOWLEDGE    — "what has history taught us?"          -> MIN
Not everything an organ produces should become WorldState-visible —
only the meaningful output of each stream, not every intermediate
calculation, or WorldState becomes contaminated with scratch work.

STATE vs EVENT (per chat — a distinction worth carrying into
detect_material_change(), not yet built as its own subsystem):
    STATE  answers "where are we?"     e.g. thesis_status = CONTESTED
    EVENT  answers "what just happened?" e.g. thesis_status changed
           WEAKENING -> CONTESTED
ThesisTransition already IS this distinction for thesis_status
specifically. A general changes_since_last_cycle concept at the
WorldState level (phase_changed, counter_case_strengthened, etc.) is
the natural extension, and is likely the actual foundation for a
future Market Event system — but per chat, don't build that generic
version until it's needed, ThesisTransition covers what reconcile()
needs today.

PROVENANCE (per chat, implemented narrowly so far — see
worldstate_version fields on MarketUnderstanding/ResearchEvidence):
MIN runs as its own async process on its own cron offset, so its
research can genuinely be generated against an older WorldState than
whatever MIL is currently reconciling. Every meaningful cross-organ
output should eventually be able to answer who produced it, when, from
which WorldState version, and whether it's observation/intelligence/
knowledge — but per chat, that's only worth baking in fully once
provenance actually matters somewhere, not speculatively on every
field now.

NOT YET DECIDED, deliberately: whether/how MIN's research (Forward
Observation, TIB -> ResearchEvidence) eventually flows back INTO
WorldState too, and where Brain/HFIS sit relative to MIL (MIL -> Brain,
or WorldState -> Brain with MIL as a sibling reader, or something else
entirely). Per chat: don't force these decisions before the organism
is understood as a whole — but keep any new contract (like
ResearchEvidence) general enough that it isn't secretly shaped only
for LIVE's path and has to be redone once MIN's feedback loop is
actually designed.
---------------------------------------------------------------------
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Thesis status — closed vocabulary, same discipline as Phase/TransitionCause
# in scanner_observation.py (fixed taxonomy, not free text, so transitions
# can be validated and later counted/grouped).
# ---------------------------------------------------------------------------

class ThesisStatus(str, Enum):
    UNKNOWN      = "UNKNOWN"       # bootstrap only — no prior thesis to compare against
    SUPPORTED    = "SUPPORTED"     # primary thesis holding, no material weakness
    WEAKENING    = "WEAKENING"     # failure_risk elevated or a weakness gained, direction unchanged
    CONTESTED    = "CONTESTED"     # a real counter-thesis has formed with its own evidence
    TRANSITIONING = "TRANSITIONING"  # counter-thesis has cleared its own promotion bar
    INVALIDATED  = "INVALIDATED"   # the STATED failure_condition actually fired


# Fixed transition table: (from_state) -> set of legal to_states.
# This does NOT decide when a transition happens (that's reconcile()'s
# job, reading real thesis/phase fields) — it only rejects illegal jumps,
# e.g. SUPPORTED -> TRANSITIONING with no CONTESTED step (except the one
# explicit exception below).
LEGAL_TRANSITIONS = {
    ThesisStatus.UNKNOWN:       {ThesisStatus.SUPPORTED},
    ThesisStatus.SUPPORTED:     {ThesisStatus.WEAKENING, ThesisStatus.INVALIDATED},
    ThesisStatus.WEAKENING:     {ThesisStatus.SUPPORTED, ThesisStatus.CONTESTED,
                                  ThesisStatus.INVALIDATED},
    ThesisStatus.CONTESTED:     {ThesisStatus.WEAKENING, ThesisStatus.TRANSITIONING,
                                  ThesisStatus.INVALIDATED},
    ThesisStatus.TRANSITIONING: {ThesisStatus.INVALIDATED, ThesisStatus.SUPPORTED},
    ThesisStatus.INVALIDATED:   {ThesisStatus.UNKNOWN, ThesisStatus.SUPPORTED},
}

# The one hard rule this whole file exists to enforce: you may only reach
# INVALIDATED because failure_condition actually fired. Every other legal
# transition may proceed without it, but MUST carry a reconciliation.
REQUIRES_INVALIDATION_FIRED = {ThesisStatus.INVALIDATED}

# Transitions that are legal WITHOUT invalidation firing, but where a
# missing/insufficient reconciliation must route to unresolved_questions
# instead of silently completing the transition.
REQUIRES_RECONCILIATION_IF_NOT_INVALIDATED = {
    ThesisStatus.TRANSITIONING,  # -> SUPPORTED (new direction) without invalidation
}


@dataclass
class ThesisTransition:
    """One record per thesis_status change. Append-only — never edited
    or overwritten once written. This IS the nervous-system memory:
    the audit trail answering "what did I believe, and why did that
    change" that the current architecture cannot produce today."""
    from_state: ThesisStatus
    to_state: ThesisStatus
    at: str                              # scan timestamp (ISO 8601)
    transition_cause: str                # reuses TransitionCause enum values
                                          # from scanner_observation.py as-is
                                          # (FRESH_BOS/CHOCH/SWEEP_RECLAIM/
                                          # BIAS_FLIP/EMA_EXHAUSTION/
                                          # FAILED_CONTINUATION/UNKNOWN)
    invalidation_fired: bool             # did the STATED failure_condition
                                          # actually trigger this scan?
    invalidation_mechanism: Optional[str] = None   # "origin_violated" / "retrace_violated" / None
    superseding_evidence: list = field(default_factory=list)
    reconciliation: Optional[str] = None  # mandatory (see validate_transition)
                                           # unless to_state is a same-thesis
                                           # status change (WEAKENING/CONTESTED)


@dataclass
class CounterCandidateRecord:
    """
    One RESOLVED episode of an opposing candidate leg building, then
    either fizzling or being promoted. Append-only — written exactly
    once, when the episode's fate is decided, never edited after (same
    discipline as ThesisTransition above). This is the answer to "was
    there a counter-case building yesterday" that emerging_counter_case
    alone can't give once that episode is over and cleared.

    Deliberately separate from ThesisTransition/history: a fizzled
    counter-candidate never touched thesis_status at all (it never
    qualified), so it has no business in the primary-thesis transition
    log. Two different questions -> two different records. (Per chat,
    JUINIOR — "the counter case is more in depth", 2026-0X-XX.)
    """
    direction: str
    first_observed_at: str
    resolved_at: str
    resolution: str        # "fizzled" | "promoted" — closed vocabulary,
                            # same as every other status field in this file
    peak_maturity: str     # highest maturity this candidate ever reached
                            # before resolving — "early" | "building" |
                            # "qualified_awaiting_promotion". Ratchets
                            # forward only (see track_counter_candidate) so
                            # a candidate that briefly qualified then chopped
                            # back down still shows what it actually reached.


@dataclass
class FailureCondition:
    """Structured replacement for MarketThesis.invalidation's prose string.
    Both real mechanisms explicit — today's thesis text only ever
    describes origin_direction, never the retrace path, which is the
    root cause of the GBPUSD case."""
    origin_level: float
    origin_direction: str        # "above" / "below" — clean close through this level
    retrace_level: float         # computed from INVALIDATION_RETRACE (0.786) x leg range,
                                  # same inputs check_leg_anchor_survival already has


@dataclass
class MarketUnderstanding:
    """The one persistent, canonical belief object. Built once, then
    UPDATED scan over scan via reconcile() — never rebuilt from scratch,
    which is the core behavioral difference from how MarketThesis/
    MarketIntent work today (fresh synthesis every call)."""
    thesis_status: ThesisStatus
    primary_thesis: dict                 # {direction, state(Phase), narrative,
                                          #  expected_next_event, confidence}
                                          # — lifted from MarketThesis fields
    supporting_evidence: list            # = MarketThesis.evidence
    conflicting_evidence: list           # = MarketThesis.weaknesses
    failure_condition: FailureCondition
    expectations: list                   # expected_next_event + CONFIRMATION-role
                                          # watching_for entries (MarketIntent)
    confidence: str                      # fixed label, never a synthesized number —
                                          # same discipline as MarketThesis.confidence
    counter_thesis: Optional[dict] = None   # built from MarketIntent's LOCATION +
                                             # CONFIRMATION coded entries when present
    unresolved_questions: list = field(default_factory=list)
    history: list = field(default_factory=list)   # list[ThesisTransition]
    last_reconciled_at: Optional[str] = None
    worldstate_version: Optional[int] = None   # stamped by LIVE at commit time —
                                                # which WorldState cycle produced this belief
    # ADDED (per chat, JUINIOR — "the counter case is more in depth",
    # 2026-0X-XX): see track_counter_candidate() below and
    # CounterCandidateRecord's own docstring for the full reasoning.
    # Deliberately separate from counter_thesis (reconcile()'s field,
    # populated only once promotion_confirmed) — this tracks the SAME
    # candidate earlier and continuously, but never substitutes for or
    # lowers reconcile()'s own promotion bar.
    emerging_counter_case: Optional[dict] = None
    counter_candidate_history: list = field(default_factory=list)   # list[CounterCandidateRecord]


class TransitionError(ValueError):
    """Raised when reconcile() attempts an illegal or under-evidenced
    status change. Meant to be loud — a MIL that can silently make an
    illegal transition is exactly the failure mode this file exists to
    prevent."""


def validate_transition(transition: ThesisTransition) -> None:
    """
    PURE. Enforces the two hard rules from the transition table:

      1. The (from_state -> to_state) edge must be one of the
         enumerated legal transitions.
      2. INVALIDATED may only be reached with invalidation_fired=True.
      3. TRANSITIONING -> SUPPORTED without invalidation_fired must
         carry a non-empty reconciliation naming what actually
         justified the change (superseding_evidence non-empty AND
         reconciliation is not None/blank). If neither condition is
         satisfiable, the caller must NOT call this with to_state=
         SUPPORTED at all — it should instead leave thesis_status at
         TRANSITIONING and append to unresolved_questions.

    Raises TransitionError on any violation. Does not mutate anything —
    callers apply the transition themselves only after this passes.
    """
    legal_targets = LEGAL_TRANSITIONS.get(transition.from_state, set())
    if transition.to_state not in legal_targets:
        raise TransitionError(
            f"Illegal transition: {transition.from_state} -> {transition.to_state}"
        )

    if transition.to_state in REQUIRES_INVALIDATION_FIRED:
        if not transition.invalidation_fired:
            raise TransitionError(
                f"{transition.to_state} requires invalidation_fired=True "
                f"(mechanism={transition.invalidation_mechanism!r}); got False."
            )

    if (transition.from_state in REQUIRES_RECONCILIATION_IF_NOT_INVALIDATED
            and transition.to_state == ThesisStatus.SUPPORTED
            and not transition.invalidation_fired):
        if not transition.superseding_evidence or not transition.reconciliation:
            raise TransitionError(
                "TRANSITIONING -> SUPPORTED without invalidation firing requires "
                "non-empty superseding_evidence AND a written reconciliation. "
                "If neither is available, do not complete this transition — "
                "leave status at TRANSITIONING and record an unresolved_question "
                "instead of forcing a confident-sounding narrative over an "
                "ambiguous case."
            )


@dataclass
class ResearchEvidence:
    """The MIN -> MIL contract (per chat). MIL must never read MIN's raw
    dashboard output directly — it asks a bounded question and gets a
    bounded answer shaped like this. Built from MIN's existing outputs
    (format_scenario_summary()'s empirical fate splits, the Validation
    Engine's calibration checks, TIB's effect-size findings) — none of
    which need to change to produce this; this is a projection, not a
    new computation.

    provenance: MIN runs as its own async process on its own cron
    offset (confirmed real infra), so its evidence can genuinely be
    generated against an OLDER WorldState than the one MIL is currently
    reconciling. generated_from_worldstate_version lets MIL detect that
    and discount/flag stale research rather than silently treating it
    as current — per chat, this is cheap to add now and expensive to
    retrofit once evidence is actually flowing."""
    source: str              # e.g. "forward_observation", "EXP2_FIB", "TIB"
    claim: str                # human-readable, e.g. "bearish + expansion favors continuation"
    condition: dict           # the (phase, transition_cause, ...) bucket this claim is scoped to
    sample_size: int
    generated_from_worldstate_version: Optional[int] = None
    expectancy: Optional[float] = None
    reliability: Optional[str] = None    # e.g. Validation Engine's calibration verdict
    relevant_tags: list = field(default_factory=list)
    generated_at: Optional[str] = None


@dataclass
class ObservedLegState:
    """
    ingest()'s output contract — exactly what MIL is allowed to receive
    about the macro leg, named after the REAL state.json keys
    compute_macro_bias()/check_leg_anchor_survival() write today. This
    is the "define the contract before the implementation" step: MIL
    reads these fields, never re-derives them from candles.
    """
    macro_bias_confirmed: str          # "BULLISH" / "BEARISH" / "CONSOLIDATION"
    macro_bias_stale: bool             # already computed today, currently unused downstream
    macro_leg_origin: Optional[float]
    macro_leg_extreme: Optional[float]
    macro_leg_direction: Optional[str]
    macro_leg_was_choch: Optional[bool]
    # Set only on the scan a leg actually died (origin/retrace violated).
    # Requires the scanner_observation.py patch persisting these instead
    # of discarding them (previously computed and thrown away).
    last_invalidation_origin_violated: Optional[bool] = None
    last_invalidation_retrace_violated: Optional[bool] = None
    # Unqualified reversal candidate — present even while the current
    # leg is still alive (compute_macro_bias's "candidate did not
    # qualify to supersede" path). Earliest possible counter-evidence
    # signal, weaker than a stale+15M-promotion candidate below.
    macro_candidate_leg_direction: Optional[str] = None
    macro_candidate_leg_atr_ok: Optional[bool] = None
    macro_candidate_leg_vs_prior_ok: Optional[bool] = None
    # CORRECTION (per audit — these were already computed and persisted
    # by compute_macro_bias(); the gap was only that MIL never read
    # them, not that they were missing). Lets emerging_counter_case say
    # "1.8 of 3.0 ATR" instead of just atr_ok=False — same numbers the
    # [BIAS] candidate log line already prints, just not previously
    # flowing into MIL's contract.
    macro_candidate_leg_range_pips: Optional[float] = None
    macro_candidate_leg_required_atr_pips: Optional[float] = None
    macro_candidate_leg_required_prior_pips: Optional[float] = None
    # 15M promotion candidate — only meaningful while macro_bias_stale.
    leg15_direction: Optional[str] = None
    leg15_break_count: Optional[int] = None
    promotion_confirmed: Optional[bool] = None   # _promotion_confirmed()'s result, if it ran this scan


# ---------------------------------------------------------------------------
# Core interface (per friend's proposed shape).
# ---------------------------------------------------------------------------

def ingest(world_state: dict) -> ObservedLegState:
    """Pull this scan's relevant facts out of WorldState/state into the
    bounded ObservedLegState contract above. Read-only — MIL never
    recomputes anything WorldState already contains. A missing key
    reads as None, never as a guessed default."""
    g = world_state.get
    return ObservedLegState(
        macro_bias_confirmed=g("macro_bias_confirmed"),
        macro_bias_stale=bool(g("macro_bias_stale", False)),
        macro_leg_origin=g("macro_leg_origin"),
        macro_leg_extreme=g("macro_leg_extreme"),
        macro_leg_direction=g("macro_leg_direction"),
        macro_leg_was_choch=g("macro_leg_was_choch"),
        last_invalidation_origin_violated=g("macro_leg_last_invalidation_origin_violated"),
        last_invalidation_retrace_violated=g("macro_leg_last_invalidation_retrace_violated"),
        macro_candidate_leg_direction=g("macro_candidate_leg_direction"),
        macro_candidate_leg_atr_ok=g("macro_candidate_leg_atr_ok"),
        macro_candidate_leg_vs_prior_ok=g("macro_candidate_leg_vs_prior_ok"),
        macro_candidate_leg_range_pips=g("macro_candidate_leg_range_pips"),
        macro_candidate_leg_required_atr_pips=g("macro_candidate_leg_required_atr_pips"),
        macro_candidate_leg_required_prior_pips=g("macro_candidate_leg_required_prior_pips"),
        leg15_direction=g("leg15_direction"),
        leg15_break_count=g("leg15_break_count"),
        # Requires the second scanner_observation.py patch — promo_ok was
        # also being computed inline and discarded, same pattern as the
        # invalidation mechanism.
        promotion_confirmed=g("macro_leg_promotion_confirmed"),
    )


def reconcile(prev: Optional[MarketUnderstanding], observed: ObservedLegState,
              now_iso: str) -> MarketUnderstanding:
    """
    The core function, implementing the decision tree against REAL
    mechanical paths traced out of compute_macro_bias():

        no prior understanding           -> UNKNOWN -> SUPPORTED (bootstrap)
        direction unchanged, not stale    -> stays SUPPORTED (or recovers
                                              from WEAKENING/CONTESTED)
        direction unchanged, now stale,
          no candidate/leg15 opposite     -> SUPPORTED -> WEAKENING
        direction unchanged, stale,
          candidate/leg15 opposite exists
          but not yet promotion-confirmed -> WEAKENING -> CONTESTED
        direction unchanged, invalidation
          fields set this scan            -> * -> INVALIDATED (hard gate,
                                              only path in)
        direction CHANGED this scan       -> must reconcile: was
                                              invalidation_fired? If yes,
                                              -> INVALIDATED then a fresh
                                              UNKNOWN->SUPPORTED next scan.
                                              If no, -> TRANSITIONING ->
                                              SUPPORTED(new direction),
                                              MANDATORY reconciliation
                                              naming promotion_confirmed
                                              as superseding_evidence. If
                                              promotion_confirmed is also
                                              not True, do NOT force the
                                              transition — leave at
                                              TRANSITIONING and record an
                                              unresolved_question instead.
    """
    invalidation_fired = bool(
        observed.last_invalidation_origin_violated or observed.last_invalidation_retrace_violated
    )
    invalidation_mechanism = (
        "origin_violated" if observed.last_invalidation_origin_violated else
        "retrace_violated" if observed.last_invalidation_retrace_violated else
        None
    )
    has_counter_candidate = bool(
        observed.macro_candidate_leg_direction or observed.leg15_direction
    )

    if prev is None:
        transition = ThesisTransition(
            from_state=ThesisStatus.UNKNOWN, to_state=ThesisStatus.SUPPORTED, at=now_iso,
            transition_cause="UNKNOWN", invalidation_fired=False,
            reconciliation="Bootstrap — no prior MarketUnderstanding to reconcile against.",
        )
        validate_transition(transition)
        return MarketUnderstanding(
            thesis_status=ThesisStatus.SUPPORTED,
            primary_thesis={"direction": observed.macro_bias_confirmed},
            supporting_evidence=[], conflicting_evidence=[],
            failure_condition=FailureCondition(0.0, "above", 0.0),  # placeholder — filled by maintain_thesis()
            expectations=[], confidence="Research only",
            history=[transition], last_reconciled_at=now_iso,
        )

    direction_changed = (
        prev.primary_thesis.get("direction") != observed.macro_bias_confirmed
    )

    if not direction_changed:
        if invalidation_fired:
            # Same direction, but the anchor died this scan and nothing
            # replaced it yet — genuinely invalidated, not just stale.
            to_state = ThesisStatus.INVALIDATED
        elif observed.macro_bias_stale and has_counter_candidate:
            to_state = ThesisStatus.CONTESTED
        elif observed.macro_bias_stale:
            to_state = ThesisStatus.WEAKENING
        else:
            to_state = ThesisStatus.SUPPORTED
        transition = ThesisTransition(
            from_state=prev.thesis_status, to_state=to_state, at=now_iso,
            transition_cause="FAILED_CONTINUATION" if invalidation_fired else "UNKNOWN",
            invalidation_fired=invalidation_fired,
            invalidation_mechanism=invalidation_mechanism,
        )
    else:
        # Direction changed — this is the exact GBPUSD case. Never accept
        # this silently.
        if invalidation_fired:
            to_state = ThesisStatus.INVALIDATED
            reconciliation = f"Old thesis invalidated via {invalidation_mechanism}; new direction follows."
            superseding = [invalidation_mechanism]
        elif observed.promotion_confirmed:
            # A promotion event lands at TRANSITIONING first, regardless of
            # prior status (SUPPORTED/WEAKENING/CONTESTED all jump here on
            # first sight of a confirmed promotion) — it only COMPLETES to
            # SUPPORTED on a later scan where the status was ALREADY
            # TRANSITIONING and the new direction still holds. This is a
            # deliberate one-scan confirmation delay: flipping the whole
            # belief the instant a mechanical bar is crossed is the exact
            # naive-binary problem thesis_status exists to avoid.
            reconciliation = (
                "Old thesis went stale (macro_bias_stale=True) without its stated "
                "failure_condition firing. A 15M structure "
                f"({observed.leg15_direction}, break_count={observed.leg15_break_count}) "
                "cleared the mechanical promotion bar (_promotion_confirmed)."
            )
            superseding = ["stale_bias_promotion"]
            to_state = (
                ThesisStatus.SUPPORTED if prev.thesis_status == ThesisStatus.TRANSITIONING
                else ThesisStatus.TRANSITIONING
            )
        else:
            # Direction field disagrees but neither invalidation nor
            # promotion criteria are satisfied in what we were handed —
            # do NOT force a confident narrative over an ambiguous case.
            to_state = ThesisStatus.TRANSITIONING
            reconciliation = None
            superseding = []

        transition = ThesisTransition(
            from_state=prev.thesis_status, to_state=to_state, at=now_iso,
            transition_cause="BIAS_FLIP", invalidation_fired=invalidation_fired,
            invalidation_mechanism=invalidation_mechanism,
            superseding_evidence=superseding, reconciliation=reconciliation,
        )

    try:
        validate_transition(transition)
    except TransitionError:
        # Could not justify the change with what we have — freeze at
        # TRANSITIONING and surface it rather than raising past MIL's
        # caller. This IS the "unresolved question" case.
        transition.to_state = ThesisStatus.TRANSITIONING
        prev.unresolved_questions.append(
            f"Direction changed ({prev.primary_thesis.get('direction')} -> "
            f"{observed.macro_bias_confirmed}) without invalidation firing or "
            f"promotion confirming, at {now_iso}."
        )

    prev.thesis_status = transition.to_state
    if transition.to_state == ThesisStatus.SUPPORTED:
        # Belief actually adopts the new direction — either the same
        # direction persisting, or a TRANSITIONING event completing.
        prev.primary_thesis["direction"] = observed.macro_bias_confirmed
        prev.counter_thesis = None
    elif transition.to_state == ThesisStatus.INVALIDATED:
        # Old belief is dead. Do NOT hand it the new direction for free —
        # that's exactly the silent-overwrite bug. A fresh thesis forms
        # (UNKNOWN -> SUPPORTED) on a later scan, not this one.
        prev.primary_thesis["direction"] = None
    elif transition.to_state == ThesisStatus.TRANSITIONING:
        # Old direction stays primary and in force. The incoming
        # direction is a CHALLENGER, not yet believed — parked in
        # counter_thesis so it's visible without being adopted.
        prev.counter_thesis = {"direction": observed.macro_bias_confirmed,
                                "status": "awaiting_confirmation"}
    # WEAKENING / CONTESTED only occur on the not-direction-changed path,
    # so primary_thesis["direction"] is already correct — nothing to do.

    prev.history.append(transition)
    prev.last_reconciled_at = now_iso
    return prev


def maintain_thesis(understanding: MarketUnderstanding, thesis_evidence: list,
                     thesis_weaknesses: list) -> MarketUnderstanding:
    """
    Lifted from build_market_thesis()'s evidence/weakness assembly — NOT
    reimplemented here from scratch. Refreshes supporting_evidence/
    conflicting_evidence to THIS scan's MarketThesis output every call —
    these describe the current thesis snapshot, not accumulated history
    (continuity/history is ThesisTransition's job, already handled by
    reconcile()). Call site: after thesis is built this scan (~line 3063
    in scanner_live.py), passing thesis.evidence_prose/thesis.
    weaknesses_prose — the PROSE variants, deliberately, not thesis.
    evidence/thesis.weaknesses. supporting_evidence/conflicting_evidence
    feed straight into hfis.py's conversational narration
    (_weave_evidence()), which expects well-formed sentence fragments;
    the label-style evidence/weaknesses fields are terse machine tags
    (e.g. "4 breaks into this leg — getting old") meant for structured
    display, not for reading aloud. FIXED 2026-09-08 — this call site
    was passing the label-style fields, which is why narration text was
    reading like a raw data dump instead of a sentence.
    """
    understanding.supporting_evidence = list(thesis_evidence or [])
    understanding.conflicting_evidence = list(thesis_weaknesses or [])
    return understanding


# Cap on counter_candidate_history length — same pattern as
# MARKET_EVENT_LOG_MAX_LEN in scanner_observation.py. Trimmed oldest-
# first in track_counter_candidate() below whenever a new record is
# appended; this is a rolling window for conversational recall, not a
# permanent audit log (ThesisTransition.history already IS the
# permanent audit trail for anything that actually became the primary
# thesis — this only needs to cover recent fizzled/promoted episodes).
COUNTER_CANDIDATE_HISTORY_MAX_LEN = 20

_COUNTER_MATURITY_ORDER = ["early", "building", "qualified_awaiting_promotion"]


def _counter_candidate_maturity(atr_ok: Optional[bool], vs_prior_ok: Optional[bool]) -> str:
    """
    PURE. Deterministic label from the exact two gate booleans
    compute_macro_bias() already computes and persists
    (macro_candidate_leg_atr_ok / macro_candidate_leg_vs_prior_ok) —
    zero new detection, same discipline as every relate_*()/classify_*()
    function elsewhere in this codebase. Both True is the SAME bar
    reconcile() itself uses for promotion (see qualifies = atr_ok and
    vs_prior_ok in compute_macro_bias()) — "qualified_awaiting_promotion"
    here means "has cleared the bar," not a softer or different bar.
    """
    if atr_ok and vs_prior_ok:
        return "qualified_awaiting_promotion"
    if atr_ok or vs_prior_ok:
        return "building"
    return "early"


def track_counter_candidate(understanding: MarketUnderstanding, observed: ObservedLegState,
                             now_iso: str) -> MarketUnderstanding:
    """
    Maintains understanding.emerging_counter_case — a continuously
    updated view of an opposing candidate leg from the moment it FIRST
    appears, independent of reconcile()'s promotion bar. (Per chat,
    JUINIOR — "the counter case is more in depth" — the gap being fixed:
    reconcile() only ever surfaces a counter_thesis once
    promotion_confirmed is already True, so a candidate that's, say,
    only atr_ok so far is invisible today even though MIL already
    receives both gate facts every scan via ObservedLegState.)

    HARD RULE, ENFORCED BY CONSTRUCTION: this function NEVER writes
    thesis_status, primary_thesis, or counter_thesis — those remain
    exclusively reconcile()'s. Tracking a candidate earlier must not
    mean believing it earlier; the promotion bar this function watches
    is not a bar it is allowed to lower or bypass.

    CALL ORDER: must run AFTER reconcile() in the same scan (reconcile()
    may have just promoted this exact candidate into counter_thesis —
    this function needs to see that to resolve the matching history
    record as "promoted" rather than leaving a stale duplicate sitting
    in emerging_counter_case alongside the now-official counter_thesis).

    Candidate direction: macro_candidate_leg_direction (the earliest
    possible signal — present even while the 1H leg is still alive, per
    compute_macro_bias()'s own comment) takes precedence over
    leg15_direction when both are present and disagree — the 1H
    candidate is the structurally larger claim. This is a deliberate
    simplification, not a claim that leg15 is irrelevant: leg15's own
    break_count still feeds vs_prior_ok/atr_ok when IT is the only
    candidate present (the stale-bias-promotion path, where there is no
    1H candidate at all).

    ABSENCE DISCIPLINE: no candidate this scan (None, or it now agrees
    with primary_thesis direction) resolves any currently-tracked
    episode as "fizzled" and clears emerging_counter_case to None —
    never left stale from a scan that's no longer true.
    """
    candidate_direction = observed.macro_candidate_leg_direction or observed.leg15_direction
    primary_direction = understanding.primary_thesis.get("direction")
    current = understanding.emerging_counter_case

    def _append_history(record: CounterCandidateRecord) -> None:
        understanding.counter_candidate_history.append(record)
        if len(understanding.counter_candidate_history) > COUNTER_CANDIDATE_HISTORY_MAX_LEN:
            understanding.counter_candidate_history = (
                understanding.counter_candidate_history[-COUNTER_CANDIDATE_HISTORY_MAX_LEN:]
            )

    # ---- No live opposing candidate this scan -> resolve as fizzled.
    if candidate_direction is None or candidate_direction == primary_direction:
        if current is not None:
            _append_history(CounterCandidateRecord(
                direction=current["direction"],
                first_observed_at=current["first_observed_at"],
                resolved_at=now_iso,
                resolution="fizzled",
                peak_maturity=current["peak_maturity"],
            ))
            understanding.emerging_counter_case = None
        return understanding

    # ---- reconcile() already promoted this exact candidate this scan
    # (counter_thesis freshly set to it, status=="awaiting_confirmation")
    # -> resolve as promoted rather than leaving a duplicate, less-
    # authoritative copy sitting in emerging_counter_case.
    if (understanding.counter_thesis
            and understanding.counter_thesis.get("direction") == candidate_direction
            and understanding.counter_thesis.get("status") == "awaiting_confirmation"
            and current is not None and current["direction"] == candidate_direction):
        _append_history(CounterCandidateRecord(
            direction=current["direction"],
            first_observed_at=current["first_observed_at"],
            resolved_at=now_iso,
            resolution="promoted",
            peak_maturity="qualified_awaiting_promotion",
        ))
        understanding.emerging_counter_case = None
        return understanding

    maturity = _counter_candidate_maturity(
        observed.macro_candidate_leg_atr_ok, observed.macro_candidate_leg_vs_prior_ok
    )

    if current is None or current["direction"] != candidate_direction:
        # Fresh episode — either genuinely new, or the guard clauses
        # above already resolved whatever was tracked before this point
        # this same call, so there is nothing stale left to overwrite.
        understanding.emerging_counter_case = {
            "direction": candidate_direction,
            "maturity": maturity,
            "atr_ok": bool(observed.macro_candidate_leg_atr_ok),
            "vs_prior_ok": bool(observed.macro_candidate_leg_vs_prior_ok),
            # Raw pip figures (already computed by compute_macro_bias(),
            # see ObservedLegState's correction note) — lets a caller say
            # "1.8 of 3.0 ATR" rather than only a pass/fail boolean.
            "range_pips": observed.macro_candidate_leg_range_pips,
            "required_atr_pips": observed.macro_candidate_leg_required_atr_pips,
            "required_prior_pips": observed.macro_candidate_leg_required_prior_pips,
            "first_observed_at": now_iso,
            "peak_maturity": maturity,
        }
    else:
        current["maturity"] = maturity
        current["atr_ok"] = bool(observed.macro_candidate_leg_atr_ok)
        current["vs_prior_ok"] = bool(observed.macro_candidate_leg_vs_prior_ok)
        current["range_pips"] = observed.macro_candidate_leg_range_pips
        current["required_atr_pips"] = observed.macro_candidate_leg_required_atr_pips
        current["required_prior_pips"] = observed.macro_candidate_leg_required_prior_pips
        # peak_maturity only ratchets forward — a candidate that reached
        # "building" then chopped back to "early" (atr_ok flips False
        # again) should still show it once reached "building" rather
        # than losing that fact to the most recent scan's read alone.
        if _COUNTER_MATURITY_ORDER.index(maturity) > _COUNTER_MATURITY_ORDER.index(current["peak_maturity"]):
            current["peak_maturity"] = maturity

    return understanding


def evaluate_scenarios(understanding: MarketUnderstanding,
                        intent_watching_for: list) -> MarketUnderstanding:
    """
    Builds counter_thesis from MarketIntent's LOCATION-role watching_for
    entries — but ONLY enriches an ALREADY-PROMOTED counter_thesis
    (reconcile() sets one only on TRANSITIONING, when the mechanical
    promotion bar has actually been cleared). Deliberately does NOT
    create a counter_thesis from a LOCATION entry alone — a "pullback to
    OB worth watching" is not the same claim as "a challenger direction
    has cleared its promotion bar," and conflating the two would let
    MarketIntent quietly manufacture belief transitions reconcile()
    never earned. If no counter_thesis exists this scan, this is a
    no-op.
    """
    if understanding.counter_thesis is None:
        return understanding
    location_entries = [w for w in (intent_watching_for or []) if w.get("role") == "LOCATION"]
    if location_entries:
        understanding.counter_thesis["watching_for"] = location_entries
    return understanding


def generate_expectations(understanding: MarketUnderstanding,
                           expected_next_event: Optional[str],
                           intent_watching_for: list) -> MarketUnderstanding:
    """
    expected_next_event + CONFIRMATION-role watching_for entries,
    assembled, not re-detected. Unconditional every scan (unlike
    evaluate_scenarios) — "what would confirm this" is always relevant
    context regardless of thesis_status, not contingent on a promotion
    event having occurred.
    """
    expectations = []
    if expected_next_event:
        expectations.append({"type": "expected_next_event", "text": expected_next_event})
    confirmation_entries = [w for w in (intent_watching_for or []) if w.get("role") == "CONFIRMATION"]
    expectations.extend({"type": "confirmation", **w} for w in confirmation_entries)
    understanding.expectations = expectations
    return understanding


def detect_material_change(prev: Optional[MarketUnderstanding], observed: dict) -> list:
    """Same job as classify_thesis_delta() today, but the trigger for
    reconcile() to run rather than the end product itself."""
    raise NotImplementedError


def persist_reasoning_state(understanding: MarketUnderstanding) -> dict:
    """
    Serializes MarketUnderstanding into a plain dict payload for the
    CALLER (LIVE) to commit into state.json/WorldState alongside
    everything else it writes this scan.

    MIL does NOT open state.json / world_state and write into it
    directly — per chat, that's the "database administrator" pattern
    we're explicitly avoiding. LIVE already has a controlled commit
    point every scan; MIL is a consumer/producer of a payload, not an
    independent writer. This function's ONLY job is producing that
    payload — persistence itself belongs to whatever LIVE already uses
    (apply_state_updates / save_state in scanner_common.py).
    """
    return understanding_to_dict(understanding)


def understanding_to_dict(u: MarketUnderstanding) -> dict:
    """JSON-safe serialization of a MarketUnderstanding — enums become
    their .value strings, nested dataclasses become plain dicts. The
    inverse of understanding_from_dict(). Written here (MIL's own file)
    rather than in scanner_live.py — LIVE persists the payload, it
    should never need to know MIL's internal dataclass shapes to do so."""
    return {
        "thesis_status": u.thesis_status.value,
        "primary_thesis": dict(u.primary_thesis),
        "supporting_evidence": list(u.supporting_evidence),
        "conflicting_evidence": list(u.conflicting_evidence),
        "failure_condition": {
            "origin_level": u.failure_condition.origin_level,
            "origin_direction": u.failure_condition.origin_direction,
            "retrace_level": u.failure_condition.retrace_level,
        } if u.failure_condition else None,
        "expectations": list(u.expectations),
        "confidence": u.confidence,
        "counter_thesis": dict(u.counter_thesis) if u.counter_thesis else None,
        "unresolved_questions": list(u.unresolved_questions),
        "history": [
            {
                "from_state": t.from_state.value,
                "to_state": t.to_state.value,
                "at": t.at,
                "transition_cause": t.transition_cause,
                "invalidation_fired": t.invalidation_fired,
                "invalidation_mechanism": t.invalidation_mechanism,
                "superseding_evidence": list(t.superseding_evidence),
                "reconciliation": t.reconciliation,
            }
            for t in u.history
        ],
        "last_reconciled_at": u.last_reconciled_at,
        "worldstate_version": u.worldstate_version,
        # ADDED (per chat — counter-candidate depth): emerging_counter_case
        # is already a plain dict (built that way in track_counter_candidate,
        # same as counter_thesis above), so it needs no conversion.
        "emerging_counter_case": dict(u.emerging_counter_case) if u.emerging_counter_case else None,
        "counter_candidate_history": [
            {
                "direction": r.direction,
                "first_observed_at": r.first_observed_at,
                "resolved_at": r.resolved_at,
                "resolution": r.resolution,
                "peak_maturity": r.peak_maturity,
            }
            for r in u.counter_candidate_history
        ],
    }


def understanding_from_dict(d: Optional[dict]) -> Optional[MarketUnderstanding]:
    """Inverse of understanding_to_dict(). Returns None on missing/empty
    input — that's the correct "no prior understanding" signal reconcile()
    already treats as bootstrap, not an error to guard against separately."""
    if not d:
        return None
    fc = d.get("failure_condition")
    history = [
        ThesisTransition(
            from_state=ThesisStatus(t["from_state"]),
            to_state=ThesisStatus(t["to_state"]),
            at=t["at"],
            transition_cause=t["transition_cause"],
            invalidation_fired=t["invalidation_fired"],
            invalidation_mechanism=t.get("invalidation_mechanism"),
            superseding_evidence=t.get("superseding_evidence", []),
            reconciliation=t.get("reconciliation"),
        )
        for t in d.get("history", [])
    ]
    counter_candidate_history = [
        CounterCandidateRecord(
            direction=r["direction"],
            first_observed_at=r["first_observed_at"],
            resolved_at=r["resolved_at"],
            resolution=r["resolution"],
            peak_maturity=r["peak_maturity"],
        )
        for r in d.get("counter_candidate_history", [])
    ]
    return MarketUnderstanding(
        thesis_status=ThesisStatus(d["thesis_status"]),
        primary_thesis=dict(d.get("primary_thesis", {})),
        supporting_evidence=list(d.get("supporting_evidence", [])),
        conflicting_evidence=list(d.get("conflicting_evidence", [])),
        failure_condition=FailureCondition(**fc) if fc else FailureCondition(0.0, "above", 0.0),
        expectations=list(d.get("expectations", [])),
        confidence=d.get("confidence", "Research only"),
        counter_thesis=d.get("counter_thesis"),
        unresolved_questions=list(d.get("unresolved_questions", [])),
        history=history,
        last_reconciled_at=d.get("last_reconciled_at"),
        worldstate_version=d.get("worldstate_version"),
        emerging_counter_case=d.get("emerging_counter_case"),
        counter_candidate_history=counter_candidate_history,
    )
