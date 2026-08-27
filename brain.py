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
comparison that matters. For a campaign N continuation legs deep
(Vally's actual live example — 31 legs in, 2026-08-25), structural_
context correctly resolves to "continuing_established_structure" — it
does NOT keep re-litigating a transition that already resolved several
legs ago. Whether "general bias" needs to look back further than one leg
is still an open question from chat that nobody's supposed to answer by
guessing a number yet. See the review history in world_state_schema.md /
this file's test suite for both scenarios exercised side by side.

CORRECTION (2026-08-25, per chat — a prior version of this docstring
described prior_macro_leg wrong): "prior_macro_leg" is NOT "the previous
same-direction continuation leg." It's the current swing's FOUNDING
origin — captured once, at the leg's birth, and untouched by every
same-direction continuation after that (detect_bos_impulse()'s own
contract: impulse_start "does NOT move on a same-direction continuation
break; it can be many bars and several continuations old"). Confirmed
live in Vally's 31-leg campaign: prior_macro_leg_origin still equals
campaign_origin — the campaign's very FIRST leg — five days and 30
continuations after the fact. That's correct, not stale: it's exactly
the founding-structure reference relate_current_leg_to_context()'s
reversal-confirmation check needs (the level that must be reclaimed to
invalidate the WHOLE current swing, not just its latest continuation).

Because of that, this module previously had NO fact at all for "the leg
immediately before the most recent continuation" — a genuinely different,
finer-grained question than prior_macro_leg answers. WorldState now
additionally carries phase.prior_continuation_leg (added 2026-08-25) for
exactly that — see capture_prior_continuation_snapshot()'s docstring in
scanner_observation.py. relate_current_leg_to_context() below surfaces it
as an extra REFERENCE field only; it does not change alignment/
structural_transition_status, which correctly keep using the founding-
leg comparison above.

PHASE 3 ADDITION (per chat with Vally, 2026-08-24): the market_assessment
block. Motivating problem — the old Market Thesis (scanner_observation.py)
told you WHAT the indicators said (break count, ATR, campaign extension)
but never what the COMBINATION meant, so it read like a diagnostic dump
rather than an opinion. The fix isn't new sensors — every fact needed
already exists in WorldState (thesis.mtf_5m/mtf_15m are already atomic
dicts, not prose; phase.* already carries the upstream EXPANSION/
EXHAUSTION verdict). The fix is a second Layer A relationship
(relate_timeframe_conflict) plus a leg-maturity relationship
(relate_leg_maturity), both as separate, narrow, single-question
functions — NOT folded into relate_current_leg_to_context, and NOT
merged into one junk-drawer function — feeding a new Layer B function
(synthesize_market_understanding) that decides what hypothesis is
actually being threatened, rather than aggregating warnings into a
failure_risk score (that aggregation is exactly the mistake this is
meant to fix — see synthesize_market_understanding's own docstring).

IMPORTANT, EXPLICITLY CALLED OUT IN CHAT: thesis.trend_health is
STITCHED PROSE ("Aging — break-count exhaustion trigger, 4 break(s)...")
— not a fact. relate_leg_maturity() deliberately reads world_state
["phase"] only (phase/aging_reason/break_count/dist_in_atr — the
atomic, upstream-classified fields), never thesis.trend_health. Brain
must never parse its own downstream prose as if it were a fact; that's
the same discipline that keeps this file from becoming a second
scanner.

ABSENCE DISCIPLINE (per chat): every new relate_*() function returns
None — never a fabricated "unknown" — when its required inputs aren't
available (5M read missing, no active leg, etc.). "Unknown" as a
returned VALUE risks being reasoned about by Layer B as if the system
deliberately established that state; only a genuine None communicates
"this relationship could not be formed yet."

ADDITIVE, NOT A REPLACEMENT (per chat): market_assessment is a new key
alongside the existing current_condition/structural_context/
developing_scenario fields, which are UNCHANGED. This keeps the old
and new understanding comparable side by side without simultaneously
changing /understand, its Telegram formatting, or any scanner_live.py
call site. Promoting market_assessment to be the primary surface (and
possibly retiring the older fields) is a decision for after it's been
checked against real scans, not now.

EXTENSIBILITY (per chat): synthesize_market_understanding() takes a
single `relationships` dict keyed by name, not positional args for each
Layer A function. The three keys populated today (leg_context,
timeframe_conflict, leg_maturity) are not meant to be the permanent
universe — the principle is "Layer A establishes relationships, Layer B
interprets whatever relationships it's given," so future relate_*()
additions don't require changing this function's signature.

PHASE 4 ADDITION (per chat with Vally + friend, 2026-08-24): Market
Intent redesign. Three new functions — build_market_logic(),
build_market_intent_hypothesis(), build_market_briefing() — with one
sequencing rule that was decided BEFORE any of them were written:
define the semantics first, don't let synthesis code quietly invent a
relationship the tracking layer never asserted.

WHAT CHANGED UPSTREAM FIRST, AND WHY IT HAD TO: build_market_intent_
hypothesis() needs to read watching_for's zone_low/zone_high and role
directly rather than re-detecting POIs — that was always the plan. But
auditing scanner_live.py found that state["market_intent_watch_codes"]
was being persisted as `[w["code"] for w in intent.watching_for]` —
bare code strings only, zone data discarded before it ever reached
state.json. WorldState.intent.watching_for could therefore never have
supported this, regardless of what got built here. Fixed at the source
(scanner_live.py now persists the full dict; format_market_intent_
report() in min_scanner.py updated, isinstance-guarded, to keep its
exact existing dev output from the richer shape). Recorded here because
it's exactly the kind of gap this file's own boundary rule exists to
surface: "if Brain needs a fact WorldState doesn't contain, fix
WorldState" — this is that rule catching a real miss, not a hypothetical
one.

WHERE THE WATCHCODE/CAUTIONCODE ROLE MAP LIVES, AND WHY NOT HERE: role
(LOCATION / CONFIRMATION / CAUTION) is decided in scanner_observation.py,
next to the WatchCode/CautionCode enums that own the vocabulary, and
tagged onto each watching_for/not_interested_in entry AT CREATION TIME
— not looked up here. This file's own boundary rule (imports nothing
from the scanner files) meant a role table living in brain.py would
either force an import that isn't allowed, or duplicate the table in
two places that could drift as codes are added. Tagging the role onto
the WorldState fact itself avoids both — Brain groups by role, it
doesn't decide role. See scanner_observation.IntentRole for the full
reasoning, including why LOCATION/CONFIRMATION are never collapsed to a
single "primary" pick: no priority between coexisting codes of the same
role is declared anywhere, so none may be invented here either.

build_market_logic()'s JOB IS NARROWER THAN "EXPLAIN THE EVIDENCE" (per
chat, explicitly corrected mid-design): it does not touch
thesis.evidence/thesis.weaknesses at all. Those stay raw, available
on-request in a briefing's reasoning_snapshot, not templated into prose
by this file — turning a free-text evidence bullet into a "because"
sentence would imply an independence between evidence items (e.g. "OB
confirmed" and "low ATR") that hasn't been established. Instead
build_market_logic() re-describes the SAME market_assessment/
timeframe_conflict/leg_maturity relationships synthesize_market_
understanding() already produced — the "why" companion to that
function's "what" — using relational language ("while", "remains",
"is already") and never causal language ("because", "X caused Y").

build_market_briefing()'s reasoning_snapshot/intent_snapshot are bundled
INSIDE the returned dict, not written to a new persistence file (per
chat: try the free option first — a new market_briefing_log.jsonl is
only justified once it's established that the briefing object doesn't
survive long enough elsewhere for a later "why did you say X at 9am"
question to reach it; that hasn't been established yet, so this doesn't
speculatively add one).
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
    # Reference-only, added 2026-08-25 — see this function's own docstring
    # and the module docstring's 2026-08-25 correction. Not used in any
    # alignment/status comparison below; exposed purely so a caller (or a
    # human reading /understand's dev view) can see "the last continuation"
    # right next to "the founding leg" without cross-referencing WorldState
    # separately.
    prior_continuation = phase.get("prior_continuation_leg") or {}

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
        # Reference-only, added 2026-08-25 — the leg immediately before
        # the MOST RECENT continuation (as opposed to prior_origin_price/
        # prior_extreme_price above, which are the founding leg before the
        # whole current swing). None until this campaign's first
        # continuation since the field shipped — same absence-not-
        # fabrication discipline as everything else here. NOT read by
        # alignment/structural_transition_status above.
        "nearest_continuation_origin_price": prior_continuation.get("origin"),
        "nearest_continuation_extreme_price": prior_continuation.get("extreme"),
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


def relate_timeframe_conflict(world_state):
    """
    LAYER A — cross-timeframe directional relationship: what is the HTF
    bias vs 15M vs 5M saying about the SAME market right now. A
    deliberately separate question from relate_current_leg_to_context()
    (which compares the current leg to the leg before it, same
    timeframe) — see module docstring for why these stay two functions
    instead of one.

    Every field here is a direct pass-through of atoms
    scanner_observation.compute_5m_read() already computed — mtf_5m is
    already a flat dict (m5_direction, m5_relationship_to_htf,
    m5_relationship_to_15m, m5_was_choch), not prose. Nothing new is
    detected; this only re-exposes those atoms as a named relationship
    alongside phase.macro_bias.

    Returns None if the HTF bias isn't directional yet or the 5M read
    isn't available (bos5 was None in compute_5m_read) — absence
    preserved, not fabricated as "unknown" (per chat).
    """
    phase = (world_state or {}).get("phase") or {}
    thesis = (world_state or {}).get("thesis") or {}
    mtf_5m = thesis.get("mtf_5m") or {}

    htf_bias = phase.get("macro_bias")
    m5_direction = mtf_5m.get("m5_direction")

    if htf_bias not in ("BULLISH", "BEARISH") or m5_direction is None:
        return None

    return {
        "htf_bias": htf_bias,
        "m5_direction": m5_direction,
        # "aligned" / "countertrend" — narrowly directional ONLY. Per
        # chat: alignment does NOT mean "no conflict" in the broader
        # market (all-timeframes-aligned can still be exhausted,
        # over-extended, or approaching a decision point) — that
        # broader read is relate_leg_maturity()'s / Layer B's job, not
        # a claim this field is allowed to make.
        "m5_vs_htf": mtf_5m.get("m5_relationship_to_htf"),
        "m5_vs_15m": mtf_5m.get("m5_relationship_to_15m"),
        "m5_fresh_choch": mtf_5m.get("m5_was_choch"),
    }


# Brain's own threshold (Layer B, not upstream) for when the 15M read is
# strong enough to call it "reaccelerating" rather than just "not flat."
# Deliberately separate from anything in scanner_common.py's PHASE_* /
# MARKET_STATE_* constants — this module owns its OWN interpretive bar for
# what counts as a strong push, same as `both_signals` below owns its own
# "both aging signals fired" bar. Not tuned against real data yet; revisit
# once this branch has actually fired a few times live.
RECENT_MOMENTUM_STRENGTH_ATR_MULT = 2.0


def relate_recent_momentum(world_state):
    """
    LAYER A — is the CURRENT push (15M grain) itself showing strength or
    fading, independent of how old the 1H leg is. Added 2026-08-27, per
    chat — real /understand case: a 38-hour-old, 4-break, 5.5-ATR-extended
    1H leg (genuinely mature by every 1H measure) got labeled "exhaustion"
    on the exact scan where the 15M data showed trend_strength_atr_mult=4.0
    and volatility_state="expanding" — the leg's own most recent push was
    accelerating, not fading, and nothing upstream of Brain ever compares
    those two facts to each other. compute_market_state() (scanner_
    observation.py) was already computing and persisting both fields into
    thesis.mtf_15m every scan; this function only re-exposes those atoms
    as a named relationship, same discipline as relate_timeframe_conflict()
    just above — nothing new is detected here.

    Deliberately narrow: this says whether the MOST RECENT push is strong/
    expanding, nothing about how mature the underlying leg is (that's
    relate_leg_maturity()'s job) and nothing about whether the two facts
    should override each other (that reconciliation is Layer B's job, in
    synthesize_market_understanding() below).

    Returns None if mtf_15m isn't available yet or its trend_strength
    reading has no BOS to measure against (see compute_market_state():
    trend_strength_atr_mult is None with no active bos) — absence
    preserved, not fabricated.
    """
    thesis = (world_state or {}).get("thesis") or {}
    mtf_15m = thesis.get("mtf_15m") or {}

    trend_strength_atr_mult = mtf_15m.get("trend_strength_atr_mult")
    volatility_state = mtf_15m.get("volatility_state")

    if trend_strength_atr_mult is None or volatility_state is None:
        return None

    return {
        "trend_strength_atr_mult": trend_strength_atr_mult,
        "volatility_state": volatility_state,
        "is_expanding": volatility_state == "expanding",
        "is_strong_push": trend_strength_atr_mult >= RECENT_MOMENTUM_STRENGTH_ATR_MULT,
    }


def _reaccelerating(momentum):
    """Shared gate for the reaccelerating branch below — both conditions,
    not just one, per chat: an expanding-but-small push, or a large-but-
    flat one, isn't the specific "aging leg, hot current push" combination
    this branch exists for."""
    return bool(momentum and momentum.get("is_expanding") and momentum.get("is_strong_push"))


def relate_leg_maturity(world_state):
    """
    LAYER A — leg-age/exhaustion facts. Deliberately reads
    world_state["phase"] ONLY — never thesis.trend_health, which is
    already-stitched prose (see module docstring). The EXPANSION vs
    EXHAUSTION verdict, and the threshold constants that produced it
    (PHASE_EXHAUSTION_MIN_BREAK_COUNT etc.), were already decided
    upstream in scanner_observation.py; this file has no access to
    those constants by design (see the file-level boundary note at the
    top of this module) and isn't supposed to re-derive the verdict —
    only consume it, the same way relate_current_leg_to_context()
    consumes macro_leg.direction rather than re-detecting direction
    from candles.

    Answers ONLY "what is the state of this leg's progression" — NOT
    "is this dangerous." Collapsing maturity straight into a risk
    verdict here would repeat the exact mistake being fixed (the old
    failure_risk = HIGH from aging + low ATR added together). That
    judgment belongs one layer up, in synthesize_market_understanding().

    Returns None if there's no active leg to assess (phase is neither
    EXPANSION nor EXHAUSTION — e.g. MANIPULATION or a fresh/unclassified
    state).

    BUG FIX (2026-08-26, per chat): this used to compare leg_phase
    against ("EXPANSION", "EXHAUSTION") — uppercase — but WorldState.
    phase.phase is sourced from scanner_observation.Phase.value, which
    is lowercase by design ("expansion"/"exhaustion"/"transition"/
    "manipulation"; see the Phase enum). The uppercase comparison could
    therefore never match, so this function returned None on every
    single call regardless of the real phase — the exact cause of
    /understand's "Leg maturity: — (no active leg to assess)" showing
    up even when "Current: ... phase=exhaustion" was right above it.
    This module deliberately doesn't import scanner_observation.Phase
    (see module-level boundary note at the top of this file), so the
    fix is matching WorldState's actual lowercase string values
    directly rather than importing the enum.
    """
    phase = (world_state or {}).get("phase") or {}
    leg_phase = phase.get("phase")

    if leg_phase not in ("expansion", "exhaustion"):
        return None

    return {
        "leg_phase": leg_phase,
        "aging_reason": phase.get("aging_reason"),  # None when leg_phase == EXPANSION
        "break_count": phase.get("break_count"),
        "dist_in_atr": phase.get("dist_in_atr"),
        # BUG FIX (2026-08-26, per chat): same uppercase/lowercase
        # mismatch as the guard clause above — leg_phase is always
        # lowercase ("exhaustion"), so comparing to "EXHAUSTION" always
        # evaluated False. is_aging fed straight into synthesize_market_
        # understanding()'s countertrend/is_aging branches, so this alone
        # meant a genuinely aging leg could never be reported as aging —
        # it always fell through to the "not aging" branches.
        "is_aging": leg_phase == "exhaustion",
    }


def synthesize_market_understanding(relationships, current_condition):
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
                    "a fresh bearish extreme while 15-minute expansion holds "
                    "would confirm the mature leg is still capable of genuine "
                    "continuation, not just a temporary push."
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
                    "whether this reacceleration produces another genuine "
                    "bearish extreme, not a CHoCH. A CHoCH or range would "
                    "only become the relevant next event if this expansion "
                    "stalls without ever making a fresh extreme"
                ),
            }
        return {
            "state": f"{htf_bias.lower()}_continuation_maturing",
            "primary_thesis": (
                f"{direction_label} structure remains intact and the lower "
                f"timeframe isn't opposing it, but the current leg is aging."
                f"{_aging_qualifier()}"
            ),
            "primary_threat": "Leg maturity — extension quality may be declining even without lower-timeframe opposition yet.",
            "structural_status": "not_invalidated",
            "confirmation_needed": "Fresh continuation impulse rather than further aging.",
            "alternative": None,
            "invalidation": None,
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
    }


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

    current_condition = {
        "phase": phase.get("phase"),
        "macro_bias": phase.get("macro_bias"),
        "leg_direction": (phase.get("macro_leg") or {}).get("direction"),
    }

    # ---- PHASE 3 ADDITION (per chat, 2026-08-24): market_assessment.
    # Purely additive — current_condition/structural_context/
    # relationship_facts/developing_scenario above are UNCHANGED, so
    # this can be compared side by side with the pre-existing output
    # rather than replacing anything /understand already relies on.
    timeframe_conflict = relate_timeframe_conflict(world_state)
    leg_maturity = relate_leg_maturity(world_state)
    # ADDED (2026-08-27, per chat — see relate_recent_momentum() docstring):
    # purely additive, same pattern as timeframe_conflict/leg_maturity above.
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
        "generated_at": now_utc.isoformat(),
        "current_condition": current_condition,
        "structural_context": structural_state,
        "relationship_facts": relationship_facts,
        "developing_scenario": developing_scenario,
        "key_structural_test": key_structural_test,
        # Sourced from MarketThesis, not independently computed — Brain
        # doesn't invent new invalidation logic beyond what Thesis
        # already tracks per leg (per chat: don't duplicate what
        # already exists elsewhere).
        "invalidation_context": thesis.get("invalidation"),
        # ADDED (2026-08-26, per chat — human-facing /understand):
        # same "sourced from Thesis, not re-derived" discipline as
        # invalidation_context immediately above. Thesis already decides
        # WHICH weaknesses/next-event are relevant to the current leg
        # (see build_market_thesis()'s conditional appends in
        # scanner_observation.py — a weakness only gets added when its
        # own gate is true that scan), so format_understanding_narrative()
        # below can use these directly without re-litigating relevance.
        "thesis_weaknesses": thesis.get("weaknesses") or [],
        "thesis_weaknesses_prose": thesis.get("weaknesses_prose") or [],
        # ADDED (2026-08-27, per chat — /understand repetition fix): see
        # scanner_observation.MarketThesis.weakness_categories docstring.
        # format_understanding_narrative() uses this to avoid restating a
        # fact leg_maturity/market_assessment already synthesized below.
        "thesis_weakness_categories": thesis.get("weakness_categories") or [],
        "thesis_expected_next_event": thesis.get("expected_next_event"),
        # New Layer A facts this block's market_assessment was built
        # from, exposed raw so format_market_understanding()'s dev/audit
        # view can show "what Brain saw" next to "what Brain concluded"
        # — same audit principle as relationship_facts/developing_scenario
        # above.
        "timeframe_conflict": timeframe_conflict,
        "leg_maturity": leg_maturity,
        # ADDED (2026-08-27, per chat): raw momentum fact this block's
        # reaccelerating branch (see synthesize_market_understanding) was
        # built from — same dev/audit-parity reasoning as the two fields
        # immediately above.
        "recent_momentum": recent_momentum,
        "market_assessment": market_assessment,
    }


def format_market_understanding(understanding):
    """
    Telegram-friendly rendering of build_market_understanding()'s output —
    same pairing as format_world_state()/build_world_state(). Deliberately
    a DEVELOPMENT/AUDIT surface (per chat), not the eventual Assistant
    experience: it shows the relationship facts Brain used, side by side
    with what Brain concluded from them, specifically so this can be
    checked against real situations before anything downstream (e.g.
    folding this into /world, or a future Assistant) is allowed to trust
    it silently. Once Brain is trusted, this can be retired in favor of
    surfacing developing_scenario inside /world directly — not before.

    Returns a plain string. Never raises — a missing/None field prints as
    '—' rather than crashing the command.
    """
    cc = understanding.get("current_condition") or {}
    rf = understanding.get("relationship_facts")

    lines = ["*Market Understanding* _(dev/audit view — not yet in /world)_", ""]
    lines.append(f"Current: {cc.get('leg_direction') or '—'} leg, "
                 f"phase={cc.get('phase') or '—'}, bias={cc.get('macro_bias') or '—'}")

    if rf is None:
        lines.append("")
        lines.append("_No relationship facts yet — first leg the bot has tracked, "
                      "or no prior_macro_leg persisted._")
        # NOTE (fixed 2026-08-24): this used to `return` here, which
        # meant market_assessment below — which does NOT depend on
        # relationship_facts, e.g. the bias_only state — silently never
        # printed whenever this early branch was hit. Fall through
        # instead so the new section always gets a chance to show
        # whatever it has.
    else:
        lines.append(f"Prior leg: {rf.get('prior_leg_direction') or '—'}")
        lines.append(f"Alignment: {rf.get('current_vs_prior_alignment') or '—'}")
        lines.append(f"Context depth: {rf.get('context_depth') or '—'} "
                     "_(admitted limitation — see brain.py docstring)_")

        lines.append("")
        lines.append(f"Structural context: {understanding.get('structural_context') or '—'}")

        scenario = understanding.get("developing_scenario")
        if scenario:
            lines.append("")
            lines.append(f"Understanding: {scenario}")

        kst = understanding.get("key_structural_test")
        if kst is not None:
            lines.append("")
            lines.append(f"Key structural test: `{kst}`")

        inv = understanding.get("invalidation_context")
        if inv:
            lines.append("")
            lines.append(f"Invalidation (from Thesis): {inv}")

    # ---- PHASE 3 ADDITION (per chat, 2026-08-24) — shown in its own
    # clearly-separated section, below the original output, so this
    # remains an A/B comparison rather than quietly swapping what
    # /understand has been showing.
    tf = understanding.get("timeframe_conflict")
    lm = understanding.get("leg_maturity")
    ma = understanding.get("market_assessment")

    lines.append("")
    lines.append("— — —")
    lines.append("*Market Assessment* _(new — Layer A/B synthesis, see chat 2026-08-24)_")

    if tf:
        lines.append(f"Timeframe: HTF={tf.get('htf_bias') or '—'}, "
                      f"5M={tf.get('m5_direction') or '—'} "
                      f"({tf.get('m5_vs_htf') or '—'} vs HTF, "
                      f"{tf.get('m5_vs_15m') or '—'} vs 15M)"
                      + (", fresh CHoCH" if tf.get("m5_fresh_choch") else ""))
    else:
        lines.append("Timeframe: — _(5M read unavailable)_")

    if lm:
        lines.append(f"Leg maturity: {lm.get('leg_phase') or '—'}"
                      + (f" ({lm.get('aging_reason')})" if lm.get("aging_reason") else "")
                      + f", {lm.get('break_count') if lm.get('break_count') is not None else '—'} break(s)")
    else:
        lines.append("Leg maturity: — _(no active leg to assess)_")

    if ma:
        lines.append("")
        lines.append(f"State: {ma.get('state') or '—'}")
        lines.append(f"Thesis: {ma.get('primary_thesis') or '—'}")
        if ma.get("primary_threat"):
            lines.append(f"Primary threat: {ma.get('primary_threat')}")
        if ma.get("confirmation_needed"):
            lines.append(f"Needs: {ma.get('confirmation_needed')}")
        if ma.get("alternative"):
            lines.append(f"Alternative: {ma.get('alternative')}")
    else:
        lines.append("")
        lines.append("_No market assessment yet — HTF bias not directional._")

    return "\n".join(lines)


def _plain_clause(text):
    """Lowercases the first character of a MarketThesis label/sentence so
    it reads naturally mid-paragraph (e.g. as a clause after 'Weighing
    against that:') instead of like a new bulleted heading. Purely
    cosmetic — never touches the wording itself, only its casing."""
    return text[0].lower() + text[1:] if text else text


def format_understanding_narrative(understanding):
    """
    Human-facing rendering of build_market_understanding()'s output — the
    "ASSISTANT RESPONSE" layer from the friend's WORLDSTATE -> BRAIN ->
    INTERPRETATION -> ASSISTANT RESPONSE diagram (per chat, 2026-08-26).
    This is what /understand now sends by default; format_market_
    understanding()'s field-by-field dump is retired from any Telegram
    command (per chat, 2026-08-26: only /understand, /thesis,
    /marketintent remain as market-explanatory commands).

    STILL NO FREE INFERENCE — same discipline as every other function in
    this module (see the file-level docstring). This does not decide
    anything new. market_assessment's primary_thesis/primary_threat/
    confirmation_needed/alternative are already written as full sentences
    by synthesize_market_understanding() above; thesis_weaknesses_prose
    is already phrased as natural clauses by build_market_thesis()
    (scanner_observation.py) at the exact point it decides a weakness is
    relevant — this function's only job is choosing ORDER and OMISSION,
    never wording. Per chat: "it should know which 3 facts actually
    matter right now, not know 50 facts."

    Uses thesis_weaknesses_prose (not thesis_weaknesses) — the raw labels
    like "Very-low-ATR warning — 3.5p, below the 4p floor" are dev/audit
    formatting; the _prose twin of each ("volatility is very low, at 3.5
    pips against a 4 pip floor") is what belongs in a sentence read by a
    person. See MarketThesis.weaknesses_prose's docstring.

    Returns a plain string. Never raises — degrades to a short, honest
    "not enough to say yet" line rather than printing dashes or crashing
    the command.
    """
    ma = understanding.get("market_assessment")
    cc = understanding.get("current_condition") or {}
    bias = cc.get("macro_bias")

    if not ma:
        if bias in ("BULLISH", "BEARISH"):
            return (f"{bias.title()} is the higher-timeframe bias, but there isn't "
                    "enough structure yet to say more than that.")
        return "No directional read yet — the higher-timeframe bias isn't confirmed either way."

    sentences = [ma["primary_thesis"]]

    # DEDUP FIX (2026-08-27, per chat with friend — /understand was
    # repeating itself): thesis_weaknesses_prose and market_assessment's
    # own primary_thesis/_aging_qualifier() text can describe the exact
    # same underlying signal, because leg_maturity's is_aging (full
    # PHASE_EXHAUSTION_* threshold) and this thesis's leg_break_count/
    # ema_extension weaknesses (FAILURE_RISK_APPROACHING_FRACTION of
    # that same threshold) key off the same break_count/dist_in_atr —
    # so whenever is_aging fires, the break_count weakness has ALWAYS
    # also fired (lower bar), and whenever both_signals fires, the
    # ema_extension weakness has ALWAYS also fired. This isn't an
    # occasional overlap, it's guaranteed by the threshold math, so it
    # was showing up on every single aging leg (see the friend's
    # /understand review, 2026-08-27). Fix: once market_assessment has
    # already spoken for a category (via is_aging/both_signals below),
    # drop that category's line from weaknesses_prose rather than
    # printing both. Every OTHER category (bias_stale, ob_mitigated,
    # atr_floor) is untouched — those are facts Brain doesn't currently
    # synthesize into primary_thesis at all.
    lm = understanding.get("leg_maturity") or {}
    is_aging = lm.get("is_aging")
    both_signals = lm.get("aging_reason") == "break_count+ema_distance"
    suppressed_categories = set()
    if is_aging:
        suppressed_categories.add("leg_break_count")
    if both_signals:
        suppressed_categories.add("ema_extension")

    weaknesses_prose = understanding.get("thesis_weaknesses_prose") or []
    weakness_categories = understanding.get("thesis_weakness_categories") or []
    if suppressed_categories and len(weakness_categories) == len(weaknesses_prose):
        weaknesses_prose = [w for w, c in zip(weaknesses_prose, weakness_categories)
                             if c not in suppressed_categories]
    # else: categories missing or mismatched length (e.g. state.json
    # written before this field existed) — defensive fallback, print
    # everything rather than silently dropping a real weakness on a
    # guess about which entry is which.

    if weaknesses_prose:
        sentences.append("Weighing against that: " + "; ".join(weaknesses_prose) + ".")

    if ma.get("primary_threat"):
        sentences.append(ma["primary_threat"])

    if ma.get("confirmation_needed"):
        sentences.append(f"What would confirm it: {_plain_clause(ma['confirmation_needed'])}")

    if ma.get("alternative"):
        sentences.append(ma["alternative"])

    # FRAMING FIX (2026-08-27, per chat with friend): "Next signal I'd
    # expect" reads as a forecast of the exact next event. EXPECTED_NEXT_
    # EVENT_MAP entries are frequently disjunctive ("CHoCH or range —
    # leg showing age") precisely because Brain isn't entitled to predict
    # which one happens — it's a list of what would count as meaningful
    # next evidence, not a prediction. "What I'm watching for" says that
    # honestly without changing the underlying data or wording (which
    # stays Thesis's, per this function's own no-free-wording discipline
    # — only the lead-in phrase, which belongs to this function, changed).
    # NOTE (flagged, not fixed): the deeper issue the friend raised —
    # that a CHoCH (structural event) and a range (a condition) are not
    # the same kind of thing and shouldn't share one "next event" slot —
    # is a schema change to EXPECTED_NEXT_EVENT_MAP itself (scanner_common.py),
    # consumed elsewhere in exact-string form (delta diffing, dev prints).
    # Left as a separate follow-up rather than bundled into this pass.
    # FIX (2026-08-27, per audit): thesis_expected_next_event is computed
    # entirely in scanner_observation.py from EXPECTED_NEXT_EVENT_MAP,
    # keyed only on (phase, cause) — it has no access to relate_recent_
    # momentum()'s reacceleration read and never will, by construction
    # (see that map's docstring in scanner_common.py). Left unpatched,
    # this meant market_assessment could conclude "the push is
    # reaccelerating, watch for a fresh extreme" one sentence and then
    # this block would unconditionally append "watching for CHoCH or
    # range — leg showing age" right after it, in the SAME paragraph —
    # the exact contradiction flagged in the friend's /understand review.
    # market_assessment branches that have already reasoned about what's
    # next (currently: the reaccelerating branch above) now say so
    # themselves via "next_watch", and that takes priority here. Branches
    # that haven't stated an opinion fall back to Thesis's map, unchanged.
    next_event = ma.get("next_watch") or understanding.get("thesis_expected_next_event")
    if next_event:
        sentences.append(f"What I'm watching for: {_plain_clause(next_event)}.")

    return " ".join(sentences)


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


def build_market_intent_hypothesis(world_state):
    """
    LAYER B — turns the tracking layer's flat watching_for/not_interested_in
    lists into role-grouped scenario language, using the `role` field
    scanner_observation.build_market_intent() now tags onto each entry
    (see module docstring's PHASE 4 note for why role is decided there,
    not here). This function GROUPS by an already-stated fact; it does
    not decide which LOCATION code is "the" scenario, or that a
    CONFIRMATION code resolves a specific LOCATION code, beyond what the
    role table already asserts — per chat, that would be inventing a
    relationship the tracking layer never asserted.

    locations/confirmations are always LISTS, even when only one entry
    is open — multiple LOCATION codes (e.g. both an OB and a Fib pocket
    unmitigated at once) are preserved as alternatives, never collapsed
    to a single pick, because no priority between them is declared
    anywhere (see scanner_observation.IntentRole).

    DEFENSIVE, NOT SILENT: an entry that's a bare string (state.json from
    before this change) or a dict missing "role" (a WatchCode/CautionCode
    added to the enum without an entry in WATCH_CODE_ROLE) goes into
    `unclassified` rather than being guessed into LOCATION/CONFIRMATION
    or silently dropped — same absence discipline as the relate_*()
    functions above.

    Returns None if there is nothing open at all (no watching_for, no
    not_interested_in) — absence preserved, not fabricated as empty
    lists standing in for "nothing to report."
    """
    intent = (world_state or {}).get("intent") or {}
    watching_for = intent.get("watching_for") or []
    not_interested_in = intent.get("not_interested_in") or []

    if not watching_for and not not_interested_in:
        return None

    locations, confirmations, unclassified = [], [], []
    for w in watching_for:
        if not isinstance(w, dict) or "role" not in w:
            unclassified.append(w if isinstance(w, dict) else
                                 {"code": w, "sentence": None, "zone_low": None, "zone_high": None})
            continue
        role = w.get("role")
        if role == "LOCATION":
            locations.append(w)
        elif role == "CONFIRMATION":
            confirmations.append(w)
        else:
            unclassified.append(w)

    cautions = [c if isinstance(c, dict) else {"code": c, "sentence": None}
                for c in not_interested_in]

    return {
        "locations": locations,
        "confirmations": confirmations,
        "cautions": cautions,
        "unclassified": unclassified,
    }


def format_intent_narrative(intent_hypothesis):
    """
    Human-facing rendering of build_market_intent_hypothesis()'s output.
    ADDED (2026-08-27, per audit — Phase 0 authority routing): this is
    the piece that was MISSING for /marketintent to route through Brain
    at all. build_market_intent_hypothesis() already existed and already
    did the grouping (locations/confirmations/cautions/unclassified) —
    it just had no live command pointed at it; the only thing that ever
    rendered it was format_market_briefing()'s dev/audit bullet-list view
    (retired from Telegram, per its own docstring).

    SAME NO-FREE-WORDING DISCIPLINE AS format_understanding_narrative():
    every sentence fragment here is a pass-through of a "sentence" field
    scanner_observation.build_market_intent() already wrote per watch/
    caution code, at the exact point it decided that code was relevant
    this scan. This function does not invent language, evaluate whether
    a location/confirmation/caution matters, or rank them against each
    other — only groups and joins what's already been decided elsewhere,
    same as every other formatter in this file.

    unclassified entries are deliberately NOT surfaced here (no written
    sentence to show a human — see build_market_intent_hypothesis()'s own
    docstring on why they exist) — an entry landing there is a data/audit
    signal, not something to show conversationally. Left in the returned
    dict for anyone reading intent_hypothesis directly (e.g. a future
    /marketintent audit view), just not spoken here.

    Returns a plain string. Never raises — degrades to a short, honest
    "nothing specific" line rather than printing nothing or crashing the
    command.
    """
    if not intent_hypothesis:
        return "Nothing specific being tracked right now."

    locations = intent_hypothesis.get("locations") or []
    confirmations = intent_hypothesis.get("confirmations") or []
    cautions = intent_hypothesis.get("cautions") or []

    def _sentences(entries):
        return [e.get("sentence") or e.get("code") for e in entries
                if e.get("sentence") or e.get("code")]

    loc_txt = _sentences(locations)
    conf_txt = _sentences(confirmations)
    caution_txt = _sentences(cautions)

    if not loc_txt and not conf_txt and not caution_txt:
        return "Nothing specific being tracked right now."

    sentences = []

    if loc_txt:
        lead = "Watching " if len(loc_txt) == 1 else "Watching a few things: "
        sentences.append(lead + "; ".join(loc_txt) + ".")

    if conf_txt:
        sentences.append("What would confirm it: " + "; ".join(conf_txt) + ".")

    if caution_txt:
        sentences.append("Staying out because: " + "; ".join(caution_txt) + ".")

    return " ".join(sentences)


def build_market_briefing(world_state):
    """
    Main entry point — Phase 4 (per chat, 2026-08-24): assembles Thesis +
    Logic + Intent + Areas of Interest + Confirmation/Invalidation into
    the single coherent object the 9am/12pm/3pm push is meant to send,
    replacing the old pattern of Telegram independently stitching
    together separate thesis/intent messages.

    REPRODUCIBILITY (per chat): a later "why did you say X at 9am"
    question must be answerable from what THIS briefing actually saw —
    not from recomputing against whatever the market looks like when the
    question is asked. reasoning_snapshot/intent_snapshot below exist for
    exactly that, bundled INSIDE this returned object rather than a new
    persistence file (see module docstring — that's only justified once
    it's established the briefing object itself doesn't survive long
    enough elsewhere for a later question to reach it; not established
    yet, so not built speculatively). Whether/where a caller keeps this
    object around long enough to answer that later question is a
    decision for whoever wires this into the 9am/12pm/3pm push — this
    function's only job is making sure the snapshot exists to be kept.

    Every field here is either already built by build_market_understanding()/
    build_market_logic()/build_market_intent_hypothesis(), or a direct
    pass-through of WorldState.thesis fields those don't already surface
    — zero new detection, same as everything else in this file.
    """
    understanding = build_market_understanding(world_state)
    thesis = (world_state or {}).get("thesis") or {}

    market_assessment = understanding.get("market_assessment") if understanding else None
    timeframe_conflict = understanding.get("timeframe_conflict") if understanding else None
    leg_maturity = understanding.get("leg_maturity") if understanding else None
    recent_momentum = understanding.get("recent_momentum") if understanding else None

    logic = build_market_logic(market_assessment, timeframe_conflict, leg_maturity)
    intent_hypothesis = build_market_intent_hypothesis(world_state)

    confirmation_sentences = None
    if intent_hypothesis and intent_hypothesis.get("confirmations"):
        confirmation_sentences = [c.get("sentence") for c in intent_hypothesis["confirmations"]]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),

        "thesis": {
            "current_state": thesis.get("current_state"),
            "confidence": thesis.get("confidence"),
        },
        "understanding": understanding,
        "logic": logic,
        "intent_hypothesis": intent_hypothesis,
        "confirmation": confirmation_sentences,
        # Sourced from MarketThesis, same as build_market_understanding()'s
        # invalidation_context — Brain doesn't invent a second
        # invalidation logic (per chat: don't duplicate what already
        # exists elsewhere).
        "invalidation": thesis.get("invalidation"),

        "reasoning_snapshot": {
            "leg_context": understanding.get("relationship_facts") if understanding else None,
            "timeframe_conflict": timeframe_conflict,
            "leg_maturity": leg_maturity,
            # ADDED (2026-08-27, per chat): snapshot parity with build_
            # market_understanding()'s own dev/audit exposure of this fact.
            "recent_momentum": recent_momentum,
            "thesis_evidence": thesis.get("evidence") or [],
            "thesis_weaknesses": thesis.get("weaknesses") or [],
        },
        "intent_snapshot": (world_state or {}).get("intent"),
    }


def format_market_briefing(briefing):
    """
    Telegram-friendly rendering of build_market_briefing()'s output —
    same dev/audit pairing as format_world_state()/format_market_
    understanding(). Deliberately NOT yet the eventual 9am/12pm/3pm
    Assistant experience (per chat: this needs checking against real
    scans first) — a plain, readable stand-in so the assembled object
    can be sanity-checked before anything schedules it.

    Never raises — a missing/None field prints as '—' rather than
    crashing the command.
    """
    th = briefing.get("thesis") or {}
    ih = briefing.get("intent_hypothesis")

    lines = ["*Market Briefing* _(dev/audit view — not yet the scheduled push)_", ""]

    lines.append("*Market Thesis*")
    lines.append(th.get("current_state") or "—")
    if th.get("confidence"):
        lines.append(f"_Confidence: {th['confidence']}_")

    lines.append("")
    lines.append("*Market Logic*")
    lines.append(briefing.get("logic") or "_Not enough relationship data yet._")

    if ih:
        if ih.get("locations"):
            lines.append("")
            lines.append("*Areas of Interest*" + (" _(alternatives — no priority between them)_" if len(ih["locations"]) > 1 else ""))
            for loc in ih["locations"]:
                zone = (f" ({loc['zone_low']:.5f}-{loc['zone_high']:.5f})"
                        if loc.get("zone_low") is not None and loc.get("zone_high") is not None else "")
                lines.append(f"  • {loc.get('sentence') or loc.get('code')}{zone}")

        if ih.get("confirmations"):
            lines.append("")
            lines.append("*What would confirm it*")
            for c in ih["confirmations"]:
                lines.append(f"  • {c.get('sentence') or c.get('code')}")

        if ih.get("cautions"):
            lines.append("")
            lines.append("*Not interested in*")
            for c in ih["cautions"]:
                lines.append(f"  • {c.get('sentence') or c.get('code')}")

        if ih.get("unclassified"):
            lines.append("")
            lines.append("_Unclassified watch/caution codes (no role assigned — audit "
                          "scanner_observation.WATCH_CODE_ROLE):_")
            for u in ih["unclassified"]:
                lines.append(f"  • `{u.get('code')}`")

    inv = briefing.get("invalidation")
    if inv:
        lines.append("")
        lines.append(f"*What would invalidate it*\n{inv}")

    lines.append("")
    lines.append("_Ask \"why?\" to see this briefing's reasoning_snapshot/intent_snapshot._")

    return "\n".join(lines)
