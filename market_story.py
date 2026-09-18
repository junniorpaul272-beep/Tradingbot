"""
market_story.py — Story Interpreter (Market Event impact layer)

ADDED (2026-09-15, per chat — friend's architectural review of the
notification layer, credited to the same collaborator as brain.py's
Vally-attributed Market Event work). Sits BETWEEN brain.py's
evaluate_market_event() (the significance filter — decides IF a delta
is even a candidate) and hfis.py's narrate() (the narrator — decides
HOW to say it). Neither of those asks the question this module answers:
does this event support, challenge, or break the story we're currently
telling about this market, and is that impact worth interrupting the
user for.

WHY THIS IS A NEW MODULE AND NOT A BIGGER evaluate_market_event()
(per chat — "don't make the Event Engine a giant list of patterns"):
evaluate_market_event() is a stable, already-correct significance gate
sitting on top of Brain's synthesis (bias flips, state-family
transitions, new locations, Tier 3). This module does not touch that
gate, does not re-derive anything it already decided, and does not add
a fourth `if` branch to it. It is a strictly-additive classification
pass over whatever that gate already returned — same layering
discipline the rest of this codebase already uses (WorldState doesn't
re-derive LIVE's sensors; Brain doesn't re-derive WorldState's facts).

TAXONOMY (per chat, friend's "story impact" proposal):
    IGNORE      — technically true, not worth a word (not currently
                  reachable from evaluate_market_event(), which already
                  only returns significant deltas — kept in the enum
                  for when a lower-level/raw event source is ever added
                  upstream of this layer, so it has somewhere to go
                  other than a bespoke suppression rule).
    BACKGROUND  — a real, named change, but not push-worthy on its own
                  (e.g. a lateral state-family shuffle — see
                  _classify_state() below).
    SUPPORT     — strengthens the current story.
    CHALLENGE   — weakens the current story.
    BREAK       — invalidates or materially changes the story.
    OPPORTUNITY — a concrete thing now worth watching.
    SURPRISE    — price did something the active thesis didn't expect,
                  without a clean mechanical invalidation to point to.

Only SUPPORT/CHALLENGE/BREAK/OPPORTUNITY/SURPRISE are push-worthy (see
PUSH_WORTHY / push_worthy() below) — IGNORE/BACKGROUND are still
returned (so callers can log/shadow them) but never reach Telegram.

THE MISSING "A" RUNG (per chat — the friend's core complaint: "jumping
from A to B, then dressing B up with terminology"). build_mechanism()
below is the other half of this module: a short, evidence-first clause
stating WHAT concretely happened — for a "structure" event, from MIL's
own invalidation_mechanism/reconciliation; for a "state" event, it
deliberately returns nothing now (see build_mechanism()'s own docstring
for why — Brain's primary_thesis/primary_threat, via build_market_
story() below, already carry that for state events, and the rejection-
count fact used to be duplicated here was already stated once by
hfis.py's own evidence-weave tail). It never re-detects anything, never
invents a level or a count, and never reads thesis_weaknesses_prose —
same discipline as hfis.py.

Both classify_events() and build_mechanism() are defensive: neither
raises. A classification error fails OPEN (defaults to CHALLENGE, a
push-worthy impact) rather than silently swallowing an event that
evaluate_market_event() already decided was significant — the one
thing worse than a slightly-wrong label here is a dropped event no one
ever sees. A mechanism-building error returns "" (say nothing), which
is always a safe degrade for a supplementary clause.

ALSO IN THIS MODULE (added same day, see each function's own docstring
for detail): build_market_story() — the friend's 8-field Market Story
schema, a pass-through of Brain's market_assessment; situation_key() /
is_repeat_situation() — the "one event, one narrative identity" dedup,
keyed off Brain's own canonical per-situation state string. All three
are called from scanner_live.py, never from hfis.py directly — this
file and hfis.py are mutually forbidden from importing each other (see
check_layer_imports.py), so anything this module produces for hfis.py
to render is passed through scanner_live.py as a plain dict.
"""

from enum import Enum

from brain import _state_family


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
    headline = (event.get("headline") or "").lower()
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
        if "confirmed" in headline or "reclaimed" in headline:
            return StoryImpact.SUPPORT
        if latest and latest.get("invalidation_fired"):
            return StoryImpact.BREAK
        return StoryImpact.SURPRISE

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
