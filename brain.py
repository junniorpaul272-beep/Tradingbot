"""
brain.py — Phase 2 (per chat, 2026-08-19): the interpretation layer over
WorldState. Its one job is turning "here is everything the system
currently knows" (WorldState) into "here is the market story those facts
describe" (a Market Understanding) — the layer between raw facts and the
eventual conversational Assistant.

THE BOUNDARY, ENFORCED STRUCTURALLY, NOT JUST BY CONVENTION: this module
imports nothing from scanner_common / scanner_observation / min_scanner /
scanner_live. It receives a WorldState dict (from
min_scanner.build_world_state()) and nothing else — no candles, no direct
file reads, no access to anything WorldState doesn't already expose. If
Brain ever needs a fact WorldState doesn't contain, the fix is to add that
fact to WorldState — not to have this file quietly become a second
scanner. (Per chat: "That keeps the architecture honest.")

STILL NO FREE INFERENCE. Same discipline as stitch_narrative() in
scanner_observation.py: every relationship computed here is a direct,
deterministic comparison between facts WorldState already contains —
never a new detection, never a guess. The upgrade over MarketThesis isn't
"Brain is allowed to infer things" — it's "Brain is allowed to compare
things to each other," which MarketThesis's own conservative,
per-leg-only design deliberately doesn't do.

NEVER CALL THE OUTPUT "BIAS". Per chat: labeling this bullish/bearish
would recreate the exact rule ("not confirmed bullish" -> "therefore
bearish") everyone agreed to avoid. The output is a structured
understanding — current condition, structural context, and what would
resolve the difference between them — not a verdict.

KNOWN, ADMITTED LIMITATION (found while building this, not guessed at in
advance): relate_current_leg_to_context() only looks ONE leg back
(WorldState.phase.prior_macro_leg — that's all that's currently
persisted). For a genuine fresh reversal leg, that's exactly the
comparison that matters. But for a campaign N continuation legs deep
(Vally's actual live example — 14 legs in), "prior_macro_leg" is just the
previous same-direction continuation leg by then, not the opposing
structure from before the original flip. So structural_context correctly
resolves to "continuing_established_structure" in that case — it does
NOT keep re-litigating a transition that already resolved several legs
ago. Whether that's the right long-term answer, or whether "general
bias" needs to look back further than one leg, is exactly the open
question from chat that nobody's supposed to answer by guessing a number
yet. See the review history in world_state_schema.md / this file's test
suite for both scenarios exercised side by side.
"""

from datetime import datetime, timezone


def relate_current_leg_to_context(world_state):
    """
    LAYER A — pure structural relationships. Every field here is a
    direct comparison between two facts already in WorldState.phase;
    nothing new is detected. Returns None if the facts needed to relate
    anything aren't available yet (first leg the bot's ever tracked, or
    a first-run WorldState with no prior_macro_leg) — preserve absence,
    same discipline as WorldState itself, rather than fabricate a
    relationship out of missing data.

    KNOWN LIMITATION, STATED EXPLICITLY RATHER THAN HIDDEN: this compares
    the CURRENT LEG'S OWN RUNNING EXTREME against the PRIOR LEG'S
    EXTREME — not live tick price against the prior extreme. WorldState
    doesn't currently carry a standalone "current price" fact (checked:
    it's computed ad-hoc from the candle dataframe at specific call
    sites in scanner_observation.py, never persisted) — per this file's
    own rule, that's a WorldState gap to fill later, not something to
    quietly work around here by reaching past what WorldState provides.
    For an actively-forming leg the two are usually close, but not
    guaranteed identical.
    """
    phase = (world_state or {}).get("phase") or {}
    current_leg = phase.get("macro_leg")
    prior_leg = phase.get("prior_macro_leg")

    if not current_leg or not prior_leg:
        return None

    current_direction = current_leg.get("direction")
    prior_direction = prior_leg.get("direction")
    current_extreme = current_leg.get("extreme")
    prior_origin = prior_leg.get("origin")
    prior_extreme = prior_leg.get("extreme")

    if current_direction is None or prior_direction is None:
        return None

    alignment = ("continuation" if current_direction == prior_direction
                 else "opposing_direction")

    # CORRECTED (caught by testing against a real scenario, not guessed
    # right the first time): the level that matters for "has this
    # reversal actually gone anywhere" is where the OPPOSING leg
    # STARTED (prior_leg.origin) — not where it ended
    # (prior_leg.extreme). A bearish leg that ran from 1.35800 down to
    # 1.34759 is only genuinely displaced once price reclaims 1.35800;
    # comparing against 1.34759 instead is trivially true almost
    # immediately after any bounce and confirms nothing. First draft of
    # this function compared against prior_extreme and it produced a
    # false "confirmed" reading on the very first test — see this file's
    # test suite for the exact case that caught it.
    current_vs_prior_extreme = None
    if current_extreme is not None and prior_origin is not None:
        if current_direction == "BULLISH":
            current_vs_prior_extreme = "beyond" if current_extreme > prior_origin else "below"
        elif current_direction == "BEARISH":
            current_vs_prior_extreme = "beyond" if current_extreme < prior_origin else "below"

    # Only meaningful when this leg is actually opposing the one before
    # it — a same-direction continuation isn't "testing" anything about
    # the prior structure, so there's nothing to confirm or leave
    # unconfirmed.
    if alignment == "continuation":
        structural_transition_status = "not_applicable"
    elif current_vs_prior_extreme == "beyond":
        structural_transition_status = "confirmed"
    elif current_vs_prior_extreme == "below":
        structural_transition_status = "unconfirmed"
    else:
        structural_transition_status = None  # origin data missing

    return {
        "current_leg_direction": current_direction,
        "prior_leg_direction": prior_direction,
        "current_vs_prior_alignment": alignment,
        "current_extreme_vs_prior_origin": current_vs_prior_extreme,
        "structural_transition_status": structural_transition_status,
        "prior_origin_price": prior_origin,    # the level that must be reclaimed — this is what key_structural_test uses
        "prior_extreme_price": prior_extreme,  # kept for reference (e.g. measured-move context) — NOT the confirmation level
        "context_depth": "single_prior_leg",  # admitted limit — see module docstring
    }


def determine_structural_state(relationship_facts):
    """
    Turns Layer A's raw comparisons into a compact, named state — still
    deterministic, still zero inference, just a label for a combination
    of facts rather than the facts themselves. Returns None if
    relationship_facts is None (nothing to relate yet).
    """
    if relationship_facts is None:
        return None

    alignment = relationship_facts["current_vs_prior_alignment"]
    status = relationship_facts["structural_transition_status"]

    if alignment == "continuation":
        return "continuing_established_structure"
    if status == "confirmed":
        return "structural_transition_confirmed"
    if status == "unconfirmed":
        return "structural_transition_unconfirmed"
    return "insufficient_data"


def interpret_structure(relationship_facts, structural_state):
    """
    LAYER B — synthesis. Still templated, not freeform (same reasoning
    as scanner_observation.stitch_narrative(): the story is assembled
    from branches keyed on already-computed facts, never generated). The
    upgrade over stitch_narrative() is that these branches key off
    RELATIONSHIPS (Layer A), not raw individual facts, so the sentence
    can talk about how two things relate rather than just naming one of
    them. Returns None if there's nothing to interpret yet.
    """
    if relationship_facts is None or structural_state is None:
        return None

    current_dir = (relationship_facts["current_leg_direction"] or "?").title()
    prior_dir = (relationship_facts["prior_leg_direction"] or "?").title()
    prior_origin = relationship_facts.get("prior_origin_price")

    if structural_state == "continuing_established_structure":
        return f"{current_dir} pressure continuing in line with the structure already in place."

    if structural_state == "structural_transition_confirmed":
        return (f"{current_dir} pressure has reclaimed the level the prior {prior_dir.lower()} "
                f"leg started from — the higher-degree transition looks confirmed, "
                f"not just a leg-level move.")

    if structural_state == "structural_transition_unconfirmed":
        level_txt = f"{prior_origin}" if prior_origin is not None else "the prior structural level"
        return (f"{current_dir} pressure is developing, but the broader structure "
                f"(still {prior_dir.lower()}) hasn't been displaced yet. The meaningful "
                f"test is a break of {level_txt}.")

    return "Not enough structural history yet to relate this leg to what came before it."


def build_market_understanding(world_state):
    """
    Main entry point — Phase 2 (per chat, 2026-08-19, approved
    architecture: min_scanner.py builds reality via build_world_state(),
    this file interprets it). Takes a WorldState dict and ONLY a
    WorldState dict; produces a Market Understanding, never a bias
    label.

    Every field is either a direct pass-through of something WorldState
    already computed, or a deterministic relationship/synthesis derived
    from Layer A/B above — nothing here is a new detection.
    """
    now_utc = datetime.now(timezone.utc)
    phase = (world_state or {}).get("phase") or {}
    thesis = (world_state or {}).get("thesis") or {}

    relationship_facts = relate_current_leg_to_context(world_state)
    structural_state = determine_structural_state(relationship_facts)
    developing_scenario = interpret_structure(relationship_facts, structural_state)

    key_structural_test = None
    if relationship_facts and relationship_facts.get("structural_transition_status") == "unconfirmed":
        key_structural_test = relationship_facts.get("prior_origin_price")

    return {
        "generated_at": now_utc.isoformat(),
        "current_condition": {
            "phase": phase.get("phase"),
            "macro_bias": phase.get("macro_bias"),
            "leg_direction": (phase.get("macro_leg") or {}).get("direction"),
        },
        "structural_context": structural_state,
        "relationship_facts": relationship_facts,
        "developing_scenario": developing_scenario,
        "key_structural_test": key_structural_test,
        # Sourced from MarketThesis, not independently computed — Brain
        # doesn't invent new invalidation logic beyond what Thesis
        # already tracks per leg (per chat: don't duplicate what
        # already exists elsewhere).
        "invalidation_context": thesis.get("invalidation"),
    }
