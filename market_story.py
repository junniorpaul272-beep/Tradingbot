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
stating WHAT concretely happened, built ONLY from facts already
computed and already exposed elsewhere (current_condition's rejection
counts, MIL's own invalidation_mechanism/reconciliation, Brain's
thesis_weaknesses_prose). It never re-detects anything and never
invents a level or a count — same discipline as hfis.py.

Both classify_events() and build_mechanism() are defensive: neither
raises. A classification error fails OPEN (defaults to CHALLENGE, a
push-worthy impact) rather than silently swallowing an event that
evaluate_market_event() already decided was significant — the one
thing worse than a slightly-wrong label here is a dropped event no one
ever sees. A mechanism-building error returns "" (say nothing), which
is always a safe degrade for a supplementary clause.
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
    condition, mil_understanding.history, thesis_weaknesses_prose);
    this function adds no new detection and cites no number that isn't
    already sitting in one of those structures.

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

        if category == "state":
            rc = cc.get("macro_leg_extreme_rejection_count")
            last_px = cc.get("macro_leg_last_rejection_price")
            if rc and rc >= 2 and last_px is not None:
                return f"{rc} failed attempts against {last_px:.5f} so far."
            weaknesses = (understanding or {}).get("thesis_weaknesses_prose") or []
            if weaknesses:
                return weaknesses[0]

        return ""
    except Exception as e:
        print("[MARKET STORY MECHANISM ERROR] " + str(e))
        return ""
