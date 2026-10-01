"""
STEP 2 REDUCTION (2026-09-30, communication rebuild, per chat): this file
now contains ONLY fact functions — the relate_* comparisons,
determine_structural_state, interpret_structure, _reaccelerating and
build_market_intent_hypothesis — which market_read.py (the interpretation
authority) and scanner_live.py import. Everything else that used to live
here was deleted because it had exactly one legitimate owner elsewhere:
  - synthesis / event / state-family machinery and the template constants
    -> market_read.py (byte-identical copies; evaluate_market_event here was
       the STALE 5-argument version, never called)
  - format_* / _plain_clause (presentation) -> market_presenter.py
    (byte-identical copies, verified by diff before deletion)
  - build_market_briefing / build_market_understanding / build_market_logic
    (briefing assembly) -> zero live callers; dead
The docstring below is the original Phase 2 design history and still
describes the surviving functions accurately; references to deleted
functions in it are history, not current API.
"""
"""
brain.py — Phase 2 (per chat, 2026-08-19): the interpretation layer over
WorldState. Its one job is turning "here is everything the system
currently knows" (WorldState) into "here is the market story those facts
describe" (a Market Understanding) — the layer between raw facts and the
eventual conversational Assistant.

THE BOUNDARY, ENFORCED STRUCTURALLY, NOT JUST BY CONVENTION: this module
imports nothing from scanner_common / scanner_observation / min_scanner /
scanner_live / worldstate. It receives a WorldState dict (from
worldstate.build_world_state()) and nothing else — no candles, no direct
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



