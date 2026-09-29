"""
market_presenter.py — THE single, shared presentation layer.

MIGRATION (frozen plan, per chat): absorbs brain.py's format_*
functions (format_market_understanding, format_understanding_narrative,
format_intent_narrative, format_market_event, format_market_briefing,
_state_family_label + its phrase-bank templates). Every docstring below
is preserved from the original — structural relocation, not a rewrite.

STATUS — PARTIAL, FLAGGED EXPLICITLY: this file currently contains only
the brain.py-derived formatters. hfis.py's narrate() phrase-bank
(variety templates for structure-flip/confirmed-transition/opportunity/
generic composition), narration_library.py's recap_line()/elapsed_
label()/light_summary()/deep_read(), and scanner_live.py's
_group_report_text()/_group_market_event_text() (the separate
"beginner voice") are NOT yet ported into this file. That is the next
piece of work, done as its own reviewable step rather than folded in
here — those three sources total roughly 1,100 lines of tuned production
wording, and retyping that volume in the same pass as the architectural
move is exactly the kind of scope-mixing that was explicitly ruled out.
min_scanner.py's _compose_resolution_text() duplicate (the second phrase
bank forced into existence by the old CI layer-boundary wall) is the
first thing that duplicate work should be checked against once ported,
since it's the shortest of the three and the most directly related to
the original duplicate-zone bug report.

PRESENTER PURITY (the corrected invariant, per chat): every function
below may only render MarketRead field VALUES through fixed templates —
connective language ("still", "intact", "the idea") is fine; it must
never introduce a market-derived claim, state, cause, threshold, or
watch condition that isn't already a field on the MarketRead object
passed in. No branching on market state beyond selecting which already-
computed field to display.

SHARED ACROSS BOTH JOBS: this file must be importable from both
min-scan.yml and live-scan.yml without violating the old hfis.py CI
wall (check_layer_imports.py's FORBIDDEN dict) — it should be placed
alongside scanner_common.py, which both jobs already import cleanly,
not inside either job-specific module. Once hfis.py's content is ported
in here, hfis.py itself is retired as an independent layer (not
deleted immediately — same "retire the architectural role, not
necessarily the file, immediately" approach as agreed).

Callers pass the dict `build_market_read()` returns (see market_read.py)
in place of the old `understanding` argument these functions already
took — the parameter name `understanding` below is kept as-is since the
shape is unchanged; only what produces it moved.
"""

import random

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
    by synthesize_market_understanding() above — this function's only job
    is choosing ORDER and OMISSION among those, never wording. Per chat:
    "it should know which 3 facts actually matter right now, not know
    50 facts."

    NEVER READS thesis_weaknesses / thesis_weaknesses_prose (2026-09-16 —
    caught by a code reviewer's audit, comparing this function against the
    rule evaluate_market_event()'s own docstring already states as settled:
    "NEITHER this function nor market_story.py ever reads thesis_weaknesses
    (_prose) or MIL's conflicting_evidence for anything human-facing." This
    function was the third sibling composer of that same class — same
    codebase, same day the rule was written elsewhere, never actually
    applied here. It used to join weaknesses_prose directly into /understand
    and /thesis's output ("Weighing against that: {ob_mitigated prose}; {atr_
    floor prose}."). Every current weakness category (ob_mitigated,
    atr_floor, ema_extension, leg_break_count, bias_stale) is internal risk-
    scoring content — banned from narration regardless of prose quality,
    for the same reason it's banned from hfis.py and market_story.py. The
    dedup logic that used to sit here (suppressing leg_break_count/
    ema_extension when market_assessment's own aging language already
    covered them) is removed along with it — it existed solely to filter
    THIS bank before printing it, and has nothing left to filter now.

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
    # PHASE 1 UPDATE (per audit, 2026-08-27): the `or understanding.get(
    # "thesis_expected_next_event")` fallback that used to sit here is
    # REMOVED. It was a stopgap for the period where only the
    # reaccelerating branch had its own next_watch opinion — every other
    # branch silently fell through to Thesis's field. Now every branch in
    # synthesize_market_understanding() sets next_watch explicitly (own
    # text, the shared WorldState default, or a deliberate None — see
    # that function's wrapper docstring). Falling back to Thesis's field
    # here would mean: the one time Brain's branch logic has a bug and
    # forgets to set next_watch, this line would silently paper over it
    # by showing Thesis's un-vetted conclusion instead — reintroducing
    # exactly the two-authorities problem this file exists to remove,
    # and hiding the bug instead of surfacing it. Better to show nothing
    # than to show a conclusion Brain didn't actually make.
    next_event = ma.get("next_watch")
    if next_event:
        sentences.append(f"What I'm watching for: {_plain_clause(next_event)}.")

    return " ".join(sentences)


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


def format_market_event(events, understanding, intent_hypothesis):
    """
    Composes the Telegram push for one or more Market Events firing on
    the same scan. Per chat: "the push is just another interface to the
    Brain, not another intelligence system" — this function adds NO new
    interpretation. The headline(s) come from evaluate_market_event()
    above; everything else is the exact same format_understanding_
    narrative()/format_intent_narrative() output /understand and
    /marketintent already render, so a push never says something those
    commands wouldn't say if asked right now on the same scan.

    CONTRACT, MADE EXPLICIT (2026-08-27, per chat — Vally's review of
    live output: "is an event a historical snapshot of the transition
    that triggered it, or a live summary of the current state? I'd make
    it historical transition snapshot"). This function and evaluate_
    market_event() run together, in the same call, at the moment the
    transition is detected (see _evaluate_and_push_market_event() in
    scanner_live.py — nothing re-fetches or re-renders later). Nothing
    about this is a live/rolling view — it's already a snapshot, taken
    at transition time, same as Vally asked for. This paragraph exists
    so that contract is stated, not just true by accident of how the
    caller happens to invoke this.

    SCOPE FIX (2026-08-27, per chat — same review: "don't let every
    event become a complete copy of /understand + /marketintent").
    Areas of interest is now ONLY appended when an opportunity event is
    among those firing — a pure structure/state event (a bias flip, a
    condition change) is about what changed, not a re-statement of
    everything currently being watched. If intent_hypothesis is later
    genuinely relevant to a structure/state event too, that's a reason
    to reconsider this, not a reason to silently always include it.

    Returns None if events is empty (nothing to push) — callers should
    check for this rather than pushing an empty message.
    """
    if not events:
        return None

    icon = {"structure": "🧱", "state": "🌊", "opportunity": "🎯"}
    lines = ["📡 *Market Event*", ""]
    for ev in events:
        lines.append(f"{icon.get(ev['category'], '•')} {ev['headline']}")

    lines.append("")
    lines.append(format_understanding_narrative(understanding))

    has_opportunity = any(ev.get("category") == "opportunity" for ev in events)
    if has_opportunity:
        intent_txt = format_intent_narrative(intent_hypothesis)
        if intent_txt and intent_txt != "Nothing specific being tracked right now.":
            lines.append("")
            lines.append("*Areas of interest:*")
            lines.append(intent_txt)

    return "\n".join(lines)


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
def elapsed_label(elapsed_seconds):
    """
    A plain, fixed label for "how long since we last spoke" — replaces
    hfis.py's old _time_bucket()/_EVENT_LEADINS/_QUIET_LEADINS random-
    choice banks. One deterministic label per bucket; no variety, same
    reasoning as the rest of this file. `elapsed_seconds` is None on the
    very first overview ever sent (no prior timestamp to compare
    against) — returns the "first time" label rather than guessing.
    """
    try:
        if elapsed_seconds is None:
            return "Here's where things stand:"
        if elapsed_seconds < 1800:
            return "In the last little while:"
        if elapsed_seconds < 10800:
            return "In the last few hours:"
        return "Since we last spoke:"
    except Exception as e:
        print("[NARRATION LIBRARY ELAPSED_LABEL ERROR] " + str(e))
        return "Here's where things stand:"


def light_summary(story, thesis_status=None):
    """
    The short overview read. Fields used from `story`: direction, what_
    price_has_done, what_price_is_doing_now. `thesis_status` (a plain
    string, e.g. "SUPPORTED"/"WEAKENING"/"CHALLENGED") only selects
    which fixed opening line is used — not a random pick, a lookup.

    Deliberately does NOT include: why_it_matters, counter_thesis,
    failure_condition, what_would_change_it. Those are deep_read()'s
    job. Adding one of them back here is the exact bug this redesign
    fixed — check deep_read() first.
    """
    try:
        s = story or {}
        openers = {
            "SUPPORTED": "Same read as before — nothing here changes it.",
            "WEAKENING": "The move's aging, and it's starting to show.",
            "CHALLENGED": "This read is under real pressure right now.",
        }
        parts = [openers.get(thesis_status, openers["SUPPORTED"])]
        if s.get("direction"):
            parts.append(f"Reading {s['direction'].lower()} on the higher timeframe.")
        if s.get("what_price_has_done"):
            parts.append(s["what_price_has_done"])
        if s.get("what_price_is_doing_now"):
            parts.append(s["what_price_is_doing_now"])
        return " ".join(parts)
    except Exception as e:
        print("[NARRATION LIBRARY LIGHT_SUMMARY ERROR] " + str(e))
        return ""


def deep_read(story):
    """
    Plain, labeled deep-thesis block — no prose composition. Fields used
    from `story`: what_price_is_doing_now, why_it_matters, counter_
    thesis (dict: kind/direction/status/watching_for, or kind/direction/
    maturity/range_pips/fizzled_before), failure_condition (dict: level/
    direction), what_would_change_it. Every line is a fixed label plus a
    value — nothing here is randomized, and nothing here reads anything
    NOT already present on `story` (see market_story.build_market_
    story()'s docstring for where each of these fields comes from).
    """
    try:
        s = story or {}
        lines = []

        if s.get("what_price_is_doing_now"):
            lines.append(f"Primary thesis: {s['what_price_is_doing_now']}")
        if s.get("why_it_matters"):
            lines.append(f"Primary threat: {s['why_it_matters']}")

        counter = s.get("counter_thesis")
        if counter:
            direction = counter.get("direction", "")
            if counter.get("kind") == "confirmed_candidate":
                status = (counter.get("status") or "awaiting_confirmation").replace("_", " ")
                watching = counter.get("watching_for") or []
                if watching:
                    w = watching[0]
                    w_sentence = w.get("sentence", w.get("code", ""))
                    if w.get("zone_low") is not None and w.get("zone_high") is not None:
                        w_sentence += f" (zone: {w['zone_low']:.5f}-{w['zone_high']:.5f})"
                    lines.append(f"Counter thesis: {direction}, {status} — {w_sentence}")
                else:
                    lines.append(f"Counter thesis: {direction}, {status}")
            else:  # "emerging"
                bits = [direction]
                if counter.get("maturity"):
                    bits.append(str(counter["maturity"]))
                if counter.get("range_pips") is not None:
                    bits.append(f"{counter['range_pips']} pips deep")
                line = "Counter thesis: " + ", ".join(bits)
                if counter.get("fizzled_before"):
                    line += f" (a {direction.lower()} case has fizzled here before)"
                lines.append(line)

        fc = s.get("failure_condition")
        if fc and fc.get("level"):
            level_phrase = (f"clean close {fc['direction']} {fc['level']:.5f}"
                             if fc.get("direction") else f"{fc['level']:.5f}")
            lines.append(f"Failure condition: {level_phrase}")

        if s.get("what_would_change_it"):
            lines.append(f"What matters next: {s['what_would_change_it']}")

        return "\n".join(lines)
    except Exception as e:
        print("[NARRATION LIBRARY DEEP_READ ERROR] " + str(e))
        return ""


def recap_line(event, story=None):
    """
    One "what changed" line for the overview's recap section, for an
    event that was ALREADY pushed live — never "just discovered" framing
    (that was the duplicate-message bug: reusing live-push language for
    something the reader was already told).

    Fields used from `event`: category, headline (the sentence already
    captured by evaluate_market_event() at the moment it fired — never
    re-derived from current state, which was the OTHER duplicate bug:
    two different pending events both re-fetching the same "current"
    location and rendering identically), and — category "state" only —
    what_price_is_doing_now_at_push (a snapshot scanner_live.py captures
    at the moment the event is pushed, NOT read from `story` here. Found
    during a stress-test pass: two genuinely different state-category
    events pending in the same batch would otherwise both render
    whatever `story` currently says, since "currently" is the same
    instant for both when the recap is being built — collapsing two
    real events into identical text, the same failure shape as the
    opportunity-recap bug, just one branch over).

    `story` is accepted for backward compatibility with any already-
    queued pending event from before this fix (no snapshot field yet) —
    used ONLY as that fallback, never as the primary source for a state
    recap line.
    """
    try:
        category = (event or {}).get("category")
        headline = (event or {}).get("headline", "")
        s = story or {}

        if category == "structure":
            return f"Structure flipped — {headline}" if headline else "Structure flipped."
        if category == "state":
            text = (event or {}).get("what_price_is_doing_now_at_push") or s.get("what_price_is_doing_now") or headline
            return f"Also worth noting — the read shifted: {text}" if text else ""
        if category == "opportunity":
            # Generic opener (2026-09-16, found during stress-test):
            # "Also came into range:" reads fine for a Tier 1/2 zone but
            # is a category mismatch for a Tier 3 structural-condition
            # headline ("Tier 3 structural condition has been reached
            # on the current leg...") — there's no "range" in that
            # sentence. "Also flagged:" fits either shape.
            return f"Also flagged: {headline}" if headline else "Also flagged: a new development."
        return f"Also happened: {headline}" if headline else ""
    except Exception as e:
        print("[NARRATION LIBRARY RECAP_LINE ERROR] " + str(e))
        return ""

# ---------------------------------------------------------------------
# GROUP-TEXT PRESENTERS (Phase 2A cont. — split from scanner_live.py's
# _group_status_text/_group_watch_text/_group_report_text/
# _group_market_event_text). Wording/behavior preserved exactly from
# the originals — this is the architectural split only, per the frozen
# plan's explicit instruction not to redesign wording in this pass.
#
# THIRD LAYERING ISSUE FOUND DURING THIS SPLIT (reporting per the
# stop-and-flag rule, not fixed silently): the original
# _group_report_text() mutated state directly
# (state["group_report_last_state_key"] = ...) in the middle of
# rendering. A presenter cannot mutate persisted state any more than it
# can fetch it. That responsibility moves to the orchestrator side
# below, alongside the fetch — present_group_report() takes the
# already-computed `changed` flag as a plain argument and does not
# touch `state` at all.
# ---------------------------------------------------------------------

def present_group_status(market_read):
    """Pure render for /status. Orchestrator (scanner_live.py) does:
        ws = build_world_state(state)
        market_read = build_market_read(ws, prior_intent_state=...)
        text = present_group_status(market_read)
    """
    cc = (market_read or {}).get("current_condition") or {}
    bias  = cc.get("macro_bias") or "Not yet confirmed"
    phase = cc.get("phase")
    phase_line = phase.replace("_", " ").title() if phase else "Not yet established"
    narrative = format_understanding_narrative(market_read) if market_read else \
        "No directional read yet — the higher-timeframe bias isn't confirmed either way."
    return (
        "📊 *GBPUSD — Market Status*\n\n"
        f"Higher-timeframe bias: *{bias.title() if bias not in ('Not yet confirmed',) else bias}*\n"
        f"Current phase: *{phase_line}*\n\n"
        f"{narrative}\n\n"
        "_This is a market update, not a trade instruction._"
    )


def present_group_watch(market_read):
    """Pure render for /watch. Orchestrator does:
        ws = build_world_state(state)
        market_read = build_market_read(ws, prior_intent_state=...)
        text = present_group_watch(market_read)
    Reads market_read['intent_hypothesis'] — same locations/confirmations
    the original read straight off build_market_intent_hypothesis().
    """
    intent_hypothesis = (market_read or {}).get("intent_hypothesis") or {}
    locations     = intent_hypothesis.get("locations") or []
    confirmations = intent_hypothesis.get("confirmations") or []
    if not locations and not confirmations:
        return (
            "👀 *GBPUSD — Watch*\n\n"
            "Nothing specific is being tracked beyond the current bias "
            "right now.\n\n"
            "_Not a trade signal._"
        )
    lines = ["👀 *GBPUSD — Watch*", ""]
    if locations:
        lines.append("Areas being watched:")
        for loc in locations:
            lines.append(f"• {loc.get('sentence') or loc.get('code')}")
        lines.append("")
    if confirmations:
        lines.append("What's still needed:")
        for c in confirmations:
            lines.append(f"• {c.get('sentence') or c.get('code')}")
        lines.append("")
    lines.append("_Watching. No trade signal._")
    return "\n".join(lines)


def present_group_report(market_read, now_utc, changed, evidence_prose,
                          invalidation, signal_line):
    """Pure render for the hourly report / /report pull. Orchestrator
    does the fetch AND the state mutation the original did inline:
        ws = build_world_state(state)
        market_read = build_market_read(ws, prior_intent_state=...)
        ma = (market_read or {}).get("market_assessment") or {}
        current_state_key = ma.get("state")
        prev_state_key = state.get("group_report_last_state_key")
        changed = prev_state_key is not None and prev_state_key != current_state_key
        state["group_report_last_state_key"] = current_state_key   # mutation stays HERE, not in the presenter
        evidence_prose = state.get("market_thesis_evidence_prose") or []
        invalidation = state.get("market_thesis_invalidation")
        owner = get_leg_owner(state)
        if owner and owner.get("status") == "FIRED" and owner.get("tier"):
            tier_num = TIER_NUMBER.get(owner["tier"])
            signal_line = f"A {tier_num or owner['tier']} setup is currently live — see the last signal alert."
        else:
            signal_line = "No new trade signal at this time."
        text = present_group_report(market_read, now_utc, changed, evidence_prose, invalidation, signal_line)

    `changed` and `signal_line`/`evidence_prose`/`invalidation` are all
    plain values computed by the orchestrator from state — the
    presenter only lays them out. No state access here.
    """
    ma = (market_read or {}).get("market_assessment") or {}
    header = now_utc.strftime("🕐 *GBPUSD — %H:%M UTC Report*")
    lines = [header, ""]

    if not ma:
        bias = (market_read.get("current_condition") or {}).get("macro_bias") if market_read else None
        if bias in ("BULLISH", "BEARISH"):
            lines.append(f"*{bias.title()}* is the higher-timeframe bias, but there "
                         "isn't enough structure yet to say more than that.")
        else:
            lines.append("No directional read yet — the higher-timeframe bias "
                          "isn't confirmed either way.")
        lines.append("")
        lines.append("*Signal:* No new trade signal at this time.")
        return "\n".join(lines)

    if changed:
        lines.append("_Something's changed since the last report._")
        lines.append("")

    lines.append("*Primary thesis:*")
    lines.append(ma["primary_thesis"])

    if evidence_prose:
        lines.append("")
        lines.append("*Why the system sees it this way:*")
        lines.append("; ".join(evidence_prose) + ".")

    if ma.get("primary_threat"):
        lines.append("")
        lines.append("*Counter-thesis:*")
        lines.append(ma["primary_threat"])

    watch_bits = []
    if ma.get("confirmation_needed"):
        watch_bits.append(f"What would confirm it: {_plain_clause(ma['confirmation_needed'])}.")
    if ma.get("next_watch"):
        watch_bits.append(f"What ValleyBot is watching for: {_plain_clause(ma['next_watch'])}.")
    if watch_bits:
        lines.append("")
        lines.append("*Watching:*")
        lines.append(" ".join(watch_bits))

    if ma.get("alternative"):
        lines.append("")
        lines.append("*Alternative read:*")
        lines.append(ma["alternative"])

    if invalidation:
        lines.append("")
        lines.append("*What would invalidate this thesis:*")
        lines.append(invalidation)

    lines.append("")
    lines.append(f"*Signal:* {signal_line}")

    if not changed:
        lines.append("")
        lines.append("_Nothing structurally new since the previous report — the thesis above still holds._")

    return "\n".join(lines)


_STORY_IMPACT_ICON = {
    "structure": "🧱", "state": "🌊", "opportunity": "🎯",
}

_STORY_IMPACT_GROUP_LABEL = {
    "support":     "This supports the current thesis.",
    "challenge":   "This pushes against the current thesis.",
    "break":       "This breaks the current thesis — the view has genuinely changed.",
    "opportunity": "This is a new opportunity to watch, not a change to the thesis itself.",
    "surprise":    "This is a surprise — price didn't do what the thesis expected, "
                   "though nothing has cleanly invalidated it yet.",
}


def present_group_market_event(events_to_push, mechanisms, story, signal_line):
    """Pure render for the group Market Event push. Orchestrator does:
        ... (events_to_push, mechanisms, story already built by
        _evaluate_and_push_market_event(), unchanged) ...
        owner = get_leg_owner(state)
        signal_line = <same computation as present_group_report's>
        text = present_group_market_event(events_to_push, mechanisms, story, signal_line)
    events_to_push/mechanisms/story are passed straight through exactly
    as the original did — no new detection, no new classification here
    either, same as before.
    Returns None if there is nothing to say — caller should skip
    sending rather than push an empty message (unchanged behavior).
    """
    if not events_to_push:
        return None

    lines = ["🔔 *GBPUSD — Market Event*", ""]
    for i, ev in enumerate(events_to_push):
        icon = _STORY_IMPACT_ICON.get(ev.get("category"), "•")
        lines.append(f"{icon} {ev.get('headline', '')}")
        mech = mechanisms[i] if mechanisms and i < len(mechanisms) else ""
        if mech:
            lines.append(f"_{mech}_")
        effect = _STORY_IMPACT_GROUP_LABEL.get(ev.get("story_impact"))
        if effect:
            lines.append(effect)
        lines.append("")

    deep = deep_read(story)
    if deep:
        lines.append(deep)
        lines.append("")

    lines.append(f"*Signal:* {signal_line}")

    return "\n".join(lines).rstrip()

# ---------------------------------------------------------------------
# MIN-ONLY PRESENTERS (Phase 2C — replaces min_scanner.py's local
# _summarize_pending_events()/_compose_resolution_text(), the second
# phrase bank that existed only because hfis.py was CI-walled off from
# min-scan.yml. market_presenter.py has no hfis.py dependency and is
# importable from both jobs, so that constraint no longer applies —
# MIN now shares this one presentation path instead of maintaining its
# own.
#
# present_pending_summary() DELIBERATELY DROPS the old consecutive-
# identical-pair dedup step that used to live here. That dedup existed
# to paper over duplicate entries reaching the pending file — the
# write-site fix (canonical_watch_identity()/watch_conditions_match()
# gating scanner_live.py's pending.append()) now prevents duplicate
# identities from ever being written in the first place. Keeping a
# second dedup here, after the write-site already guarantees
# uniqueness, would be exactly the "two places independently deciding
# what's a duplicate" problem this migration was built to eliminate.
# Only the >3-items truncation is kept — that's presentation
# (how to lay out a long but genuinely-distinct list), not
# interpretation.
# ---------------------------------------------------------------------

def present_pending_summary(matched):
    """Composes the "Earlier: ..." line for a Market Event resolution
    follow-up, from an ALREADY-DEDUPED `matched` list (see this
    section's header note - dedup now happens at the write site, not
    here). If more than 3 distinct events are present, summarizes to
    first -> ... -> last plus a count rather than dumping the full
    list - unchanged formatting behavior from the original.
    """
    if not matched:
        return ""
    if len(matched) <= 3:
        return "; ".join(p.get("headline", "") for p in matched if p.get("headline"))

    first = matched[0].get("headline", "")
    last = matched[-1].get("headline", "")
    n_between = len(matched) - 2
    return (
        f"{first} ... then {n_between} more condition change"
        f"{'s' if n_between != 1 else ''} while this leg stayed open ... "
        f"most recently: {last}"
    )


def present_resolution_text(headlines_summary, direction, fate, bars_open):
    """Shared composer for the leg-resolution follow-up. Wording ported
    unchanged from min_scanner.py's former _compose_resolution_text() -
    per the frozen plan, architectural moves in this pass do not change
    wording; that's a separate, later pass. (Known open item, tracked
    separately, not fixed here: `bars_open` is still surfaced as a raw
    internal counter with no unit - flagged earlier in this
    conversation, out of scope for this migration step.)
    Never raises - falls back to a plain string on any error, same
    discipline as every other composer in this codebase.
    """
    try:
        openers = {
            "CONTINUED": [
                "That one played out the way it was leaning.",
                "Followed through, as expected.",
                "No surprise here — it kept going the way it was already going.",
            ],
            "REVERSED": [
                "That one didn't go the way it looked like it would.",
                "Turned around on us.",
                "Worth remembering this one — it flipped from where it stood earlier.",
            ],
            "INVALIDATED": [
                "That leg's done — invalidated.",
                "Closed out as invalidated.",
                "That one got taken out cleanly.",
            ],
        }
        opener = random.choice(openers.get((fate or "").upper(), ["That leg has now resolved."]))
        parts = [opener]
        if direction:
            parts.append(f"That {direction.lower()} leg ran {bars_open} bars before resolving as *{fate}*.")
        else:
            parts.append(f"Ran {bars_open} bars before resolving as *{fate}*.")
        if headlines_summary:
            parts.append(f"What led up to it: {headlines_summary}")
        return " ".join(parts)
    except Exception as e:
        print("[RESOLUTION COMPOSE ERROR] " + str(e))
        return f"Earlier: {headlines_summary}\n\nThat {(direction or '').lower()} leg has now resolved as *{fate}* after {bars_open} bars."
