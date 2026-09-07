"""
hfis.py — Human-Facing Interface System

CORRECTED (per chat): no paid API calls, anywhere, ever, in the live bot —
cost is a hard constraint, not a preference. This is NOT a wrapper around
Claude or any other model. It's a real, from-scratch "make-shift LLM": a
deep combinatorial phrase grammar that composes genuinely varied narration
from already-finalized structured facts, entirely offline, entirely free.

The render-type sandbox (hfis_sandbox.html) exists to test THIS module's
actual output — the JS in that file mirrors this file's logic exactly, so
what gets tuned in the sandbox is what runs live, not an approximation of it.

Why combinatorial variety actually works here, and isn't just "more
templates": this replaces brain.py's _STATE_TRANSITION_TEMPLATES (4 WHOLE
sentences, picked with random.choice(), {prev}/{curr} substituted — the
confirmed cause of the "stale vocabulary" complaint) with independent axes
that COMPOSE: an opener, a fact-statement, an optional evidence weave, a
closer — each drawn from its own pool of 4-12 options, chosen independently.
Four axes at ~8 options each is already 4,096 combinations for a single
fixed scenario, before the actual price levels and reconciliation text
(which are never templated — always the real numbers/strings) vary anything
at all. That's the actual mechanism "make-shift LLM" refers to: depth
through composition, not a longer flat list.

Per chat: "organism" is a design-conversation metaphor only — it must never
appear in any output here.

Still zero interpretive authority: every fact used below already exists in
the structured input (state.json's mil_understanding, WorldState's
current_condition, MarketIntent's location/confirmation entries). This
module composes prose around real numbers and real strings — it never
invents a level, a count, or a conclusion.

Failure discipline unchanged from the original design: narrate() must never
raise. On any unexpected error it returns None, and the caller
(scanner_live.py) falls back to format_market_event()'s existing template
text — belt-and-suspenders now, rather than the primary expected path,
since this module makes no network call and should essentially always
succeed.
"""

import random


def _fmt(level):
    """Consistent price formatting — 5 decimal places, matching this
    codebase's own convention elsewhere (e.g. brain.py's event headlines)."""
    try:
        return f"{float(level):.5f}"
    except (TypeError, ValueError):
        return str(level)


# ---------------------------------------------------------------------------
# Register banks — keyed by thesis_status. Each is an independent axis:
# an opener and a closer, chosen separately from the fact being stated.
# ---------------------------------------------------------------------------

REGISTER_OPENERS = {
    "SUPPORTED": [
        "Structure's holding up cleanly here.",
        "Nothing complicated about the picture right now —",
        "This one's straightforward:",
        "Still tracking the same story:",
        "No surprises on the higher timeframe —",
        "Textbook continuation so far —",
        "Confidence stays high on this one.",
        "Same read as it's been, and it's still working:",
    ],
    "WEAKENING": [
        "Still the same direction, but it's starting to lose some steam.",
        "Nothing's broken yet, though it's not looking as clean as it was.",
        "Worth keeping an eye on this one — the edges are fraying a bit.",
        "The move's aging, and it's starting to show.",
        "Not a reversal signal, just a bit tired.",
        "Same read as before, with a little less conviction behind it.",
    ],
    "CONTESTED": [
        "This one's genuinely up for debate right now.",
        "Honestly? Not settled. Here's why:",
        "There's a real case on both sides at the moment.",
        "I wouldn't call this one yet.",
        "Two stories are fighting for control here.",
        "This is exactly the kind of spot where it could go either way.",
    ],
    "TRANSITIONING": [
        "Here's where it gets interesting —",
        "Something's genuinely shifting, and it's worth walking through carefully.",
        "The old read hasn't been thrown out, but it's under real pressure now.",
        "This is the moment the story might actually be changing.",
        "Not flipping the switch yet, but it's close.",
        "Pay attention here — this is a real structural challenge, not noise.",
    ],
    "INVALIDATED": [
        "That's it — the level's gone.",
        "Clean break. The old read is done.",
        "No ambiguity here: the level that mattered just got taken out.",
        "That's a real invalidation, not a close call.",
    ],
}

REGISTER_CLOSERS = {
    "SUPPORTED": [
        "Nothing here changes that read.",
        "No reason to second-guess it yet.",
        "Sticking with this until something actually breaks it.",
        "That's the whole story at the moment.",
        "Watching for the first real sign of trouble, but not there yet.",
    ],
    "WEAKENING": [
        "Not enough to change anything yet, but noted.",
        "If this continues, expect the read to shift soon.",
        "Keeping a closer eye on the next few bars than usual.",
        "Still leaning the same way, just watching closer than usual.",
        "Nothing urgent here — a flag, not an alarm.",
    ],
    "CONTESTED": [
        "Would need to see one side actually win before trusting either.",
        "Watching closely — this should resolve one way or another soon.",
        "Not the moment to be confident in either direction.",
        "The next clean break, whichever way it goes, should settle this.",
    ],
    "TRANSITIONING": [
        "One more confirming move and this becomes the new story.",
        "Not confirmed yet — but it's earned the right to be taken seriously.",
        "Give it one more scan before treating this as settled.",
        "The next move decides which side of this wins.",
    ],
    "INVALIDATED": [
        "Fresh slate from here.",
        "Whatever comes next starts from a clean read, not a leftover bias.",
        "That chapter's closed.",
        "Nothing left to defend on the old side.",
    ],
}

# ---------------------------------------------------------------------------
# Structure-flip fact statement — the actual numbers, never templated as a
# whole sentence like brain.py's old approach. Each variant is a different
# SHAPE of sentence, not a synonym swap of the same shape.
# ---------------------------------------------------------------------------

_NEW_LEG_PHRASES = [
    "The new {new_dir} leg is anchored at {new_origin}, already stretched to {new_extreme}.",
    "New structure formed off {new_origin}, and price has pushed as far as {new_extreme} since.",
    "{new_extreme} is where price sits now, with the fresh {new_dir} leg's origin back at {new_origin}.",
    "Origin on the new leg: {new_origin}. It's already run to {new_extreme}.",
    "Price built a new {new_dir} structure from {new_origin} and hasn't looked back — {new_extreme} as of now.",
]

_OLD_LEVEL_PHRASES = [
    "the old {old_dir} leg's line in the sand was {old_origin}",
    "{old_origin} was the level that would have made the {old_dir} case officially dead",
    "the prior {old_dir} structure's origin, {old_origin}, is what actually mattered here",
    "{old_origin} — that's the level the old read needed to lose",
]

_RECONCILIATION_LEADINS_NOT_INVALIDATED = [
    "Here's the part that actually matters:",
    "And this is the honest part:",
    "What actually justified the change:",
    "Worth being precise about why:",
    "Not going to dress this up —",
    "Straight answer on why:",
]

_RECONCILIATION_LEADINS_INVALIDATED = [
    "And this one's clean —",
    "No ambiguity on why:",
    "Simple reason this time:",
    "Nothing subtle about this one:",
]

# ---------------------------------------------------------------------------
# Struggle/rejection phrases — per chat ("add rejections"). Only fires on a
# genuinely repeated streak (count >= 2) against the leg's own extreme;
# count == 1 is just a single normal wick, not "struggling" yet, and count
# == 0 means nothing to say. Real number, real level — never invented.
# ---------------------------------------------------------------------------

_STRUGGLE_PHRASES = [
    "This is the {count_word} time it's tried and failed to clear {level}.",
    "{level} has held on every single test so far — {count} attempts now.",
    "Keeps finding a wall at {level}. That's {count} failed pokes at it.",
    "Can't seem to get through {level} no matter how many times it comes back — {count} tries and counting.",
    "{count} separate failed attempts at {level} now. Something's absorbing every push.",
]

_COUNT_WORDS = {2: "second", 3: "third", 4: "fourth", 5: "fifth"}


def _struggle_phrase(rejection_count, level):
    if not rejection_count or rejection_count < 2 or level is None:
        return ""
    count_word = _COUNT_WORDS.get(rejection_count, f"{rejection_count}th")
    return random.choice(_STRUGGLE_PHRASES).format(
        count_word=count_word, count=rejection_count, level=_fmt(level))


def _compose_structure_flip(headline_text, cc, mil_understanding):
    cc = cc or {}
    new_origin = cc.get("macro_leg_origin")
    new_extreme = cc.get("macro_leg_extreme")
    old_origin = cc.get("prior_macro_leg_origin")
    new_dir = (cc.get("macro_bias") or "").lower() or "new"
    old_dir = "bearish" if new_dir == "bullish" else "bullish" if new_dir == "bearish" else "prior"

    status = (mil_understanding or {}).get("thesis_status")
    parts = [random.choice(REGISTER_OPENERS["TRANSITIONING"]) if status == "TRANSITIONING"
             else "Structure just flipped."]

    if new_origin is not None and new_extreme is not None:
        parts.append(random.choice(_NEW_LEG_PHRASES).format(
            new_dir=new_dir, new_origin=_fmt(new_origin), new_extreme=_fmt(new_extreme)))

    history = (mil_understanding or {}).get("history") or []
    last_transition = history[-1] if history else None

    if last_transition:
        if last_transition.get("invalidation_fired"):
            if old_origin is not None:
                parts.append(
                    f"{random.choice(_RECONCILIATION_LEADINS_INVALIDATED)} "
                    f"{random.choice(_OLD_LEVEL_PHRASES).format(old_origin=_fmt(old_origin), old_dir=old_dir)}, "
                    f"and price closed clean through it "
                    f"({last_transition.get('invalidation_mechanism', 'invalidation')})."
                )
        elif last_transition.get("reconciliation"):
            lead = random.choice(_RECONCILIATION_LEADINS_NOT_INVALIDATED)
            old_ref = ""
            if old_origin is not None:
                old_ref = (f" {random.choice(_OLD_LEVEL_PHRASES).format(old_origin=_fmt(old_origin), old_dir=old_dir)}, "
                           f"and it was never breached.")
            parts.append(f"{lead}{old_ref} {last_transition['reconciliation']}")

    if status and status in REGISTER_CLOSERS:
        parts.append(random.choice(REGISTER_CLOSERS[status]))

    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Confirmed-transition composer
# ---------------------------------------------------------------------------

_CONFIRMED_OPENERS = [
    "That's a real confirmation, not a maybe:",
    "This just became official:",
    "The reclaim just went through:",
]

_CONFIRMED_BODY = [
    "Price took back {old_origin} — the prior leg's origin — and that's the confirming move.",
    "{old_origin} just got reclaimed, which is exactly what this needed to become real.",
]

_CONFIRMED_CLOSERS = [
    "Consider this locked in unless something equally clean reverses it.",
    "Solid footing from here.",
    "That's the kind of move that actually earns a change of mind.",
]


def _compose_confirmed_transition(cc):
    cc = cc or {}
    old_origin = cc.get("prior_macro_leg_origin")
    parts = [random.choice(_CONFIRMED_OPENERS)]
    if old_origin is not None:
        parts.append(random.choice(_CONFIRMED_BODY).format(old_origin=_fmt(old_origin)))
    parts.append(random.choice(_CONFIRMED_CLOSERS))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Opportunity composer
# ---------------------------------------------------------------------------

_OPPORTUNITY_OPENERS = [
    "New spot worth watching just showed up:",
    "Something new on the radar —",
    "Adding this to the watch list:",
    "Fresh area of interest:",
    "Worth flagging —",
]

_ZONE_PHRASES = [
    "{sentence}, sitting between {zone_low} and {zone_high}.",
    "{sentence} — the zone runs {zone_low} to {zone_high}.",
    "{sentence}. Range on it: {zone_low}-{zone_high}.",
    "{sentence}, boxed in between {zone_low} and {zone_high}.",
]

_OPPORTUNITY_CLOSERS = [
    "Not acting on it yet — just tracking.",
    "Nothing to do here except watch how price treats it.",
    "Filed away for now.",
    "Keeping this one on the list.",
]


def _compose_opportunity(intent_hypothesis, headline_text):
    ih = intent_hypothesis or {}
    locations = ih.get("locations_watching") or ih.get("locations") or []
    parts = [random.choice(_OPPORTUNITY_OPENERS)]
    if locations:
        entry = locations[-1]  # most recently added
        sentence = entry.get("sentence", entry.get("code", "a new level"))
        if entry.get("zone_low") is not None and entry.get("zone_high") is not None:
            parts.append(random.choice(_ZONE_PHRASES).format(
                sentence=sentence, zone_low=_fmt(entry["zone_low"]), zone_high=_fmt(entry["zone_high"])))
        else:
            parts.append(sentence + ".")
    else:
        parts.append(headline_text)
    parts.append(random.choice(_OPPORTUNITY_CLOSERS))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Evidence weave — for working ONE existing evidence/weakness string
# (already well-formed English from MarketThesis) into the narration with
# varied connective tissue, rather than dumping a list or ignoring them.
# ---------------------------------------------------------------------------

_SUPPORTING_CONNECTORS = [
    "and for what it's worth,", "backing this up:", "which lines up with",
    "on top of that,", "adding weight to this:",
]
_CONFLICTING_CONNECTORS = [
    "though,", "that said,", "worth weighing against this:",
    "the counterpoint:", "still,", "the thing nagging at this read:",
]


def _weave_evidence(mil_understanding):
    mu = mil_understanding or {}
    supporting = mu.get("supporting_evidence") or []
    conflicting = mu.get("conflicting_evidence") or []
    if conflicting and random.random() < 0.6:
        return f"{random.choice(_CONFLICTING_CONNECTORS)} {random.choice(conflicting)}."
    if supporting:
        return f"{random.choice(_SUPPORTING_CONNECTORS)} {random.choice(supporting)}."
    if conflicting:
        return f"{random.choice(_CONFLICTING_CONNECTORS)} {random.choice(conflicting)}."
    return ""


# ---------------------------------------------------------------------------
# Generic fallback — for any event category not specifically composed above.
# Still varies the wrapping even though it has to fall back to the raw
# headline for the actual fact.
# ---------------------------------------------------------------------------

_GENERIC_OPENERS = [
    "Worth a note:", "Quick update —", "Something changed:",
    "Flagging this:", "Here's the latest:",
]


def _compose_generic(headline_text):
    return f"{random.choice(_GENERIC_OPENERS)} {headline_text}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def narrate(events, understanding, intent_hypothesis, mil_understanding=None):
    """
    Returns a composed narration string, or None if there's nothing to
    narrate or something unexpected goes wrong — caller falls back to
    format_market_event()'s template text either way. Never raises.
    """
    try:
        if not events:
            return None
        cc = (understanding or {}).get("current_condition") or {}
        rendered = []
        for e in events:
            category = e.get("category")
            headline = e.get("headline", "")
            if category == "structure":
                rendered.append(_compose_structure_flip(headline, cc, mil_understanding))
            elif category == "opportunity":
                rendered.append(_compose_opportunity(intent_hypothesis, headline))
            elif "confirmed" in (headline or "").lower() or "reclaimed" in (headline or "").lower():
                rendered.append(_compose_confirmed_transition(cc))
            else:
                rendered.append(_compose_generic(headline))

        evidence_line = _weave_evidence(mil_understanding)
        if evidence_line and rendered:
            rendered[0] = rendered[0] + " " + evidence_line

        struggle_line = _struggle_phrase(
            cc.get("macro_leg_extreme_rejection_count"),
            cc.get("macro_leg_last_rejection_price") or cc.get("macro_leg_extreme"))
        if struggle_line and rendered:
            rendered.append(struggle_line)

        text = " ".join(r for r in rendered if r).strip()
        return text or None
    except Exception as e:
        print("[HFIS NARRATE ERROR] " + str(e))
        return None
