"""
narration_library.py — the Library (pure text-rendering over the Story
object)

ADDED (2026-09-16, per chat — the architecture redesign): "making it a
very clear path with a library type file in-between the machine process
and the human output so that wah's meant for machine stays in machine
and all these narrations can be picked, edited or removed without any
implications or duplication or other sister functions."

THE PROBLEM THIS FILE EXISTS TO PREVENT: every bug found across the last
two days traced back to the same root cause — a composer function
reaching into raw machine state (mil_understanding, intent_hypothesis,
market_assessment) independently, with no way to know what some OTHER
composer already said or where else that same field was already being
read. hfis.py accumulated several of these in parallel (_compose_
current_conditions, compose_deep_read_plain, _weave_evidence,
_compose_counter_case, narrate_deep_thesis) because each one was patched
in at a different time without a shared contract. That's what "grabbing
from another file" and "forced... one way order" were describing.

THE RULE THIS FILE ENFORCES BY CONSTRUCTION, NOT CONVENTION:
  - Every function below takes ONLY the story dict (market_story.
    build_market_story()'s output) — plus, where noted, one small
    event-specific argument. NONE of them import brain.py, mil.py,
    scanner_observation.py, or reach into a raw understanding/mil_
    understanding dict. If a fact isn't already a field on the story
    dict, it doesn't belong in a function here — add the field to
    build_market_story() (market_story.py) instead. This is what makes
    "can I edit or delete this without breaking something else" a
    one-line answer: check the function's own docstring for its field
    list, and check whether any OTHER function here reads the same
    field (the module docstring's "no duplication" contract).
  - No function here knows about Telegram formatting (no emoji header,
    no markdown bold, no message assembly). That's scanner_live.py's
    job — this file only returns plain strings for scanner_live.py to
    label and join.
  - Deterministic. No random.choice() anywhere in this file — these are
    read regularly and compared against, and variety was the actual
    mechanism behind most of the bugs (independent composers each
    deciding differently whether/how to phrase the same fact). Variety-
    for-its-own-sake lives in exactly one place now: hfis.py's narrate()
    (the live event push) — a deliberate, isolated exception, not
    something this file reproduces.
  - Every function returns "" (never None) when it has nothing to say,
    and never raises.

WHICH FACT LIVES WHERE (the actual fix for the duplication bug — read
this before adding a line to either function):
  light_summary() —  direction, what_price_has_done, what_price_is_
                      doing_now. Nothing else. Ever.
  deep_read()     —  what_price_is_doing_now (again, as a labeled
                      "Primary thesis" — the one intentional overlap,
                      the connecting thread between a light read and
                      the full one), why_it_matters, counter_thesis,
                      failure_condition, what_would_change_it.
"""


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
