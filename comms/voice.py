"""
voice.py — how ValleyBot SOUNDS. Pure stdlib; imports nothing from this codebase; holds no facts.

THE RULE THAT KEEPS IT TRUSTWORTHY: voice only ever adds glue around a body of text that something else already
established and (where it applies) audited. It never states a number, a price, a time, a direction, a concept or a
result, so it cannot change what a reply means or how sure it is. A test fails if any phrase here contains a digit,
and another fails if a wrapped reply's body is altered in any way.

Phrases are chosen by a seed (the Telegram update id) so a retried reply is word-for-word the same, and two
different replies in a row don't sound like one template.
"""
import re

_LEADS = {
    "answer": ("Here's where things stand.", "Okay, here's what I've got.", "Sure, this is what I'm seeing.",
               "Right, here's the picture."),
    "reconstruct": ("Let me take you back through it.", "Okay, going back through it with you.",
                    "Alright, here's what I can piece together."),
    "saved": ("Got you.", "Noted.", "Okay, saved."),
    "clarify": ("Quick check before I do anything.", "Just so I get this right.", "One quick question."),
    "gap": ("There's a gap I should be upfront about.",),
}
_UNKNOWN = ("Hmm, I didn't quite catch that one. You can just ask me what I'm seeing, ask how a session went, tell me about a trade, "
            "or tell me what you've noticed, in your own words. /help has the full list.",
            "I'm not sure what you're after with that. Try asking what I'm seeing, how London or New York went, or an alert like "
            "'alert me when price goes above' a level. /help lists everything.")

_GREET = ("Hey! I'm here. Ask me what I'm seeing, how London or New York went, or tell me about a trade, in your own words. /help has the rest.",
          "Hey, I'm listening. You can ask what I'm looking at, ask me to go back over a stretch of the market, or tell me what you're seeing. /help has the rest.",
          "Hi! Just talk to me normally. For example: what are you looking at, how was the overlap, or I sold gbpusd at a price. /help has the rest.")
_THANKS = ("Anytime.", "You're welcome.", "No problem.")


def greet(seed=0):
    return pick(_GREET, seed)


def thanks(seed=0):
    return pick(_THANKS, seed)


# internal gap states -> plain words. No facts: the item name and numbers come from the record, not from here.
GAP_WORDS = {
    "never_recorded": "was never recorded",
    "unavailable": "isn't available to me right now",
    "stale": "was too old by then to describe that moment",
    "timestamp_problem": "has a time on it I can't fully trust",
    "conflict": "is recorded in two conflicting ways, so I'm not using either",
    "empty": "is there but empty",
    "recorded_late": "only got written down after the fact",
}
STANCE_WORDS = {
    "observation": "something you noticed",
    "expectation": "something you expect or are waiting for",
    "reasoning": "your reasoning",
}


def pick(options, seed):
    return options[(abs(int(seed)) if str(seed).lstrip("-").isdigit() else sum(map(ord, str(seed)))) % len(options)]


def lead(kind, seed=0):
    return pick(_LEADS[kind], seed)


def unknown(seed=0):
    return pick(_UNKNOWN, seed)


def wrap(kind, body, seed=0):
    """lead + the body EXACTLY as given. Returns just the body when it already opens with glue of its own."""
    return f"{lead(kind, seed)}\n{body}"


def phrases():
    """Every phrase this module can say (for the no-digits test)."""
    out = [p for v in _LEADS.values() for p in v] + list(_UNKNOWN) + list(_GREET) + list(_THANKS) + list(GAP_WORDS.values()) + list(STANCE_WORDS.values())
    return out
