"""
hfis.py — Human-Facing Interface System (the Narrator)

═══════════════════════════════════════════════════════════════════════
PIPELINE MAP — see brain.py's evaluate_market_event() docstring for the
full path from WorldState through this file to Telegram. Short version:
this is the ONLY module that composes Telegram-facing market prose;
scanner_live.py is the only caller permitted to import it
(check_layer_imports.py enforces this structurally, not just by
convention — min_scanner.py runs as a separate GitHub Actions job and
is forbidden from importing this file).

THIS FILE NEVER READS (ban is structural, not a style preference):
  - thesis_weaknesses / thesis_weaknesses_prose (understanding's fields)
  - mil_understanding["conflicting_evidence"]
Both are internal risk-scoring content — every current weakness
category (ob_mitigated, atr_floor, ema_extension, leg_break_count,
bias_stale) is exactly the kind of internal diagnostic that must never
reach a human reader, prose-quality or not. This was violated once
(_weave_evidence(), fixed 2026-09-15) and once in market_story.py
(build_mechanism(), fixed the same day) — if you're adding a new
composer here, check whatever you're reading against this list before
wiring it in.
═══════════════════════════════════════════════════════════════════════

CORRECTED (per chat): no paid API calls, anywhere, ever, in the live bot —
cost is a hard constraint, not a preference. This is NOT a wrapper around
Claude or any other model. It's a real, from-scratch "make-shift LLM": a
deep combinatorial phrase grammar that composes genuinely varied narration
from already-finalized structured facts, entirely offline, entirely free.

The render-type sandbox (hfis_sandbox.html) exists to test THIS module's
actual output — the JS in that file mirrors this file's logic exactly, so
what gets tuned in the sandbox is what runs live, not an approximation of it.

Why combinatorial variety actually works here, and isn't just "more
templates": structure/opportunity events compose from independent axes —
an opener, a fact-statement, an optional evidence weave, a closer — each
drawn from its own pool of 4-12 options, chosen independently. Four axes
at ~8 options each is already 4,096 combinations for a single fixed
scenario, before the actual price levels and reconciliation text (which
are never templated — always the real numbers/strings) vary anything at
all. That's the actual mechanism "make-shift LLM" refers to: depth
through composition, not a longer flat list.

STATE-CATEGORY EVENTS ARE DIFFERENT (2026-09-15, per chat — see
_compose_state_story()'s docstring): these no longer use a combinatorial
template at all. brain.py's market_assessment already contains a fully-
synthesized, hand-written-per-situation market story (primary_thesis /
primary_threat / next_watch) — market_story.build_market_story() packages
that into a plain dict (this file never imports market_story.py directly;
scanner_live.py builds the dict and passes it in), and _compose_state_
story() reads that dict directly. brain.py's old _STATE_TRANSITION_
TEMPLATES + _state_family_label() combination (4 sentence shells,
{prev}/{curr} substituted from internal family names) still exists and is
still used, but ONLY as the fallback _compose_generic() reaches for if
that dict is somehow empty — not the primary path anymore.

Per chat: "organism" is a design-conversation metaphor only — it must never
appear in any output here.

Still zero interpretive authority: every fact used below already exists in
the structured input (state.json's mil_understanding, WorldState's
current_condition/market_assessment, MarketIntent's location/confirmation
entries). This module composes prose around real numbers and real
strings — it never invents a level, a count, or a conclusion.

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

_COUNT_WORDS = {2: "second", 3: "third", 4: "fourth", 5: "fifth"}


def _struggle_phrase(rejection_count, level):
    """
    UPDATED (2026-09-15, per chat — behavioral vocabulary pass): now
    routes through compose_behavior("rejection", ...) below instead of
    its own separate phrase pool, so "rejection" has exactly one bank
    of phrases whether it's reached via this gate or called directly.
    Gating (count >= 2, a real level) is unchanged — still the only
    thing deciding WHETHER to say something; compose_behavior only
    decides HOW.
    """
    if not rejection_count or rejection_count < 2 or level is None:
        return ""
    count_word = _COUNT_WORDS.get(rejection_count, f"{rejection_count}th")
    return compose_behavior("rejection", count=rejection_count, count_word=count_word, level=_fmt(level))


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

# _OPPORTUNITY_OPENERS RETIRED (2026-09-16, found during a stress-test
# pass). Was prepended to headline_text in _compose_opportunity() below —
# removed the same day because every opportunity headline already IS a
# complete announcement sentence (see that function's updated docstring);
# stacking another opener on top double-announced the same fact.

# _ZONE_PHRASES RETIRED (2026-09-16, found during a stress-test pass).
# Was paired with intent_hypothesis's locations[-1] lookup in _compose_
# opportunity() below — removed the same day for reaching into "current"
# state instead of using the event's own headline_text (see that
# function's updated docstring). headline_text already carries its zone
# bounds baked in, so nothing needs re-formatting them separately anymore.

_OPPORTUNITY_CLOSERS = [
    "Not acting on it yet — just tracking.",
    "Nothing to do here except watch how price treats it.",
    "Filed away for now.",
    "Keeping this one on the list.",
]


def _compose_opportunity(headline_text):
    """
    FIXED (2026-09-16, found during a stress-test pass, not previously
    reported): this used to always render intent_hypothesis's CURRENT
    last location (locations[-1]) whenever any location existed,
    completely ignoring `headline_text` — the actual, specific content
    for THIS event. Harmless when exactly one opportunity event fires
    in a scan (locations[-1] happens to be the right one), but Tier 3
    (evaluate_market_event(), brain.py) can fire a SECOND, independent
    "opportunity" event in the same scan — and both calls would render
    the same locations[-1] zone text, so a Tier 3 CHoCH event showed up
    as a duplicate of whatever zone was last added, with no mention of
    Tier 3 at all. Confirmed by reproducing it directly. `headline_text`
    is already a complete, specific sentence with real zone bounds baked
    in (see evaluate_market_event()'s _zones construction) — using it
    directly is strictly correct, not just a workaround.

    No opener prepended (2026-09-16, same pass): every opportunity
    headline (_NEW_OPPORTUNITY_TEMPLATES / Tier 3's own sentence, both
    in brain.py) already IS a complete announcement — "A new spot worth
    watching just came into range: ..." — prepending _OPPORTUNITY_
    OPENERS on top double-announced ("Something new on the radar — A
    new spot worth watching just came into range: ..."). The closer
    still varies; that's pure flavor with no content to duplicate.
    """
    parts = [headline_text, random.choice(_OPPORTUNITY_CLOSERS)]
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Evidence weave — for working ONE existing evidence string (already
# well-formed English from MarketThesis.evidence_prose) into the narration
# with varied connective tissue, rather than dumping a list or ignoring it.
# ---------------------------------------------------------------------------

_SUPPORTING_CONNECTORS = [
    "and for what it's worth,", "backing this up:", "in line with that:",
    "on top of that,", "adding weight to this:",
]
# GRAMMAR FIX (2026-09-16, per chat — confirmed via a production
# screenshot: "Primary read: bearish. Which lines up with the higher-
# timeframe bias is bearish." — a broken sentence fragment). The old
# entry, "which lines up with", is a mid-sentence continuation phrase
# ("this lines up WITH [a noun]") that every call site here force-
# capitalizes and periods into a standalone sentence — grammatical only
# when paired with a noun phrase, broken when paired with a full clause
# like "the higher-timeframe bias is bearish" (which every current
# supporting-evidence entry actually is). Replaced with "in line with
# that:", which reads correctly as a complete sentence opener regardless
# of what full-clause fact follows it.
# _CONFLICTING_CONNECTORS RETIRED (2026-09-15, per chat — deep-clean pass).
# Was paired with mil_understanding's conflicting_evidence, which is
# MarketThesis.weaknesses_prose — every current weakness category is
# internal-diagnostic content (see _weave_evidence()'s docstring below),
# so nothing in this codebase should ever be pulling from that bank for
# human narration. Deleted rather than left as unused-but-present, so it
# can't get quietly reconnected later without someone re-reading why it
# was removed. If a genuinely market-fact weakness category gets added to
# scanner_observation.py in the future, a connector bank like this one
# would be the right shape to reintroduce for it — deliberately, not by
# resurrecting this one blind.


def _weave_evidence(mil_understanding):
    """
    UPDATED (2026-09-15, per chat — deep-clean pass, caught by tracing
    mil_understanding's fields back to their source rather than trusting
    the dataclass comment). `conflicting_evidence` is MarketThesis.
    weaknesses_prose (mil.py's own MarketUnderstanding.conflicting_
    evidence docstring confirms this) — and every weakness category
    scanner_observation.py currently defines (ob_mitigated, atr_floor,
    ema_extension, leg_break_count, bias_stale) is exactly the internal-
    diagnostic content already banned from human narration elsewhere in
    this file (see market_story.build_mechanism()'s docstring for the
    same reasoning, applied there first). Prose-quality wording doesn't
    change the category — "the order block backing this has already
    been mitigated" is grammatically fine and still an internal
    diagnostic. `conflicting_evidence` is therefore never surfaced here,
    full stop, regardless of prose vs. raw.

    `supporting_evidence` (= MarketThesis.evidence_prose) is different —
    it's HTF bias, a fresh BOS, a confirming CHoCH, an unmitigated OB, or
    expanding 1H volatility: real market facts, already prose-formatted,
    none of them internal-diagnostic categories. Kept.
    """
    mu = mil_understanding or {}
    supporting = mu.get("supporting_evidence") or []
    if supporting:
        return f"{random.choice(_SUPPORTING_CONNECTORS)} {random.choice(supporting)}."
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
# RETIRED (2026-09-16, per chat — the Library redesign). Everything that
# used to live in this block — _time_bucket()/_EVENT_LEADINS/_QUIET_
# LEADINS, _VOLATILITY_PHRASES, _compose_current_conditions(),
# compose_deep_read_plain(), _RECAP_*/_compose_recap(), and narrate_
# overview() itself — has moved to narration_library.py (light_summary,
# deep_read, recap_line, elapsed_label) as pure, deterministic functions
# over the story dict. scanner_live.py now calls those directly and
# assembles the overview push itself, rather than calling into hfis.py
# for it. Nothing here was deleted for being wrong — see narration_
# library.py's own module docstring for why moving it (not just fixing
# it in place) was the actual point: the old versions each reached into
# raw understanding/mil_understanding/intent_hypothesis independently,
# which is exactly what let one composer duplicate another's content
# without either of them knowing it was happening.
# ---------------------------------------------------------------------------
def narrate_thesis_change(mil_understanding, prior_direction, prior_status):
    """
    NEW (2026-09-12, per chat): "when there's a thesis change... let it
    automatically send the new thesis, but this time a brief summary of
    the prior thesis and why it changed... the price points or conditions
    that triggered the change." Two parts, always in this order: (1) what
    the read WAS and what actually moved it — reusing the same "why" data
    _compose_structure_flip() already draws on (mil_understanding's own
    latest history entry: transition_cause/invalidation_mechanism/
    reconciliation — never invented, always the real record from
    reconcile()); (2) the fresh picture via narrate_deep_thesis() below —
    same function /thesis now uses, so the deep section is consistent
    everywhere it appears rather than a second, slightly-different
    version of the same content.

    `prior_direction`/`prior_status` must come from the caller's OWN
    pre-reconcile() snapshot (this file has no persistence, per its
    header — it can only compose from what it's handed). Returns None if
    there's nothing coherent to say (no real prior state to contrast
    against) rather than fabricate a before/after that isn't real.
    """
    try:
        mu = mil_understanding or {}
        history = mu.get("history") or []
        latest = history[-1] if history else None
        if not latest or not prior_direction or not prior_status:
            return None

        parts = [random.choice([
            "The read just changed — here's what moved and why.",
            "Update: the thesis shifted. Walking through what changed first.",
            "Something worth flagging — the read on this just moved.",
            "Quick pivot to explain before the new picture:",
        ])]

        parts.append(
            f"Up to now this was reading {prior_direction.lower()}, {prior_status.lower()}."
        )

        if latest.get("invalidation_fired"):
            mech = latest.get("invalidation_mechanism") or "a clean structural break"
            parts.append(f"{random.choice(_RECONCILIATION_LEADINS_INVALIDATED)} {mech} is what ended it.")
        elif latest.get("reconciliation"):
            parts.append(f"{random.choice(_RECONCILIATION_LEADINS_NOT_INVALIDATED)} {latest['reconciliation']}")
        elif latest.get("transition_cause") and latest["transition_cause"] != "UNKNOWN":
            parts.append(f"Cause on record: {latest['transition_cause'].replace('_', ' ').lower()}.")

        why_section = " ".join(parts)
        deep_section = narrate_deep_thesis(mu)
        if not deep_section:
            return why_section

        return why_section + "\n\n" + deep_section
    except Exception as e:
        print("[HFIS NARRATE_THESIS_CHANGE ERROR] " + str(e))
        return None


def narrate_deep_thesis(mil_understanding):
    """
    NEW (2026-09-11, per chat): "/thesis aren't what I requested... the
    in-depth thesis/analysis where there's primary thesis, counter thesis,
    failure conditions, etc." Traced precisely first, before writing this:
    the 9/12/3 overview (at the time, narrate_overview() — since retired,
    see this function's own "NARROWED SCOPE" note below) was a
    deliberately LIGHT summary and already behaved that way — its own
    register-bank phrases produce short, few-sentence output, confirmed
    against a real example. The actual gap was that NOTHING anywhere
    rendered MIL's structured belief at all — /thesis (format_market_
    thesis(), min_scanner.py) only ever read Brain's older, separate
    market_thesis_* fields (current_state/evidence_prose/invalidation),
    populated before MIL existed, and never once touched mil_understanding.
    Primary thesis, counter thesis, and the actual failure condition were
    being computed and persisted every scan and shown NOWHERE.

    This is the missing surface. Same voice/discipline as everything else
    in this file — composed prose from real values, reusing the existing
    register banks and evidence weave rather than inventing a second
    vocabulary, never a raw field dump. Returns None (not an empty
    section) if the thesis isn't populated yet or the shape is
    unexpected — same "degrade to nothing rather than show something
    broken" discipline as narrate().

    NARROWED SCOPE (2026-09-16, per chat): this used to ALSO be the
    "🔬 Deeper read" section bolted onto the 9/12/3 Market Overview push
    (scanner_live.py) — a 2026-09-15 fix that took the user's original
    ask ("primary thesis, counter thesis, failure conditions in the
    overview") and answered it by calling THIS narrated-prose function.
    The user has since been explicit that narrated prose is not what
    they want for that section: "not as sweet words exactly how it is" —
    they want the actual fields, labeled, plainly. That call site briefly
    used compose_deep_read_plain() (a plain-labeled version added in this
    file 2026-09-16), then moved again the same day to narration_
    library.deep_read() as part of the Library redesign — see that
    module's own docstring. This function (narrate_deep_thesis) is
    UNCHANGED and still used by /thesis (format_market_thesis(),
    min_scanner.py) — a separate, still-valid surface where prose was
    the original and correct ask.
    """
    try:
        mu = mil_understanding or {}
        status = mu.get("thesis_status")
        primary = mu.get("primary_thesis") or {}
        direction = primary.get("direction")
        if not status or not direction:
            return None

        parts = [
            f"{random.choice(REGISTER_OPENERS.get(status, REGISTER_OPENERS['SUPPORTED']))} "
            f"Primary read: {direction.lower()}."
        ]

        evidence_line = _weave_evidence(mu)
        if evidence_line:
            parts.append(evidence_line[0].upper() + evidence_line[1:])

        counter = mu.get("counter_thesis")
        if counter and counter.get("direction"):
            c_dir = counter["direction"].lower()
            c_status = (counter.get("status") or "awaiting_confirmation").replace("_", " ")
            watching = counter.get("watching_for") or []
            if watching:
                w = watching[0]
                w_sentence = w.get("sentence", w.get("code", ""))
                # PRICE-LEVEL FIX (2026-09-12, per chat — "well defined...
                # price levels of interest, be it reaction or evidence of
                # interest from the opposition"): the watch entry's own
                # zone_low/zone_high already exist (same fields
                # _compose_opportunity() already uses for regular
                # opportunity events) — this was building the sentence
                # from `sentence` alone and dropping them on the floor.
                if w.get("zone_low") is not None and w.get("zone_high") is not None:
                    w_sentence += f" (zone: {_fmt(w['zone_low'])}-{_fmt(w['zone_high'])})"
                parts.append(
                    f"There's a {c_dir} counter-case forming too, {c_status} — {w_sentence}"
                )
            else:
                parts.append(f"There's a {c_dir} counter-case forming too, {c_status}.")

        fc = mu.get("failure_condition") or {}
        origin_level = fc.get("origin_level")
        origin_dir = fc.get("origin_direction")
        if origin_level:  # 0.0 is mil.py's own explicit "not yet filled" placeholder
            level_phrase = (f"a clean close {origin_dir} {_fmt(origin_level)}"
                             if origin_dir else _fmt(origin_level))
            parts.append(f"This read fails on {level_phrase}.")

        if status in REGISTER_CLOSERS:
            parts.append(random.choice(REGISTER_CLOSERS[status]))

        return " ".join(parts)
    except Exception as e:
        print("[HFIS NARRATE_DEEP_THESIS ERROR] " + str(e))
        return None





# ---------------------------------------------------------------------------
# Behavioral vocabulary — per chat (friend's proposed vocabulary: describe
# what price DID, not the internal machinery that detected it). Each tag
# below is a composer bank in the same combinatorial-variety style as the
# rest of this file, ready to be driven by a `behavior` tag + a small
# facts dict wherever a caller has one to offer.
#
# HONEST STATUS (per chat — do not overclaim what's wired vs. scaffolded):
# "rejection" is LIVE — compose_behavior("rejection", ...) is now what
# _struggle_phrase()'s call site should route through (see narrate()'s
# struggle_line, wired below). The other eleven tags (continuation,
# failed_continuation, stalling, acceptance, sweep_reversal, compression,
# expansion, failed_breakout, retracement, deep_retracement,
# structural_change, recovery_reclaim, displacement) are fully written
# and ready to call, but NOTHING upstream currently detects/tags most of
# them — that's sensor work in scanner_observation.py this pass didn't
# touch. Wiring a new tag in is: (1) compute the fact in scanner_
# observation.py, (2) attach it to the event/leg record, (3) call
# compose_behavior(tag, **facts) from hfis.py/market_story.py. Until
# step (1)+(2) happen for a given tag, it is dead code — real prose,
# no live caller yet.
# ---------------------------------------------------------------------------

_BEHAVIOR_PHRASES = {
    "continuation": [
        "{direction} just extended through {level} with another clean push.",
        "Another clean {direction} move — price cleared {level} without hesitating.",
    ],
    "failed_continuation": [
        "Price broke {level}, but the break didn't hold — it's already back above/below it.",
        "That break of {level} got reclaimed almost immediately. Structural break, no follow-through.",
    ],
    "stalling": [
        "The push toward {level} is losing steam — each attempt is covering less ground than the last.",
        "Momentum's fading here. Price is still trying for {level}, but with noticeably less force each time.",
    ],
    "rejection": [
        "This is the {count_word} time it's tried and failed to clear {level}.",
        "{level} has held on every single test so far — {count} attempts now.",
        "Can't get through {level} no matter how many times it comes back — {count} tries and counting.",
    ],
    "acceptance": [
        "Price isn't just probing {level} anymore — it's building structure on the other side of it.",
        "That's acceptance, not a wick — price is spending real time through {level} now.",
    ],
    "sweep_reversal": [
        "Price took {level} out, then immediately snapped back on the other side of it.",
        "That sweep of {level} didn't hold — the reversal right after it is the more interesting part.",
    ],
    "compression": [
        "Price keeps pressing against the same area without separating from it — compressing, not resolving.",
        "This has tightened up considerably. Something's being absorbed here; direction isn't proven yet.",
    ],
    "expansion": [
        "That compression just resolved — this move is notably bigger than what came before it.",
        "Range just broke open. Bigger bars, real separation from where it was coiling.",
    ],
    "failed_breakout": [
        "That break outside the range didn't hold — price is already back inside it.",
        "No follow-through on that breakout. It's fallen right back into the range.",
    ],
    "retracement": [
        "This isn't a break — price is pulling back through the move, not against it.",
        "Simple retracement so far; the underlying leg hasn't been challenged.",
    ],
    "deep_retracement": [
        "This pullback is running deeper than usual relative to the original move.",
        "More of the move is being given back than is typical here — structure hasn't broken, but it's a deep one.",
    ],
    "structural_change": [
        "The swing that had been protecting this structure has now broken.",
        "This is the first structural development actually worth flagging — the protecting swing is gone.",
    ],
    "recovery_reclaim": [
        "Price just reclaimed the level that had been defended against it.",
        "That's a real reclaim — the level the other side was defending just went.",
    ],
    "displacement": [
        "That's the strongest push we've seen since this leg started, and it cleared the prior short-term high/low with it.",
        "Real displacement here — this move covered more ground, faster, than anything recent.",
    ],
}


def compose_behavior(tag, **facts):
    """
    Composes one behavioral-vocabulary sentence for `tag` (see
    _BEHAVIOR_PHRASES above), formatting whichever of `level`/
    `direction`/`count`/`count_word` the chosen template needs from
    `facts`. Returns "" for an unrecognized tag or a formatting mismatch
    (missing fact the template needed) — never raises, never fabricates
    a value that wasn't passed in.
    """
    try:
        pool = _BEHAVIOR_PHRASES.get(tag)
        if not pool:
            return ""
        template = random.choice(pool)
        return template.format(**{k: (v if v is not None else "") for k, v in facts.items()})
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _compose_state_story(story):
    """
    UPDATED (2026-09-15, deep-clean pass): reads from a market_story
    dict (market_story.build_market_story()'s output) instead of a raw
    market_assessment dict. This function used to build its own view of
    Brain's fields inline — that meant build_market_story() (which
    already does exactly this) sat unused, and this function's field
    names (primary_thesis/primary_threat/next_watch) had to be kept in
    sync with market_story.py's by hand instead of by construction. Now
    there's one schema (market_story's), built in one place, rendered
    here.

    Returns "" if `story` is missing/empty — caller falls back to
    whatever it had (see narrate()'s call site). Never raises.
    """
    try:
        s = story or {}
        parts = []
        if s.get("what_price_is_doing_now"):
            parts.append(s["what_price_is_doing_now"])
        if s.get("why_it_matters"):
            parts.append(s["why_it_matters"])
        change = s.get("what_would_change_it")
        if change:
            parts.append(f"What matters next: {change}.")
        return " ".join(parts)
    except Exception as e:
        print("[HFIS COMPOSE_STATE_STORY ERROR] " + str(e))
        return ""


def narrate(events, understanding, mil_understanding=None, mechanisms=None, market_story=None):
    """
    Returns a composed narration string, or None if there's nothing to
    narrate or something unexpected goes wrong — caller falls back to
    format_market_event()'s template text either way. Never raises.

    `mechanisms` (2026-09-15, per chat — friend's "A before B" review):
    an optional list, same length/order as `events`, each entry either a
    short evidence-first clause from market_story.build_mechanism() or
    "" (nothing to cite). When present, each event's mechanism clause is
    inserted immediately after that event's composed sentence and BEFORE
    the evidence-weave/struggle-phrase additions below — so the reader
    sees WHAT happened before WHAT IT MEANS, in that order, instead of
    only ever getting the conclusion. Omitting `mechanisms` (None) keeps
    this function's old behavior exactly, so no other call site breaks.

    `market_story` (2026-09-15, deep-clean pass): the dict from market_
    story.build_market_story() — NOT the module itself; this file is
    architecturally forbidden from importing market_story.py (see this
    file's own top docstring / check_layer_imports.py), so the caller
    (scanner_live.py, which can import both) builds it and passes the
    plain dict through. Used only for "state" category events, via
    _compose_state_story() above. Omitting it (None) falls back to
    _compose_generic(), same as an empty dict would.

    `intent_hypothesis` REMOVED from this signature (2026-09-16, found
    during a stress-test pass): it was only ever forwarded to _compose_
    opportunity(), which no longer needs it either — see that function's
    updated docstring for why (headline_text already carries everything
    needed). Removing the parameter here rather than leaving it accepted-
    but-unused, so a future edit can't be misled into thinking this
    function still reads intent_hypothesis for anything.
    """
    try:
        if not events:
            return None
        cc = (understanding or {}).get("current_condition") or {}
        rendered = []
        categories = []
        for idx, e in enumerate(events):
            category = e.get("category")
            headline = e.get("headline", "")
            if category == "structure":
                sentence = _compose_structure_flip(headline, cc, mil_understanding)
            elif category == "opportunity":
                sentence = _compose_opportunity(headline)
            elif category == "state":
                # ROUTED THROUGH BRAIN'S OWN SYNTHESIS, via market_story
                # (2026-09-15) instead of the old generic "shifted from
                # {prev} to {curr}" template — see _compose_state_story()'s
                # docstring above. Falls back to the old generic
                # composer only if market_story is somehow empty, so
                # this can never produce a blank message.
                sentence = _compose_state_story(market_story) or _compose_generic(headline)
            elif "confirmed" in (headline or "").lower() or "reclaimed" in (headline or "").lower():
                sentence = _compose_confirmed_transition(cc)
            else:
                sentence = _compose_generic(headline)

            # A BEFORE B (per chat — the ordering the friend specifically
            # asked for: state what happened, then what it means, never
            # the reverse). Mechanism clause leads; the composed
            # sentence (already the "what it means" conclusion) follows.
            mech = (mechanisms[idx] if mechanisms and idx < len(mechanisms) else "") or ""
            if mech and sentence:
                sentence = f"{mech} {sentence}"
            elif mech:
                sentence = mech

            rendered.append(sentence)
            categories.append(category)

        # SCOPE FIX (2026-09-14, per chat — the "4.4 ATR" non-sequitur
        # complaint). _weave_evidence() pulls a RANDOM entry off
        # mil_understanding's supporting_evidence — real market-fact
        # sentence fragments (HTF bias, a fresh BOS, a confirming CHoCH,
        # an unmitigated OB, expanding 1H volatility — see its own
        # docstring; conflicting_evidence was removed from that function
        # entirely on 2026-09-15, a separate fix), but not necessarily
        # about whatever specific event just fired. That's a coherent
        # thing to say alongside a structure/state event (both ARE about
        # the thesis's overall condition) but a non sequitur bolted onto
        # an opportunity event (a new zone coming into range has nothing
        # to do with the broader thesis's supporting evidence) — which is
        # exactly why "Filed away for now." was getting "still, price is
        # 4.4 ATR away..." tacked onto it with no connection to the zone
        # just described.
        # Previously unconditional on rendered[0] regardless of category.
        if categories and categories[0] in ("structure", "state"):
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
