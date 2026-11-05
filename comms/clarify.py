"""
clarify.py — when the bot does not understand a message, it ASKS what you meant instead of a generic "didn't catch that", and can learn your phrasing.

PURE (re + chat). The choices offered come from the FIXED list below, never from the user's text, so a taught phrase can only ever point at one of these questions
(it cannot be used to smuggle in an instruction). A learned phrase is looked up ONLY when the bot would otherwise have said "didn't catch that", so it can never override
something the bot already understands, and never overrides a safety boundary.
"""
import re

import chat

# key, what it is in plain words, the question the bot actually runs, concept families that point to it
CHOICES = (
    ("price", "the current price", "what is the price", {"price", "where"}),
    ("direction", "which way the market is leaning", "what is the direction of the market", {"direction", "market"}),
    ("change", "how far price has moved today", "how far has price gone today", {"change", "since", "range"}),
    ("ema", "where price sits against the 100 EMA", "is price above the 100 EMA", {"ema", "relation", "above"}),
    ("signal", "when the last signal was", "when was the last signal", {"setup", "alert", "new"}),
    ("count", "how many scans or signals there were today", "how many signals today", {"count", "scan", "shadow"}),
    ("watch", "what the engine is looking at", "what are you looking at", {"track", "bot", "level"}),
    ("trade", "talking a trade through", "I want to talk about a trade I took", {"trade", "case", "stop"}),
    ("clock", "whether the market is open", "is the market open", {"since"}),
)
DEFAULTS = ("price", "watch", "trade")
BY_KEY = {c[0]: c for c in CHOICES}


def norm_phrase(text):
    t = re.sub(r"\s+", " ", (text or "").lower()).strip(" .!?,;:")
    return t if 0 < len(t.split()) <= 12 else ""


def candidates(text, n=2):
    """Only choices the words actually point at. Nothing is padded in: with no real lead the bot just asks."""
    cs = chat.concepts((text or "").lower())
    scored = sorted(((len(cs & c[3]), i) for i, c in enumerate(CHOICES)), key=lambda x: (-x[0], x[1]))
    return [CHOICES[i] for sc, i in scored if sc > 0][:n]


_OPEN = (
    "I lost you there. What are you trying to find out? Tell me in your own words.",
    "I'm not sure what you're getting at with that. Say a bit more and I'll work it out with you.",
    "Hmm, I can't tell what you're after. What do you want to know, or what's on your mind?",
    "I don't want to guess. Can you put it another way? Even a rough version helps.",
    "That one went past me. Are you asking about the market, a trade of yours, or something else?",
)
_ONE = (
    "Not sure I follow. Are you asking about {a}? If not, tell me a bit more.",
    "Let me check I've got you: is this about {a}? If it's something else, say so and I'll follow.",
    "I might be wrong here. Do you mean {a}? Otherwise tell me more.",
)
_TWO = (
    "I'm not sure whether you mean {a} or {b}. Which is it, or is it something else?",
    "Could be {a}, could be {b}. Which one are you after? Or tell me if I've missed it.",
    "Two things come to mind: {a}, or {b}. Is it one of those?",
)


def question(text, picks, seed=0):
    k = seed if isinstance(seed, int) else 0
    if not picks:
        return _OPEN[k % len(_OPEN)]
    if len(picks) == 1:
        return _ONE[k % len(_ONE)].format(a=picks[0][1])
    return _TWO[k % len(_TWO)].format(a=picks[0][1], b=picks[1][1])


def options(phrase, picks):
    return [{"id": str(i), "label": p[1], "action": {"type": "say", "text": p[2], "learn": phrase, "key": p[0]}} for i, p in enumerate(picks, 1)]
