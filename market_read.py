"""
market_read.py — THE single interpretation authority.

MIGRATION (frozen plan, per chat): absorbs brain.py's synthesis/
significance functions (_synthesize_market_understanding_branches,
_refine_next_watch, synthesize_market_understanding, build_market_logic,
_state_family, build_event_snapshot, evaluate_market_event) and ALL of
market_story.py (StoryImpact, classify_events, build_mechanism,
build_market_story, situation_key, is_repeat_situation) into one module,
plus the new canonical_watch_identity() dedup fix and the single
build_market_read() entry point that replaces brain.py's
build_market_understanding()/build_market_briefing().

Every function's original docstring is preserved unchanged below — this
is a structural relocation, not a rewrite. No wording, no branching
logic, no thresholds were touched in the move.

WHAT THIS FILE DOES NOT DO: fetch facts (worldstate.py's job, unchanged),
compute macro_bias/market_phase (scanner_observation.py's job, unchanged
— this file reads the result off WorldState, same as brain.py always
did: compute_market_phase() is a deterministic classifier whose OUTPUT
is incorporated into synthesis here; it is never re-presented standing
alone next to this file's conclusions), or render Telegram prose
(market_presenter.py's job — presentation moved out of brain.py at the
same time this file absorbed synthesis).

DEPENDENCY DIRECTION (verified before this file was written, per the
frozen plan's requirement #1): brain.py (trimmed to Layer A/B fact/
grouping functions only) imports nothing from this file. This file
imports FROM brain.py only. No cycle.

ANTI-LOOKAHEAD ORDERING (preserved exactly): watch-condition generation
still happens in scanner_observation.py, still before Tier information
exists, still unchanged. This file consumes that already-frozen list —
it does not regenerate or re-time it.

MIL BOUNDARY (per the frozen plan's correction on intent_state): this
file's output carries `prior_intent_state` as a plain pass-through INPUT
field (what MIL committed last scan) — it is never treated as this
file's own conclusion, and this file never writes MIL's persisted
MarketUnderstanding dataclass. MIL runs AFTER build_market_read()
returns, consumes the object, and produces its own updated, separately
persisted state. One name, one direction, per scan.
"""

import random
from datetime import datetime, timezone
from enum import Enum

from brain import (
    relate_current_leg_to_context,
    determine_structural_state,
    interpret_structure,
    relate_timeframe_conflict,
    relate_recent_momentum,
    relate_leg_maturity,
    build_market_intent_hypothesis,
    _reaccelerating,  # FIX (shared-interpretation pass): _synthesize_market_understanding_branches() below calls this; it was never imported here, so the copy in this module raised NameError on the aging/momentum branch while brain.py's identical copy worked.
)
from scanner_observation import _same_leg


# ---------------------------------------------------------------------
# CANONICAL WATCH IDENTITY (new — the actual fix for the duplicate-zone
# bug). Per the frozen plan's correction #2: identity is the tuple
# below; _same_leg() is the MATCHING PREDICATE used to compare two
# identities, never itself stored as or treated as the identity.
#
# Root cause this replaces: the old debounce (scanner_live.py) and the
# old dedup (min_scanner.py's _summarize_pending_events) both compared
# RENDERED TEXT containing live float zone bounds. A fib-pocket zone
# recomputed off the current leg's moving extreme drifts a few pips
# scan-to-scan (see the 1.33647-1.33726 vs 1.33644-1.33726 case), so
# exact-string comparison saw two different sentences and treated a
# genuinely unchanged watch condition as a new one every time.
#
# Fix: identity is built from the UPSTREAM REFERENCE that produced the
# zone (the leg), never from the zone's rendered price numbers. Reuses
# _same_leg() and its already-tuned LEG_MATCH_TOLERANCE_PIPS=5 constant
# — that tolerance already exists precisely because leg-derived values
# drift as a leg extends; nothing new was invented here.
# ---------------------------------------------------------------------

def canonical_watch_identity(watch_code, macro_bias, upstream_reference):
    """
    Builds the stable identity for a watch condition (a WatchCode entry
    from scanner_observation.py's watching_for list, or a Market Event
    tagged against an open leg). `upstream_reference` is the leg_id the
    condition was derived from for leg-scoped conditions (fib pocket,
    sweep-and-reclaim, fresh-displacement, 5M-alignment — everything
    whose bounds move as the current leg's swing extreme trails). For
    order-block conditions, upstream_reference should be the OB's own
    formation reference, not the leg — OB bounds are fixed at formation,
    not leg-relative, so exact identity (not tolerance matching) is
    correct there. [OPEN ITEM: confirm detect_order_block() exposes a
    stable formation reference (e.g. origin_idx) before wiring
    PULLBACK_TO_OB through this — flagged, not assumed.]

    Returns a plain tuple. Two identities are considered the SAME
    condition when watch_code and macro_bias match exactly AND
    watch_conditions_match() (below) returns True for their upstream
    references — never by comparing rendered sentence text.
    """
    return (watch_code, macro_bias, upstream_reference)


def watch_conditions_match(identity_a, identity_b):
    """
    The matching predicate — NOT the identity itself (see this module's
    header note and the frozen plan's correction #2). Same WatchCode,
    same direction, and the upstream references count as the same leg
    per _same_leg()'s existing tolerance (LEG_MATCH_TOLERANCE_PIPS=5,
    scanner_common.py — reused unchanged, not reinvented here).
    """
    code_a, bias_a, ref_a = identity_a
    code_b, bias_b, ref_b = identity_b
    if code_a != code_b or bias_a != bias_b:
        return False
    # Leg-derived references are leg_id strings — use the existing
    # tolerance-matching predicate. A non-leg reference (e.g. a future
    # OB formation id) that isn't a leg_id-shaped string falls back to
    # exact match, which is the correct behavior for fixed-at-formation
    # zones (see canonical_watch_identity()'s OPEN ITEM above).
    if isinstance(ref_a, str) and "|" in ref_a and isinstance(ref_b, str) and "|" in ref_b:
        return _same_leg(ref_a, ref_b)
    return ref_a == ref_b


# ---------------------------------------------------------------------
# SHARED INTERPRETATION DERIVATION (§6a — one implementation, two consumers)
#
# "Given an explicitly supplied world_state, derive the interpretation."
#
#   raw/current inputs ─┐
#                       ├─> derive_interpretation_from_world_state() ─┬─> build_market_read() -> MarketRead
#   persisted inputs  ──┘   (this function; below MarketRead)        └─> Category B/D fallback (no MarketRead built)
#
# Layering rules, enforced by test_shared_interpretation.py:
#   * BELOW the MarketRead layer. It never builds a MarketRead, and never
#     calls build_market_read(). The Category B/D fallback calls this
#     directly, preserving the lifecycle rule "no MarketRead available ->
#     do not construct one".
#   * Explicit inputs only: world_state (a dict) and generated_at (the
#     caller's clock, echoed back). No clock, no global state, no MIL, no
#     scanner_live, no Telegram state, no I/O. Same inputs -> same output.
#   * Deliberately does NOT carry current-only inputs: mechanical_result
#     (world_state["live_tiers"]), prior_intent_state, intent_hypothesis /
#     meaningful_watch, or `why`. Those belong to MarketRead assembly and are
#     added by build_market_read() on top of this core. The fallback has no
#     legitimate source for them, so they are not manufactured.
#
# The synthesis block below was MOVED VERBATIM from build_market_read()
# (which had inlined a copy of brain.build_market_understanding()'s
# body). Interpretation logic is unchanged.
# ---------------------------------------------------------------------

def derive_interpretation_from_world_state(world_state, generated_at):
    """
    Pure. Returns the interpretation core: exactly the dict shape brain.py's
    build_market_understanding() returned (generated_at + current_condition,
    structural_context, relationship_facts, developing_scenario,
    key_structural_test, invalidation_context, thesis_* pass-throughs,
    timeframe_conflict, leg_maturity, recent_momentum, market_assessment).

    generated_at is supplied by the caller rather than read from a clock
    here, so this function stays deterministic and testable.
    """
    phase = (world_state or {}).get("phase") or {}
    thesis = (world_state or {}).get("thesis") or {}

    relationship_facts = relate_current_leg_to_context(world_state)
    structural_state = determine_structural_state(relationship_facts)
    developing_scenario = interpret_structure(relationship_facts, structural_state)

    key_structural_test = None
    if relationship_facts and relationship_facts.get("structural_transition_status") == "unconfirmed":
        key_structural_test = relationship_facts.get("prior_origin_price")

    current_condition = {
        "phase": phase.get("phase"),
        "macro_bias": phase.get("macro_bias"),
        "leg_direction": (phase.get("macro_leg") or {}).get("direction"),
        "default_next_event": phase.get("default_next_event"),
        "macro_leg_origin":       (phase.get("macro_leg") or {}).get("origin"),
        "macro_leg_extreme":      (phase.get("macro_leg") or {}).get("extreme"),
        "prior_macro_leg_origin": (phase.get("prior_macro_leg") or {}).get("origin"),
        "macro_leg_extreme_rejection_count": (phase.get("macro_leg") or {}).get("extreme_rejection_count"),
        "macro_leg_last_rejection_price":    (phase.get("macro_leg") or {}).get("last_rejection_price"),
    }

    timeframe_conflict = relate_timeframe_conflict(world_state)
    leg_maturity = relate_leg_maturity(world_state)
    recent_momentum = relate_recent_momentum(world_state)
    market_assessment = synthesize_market_understanding(
        relationships={
            "leg_context": relationship_facts,
            "timeframe_conflict": timeframe_conflict,
            "leg_maturity": leg_maturity,
            "momentum": recent_momentum,
        },
        current_condition=current_condition,
    )

    return {
        "generated_at": generated_at,
        "current_condition": current_condition,
        "structural_context": structural_state,
        "relationship_facts": relationship_facts,
        "developing_scenario": developing_scenario,
        "key_structural_test": key_structural_test,
        "invalidation_context": thesis.get("invalidation"),
        "thesis_weaknesses": thesis.get("weaknesses") or [],
        "thesis_weaknesses_prose": thesis.get("weaknesses_prose") or [],
        "thesis_weakness_categories": thesis.get("weakness_categories") or [],
        "thesis_expected_next_event": thesis.get("expected_next_event"),
        "timeframe_conflict": timeframe_conflict,
        "leg_maturity": leg_maturity,
        "recent_momentum": recent_momentum,
        "market_assessment": market_assessment,
    }


# ---------------------------------------------------------------------
# SINGLE ENTRY POINT — replaces brain.py's build_market_understanding()
# and build_market_briefing(). Both of those independently called
# synthesize_market_understanding()/build_market_logic()/
# build_market_intent_hypothesis() and assembled overlapping output
# shapes; per the frozen plan there is now exactly one assembly point.
#
# Return shape is DELIBERATELY the same dict shape build_market_
# understanding() already returned, with build_market_briefing()'s
# additional fields (logic, intent_hypothesis, confirmation,
# snapshots) folded in, plus two new fields (prior_intent_state,
# meaningful_watch) — not a new dataclass. Keeping the existing shape
# during the STRUCTURAL migration means every existing consumer's
# field access keeps working; introducing a new type system in the
# same pass would be scope creep beyond what was frozen.
# ---------------------------------------------------------------------

def build_market_read(world_state, prior_intent_state=None):
    """
    THE single interpretation authority entry point.

    prior_intent_state: MIL's persisted MarketUnderstanding from the
    PREVIOUS scan, read-only, passed straight through as an input field
    on the returned object. This file never derives it and never
    mutates it — MIL owns reconciling it, and does so AFTER this
    function returns (see this module's header MIL BOUNDARY note).
    """
    now_utc = datetime.now(timezone.utc)
    # NEW (Stage 1 finding): world_state already surfaces the mechanical
    # engine's own result as a fact — worldstate.py:239 writes
    # world_state["live_tiers"] = state.get("live_tier_digest"), written
    # by evaluate_rule_of_law()/build_live_tier_digest() (scanner_live.py
    # — the MECHANICAL DECISION layer). This was never read here before
    # this gap was found. Read-only pass-through — MarketRead does NOT
    # call the mechanical engine, per the frozen boundary; it only
    # carries whatever the engine already decided and already wrote to
    # WorldState as a fact. NOTE (also found this stage, reported
    # separately): given current _scan_once() call ordering, this will
    # be LAST scan's digest, not this scan's, whenever build_market_read()
    # is called before evaluate_rule_of_law() runs for the current scan.
    mechanical_result = (world_state or {}).get("live_tiers")


    interpretation = derive_interpretation_from_world_state(
        world_state, generated_at=now_utc.isoformat()
    )
    timeframe_conflict = interpretation["timeframe_conflict"]
    leg_maturity = interpretation["leg_maturity"]
    market_assessment = interpretation["market_assessment"]

    # "why" — moved here from brain.py's build_market_logic(). Reclassified
    # during implementation: build_market_logic() branches its own prose
    # on market_assessment["state"] — that's synthesis output (a market-
    # derived claim), not a mechanical rendering of already-decided facts,
    # so per the presenter-purity invariant it cannot live in
    # market_presenter.py. It belongs here, as the `why` field's VALUE.
    why = build_market_logic(market_assessment, timeframe_conflict, leg_maturity)

    intent_hypothesis = build_market_intent_hypothesis(world_state)
    confirmation_sentences = None
    if intent_hypothesis and intent_hypothesis.get("confirmations"):
        confirmation_sentences = [c.get("sentence") for c in intent_hypothesis["confirmations"]]

    # meaningful_watch — replaces raw watch_conditions being dumped as-is.
    # Carries the LOCATION-role entries from intent_hypothesis (what's
    # actually worth watching, already role-filtered upstream) alongside
    # `why`, so a presenter never has to explain "watching for X" without
    # the reasoning sitting right next to it (the exact gap flagged
    # against the old Expansion/"watching for weakness" text).
    meaningful_watch = (intent_hypothesis or {}).get("locations") or []

    return {
        **interpretation,  # generated_at + the shared interpretation core (same key order as before)
        "why": why,
        "intent_hypothesis": intent_hypothesis,
        "confirmation": confirmation_sentences,
        "meaningful_watch": meaningful_watch,
        "prior_intent_state": prior_intent_state,
        "mechanical_result": mechanical_result,
    }

def _synthesize_market_understanding_branches(relationships, current_condition):
    """
    LAYER B — the actual interpretation. Takes NAMED Layer A
    relationships (not positional args for three specific functions —
    see module docstring's EXTENSIBILITY note) and `current_condition`
    (phase/macro_bias baseline, same dict build_market_understanding()
    already assembles) and decides what's actually going on.

    THE RULE THIS EXISTS TO ENFORCE (per chat): don't aggregate
    warnings into a danger score. Ask "what hypothesis is being
    threatened, and has it actually failed" — not "how many red flags
    are there." A leg can be lower-timeframe-countertrend AND aging AND
    still structurally intact; that's "continuation under pressure,"
    not "HIGH failure risk." Conflating those was the exact bug this
    file exists to fix.

    Still templated, not freeform — same discipline as interpret_structure()
    and stitch_narrative(): branches keyed on already-computed
    combinations of Layer A facts, never generated prose. Returns None
    if there isn't enough directional information to say anything
    (no macro_bias yet).
    """
    htf_bias = (current_condition or {}).get("macro_bias")
    if htf_bias not in ("BULLISH", "BEARISH"):
        return None

    direction_label = htf_bias.title()

    leg_context = relationships.get("leg_context")
    tf_conflict = relationships.get("timeframe_conflict")
    maturity = relationships.get("leg_maturity")
    momentum = relationships.get("momentum")

    transition_status = leg_context.get("structural_transition_status") if leg_context else None
    alignment = leg_context.get("current_vs_prior_alignment") if leg_context else None
    prior_origin = leg_context.get("prior_origin_price") if leg_context else None

    m5_vs_htf = tf_conflict.get("m5_vs_htf") if tf_conflict else None
    is_aging = maturity.get("is_aging") if maturity else None
    # SEMANTIC NUANCE (2026-08-26, per chat with friend): is_aging alone
    # is a single boolean, but "leg has made 4+ consecutive breaks" and
    # "leg has ALSO travelled 2.5+ ATR from its EMA" are not the same
    # strength of claim — per the friend: "4 BOS -> exhaustion... I'd
    # call that a very mature/extended trend, but I wouldn't necessarily
    # tell you the market is exhausted. It might continue another 100
    # pips." Deliberately NOT renaming Phase.EXHAUSTION or splitting it
    # into a new phase (that touches leg_maturity/failure_risk/
    # EXPECTED_NEXT_EVENT_MAP/trend_health everywhere else Phase is
    # consumed — too large a change for this pass) — instead, the three
    # is_aging branches below read `both_signals` to say MORE when
    # break_count AND ema_distance both fired (a stronger, better-
    # supported claim) and stay at the existing, already-hedged "aging/
    # maturing" language (never "exhausted") when only break_count did.
    both_signals = (maturity.get("aging_reason") == "break_count+ema_distance") if maturity else False

    def _aging_qualifier():
        return (
            " Price is also meaningfully extended from EMA100, which adds to the case for fading strength."
            if both_signals else ""
        )

    # ---- 1. A confirmed higher-degree structural transition outranks
    # everything else below — if the opposing leg has actually reclaimed
    # the prior origin, that's the dominant fact about the market right
    # now, regardless of 5M noise or leg-age texture.
    if transition_status == "confirmed":
        return {
            "state": "structural_transition_confirmed",
            "primary_thesis": (
                f"{direction_label} pressure has reclaimed the prior leg's origin — "
                f"this looks like a confirmed higher-degree transition, not just a "
                f"leg-level move."
            ),
            "primary_threat": None,
            "structural_status": "confirmed_transition",
            "confirmation_needed": None,
            "alternative": None,
            "invalidation": None,
            # PHASE 1 (per audit, 2026-08-27): explicit opt-out of the
            # generalized default_next_event lookup (see wrapper below).
            # EXPECTED_NEXT_EVENT_MAP's "transition" entries describe an
            # UNCONFIRMED transition awaiting a break ("fresh BOS... or
            # reversion to range") — stale and wrong once the transition
            # is already confirmed, which is exactly this branch. Nothing
            # to watch for here that isn't already said above.
            "next_watch": None,
        }

    if transition_status == "unconfirmed":
        level_txt = f"{prior_origin}" if prior_origin is not None else "the prior structural level"
        return {
            "state": "structural_transition_developing",
            "primary_thesis": (
                f"{direction_label} pressure is developing against the broader "
                f"structure, but hasn't displaced it yet."
            ),
            "primary_threat": "Opposing move has not reclaimed the prior structural level.",
            "structural_status": "not_yet_confirmed",
            "confirmation_needed": f"A break of {level_txt}.",
            "alternative": "Broader structure holds and this move fails as corrective.",
            "invalidation": None,
            # PHASE 1 opt-out (see above) — confirmation_needed already
            # names the exact level; the map's generic "transition" text
            # would just restate that more vaguely underneath it.
            "next_watch": None,
        }

    # ---- 2. Continuation of established structure (or no leg_context
    # available at all — e.g. very first tracked leg) — the common case.
    # This is where the timeframe-conflict + maturity combination matters.
    countertrend = m5_vs_htf == "countertrend"

    if countertrend and is_aging:
        return {
            "state": f"{htf_bias.lower()}_continuation_under_pressure",
            "primary_thesis": (
                f"{direction_label} structure remains intact, but continuation is "
                f"weakening — the lower timeframe has turned against the higher-"
                f"timeframe direction while the current leg is already aging."
                f"{_aging_qualifier()}"
            ),
            "primary_threat": "Lower-timeframe move against the higher-timeframe direction, on an aging leg.",
            "structural_status": "not_invalidated",
            "confirmation_needed": "Lower timeframe recovers back in line with the higher-timeframe direction.",
            "alternative": "If lower-timeframe weakness propagates upward, this shifts from continuation to correction/reversal.",
            "invalidation": None,
            # EXPLICIT next_watch (2026-09-16, per chat — friend's audit,
            # confirmed by tracing: this branch used to fall through to
            # synthesize_market_understanding()'s generic default_next_
            # event fallback, which has no idea this branch's primary_
            # thesis is "the {htf} thesis is still intact." That fallback
            # produced "Follow-through BOS confirming the new direction"
            # — reframing the whole message as "waiting for the bullish
            # side to confirm," directly contradicting the bearish
            # primary_thesis stated one sentence earlier. The friend's
            # exact fix: what matters isn't whether the counter-move
            # produces another BOS in its own direction (that alone
            # proves nothing about the {htf} thesis) — it's whether that
            # weakness starts pressuring the higher-timeframe structure
            # itself, i.e. actually progresses toward invalidation.
            "next_watch": (
                f"Not whether the lower timeframe produces another push on its "
                f"own — that alone doesn't touch the {direction_label.lower()} "
                f"thesis. What matters is whether this weakness starts "
                f"pressuring the higher-timeframe structure itself."
            ),
        }

    if countertrend and not is_aging:
        return {
            "state": f"{htf_bias.lower()}_continuation_early_pressure",
            "primary_thesis": (
                f"{direction_label} structure remains intact and the leg is not "
                f"yet mature, but the lower timeframe has just turned against the "
                f"higher-timeframe direction."
            ),
            "primary_threat": "Fresh lower-timeframe move against the higher-timeframe direction.",
            "structural_status": "not_invalidated",
            "confirmation_needed": "Lower timeframe recovers back in line with the higher-timeframe direction.",
            "alternative": "Too early to distinguish a genuine early warning from routine lower-timeframe noise.",
            "invalidation": None,
            # EXPLICIT next_watch — same fix and same reasoning as the
            # aging sibling branch above (2026-09-16, per chat). This is
            # the EXACT branch the friend's screenshot traced to: HTF
            # bearish thesis intact, LTF just turned against it, and the
            # old fallback said "what matters next" was confirming the
            # NEW (bullish) direction — subtly declaring the bearish
            # thesis already secondary. Fixed the same way: what matters
            # is whether this fresh countertrend move actually starts
            # pressuring {htf} structure, not whether it confirms on its
            # own terms.
            "next_watch": (
                f"Not whether this lower-timeframe move confirms into a new "
                f"direction on its own — that alone doesn't touch the "
                f"{direction_label.lower()} thesis. What matters is whether it "
                f"continues and starts pressuring the higher-timeframe "
                f"structure itself."
            ),
        }

    if is_aging:  # aligned or no 5M read, but leg is mature
        # NEW (2026-08-27, per chat — real /understand case: a 38h-old,
        # 4-break, 5.5-ATR leg labeled "exhaustion" on the exact scan
        # where the 15M push was expanding at 4x ATR). Checked BEFORE the
        # plain maturing branch below, and scoped deliberately narrow to
        # the case that was actually evidenced: aligned/no-countertrend
        # aging leg with a currently strong, expanding 15M push. The
        # countertrend+aging branch above is NOT given this treatment —
        # "lower timeframe opposing" and "15M expanding" is a genuinely
        # different, more confusing combination that hasn't been checked
        # against a real case yet, so it stays as-is rather than guessing.
        if _reaccelerating(momentum):
            # REWRITE (2026-08-27, per audit — friend's /understand review):
            # the previous version of this branch stopped at "these two
            # facts haven't been reconciled" instead of actually reconciling
            # them — a reasoning transcript, not a conclusion. Per chat:
            # the maturity/momentum combination is not a contradiction to
            # flag, it's a completed synthesis to state. Leg-level facts
            # (break-count, EMA-distance) say the CAMPAIGN is mature;
            # push-level facts (15M expansion, range vs ATR) say the
            # CURRENT impulse still has force. Both are true at once and
            # the correct reading is: maturity bounds how much runway is
            # LEFT, it does not describe the strength of what's happening
            # RIGHT NOW. primary_threat below now states that directly.
            #
            # confirmation_needed/alternative also fixed: the old text
            # treated "expansion cooling off" and "a fresh continuation
            # impulse" as two interchangeable things that would "clarify"
            # the read. They aren't interchangeable and don't point the
            # same direction — a fresh impulse doesn't confirm aging, it
            # confirms the OPPOSITE (the mature leg can still produce
            # continuation); cooling expansion doesn't confirm exhaustion
            # either, it only removes today's evidence of reacceleration.
            # Split into two correctly-signed, non-symmetric claims.
            return {
                "state": f"{htf_bias.lower()}_continuation_maturing_reaccelerating",
                "primary_thesis": (
                    f"{direction_label} structure remains intact and the lower "
                    f"timeframe isn't opposing it. The leg is aging by break-"
                    f"count and EMA-distance, but the most recent push isn't "
                    f"fading — 15-minute volatility is expanding and this leg's "
                    f"range is {momentum['trend_strength_atr_mult']:.1f}x the "
                    f"current 15-minute ATR."
                ),
                "primary_threat": (
                    "Leg maturity is a caution about how much runway the "
                    "broader campaign has left — it is not evidence that "
                    "this specific push lacks force."
                ),
                "structural_status": "not_invalidated",
                # What would EXTEND the current reacceleration read.
                "confirmation_needed": (
                    f"a fresh {direction_label.lower()} extreme while "
                    f"15-minute expansion holds would confirm the mature "
                    f"leg is still capable of genuine continuation, not "
                    f"just a temporary push."
                ),
                # What would instead REVERT to the plain aging read — kept
                # separate because it is not the same signal as the above,
                # nor its symmetric opposite.
                "alternative": (
                    "If 15-minute expansion cools off without producing a "
                    "fresh extreme, this reverts to a plain aging/maturing "
                    "read — that combination, not expansion or a fresh push "
                    "on its own, is what would make the leg's age the more "
                    "urgent fact again."
                ),
                "invalidation": None,
                # Brain's OWN next-watch statement for this specific state,
                # used by format_understanding_narrative() in place of
                # thesis_expected_next_event — see that function's docstring
                # for why the two can otherwise contradict each other in the
                # same paragraph.
                "next_watch": (
                    f"whether this reacceleration produces another genuine "
                    f"{direction_label.lower()} extreme, not a CHoCH. A CHoCH "
                    f"or range would only become the relevant next event if "
                    f"this expansion stalls without ever making a fresh extreme"
                ),
            }
        return {
            "state": f"{htf_bias.lower()}_continuation_maturing",
            "primary_thesis": (
                f"{direction_label} structure remains intact and the lower "
                f"timeframe isn't opposing it, but the current leg is aging."
                f"{_aging_qualifier()}"
            ),
            # REWRITE (2026-08-27, per audit — found while tracing a live
            # Market Event push that read like pre-fix text). This branch
            # is the sibling of continuation_maturing_reaccelerating above
            # and was NEVER updated during that earlier fix — only the
            # reaccelerating branch was. Not the same defect (no
            # unreconciled contradiction, no ambiguous "or"), but it
            # still read as leftover, unrevised prose sitting right next
            # to a branch that got the full treatment — worth bringing
            # to the same standard. Unlike the reaccelerating case, there
            # IS no reacceleration signal here to weigh against the
            # aging read, so the caution is simply the dominant fact
            # right now — say that plainly instead of a bare assertion.
            "primary_threat": (
                "Leg maturity is a caution on how much runway the campaign "
                "has left. With no reacceleration signal to weigh against "
                "it, that caution is the more relevant read right now."
            ),
            "structural_status": "not_invalidated",
            "confirmation_needed": (
                f"A fresh continuation impulse — renewed 15-minute expansion "
                f"producing a new {direction_label.lower()} extreme — would "
                f"be needed to shift this back toward an active, forceful "
                f"read rather than a fading one."
            ),
            "alternative": None,
            "invalidation": None,
            # EXPLICIT next_watch (2026-09-16, per chat — the same audit
            # that caught the countertrend branches above also flagged
            # this file's whole reliance on default_next_event as
            # structurally risky: that field is keyed off market_phase/
            # transition_cause (the separate MarketPhase engine), NOT off
            # the countertrend/is_aging combination this function actually
            # reasons from — nothing guarantees the two ever agree. Rather
            # than trust that coincidence for this branch too, stating it
            # explicitly, reusing the same CHoCH-vs-range distinction
            # _NEXT_EVENT_REFINEMENTS already established for exactly this
            # situation ("leg showing age") so the phrasing stays
            # consistent with whatever a Market Event push says.
            "next_watch": (
                f"a {direction_label.lower()} CHoCH would be actual structural "
                f"evidence against this leg — range or stalling on its own is "
                f"weaker, just a loss of directional progression, not "
                f"necessarily reversal"
            ),
        }

    if m5_vs_htf == "aligned":
        return {
            "state": f"{htf_bias.lower()}_continuation_clean",
            "primary_thesis": (
                f"{direction_label} structure is progressing cleanly — lower "
                f"timeframe order flow is aligned and the leg isn't showing age."
            ),
            "primary_threat": None,
            "structural_status": "not_invalidated",
            "confirmation_needed": None,
            "alternative": None,
            "invalidation": None,
            # EXPLICIT next_watch (2026-09-16, per chat — same reasoning
            # as continuation_maturing above: don't trust default_next_
            # event's separate categorization to happen to agree with
            # what this branch actually concluded). Nothing is currently
            # threatening this read, so next_watch names the two ways
            # that could change rather than a single generic "next event."
            "next_watch": (
                f"the first real sign of trouble — either the lower timeframe "
                f"turning against this leg, or the leg starting to show its age"
            ),
        }

    # No leg_context, no timeframe read, no maturity read — only the
    # bare HTF bias is known. Say that plainly rather than guessing.
    return {
        "state": f"{htf_bias.lower()}_bias_only",
        "primary_thesis": f"{direction_label} is the higher-timeframe bias, but there isn't yet enough lower-timeframe or leg-age information to assess continuation quality.",
        "primary_threat": None,
        "structural_status": "insufficient_data",
        "confirmation_needed": None,
        "alternative": None,
        "invalidation": None,
        # EXPLICIT next_watch (2026-09-16, per chat — same reasoning as
        # the two branches above). There's no thesis yet to threaten or
        # confirm, so this deliberately names what's actually missing
        # rather than reaching for a "next event" that presupposes a
        # leg already exists to watch.
        "next_watch": "enough bars to establish a real leg and start assessing how it's behaving",
    }


_NEXT_EVENT_REFINEMENTS = {
    # ADDED (2026-08-27, per chat — user: "why is it still saying CHoCH or
    # range when we identified why those aren't interchangeable"). Both
    # entries below are EXPECTED_NEXT_EVENT_MAP's own text (scanner_common.py)
    # — deliberately NOT edited there, because that map's values also feed
    # market_thesis_expected_next_event, Thesis's own EXP7-tagged persisted
    # field (see that field's docstring for why its shape/content must not
    # change). This dict refines only what BRAIN DISPLAYS via next_watch —
    # the map, and everything upstream of it, is untouched.
    #
    # The defect being fixed is the SAME "or" ambiguity Vally flagged and
    # we fixed for the reaccelerating branch's confirmation_needed: these
    # two entries lump a real structural signal (CHoCH) together with a
    # much weaker one (range / consolidation) as if they're interchangeable
    # "either would confirm it" options. Per that same conversation: a
    # CHoCH is actual structural evidence against the leg; range/
    # consolidation on their own are just a stall — loss of progression,
    # not necessarily reversal. Say that distinction, don't flatten it.
    #
    # THIRD ENTRY ADDED (2026-09-11, per chat — same complaint recurred:
    # "still says CHoCH or range" was actually TWO separate bugs, not one
    # — see scanner_live.py's MIL call site for the other. While tracing
    # it, found this file's own table only ever covered 2 of the map's 3
    # ambiguous "X or Y" entries — BIAS_FLIP's "Fresh BOS... or reversion
    # to range" has the identical structural flaw and was simply missed
    # the first time. NOTE: this dict is intentionally kept self-contained
    # here rather than imported from scanner_common.py's now-equivalent
    # NEXT_EVENT_DISPLAY_REFINEMENTS (added same day, for scanner_
    # observation.py's OWN "Since last scan" delta bullet, which had the
    # same bypass problem) — brain.py's own header above states a hard,
    # structurally-enforced rule against importing scanner_common at all.
    # Keep both copies in sync by hand if this table changes again; that's
    # the accepted cost of the isolation boundary, not an oversight.
    "CHoCH or range — leg showing age": (
        "a {d} CHoCH would be actual structural evidence against this leg — "
        "range on its own is weaker, just a loss of directional progression, "
        "not necessarily reversal"
    ),
    "CHoCH or extended consolidation": (
        "a {d} CHoCH would be actual structural evidence against this leg — "
        "extended consolidation on its own is weaker, just a stall, not "
        "necessarily reversal"
    ),
    "Fresh BOS in the new direction, or reversion to range": (
        "a fresh {d} BOS would be actual structural evidence for the new "
        "direction — reverting to range on its own is weaker, just "
        "indecision, not confirmation either way"
    ),
}


def _refine_next_watch(raw_next_event, direction_label):
    """
    Applies _NEXT_EVENT_REFINEMENTS (see above) to the raw default-lookup
    text before it's shown as next_watch. Anything not in that table is
    returned completely unchanged — this is a targeted fix for two known
    ambiguous entries, not a general rewrite of the map's vocabulary.
    """
    if not raw_next_event:
        return raw_next_event
    template = _NEXT_EVENT_REFINEMENTS.get(raw_next_event)
    if template is None:
        return raw_next_event
    return template.format(d=(direction_label or "").lower())


def synthesize_market_understanding(relationships, current_condition):
    """
    PHASE 1 WRAPPER (per audit, 2026-08-27 — "Brain owns conclusions,
    not just borrows them" per the architecture chat with friend).
    Public entry point; the actual branch logic is unchanged, moved
    verbatim into _synthesize_market_understanding_branches() below.

    UPDATED (2026-09-16, per chat — friend's audit, traced to a live
    Market Event push): every branch in _synthesize_market_
    understanding_branches() now sets next_watch EXPLICITLY. The
    fallback below — filling from current_condition["default_next_
    event"] — is kept ONLY as a defensive safety net for a future branch
    that forgets to set one; as of this fix it should never actually
    fire, and if it does, that's worth investigating rather than
    trusting silently.

    WHY THE FALLBACK WAS REMOVED AS THE ACTIVE PATH: default_next_event
    is keyed off market_phase/transition_cause — the SEPARATE MarketPhase
    engine's own categorization (worldstate.py's EXPECTED_NEXT_EVENT_MAP)
    — not off the countertrend/is_aging/leg_context combination this
    file's branches actually reason from. Nothing structurally guaranteed
    those two systems ever agreed. They didn't, for exactly the branch
    the friend's audit caught: HTF bearish, LTF countertrend, leg not
    aging — primary_thesis correctly said "the bearish thesis remains
    intact," while the borrowed default_next_event said "Follow-through
    BOS confirming the new direction," silently reframing the whole
    message around the bullish counter-move being the thing to confirm.
    Both fields were true statements in isolation; only one of them
    matched what the rest of the SAME message had just said. Every
    branch now writes its own next_watch, in its own words, so it can
    never disagree with its own primary_thesis again — a coincidence of
    two separate systems is no longer relied on for coherence within a
    single message.

    THIS IS WHY thesis_expected_next_event IS NO LONGER READ ANYWHERE IN
    THIS FILE (see format_understanding_narrative() below — it used to
    fall back to it, that fallback is now removed). Every branch has an
    explicit, Brain-decided answer for "what's next" — never a second
    opinion borrowed from a different subsystem's categorization.
    """
    result = _synthesize_market_understanding_branches(relationships, current_condition)
    if result is None:
        return None
    if "next_watch" not in result:
        # DEFENSIVE ONLY (see docstring above) — should not fire today.
        print("[BRAIN WARNING] a branch reached synthesize_market_understanding() "
              "without an explicit next_watch; falling back to default_next_event. "
              "This path is meant to be dead — investigate which branch this was.")
        htf_bias = (current_condition or {}).get("macro_bias")
        direction_label = htf_bias.title() if htf_bias in ("BULLISH", "BEARISH") else None
        result["next_watch"] = _refine_next_watch(
            (current_condition or {}).get("default_next_event"), direction_label
        )
    return result


def build_market_logic(market_assessment, timeframe_conflict, leg_maturity):
    """
    LAYER B — the "why" companion to synthesize_market_understanding()'s
    "what". Takes that function's own output plus the two Layer A facts
    it was built from, and re-describes the relationship between them —
    it does NOT touch thesis.evidence/thesis.weaknesses (see module
    docstring's PHASE 4 note for why that was explicitly ruled out
    mid-design). Evidence stays raw and available on-request; this
    function's job is narrower than "explain the evidence."

    Still templated, not freeform — branches keyed on market_assessment
    ["state"], the same closed vocabulary synthesize_market_understanding()
    already produces. Language is relational ("while", "remains",
    "is already") — never causal ("because", "X caused Y"). Returns None
    if market_assessment is None (nothing to reason about yet).
    """
    if market_assessment is None:
        return None

    state = market_assessment.get("state")
    direction_label = state.split("_")[0].title() if state and "_" in state else None

    tf = timeframe_conflict or {}
    lm = leg_maturity or {}
    m5_direction = (tf.get("m5_direction") or "?").title()
    aging_reason = lm.get("aging_reason")
    break_count = lm.get("break_count")
    dist_in_atr = lm.get("dist_in_atr")

    if state == "structural_transition_confirmed":
        return ("The reclaim of the prior leg's origin is treated as the dominant fact "
                "here — the higher-degree transition is established, not provisional, so "
                "lower-timeframe or leg-age texture doesn't change the read at this level.")

    if state == "structural_transition_developing":
        return ("The current move is developing against the broader structure, but the "
                "level that would make it structurally meaningful hasn't been reclaimed "
                "yet — until then this qualifies as pressure, not a confirmed shift.")

    if state and state.endswith("_continuation_under_pressure"):
        if aging_reason:
            age_txt = f"already aging ({aging_reason}" + (f", {break_count} break(s))" if break_count is not None else ")")
        else:
            age_txt = "already aging"
        return (f"The broader {direction_label} structure remains intact, while the lower "
                f"timeframe ({m5_direction}) is currently opposing it and the leg is "
                f"{age_txt}. That combination weakens the immediate continuation case "
                f"without invalidating the broader structure.")

    if state and state.endswith("_continuation_early_pressure"):
        return (f"The broader {direction_label} structure remains intact, and the leg "
                f"isn't yet mature. The lower timeframe ({m5_direction}) has only just "
                f"turned against the higher-timeframe direction, so this doesn't yet "
                f"distinguish genuine early opposition from routine lower-timeframe noise.")

    if state and state.endswith("_continuation_maturing"):
        detail = f"{break_count} break(s)" if break_count is not None else "no break count available"
        if dist_in_atr is not None:
            detail += f", {dist_in_atr:.1f} ATR from EMA"
        return (f"The lower timeframe isn't opposing the higher-timeframe direction, but "
                f"the leg's age ({detail}) is already the more relevant qualifier on the "
                f"{direction_label} thesis than anything cross-timeframe.")

    if state and state.endswith("_continuation_clean"):
        return (f"Both the lower timeframe and the leg's age are currently consistent "
                f"with the {direction_label} direction — nothing in the relationships "
                f"Brain has available qualifies the thesis yet.")

    if state and state.endswith("_bias_only"):
        return (f"There isn't yet a lower-timeframe read or leg-age signal to relate to "
                f"the {direction_label} bias, so nothing beyond the bias itself can be said.")

    return None


# ---------------------------------------------------------------------
# MARKET EVENT DEPENDENCY REPAIR (post-audit). These five definitions were
# copied VERBATIM from the original brain.py (byte-identical block); the
# migrated evaluate_market_event()/_state_family() referenced them but the
# copy never brought them along, so every call raised NameError, swallowed
# by the caller's try/except. Authority: _STATE_FAMILY_SUFFIXES decides what
# counts as a state-family change (significance gate); the LABELS/TEMPLATES
# pools only word an event once that gate has fired. Not imported from
# brain.py: the trimmed brain does not carry them, and Brain is not the
# authority for event significance.
# ---------------------------------------------------------------------
_STATE_FAMILY_SUFFIXES = {
    "structural_transition_confirmed": "transition_confirmed",
    "structural_transition_developing": "transition_developing",
    "continuation_under_pressure": "pressure",
    "continuation_early_pressure": "pressure",
    "continuation_maturing_reaccelerating": "maturing",
    "continuation_maturing": "maturing",
    "continuation_clean": "clean",
    "bias_only": "bias_only",
}

_STATE_FAMILY_LABELS = {
    # WORDING FIX (2026-09-11, per chat — "the wording in general is just
    # not good... 'an aging maturing leg'... no variety"). Each family now
    # has multiple equivalent noun phrases instead of one fixed string,
    # chosen randomly at render time — same variety principle already
    # applied to _STATE_TRANSITION_TEMPLATES above, just one level down
    # (the SLOT filled into those templates, not just the sentence shell
    # around it). "an aging, maturing leg" specifically was the literal
    # complaint — replaced, not just supplemented, since the phrase itself
    # was the problem, not only its repetition.
    "transition_confirmed": ["a confirmed structural transition"],
    "transition_developing": ["a developing structural transition"],
    "pressure": [
        "the leg coming under opposing pressure",
        "opposing pressure building against the leg",
        "the leg starting to meet real resistance from the other side",
    ],
    "maturing": [
        "a leg that's been running a while and is starting to show its age",
        "a leg well past its opening moves, now looking stretched",
        "a leg that's lost some of its earlier momentum",
    ],
    "clean": ["a clean, unclouded continuation"],
    "bias_only": ["not enough data yet for a fuller read"],
}


def _state_family_label(family):
    """Random.choice over _STATE_FAMILY_LABELS' variants for one family,
    falling back to the raw family name unchanged if it isn't in the
    table (same "never fabricate, never crash" discipline as every other
    lookup in this file)."""
    variants = _STATE_FAMILY_LABELS.get(family)
    return random.choice(variants) if variants else family

# ADDED (2026-09-03, per user feedback on live output). Phrasing variety
# ONLY for the "state" category headline — chosen at random each time an
# event fires. Every template must read correctly with ANY pair of
# _STATE_FAMILY_LABELS values dropped into {prev}/{curr}; if you add a
# label above with unusual grammar, sanity-check it against all of these.
# This is presentation only — it does not change WHETHER an event fires,
# only what sentence reports it once evaluate_market_event() has already
# decided it should.
_STATE_TRANSITION_TEMPLATES = [
    "The market's condition has shifted from {prev} to {curr}.",
    "Condition change: {prev} is giving way to {curr}.",
    "What's changed — {prev} has moved into {curr}.",
    "The read on the market just moved: from {prev} to {curr}.",
]

# WORDING FIX (2026-09-11, per chat — "opportunity landscape" wording,
# same variety treatment as the state-category templates above.
_NEW_OPPORTUNITY_TEMPLATES = [
    "A new spot worth watching just came into range: {zones}",
    "Something new to watch for: {zones}",
    "Price is now within reach of a new area worth watching: {zones}",
]


def _state_family(state_label):
    """
    Coarse grouping of market_assessment["state"] labels (see
    synthesize_market_understanding()'s branches) into the handful of
    "kinds of condition" a human would actually distinguish. Used ONLY
    to decide whether a state change is a State Event — Brain's actual
    state label is unchanged and still used everywhere else as-is; this
    is purely a significance filter, per Vally's "a delta is not
    automatically an event" principle.
    """
    if not state_label:
        return None
    for suffix, family in _STATE_FAMILY_SUFFIXES.items():
        if state_label == suffix or state_label.endswith("_" + suffix):
            return family
    return state_label  # unrecognized label — treat as its own family (safe default: always counts as a change, never silently ignored)


def build_event_snapshot(understanding, intent_hypothesis, leg_obs_open=None):
    """
    Defines exactly what gets persisted after each scan for the NEXT
    scan's Market Event comparison (see evaluate_market_event() below).
    ADDED (2026-08-27, per chat — Market Event layer, design credited to
    Vally). Deliberately tiny and flat: this is a comparison key, not a
    second copy of the Understanding — anything more would risk drifting
    into a second interpretation store, which is the exact thing Phase 0
    /​1 removed. Persist under a NEW state key (market_event_prev_snapshot)
    — does not touch, rename, or shadow any market_thesis_*/market_intent_*
    field EXP7 or delta-tracking depend on.

    `leg_obs_open` (2026-08-27, per chat — closing the Tier 3 gap flagged
    last pass): the CURRENTLY OPEN Forward Observation record (obs_state
    ["open"], read-only peek from min_scanner.py's leg_obs store — see
    _evaluate_and_push_market_event() in scanner_live.py), or None if no
    leg is currently open. This is NOT a new detection mechanism — Tier
    3's actual live-fire logic (_tier3_structure_evaluate(), 15M CHoCH)
    already runs elsewhere and already persists its result onto this
    exact record as tier3_touched_bar (see _update_zone_touches() in
    min_scanner.py). This function only reads that already-computed
    fact, the same way it already reads Brain's already-computed
    market_assessment/intent_hypothesis for everything else here.
    """
    ma = (understanding or {}).get("market_assessment") or {}
    cc = (understanding or {}).get("current_condition") or {}
    locations = {e.get("code") for e in (intent_hypothesis or {}).get("locations", []) if e.get("code")}
    confirmations = {e.get("code") for e in (intent_hypothesis or {}).get("confirmations", []) if e.get("code")}
    return {
        "state": ma.get("state"),
        "structural_status": ma.get("structural_status"),
        "macro_bias": cc.get("macro_bias"),
        "location_codes": sorted(locations),
        "confirmation_codes": sorted(confirmations),
        "tier3_touched": bool(leg_obs_open and leg_obs_open.get("tier3_touched_bar") is not None),
    }


def evaluate_market_event(market_read, event_context):
    """
    2D BOUNDARY (this pass, per chat): public signature is now
    (market_read, event_context) — MarketRead is the current-scan
    interpretation authority; EventContext carries the historical/
    lifecycle data that is explicitly NOT part of MarketRead (frozen
    decision: EventContext stays separate, is never folded into
    MarketRead's schema). Unpacked at the top of the function body into
    the same local names this function already used — every line past
    the unpacking is UNCHANGED from before this pass; only the public
    boundary moved, not the internal logic.

    event_context is a plain dict with exactly four keys:
        prev_snapshot            - state["market_event_prev_snapshot"]
        leg_obs_open              - load_leg_obs_state()["open"]
        mil_understanding_current - state["mil_understanding"], READ
                                     AFTER this scan's MIL reconcile —
                                     deliberately NOT the same value as
                                     market_read["prior_intent_state"]
                                     (which is the PRE-reconcile value).
                                     Do not alias these two.
        last_narrated_situation   - state["last_narrated_situation"]

    ═══════════════════════════════════════════════════════════════════
    HUMAN-FACING NARRATION PIPELINE — MAP FOR EDITORS (added 2026-09-15,
    deep-clean pass; this docstring documents everything downstream of
    this function too, since nothing else in the codebase draws the
    whole picture in one place). If you're editing anything in the
    market-event/narration path, read this first.

        WorldState (worldstate.py)
            |
            v
        build_market_understanding() / build_market_intent_hypothesis()
        [brain.py, above this function]
            produces `understanding` (current_condition = raw levels;
            market_assessment = BRAIN'S OWN SYNTHESIS per situation —
            state / primary_thesis / primary_threat / next_watch /
            confirmation_needed / alternative, hand-written per branch
            in _synthesize_market_understanding_branches() — this is
            already a fully-formed "market story," not a gap to fill
            downstream) and `intent_hypothesis` (Tier 1/2/3 locations,
            each with a human-facing "sentence" already built in
            scanner_observation.py).
            |
            v
        evaluate_market_event()  <- YOU ARE HERE
            THE RAW SIGNIFICANCE GATE. Decides IF a delta even
            candidates for narration (structure/state/opportunity +
            a plain headline). Does NOT decide impact, does NOT decide
            wording, does NOT know about "story impact" or situation
            identity — those are explicitly NOT this function's job,
            by design (see this function's own docstring below for why
            this stays a thin filter, not a growing pile of `if`s).
            |
            v
        market_story.py (Story Interpreter — a SEPARATE module,
        deliberately not folded into this file — see its own module
        docstring for why)
            classify_events()       -> impact tag: SUPPORT / CHALLENGE /
                                        BREAK / OPPORTUNITY / SURPRISE
                                        (push-worthy) vs BACKGROUND /
                                        IGNORE (logged only, never sent)
            is_repeat_situation()   -> situation-identity dedup, keyed
                                        off market_assessment.state —
                                        Brain's own canonical per-
                                        situation string, reused as-is,
                                        not re-derived
            build_mechanism()       -> the "what objectively happened"
                                        clause (the missing "A" before
                                        Brain's "B" conclusion)
            build_market_story()    -> ALL narratable facts, gathered
                                        once — Brain's synthesis plus
                                        structured pass-throughs of
                                        MIL's counter_thesis/failure_
                                        condition (as dicts, not
                                        sentences — the Library below
                                        renders them). This is the ONE
                                        object every downstream surface
                                        reads; nothing past this point
                                        reaches into understanding/
                                        mil_understanding/intent_
                                        hypothesis directly again.
            NEITHER this function nor market_story.py ever reads
            thesis_weaknesses(_prose) or MIL's conflicting_evidence for
            anything human-facing — both are internal-diagnostic risk-
            scoring content (ob_mitigated / atr_floor / ema_extension /
            leg_break_count / bias_stale), banned from narration
            regardless of prose quality (see hfis.py's _weave_evidence()
            docstring, which had exactly this leak until 2026-09-15).
            |
            v
        narration_library.py (the Library — ADDED 2026-09-16, per chat,
        the architecture redesign. A SEPARATE module from hfis.py,
        mutually forbidden from importing it — see check_layer_imports.py)
            Pure, DETERMINISTIC (no random.choice()) functions, each
            taking ONLY the story dict: light_summary(), deep_read(),
            recap_line(), elapsed_label(). Each function's own docstring
            lists exactly which story fields it reads — the actual fix
            for "one composer duplicating another's content without
            either of them knowing it was happening," which was the
            root cause of every duplicate-message bug found this week.
            See this module's own docstring for the full rationale.
            |
            v
        hfis.py (the Narrator — for the LIVE event push and /thesis's
        deep view ONLY now; scanner_live.py is the only caller permitted
        to import it — see check_layer_imports.py)
            narrate()             -> the live event push. "state"
                                      events route through _compose_
                                      state_story() (Brain's own
                                      primary_thesis/primary_threat/
                                      next_watch — NOT a generic
                                      template); "structure" through
                                      _compose_structure_flip();
                                      "opportunity" through _compose_
                                      opportunity(). Combinatorial
                                      phrase-bank variety is deliberately
                                      kept HERE ONLY — this is the one
                                      surface where that was ever the
                                      actual ask.
            narrate_deep_thesis() -> /thesis's narrated deep view.
                                      RETIRED from the 9/12/3 overview
                                      (2026-09-16 — see narration_
                                      library.deep_read(), its plain-
                                      labeled replacement there) but
                                      still this command's own renderer.
            |
            v
        scanner_live.py
            THE COMPOSITION LAYER (2026-09-16, per chat: "its only job
            is choosing order, never inventing text"). _build_and_send_
            overview() builds the story dict once, calls narration_
            library's three functions directly, and joins the results
            itself — no more calling into a hfis.py assembler function
            for this push. Also owns Telegram send and the relevant
            state.json keys (market_event_prev_snapshot, last_narrated_
            situation, overview_pending_events).


    A SEPARATE RUNTIME: min_scanner.py runs as its own GitHub Actions
    job and is architecturally forbidden from importing hfis.py,
    market_story.py, or narration_library.py (check_layer_imports.py
    enforces this). Its leg-resolution follow-up (_close_leg_obs() ->
    _compose_resolution_text()) is therefore a small LOCAL composer, not
    routed through this pipeline — not an oversight, a runtime boundary.
    ═══════════════════════════════════════════════════════════════════

    Market Event layer (per chat, 2026-08-27 — design credited to Vally:
    "a delta is not automatically an event. A delta is 'something
    changed.' An event is 'something changed enough that the assistant
    should interrupt you.'"). This is a THIN significance filter sitting
    on top of Brain's ALREADY-SYNTHESIZED outputs (market_assessment,
    intent_hypothesis) — it does not re-derive anything from raw atoms
    and does not add a new interpretation engine. Per the architecture
    principle this whole week has been building toward: Brain is the
    only place that interprets the market; this function only asks
    whether what Brain already concluded changed enough to matter.

    Returns a list of 0+ event dicts: {"category": "structure"|"state"|
    "opportunity", "headline": <plain-English one-liner>}. An empty list
    is the normal, expected result on most scans — most scans don't
    produce an event, same as most scans don't produce a signal.

    THREE CATEGORIES (Vally's taxonomy):
      - structure: the higher-degree structure itself changed — a bias
        flip, or a previously-unconfirmed transition becoming confirmed.
        Checked first and suppresses a same-scan state event for the
        same underlying change (avoid saying the same thing twice).
      - state: the market's condition/phase changed enough to cross into
        a different _state_family() (see above) — e.g. a clean
        continuation becoming pressured, or an aging leg reaccelerating.
      - opportunity: a new location or confirmation entered Brain's
        intent_hypothesis that wasn't there last scan (tiers 1/2 — see
        build_market_intent_hypothesis() above), OR Tier 3's structural
        condition (15M CHoCH aligned with HTF bias) was reached on the
        current leg — read from `leg_obs_open` since Tier 3 has no
        location to flow through intent_hypothesis at all (see the
        TIER 3 block below). Cautions appearing/disappearing are
        deliberately NOT event-worthy on their own — lower-signal than
        something becoming relevant.

    `leg_obs_open` (2026-08-27, per chat): the currently-open Forward
    Observation record, or None — see build_event_snapshot()'s docstring
    for exactly where this comes from and why it's read-only here too.

    `mil_understanding` (per chat, physiology work): MIL's persisted
    MarketUnderstanding dict (state.get("mil_understanding")), or None.
    Read-only, same discipline as leg_obs_open — this function still
    does not interpret anything itself. Used ONLY to attach MIL's own
    reconciliation text onto a structure event when one fires this same
    scan (MIL's reconcile() runs earlier in this same pass, per the
    insertion-point trace in mil.py's header, so its freshest
    ThesisTransition corresponds to whatever just happened). This is
    what answers "was the old thesis's invalidation actually breached,
    and if not, why did direction change anyway" directly in the event
    itself, instead of requiring a separate lookup.

    PRICE LEVELS (per chat — the original gap this revision closes):
    headlines previously stated WHAT changed with zero price detail
    ("flipped from bearish to bullish"). current_condition now carries
    macro_leg_origin/macro_leg_extreme/prior_macro_leg_origin (added to
    build_market_understanding() above — pure plumbing, WorldState
    already had these numbers). Headlines below use them; nothing new
    was detected to produce them.

    NOT A PREDICTION — the single most important rule in this function
    (per chat). Headlines state WHAT CHANGED, in Brain's own already-
    used vocabulary, never what's likely to happen next. "Bullish
    structure has flipped to bearish" is fine. "Bearish continuation
    likely" is not, and must never be added here — "what this means"
    belongs in the Market Understanding narrative pushed alongside each
    event (see format_market_event() below), not in the headline itself.

    prev_snapshot is None on the very first scan after this feature
    ships, or after a fresh state.json — returns [] rather than
    fabricating a comparison against nothing, same discipline as
    classify_thesis_delta() (scanner_observation.py) and every other
    delta-style function in this codebase.
    """
    understanding = market_read
    intent_hypothesis = (market_read or {}).get("intent_hypothesis")
    event_context = event_context or {}
    prev_snapshot = event_context.get("prev_snapshot")
    leg_obs_open = event_context.get("leg_obs_open")
    mil_understanding = event_context.get("mil_understanding_current")

    events = []
    ma = (understanding or {}).get("market_assessment")
    if not ma or not prev_snapshot:
        return events

    cc = (understanding or {}).get("current_condition") or {}
    structure_fired = False

    prev_bias = prev_snapshot.get("macro_bias")
    curr_bias = cc.get("macro_bias")
    if prev_bias and curr_bias and prev_bias != curr_bias:
        # Price-level detail (per chat) — the level the new leg formed
        # from, where it's reached so far, and the OLD leg's origin
        # (the level whose clean-close-through would have been genuine
        # invalidation) so the reader can see for themselves whether it
        # was actually touched.
        _new_origin = cc.get("macro_leg_origin")
        _new_extreme = cc.get("macro_leg_extreme")
        _old_origin = cc.get("prior_macro_leg_origin")
        _level_detail = ""
        if _new_origin is not None and _new_extreme is not None:
            _level_detail = f" New leg from {_new_origin:.5f}, currently at {_new_extreme:.5f}."
        if _old_origin is not None:
            _level_detail += f" Prior {prev_bias.lower()} leg's origin: {_old_origin:.5f}."
        # MIL reconciliation (per chat) — was the prior thesis's stated
        # failure condition actually breached, or did something else
        # justify the change? mil_understanding.history[-1] is this
        # scan's own transition record if reconcile() fired one this
        # pass (see mil.py's insertion-point trace — it runs earlier in
        # this same scan). Deliberately does not re-derive or second-
        # guess this — MIL is the sole authority for the reconciliation
        # itself, this only surfaces what it already concluded.
        _mil_detail = ""
        _mil_history = (mil_understanding or {}).get("history") or []
        if _mil_history:
            _last = _mil_history[-1]
            if _last.get("invalidation_fired"):
                _mil_detail = f" Invalidation confirmed ({_last.get('invalidation_mechanism')})."
            elif _last.get("reconciliation"):
                _mil_detail = f" {_last['reconciliation']}"
        events.append({
            "category": "structure",
            # E1: explicit semantic identity of this structure event, set from the
            # branch that fired it. Classification reads THIS, never the headline.
            "structure_kind": "bias_flip",
            "headline": (
                f"Higher-timeframe structure has flipped from "
                f"{prev_bias.lower()} to {curr_bias.lower()}."
                f"{_level_detail}{_mil_detail}"
            ),
        })
        structure_fired = True
    elif (ma.get("structural_status") == "confirmed_transition"
          and prev_snapshot.get("structural_status") != "confirmed_transition"):
        _test_level = cc.get("prior_macro_leg_origin")
        _level_detail = f" Prior leg's origin ({_test_level:.5f}) has been reclaimed." if _test_level is not None else ""
        events.append({
            "category": "structure",
            "structure_kind": "confirmed_transition",
            "headline": (
                "A structural transition has been confirmed — the "
                "broader leg has reclaimed the prior leg's origin."
                f"{_level_detail}"
            ),
        })
        structure_fired = True

    if not structure_fired:
        prev_family = _state_family(prev_snapshot.get("state"))
        curr_family = _state_family(ma.get("state"))
        # SUPPRESSION + VARIETY (2026-09-03, per user feedback on live
        # output: two consecutive pushes both leaned on "not enough data
        # yet for a fuller read" and both used the identical sentence
        # shape). Two fixes, kept narrow:
        #   1. A transition INTO bias_only is a loss of information, not
        #      a new fact — applying Vally's "a delta is not automatically
        #      an event" principle to the state category itself.
        #   2. The headline sentence is now chosen from a small template
        #      pool instead of one fixed f-string. Pure presentation —
        #      no change to what counts as a change, no new detection.
        #
        # REVISED (2026-09-15, per chat — friend's second review, flagged
        # this exact transition ("not enough data yet for a fuller read"
        # -> "a clean, unclouded continuation") as the flagship example of
        # narrating a SYSTEM state (data sufficiency) as if it were a
        # MARKET development, and it was observed firing three times in a
        # row. Traced: this file's own docstring above ("oscillating near
        # a boundary... keeps... from silently skipping a real change")
        # confirms flapping across a family boundary is EXPECTED to
        # re-fire, by design, for genuine market regimes (e.g.
        # clean<->pressure) — that's correct there. bias_only isn't a
        # market regime, though; it's "the leg hasn't run long enough to
        # classify yet," a data-sufficiency threshold. Oscillating across
        # THAT boundary during leg formation is measurement noise, not a
        # market development, and re-firing the identical sentence each
        # time it wobbles is exactly what got reported as duplicate spam.
        # This REVERSES the 2026-09-03 decision that a transition OUT of
        # bias_only "is still genuinely informative and still fires" —
        # deliberately, not silently: flagging it here in case Nexus
        # wants that one-time "we now have a read" signal back in some
        # other form (e.g. a single BACKGROUND-only log line, never
        # pushed) rather than as a "state" market event at all.
        if (prev_family and curr_family and prev_family != curr_family
                and curr_family != "bias_only" and prev_family != "bias_only"):
            template = random.choice(_STATE_TRANSITION_TEMPLATES)
            events.append({
                "category": "state",
                # PASS A: additive identity fields (never read by anything
                # except event_identity() below) -- prev_family/curr_family
                # are exactly the two values that decided this event fired,
                # already computed above. headline stays presentation-only.
                "prev_family": prev_family,
                "current_family": curr_family,
                "headline": template.format(
                    prev=_state_family_label(prev_family),
                    curr=_state_family_label(curr_family),
                ),
            })

    curr_locations = {e.get("code") for e in (intent_hypothesis or {}).get("locations", []) if e.get("code")}
    curr_confirmations = {e.get("code") for e in (intent_hypothesis or {}).get("confirmations", []) if e.get("code")}
    new_location_codes = curr_locations - set(prev_snapshot.get("location_codes") or [])
    new_confirmations = curr_confirmations - set(prev_snapshot.get("confirmation_codes") or [])
    if new_location_codes or new_confirmations:
        # Price-level detail (per chat): the raw location entries (with
        # zone_low/zone_high) already exist in intent_hypothesis — this
        # was being thrown away in favor of a generic phrase. Pure
        # surfacing, same discipline as the structure event above.
        #
        # WORDING FIX (2026-09-11, per chat): "opportunity landscape" was
        # both jargon-sounding and never varied — same fix pattern as the
        # state-category headline above (small template pool, chosen
        # randomly). The per-entry sentence itself (built in
        # scanner_observation.build_market_intent()) now states direction
        # and what would count as a reaction for the fib-pocket case that
        # was specifically flagged as missing it — this only varies the
        # wrapper sentence introducing whichever entries fired.
        _new_location_entries = [
            e for e in (intent_hypothesis or {}).get("locations", [])
            if e.get("code") in new_location_codes
        ]
        if _new_location_entries:
            _zones = "; ".join(
                f"{e.get('sentence', e.get('code'))} ({e['zone_low']:.5f}-{e['zone_high']:.5f})"
                if e.get("zone_low") is not None and e.get("zone_high") is not None
                else e.get("sentence", e.get("code"))
                for e in _new_location_entries
            )
            headline = random.choice(_NEW_OPPORTUNITY_TEMPLATES).format(zones=_zones)
        else:
            headline = "A new area worth watching has come into range, but nothing specific to react to yet."
        # NEW (additive only, per chat): preserve the identity that
        # new_location_codes/_new_location_entries already computed above
        # to decide whether this event fires at all. If more than one new
        # location fired in the same scan, the first is stored — the
        # headline already batches all of them via "; ".join above
        # (unchanged); this does not change which locations are included
        # in the headline, only which one's code survives for downstream
        # identity. No firing condition, category, or headline logic
        # changed.
        events.append({
            "category": "opportunity",
            "headline": headline,
            "code": _new_location_entries[0].get("code") if _new_location_entries else None,
        })

    # TIER 3 (2026-08-27, per chat — closes the gap flagged last pass).
    # Tier 3 has no "watching" zone the way tiers 1/2 do (see
    # _tier3_structure_evaluate()'s own docstring in scanner_observation.py
    # — it fires atomically on a 15M CHoCH, nothing to sit in a location
    # for), so it was never going to show up via the locations/
    # confirmations diff above. Read straight off the currently-open
    # Forward Observation record instead — tier3_touched_bar is already
    # computed there every pass by _update_zone_touches(), this only
    # reads it. Rising-edge only (tier3_touched flips False->True),
    # matching the same rising-edge discipline _update_leg_timeline()
    # itself already uses for this exact field.
    curr_tier3 = bool(leg_obs_open and leg_obs_open.get("tier3_touched_bar") is not None)
    if curr_tier3 and not prev_snapshot.get("tier3_touched"):
        events.append({
            "category": "opportunity",
            # PASS A: Tier 3 previously had no "code" at all, unlike the
            # location/confirmation opportunity event below -- gave it a
            # stable, headline-independent identity, distinct from a real
            # location/confirmation code (those come from intent_hypothesis
            # entries) and distinct from the None a no-entry opportunity
            # event carries. Not a location/confirmation code, so it can
            # never collide with one.
            "code": "TIER_3",
            "headline": (
                "Tier 3 structural condition has been reached on the "
                "current leg (15-minute CHoCH aligned with the higher-"
                "timeframe bias)."
            ),
        })

    return events



def event_identity(event):
    """
    PASS A (Structured Event Identity + Debounce). A stable identity for
    an event, built ONLY from the structured fields that decided it fired
    -- never from `headline`, which stays presentation-only (the E1 rule:
    headline is output, never an input to meaning, classification,
    gating, or identity -- identity is meaning too).

    Why this exists: the live debounce in scanner_live.py used to compare
    category + rendered headline to catch a same-event-back-to-back
    re-fire. State and opportunity headlines are drawn via random.choice
    over a wording pool, so two renders of the IDENTICAL event often
    don't match as text (measured: ~13% match rate for state, ~34% for
    opportunity, back-to-back) -- the debounce was firing far less than
    it looked like it should. Structure headlines are deterministic
    strings, so they never had this problem.

    category-specific identity, matching exactly what each event
    construction site already used to decide whether to fire:
      structure   -> structure_kind (bias_flip / confirmed_transition)
      state       -> (prev_family, current_family) -- the two family
                     values compared to decide this was a transition
      opportunity -> code (a location/confirmation code, "TIER_3", or
                     None for the no-specific-entry case -- matching
                     the identity already used to decide THIS particular
                     branch fired, not a global uniqueness guarantee)
    An unrecognized category falls back to headline (old behavior),
    rather than crash or silently collapse every such event together.
    """
    cat = event.get("category")
    if cat == "structure":
        return (cat, event.get("structure_kind"))
    if cat == "state":
        return (cat, event.get("prev_family"), event.get("current_family"))
    if cat == "opportunity":
        return (cat, event.get("code"))
    return (cat, event.get("headline"))


class StoryImpact(str, Enum):
    IGNORE = "ignore"
    BACKGROUND = "background"
    SUPPORT = "support"
    CHALLENGE = "challenge"
    BREAK = "break"
    OPPORTUNITY = "opportunity"
    SURPRISE = "surprise"


PUSH_WORTHY = {
    StoryImpact.SUPPORT, StoryImpact.CHALLENGE, StoryImpact.BREAK,
    StoryImpact.OPPORTUNITY, StoryImpact.SURPRISE,
}
_PUSH_WORTHY_VALUES = {i.value for i in PUSH_WORTHY}


# Coarse ranking of _state_family() buckets — used only to decide
# whether a state-family transition is getting BETTER (for the current
# bias's continuation story) or WORSE, never to change what counts as
# a transition in the first place (that's still evaluate_market_event()
# /  _state_family() in brain.py, untouched). Unrecognized families
# (raw state label passed through unchanged — see _state_family()'s own
# "safe default" comment) rank neutral, same value as the deliberately
# lateral pair (pressure/maturing) — an unknown family should not be
# assumed better or worse than what it replaced.
_FAMILY_RANK = {
    "clean": 2,
    "transition_confirmed": 2,
    "transition_developing": 1.5,
    "pressure": 1,
    "maturing": 1,
}
_NEUTRAL_RANK = 1


def _classify_state(prev_state_label, curr_state_label):
    prev_family = _state_family(prev_state_label)
    curr_family = _state_family(curr_state_label)
    prev_rank = _FAMILY_RANK.get(prev_family, _NEUTRAL_RANK)
    curr_rank = _FAMILY_RANK.get(curr_family, _NEUTRAL_RANK)
    if curr_rank > prev_rank:
        return StoryImpact.SUPPORT
    if curr_rank < prev_rank:
        return StoryImpact.CHALLENGE
    # Same rank, different family (e.g. pressure <-> maturing) — a real,
    # named change, but not clearly a strengthening or weakening of the
    # continuation story. Per chat: "if the larger bearish structure is
    # intact, [this] may simply be noise" — BACKGROUND, not a push.
    return StoryImpact.BACKGROUND


def _classify_one(event, understanding, mil_understanding, prev_snapshot):
    category = event.get("category")
    # RULE (E1): the event's headline is OUTPUT. It is never read here -- impact is
    # decided from structured event identity (category, structure_kind) and MIL's
    # structured history only. Do not reintroduce any headline/text inspection.
    mu = mil_understanding or {}
    history = mu.get("history") or []
    latest = history[-1] if history else None

    if category == "structure":
        # A bias flip backed by MIL's own confirmed invalidation is a
        # clean BREAK. A bias flip that fired WITHOUT invalidation
        # having been confirmed is the more unusual case (direction
        # changed for a reason the old thesis didn't name in advance)
        # — SURPRISE, not BREAK, so the distinction is visible in the
        # push rather than flattened into one generic "structure event."
        # A confirmed/reclaimed transition is the expected next step of
        # an already-flagged developing transition — SUPPORT (for the
        # NEW story it confirms), not a bias change in itself.
        kind = event.get("structure_kind")
        if kind == "confirmed_transition":
            return StoryImpact.SUPPORT
        if kind == "bias_flip":
            if latest and latest.get("invalidation_fired"):
                return StoryImpact.BREAK
            return StoryImpact.SURPRISE
        # A structure event with no/unknown structure_kind cannot be classified from
        # text (by design). Fail open like any unrecognized category below.
        return StoryImpact.CHALLENGE

    if category == "state":
        ma = (understanding or {}).get("market_assessment") or {}
        return _classify_state(
            (prev_snapshot or {}).get("state"), ma.get("state"))

    if category == "opportunity":
        return StoryImpact.OPPORTUNITY

    # Any category this module doesn't recognize yet — fail open rather
    # than silently drop something evaluate_market_event() already
    # decided was significant enough to return.
    return StoryImpact.CHALLENGE


def classify_events(events, understanding=None, mil_understanding=None, prev_snapshot=None):
    """
    Returns a NEW list — same event dicts, each with a "story_impact"
    key added (a StoryImpact string value). Order preserved, nothing
    dropped here — filtering to what's push-worthy is a separate,
    explicit step (see push_worthy() below) so a caller that wants to
    log/shadow the full set still can.
    """
    if not events:
        return []
    out = []
    for ev in events:
        try:
            impact = _classify_one(ev, understanding, mil_understanding, prev_snapshot)
        except Exception as e:
            print("[MARKET STORY CLASSIFY ERROR] " + str(e))
            impact = StoryImpact.CHALLENGE
        enriched = dict(ev)
        enriched["story_impact"] = impact.value
        out.append(enriched)
    return out


def push_worthy(classified_events):
    """Filters classify_events()'s output down to what should reach
    Telegram. IGNORE/BACKGROUND events are dropped here, not upstream —
    they still exist in the full list for logging/shadow purposes."""
    return [e for e in (classified_events or []) if e.get("story_impact") in _PUSH_WORTHY_VALUES]


def build_mechanism(event, understanding, mil_understanding):
    """
    The "what objectively happened" clause — cited BEFORE the
    conclusion, never instead of it. Every source read here is already
    computed and already surfaced elsewhere in the codebase (current_
    condition, mil_understanding.history); this function adds no new
    detection and cites no number that isn't already sitting in one of
    those structures.

    UPDATED (2026-09-15, deep-clean pass): thesis_weaknesses_prose is
    NOT read here, and never has been correctly — an earlier version of
    this docstring still listed it as a source after the fallback that
    used it was removed. See the "state" branch below for why it's
    excluded outright, not just unused by accident.

    Returns "" when nothing concrete is available — callers (hfis.py)
    should treat that as "say nothing," never pad with a placeholder.
    """
    try:
        cc = (understanding or {}).get("current_condition") or {}
        mu = mil_understanding or {}
        history = mu.get("history") or []
        latest = history[-1] if history else None
        category = event.get("category")

        if category == "structure" and latest:
            if latest.get("invalidation_fired") and latest.get("invalidation_mechanism"):
                return f"Confirmed via {latest['invalidation_mechanism']}."
            if latest.get("reconciliation"):
                return latest["reconciliation"]

        # "state" category deliberately returns "" unconditionally
        # (2026-09-15, per chat — cleanup after wiring _compose_state_
        # story() into hfis.narrate()): a rejection-count mechanism used
        # to be built here, but narrate()'s own evidence-weave tail
        # (_struggle_phrase(), reading this same macro_leg_extreme_
        # rejection_count field) already states that exact fact once,
        # in its own established voice — adding it again here produced
        # a literal duplicate line ("2 failed attempts against X... X
        # has held on every single test so far"). And Brain's own
        # primary_thesis/primary_threat (now wired in via _compose_
        # state_story()) already carry the "what happened and why" for
        # a state transition — the missing mechanism clause this
        # function exists for was only ever needed while state events
        # fell through to the old generic template.
        return ""
    except Exception as e:
        print("[MARKET STORY MECHANISM ERROR] " + str(e))
        return ""


# ---------------------------------------------------------------------------
# Market Story construction — ADDED (2026-09-15, per chat, friend's third
# review: "market_story.py should be responsible for ONLY this: construct
# the current market narrative from authoritative state... then HFIS can
# turn it into language").
#
# THE ACTUAL FINDING THIS IS BUILT ON (per chat — worth stating plainly,
# since it changes what this fix needed to be): the friend's proposed
# Market Story schema — direction / current phase / what price has done /
# what price is doing now / why this matters / current evidence / current
# tension / what would change the story — ALREADY EXISTS, mostly written,
# inside brain.py's market_assessment (built by
# _synthesize_market_understanding_branches()). Every branch there already
# returns primary_thesis ("what's happening and why," hand-written per
# state), primary_threat ("why it matters" / the live tension),
# confirmation_needed + alternative (the two-sided "what would change
# this"), and next_watch (the single most decision-relevant thing to
# watch). None of this was ever missing — hfis.py simply never read it,
# building its own weaker generic sentences from raw current_condition
# levels instead (_STATE_TRANSITION_TEMPLATES's "shifted from {prev} to
# {curr}", still keyed off _state_family_label()'s internal-sounding
# family names). So build_market_story() below is NOT a new detector and
# NOT new synthesis — it is a thin, literal pass-through of fields Brain
# already computes, organized into the friend's exact schema, so hfis.py
# has one clear, correctly-scoped place to pull from instead of
# reinventing weaker versions of what Brain already wrote.
#
# WHAT THIS DELIBERATELY DOES NOT FIX: a few of Brain's own
# primary_thesis/primary_threat strings (e.g. the "continuation_maturing_
# reaccelerating" branch's "aging by break-count and EMA-distance")
# still name an internal metric the friend's ban list would flag. Those
# are hand-tuned, per-branch, iterated-on prose inside brain.py itself —
# rewriting them blind, without a real output to check each one against,
# risks the exact same "confidently wrong" failure this whole review
# started from. Flagged here explicitly rather than silently rewritten:
# worth a dedicated pass through brain.py's ~8 branches on their own,
# not bundled into this one.
# ---------------------------------------------------------------------------

def build_market_story(understanding, mil_understanding=None, intent_hypothesis=None):
    """
    Returns a dict with every fact the narration library (narration_
    library.py) needs to render any message this bot sends — the single
    object both the light summary and the deep read draw from, so a
    fact exists in exactly one place regardless of how many surfaces
    show it. Every value is either a direct field from market_assessment
    (Brain's own synthesis), a plain restatement of a current_condition
    level, or a structured pass-through of a MIL field — nothing here is
    generated text, and nothing here is internal-diagnostic content
    (weaknesses_prose is deliberately never read here — see
    build_mechanism()'s docstring above for why that bank is off-limits
    for anything human-facing).

    EXTENDED (2026-09-16, per chat — the library redesign: "something is
    grabbing from another file... to me it just feels messy"). Two
    fields added so narration_library.py never has to reach into
    mil_understanding itself: `counter_thesis` and `failure_condition`
    are now carried here as STRUCTURED data (dicts, not pre-rendered
    sentences) — this module's job is still only gathering and naming
    facts, never composing prose; narration_library.py does the
    rendering. Kept structured (not stringified) because the library
    needs the raw numbers to decide HOW to phrase them, same reason
    current_condition's levels are passed as floats, not as
    what_price_has_done's already-formatted sentence would need to be
    re-parsed to reuse.

    Never raises — any missing field is simply omitted (None), same
    "never fabricate, never crash" discipline as the rest of this
    codebase. Callers should treat a None field as "say nothing for
    this part," never a placeholder.
    """
    try:
        cc = (understanding or {}).get("current_condition") or {}
        ma = (understanding or {}).get("market_assessment") or {}
        mu = mil_understanding or {}
        origin, extreme = cc.get("macro_leg_origin"), cc.get("macro_leg_extreme")

        what_price_has_done = None
        if origin is not None and extreme is not None:
            direction_word = (cc.get("leg_direction") or cc.get("macro_bias") or "").lower()
            what_price_has_done = (
                f"The current {direction_word} leg has run from {origin:.5f} to {extreme:.5f}."
                if direction_word else f"The current leg has run from {origin:.5f} to {extreme:.5f}."
            )

        # Structured pass-throughs — narration_library.py renders these,
        # this function only names and gathers them. `None` when the
        # underlying MIL field is empty/unpopulated (mil.py's own
        # placeholders — a 0.0 failure-condition level, a None counter-
        # case direction — are normalized to None here so the library
        # never has to know MIL's specific placeholder conventions).
        fc = mu.get("failure_condition") or {}
        failure_condition = (
            {"level": fc.get("origin_level"), "direction": fc.get("origin_direction")}
            if fc.get("origin_level") else None
        )

        counter = mu.get("counter_thesis")
        if counter and counter.get("direction"):
            counter_thesis = {
                "kind": "confirmed_candidate",
                "direction": counter["direction"],
                "status": counter.get("status") or "awaiting_confirmation",
                "watching_for": counter.get("watching_for") or [],
            }
        else:
            ecc = mu.get("emerging_counter_case")
            if ecc and ecc.get("direction"):
                fizzled_before = any(
                    r.get("resolution") == "fizzled"
                    and (r.get("direction") or "").lower() == ecc["direction"].lower()
                    for r in (mu.get("counter_candidate_history") or [])[-5:]
                )
                counter_thesis = {
                    "kind": "emerging",
                    "direction": ecc["direction"],
                    "maturity": ecc.get("maturity"),
                    "range_pips": ecc.get("range_pips"),
                    "fizzled_before": fizzled_before,
                }
            else:
                counter_thesis = None

        return {
            "direction": cc.get("macro_bias"),
            "phase": _state_family(ma.get("state")),
            "what_price_has_done": what_price_has_done,
            # Brain's own synthesis — see this function's module-level
            # comment above for why these four are pass-throughs, not
            # new text.
            "what_price_is_doing_now": ma.get("primary_thesis"),
            "why_it_matters": ma.get("primary_threat"),
            "tension": ma.get("alternative"),
            "what_would_change_it": ma.get("next_watch") or ma.get("confirmation_needed"),
            "counter_thesis": counter_thesis,
            "failure_condition": failure_condition,
        }
    except Exception as e:
        print("[MARKET STORY BUILD ERROR] " + str(e))
        return {}


def situation_key(understanding):
    """
    The stable identity for "what situation is this." Per chat — the
    friend's "one event gets one narrative identity" rule: Brain already
    computes a single canonical `state` string per scan (e.g.
    "bearish_continuation_under_pressure") — that string IS the
    situation's identity; nothing new needs deriving. Returns None if
    unavailable (caller should treat that as "can't dedupe by identity
    this scan," not an error).
    """
    try:
        return ((understanding or {}).get("market_assessment") or {}).get("state")
    except Exception:
        return None


def is_repeat_situation(event, understanding, last_narrated_situation):
    """
    Per chat: "if bearish_fib_retracement_123 already produced [a
    narration], another scan cannot generate [the same kind of
    announcement] unless something materially changed about that same
    situation." Only applies to "state" category events — "structure"
    and "opportunity" events already carry their own distinct identity
    (a bias flip, a specific zone code) and are handled by their
    existing dedup paths (last_for_leg debounce, location-code tracking)
    rather than this one. Returns True when the CURRENT situation is
    identical to the last one actually narrated — i.e. nothing to say
    that hasn't already been said. False (safe default) on missing data
    or any error, so a real change is never silently swallowed by this
    additional check.
    """
    try:
        if event.get("category") != "state":
            return False
        current = situation_key(understanding)
        return bool(current) and bool(last_narrated_situation) and current == last_narrated_situation
    except Exception as e:
        print("[MARKET STORY SITUATION ERROR] " + str(e))
        return False
