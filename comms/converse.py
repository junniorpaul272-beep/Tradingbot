"""
converse.py — reading a plain message ("wah is price doing rn?", "what happened by 8:30", "what did we talk about
tuesday", "buh it broke the high tho") and deciding WHAT IT IS. Pure: informal + timeref + voice. It never answers
anything itself and never stores anything; it returns a decision, and bus_runner (the I/O edge) acts on it.

DECISIONS
  command        starts with "/": none of this applies, the existing commands run as they always did.
  ask_bus        a question the BUS can answer: returns the CANONICAL command to hand it (for example "/now").
  market_recon   a stretch of the market: returns the time Window.
  convo_recon    "what did we talk about ...": returns the Window (and the words, for matching a named topic).
  need_time      asked for a reconstruction without saying when.
  followup       "and after that?" — continues the last reconstruction the bot gave.
  offer_save     a statement that sounds like something you noticed or expect: the bot ASKS before saving it.
  unknown        none of the above.

AMBIGUITY IS ASKED, NOT GUESSED: a message that could be a question for now or a note, goes to the user as a short
question with options, never silently down one path. Replies to those questions are resolved by `resolve()`.
"""
import re

import informal
import timeref

_CONVO = re.compile(r"\b(what (did|have|were|was) (we|you and i|i and you) (talk|talked|discuss|discussed|say|said|cover|covered|go over|speak|spoke|saying|talking|doing)|"
                    r"what was that (thing|conversation|talk|chat)|remind me what (i|we) (said|was thinking|told you|talked|discussed)|"
                    r"what i (told|said to) you|our (talk|conversation|chat|discussion)|what were we on|where did we (leave|stop))", re.I)
_RECON = re.compile(r"\b(what (happened|did price do|went on|did we see|was going on|was price doing)|take me through|walk me through|"
                    r"go (back )?(over|through)|reconstruct|rebuild|replay|remind me what happened|what did (it|price|gbpusd|cable) do)\b", re.I)
_FOLLOW = re.compile(r"^(and )?(what (happened|about|did it do|did price do) )?(after (that|it|this)|next|then|later|and then|what next)\??$|"
                     r"^and (after|then|next)\b", re.I)
_PRESENT = (
    (re.compile(r"\bwhat (are|is) you (looking at|seeing|watching|noticing|finding|focused on|tracking|keeping an eye on)\b|"
                r"\bwhat do you see\b|\banything (interesting|worth (watching|noting))\b|\bmost interesting\b|"
                r"\bwhat('?s| is) (catching|standing out)\b|\bwhat stands out\b|\bwhat should i (watch|look)", re.I), "/watch"),
    (re.compile(r"\bwhat do you think\b|\byour (read|take|thoughts?|view|opinion)\b|\bany thoughts\b|^thoughts\??$|"
                r"\bbullish or bearish\b|\bwhat('?s| is) (the )?(thesis|bias)\b", re.I), "/thesis"),
    (re.compile(r"\b(tell|give) me the (story|full|whole)\b|\bthe fuller read\b", re.I), "/story"),
    (re.compile(r"\bwhat changed\b|\banything (new|change|changed)\b|\bwhat'?s new\b", re.I), "/changed"),
    (re.compile(r"\bwhat would (change|flip|break|invalidate)\b", re.I), "/flip"),
    (re.compile(r"\bwhat('?s| is| are)( the)?( price| market| gbpusd| cable| gbp)? ?(doing|happening|happen|up to)\b|"
                r"\bhow('?s| is| are) (price|the market|gbpusd|cable|it|things)( looking| doing| going)?\b|"
                r"\bwhat'?s happening\b|\bwhat is going on\b|\bhow does (it|price|the market) look\b|\bhow is price\b", re.I), "/now"),
)
_YES = {"yes", "y", "yeah", "yep", "ye", "yea", "yup", "sure", "ok", "okay", "do it", "please", "yes please", "go ahead",
        "abeg", "save it", "sure thing", "alright", "fine"}
_NO = {"no", "nah", "nope", "cancel", "never mind", "nevermind", "leave it", "dont", "don't", "skip", "no thanks", "not now"}
_ORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "last": -1, "one": 1, "two": 2, "three": 3}
_STOP = set("the a an and or but of to in on at for with about that this it is are was were my me you your please yes no".split())


def present_question(text, extra=None):
    """-> canonical command for a 'right now' question, else None."""
    n = informal.normalise(text, extra)["text"].strip()
    for rx, cmd in _PRESENT:
        if rx.search(n):
            return cmd
    return None


def interpret(raw, now, extra=None):
    raw = raw or ""
    n = informal.normalise(raw, extra)
    k = informal.kind_of(raw, extra)
    base = {"raw": raw, "normalised": n["text"], "changes": n["changes"], "uncertain": n["uncertain"]}
    if k in ("command", "empty"):
        return {**base, "kind": "command" if k == "command" else "unknown"}
    low = n["text"]
    if _CONVO.search(low):
        w = timeref.parse(low, now)
        return {**base, "kind": "convo_recon", "window": w, "words": low}
    if _RECON.search(low):
        w = timeref.parse(low, now)
        if w is None:
            return {**base, "kind": "need_time"}
        return {**base, "kind": "market_recon", "window": w}
    if _FOLLOW.search(low.strip()):
        return {**base, "kind": "followup"}
    canon = present_question(raw, extra)
    if canon:
        return {**base, "kind": "ask_bus", "canonical": canon}
    if k == "question":
        return {**base, "kind": "ask_bus", "canonical": n["text"]}            # BUS's own keyword router gets the cleaned wording
    if informal.looks_like_market_talk(raw, extra) and (informal.classify_stance(raw, extra) or len(low.split()) >= 4):
        return {**base, "kind": "offer_save"}
    return {**base, "kind": "unknown"}


def resolve(options, raw, extra=None):
    """Reads a reply to a clarification. -> ('option', option) | ('cancel', None) | ('ambiguous', None) | (None, None) when it
    isn't an answer to the question at all (the message is then treated as a new one)."""
    n = informal.normalise(raw, extra)["text"].lower().strip(" .!?,")
    if not options:
        return None, None
    if n in _YES:
        return ("option", options[0]) if len(options) == 1 else ("ambiguous", None)
    if n in _NO:
        return "cancel", None
    m = re.fullmatch(r"(?:option |number |#)?(\d)(?:st|nd|rd|th)?", n)
    if m and 1 <= int(m.group(1)) <= len(options):
        return "option", options[int(m.group(1)) - 1]
    for w, i in _ORD.items():
        if re.search(rf"\b{w}\b", n) and len(n.split()) <= 4:
            idx = len(options) - 1 if i == -1 else i - 1
            if 0 <= idx < len(options):
                return "option", options[idx]
    toks = {t for t in re.findall(r"[a-z']{3,}", n) if t not in _STOP}
    if toks:
        scored = sorted(((len(toks & {t for t in re.findall(r"[a-z']{3,}", o["label"].lower()) if t not in _STOP}), i) for i, o in enumerate(options)),
                        reverse=True)
        if scored[0][0] >= 1 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return "option", options[scored[0][1]]
        if scored[0][0] >= 1:
            return "ambiguous", None
    return None, None
