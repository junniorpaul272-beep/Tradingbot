"""
informal.py — understanding how you actually type, WITHOUT ever changing what you said. Pure stdlib; imports nothing
from this codebase.

TWO JOBS, KEPT APART
  UNDERSTAND  `normalise()` turns "wah is price doing rn?" into "what is price doing right now?" so the router can
              answer it. That is used for ROUTING ONLY.
  RECORD      Nothing here is ever stored in place of your message. The ledger keeps the raw text; anything this
              module works out (the expanded wording, a stance such as "expectation") is stored beside it, labelled
              as interpreted and unverified.

HONESTY RULES
  * A word the table knows is expanded and the expansion is reported (`changes`), so a reader can see exactly what
    was assumed.
  * A word that MIGHT be slang but could be ordinary English ("go", "na", "dey") is never silently expanded; it is
    listed in `uncertain`.
  * Typo repair only ever targets a small vocabulary the router uses, needs a very close match, and is reported.
  * A stance ("expectation", "observation", "reasoning") is a reading of YOUR wording, with the exact words that
    triggered it. It is never a market fact and never verified.
  * The table is a floor, not a ceiling: you can teach the bot a word with /slang, which only affects routing.
"""
import difflib
import re

# word -> what it means. Routing only.
SLANG = {
    "rn": "right now", "atm": "at the moment", "wah": "what", "wat": "what", "wht": "what", "wetin": "what",
    "buh": "but", "bt": "but", "tho": "though", "thru": "through", "abeg": "please", "pls": "please", "plz": "please",
    "u": "you", "ur": "your", "r": "are", "wen": "when", "tink": "think", "thnk": "think", "cud": "could", "cld": "could", "shud": "should", "wud": "would", "den": "then", "dat": "that", "lst": "last", "lsat": "last", "lastest": "latest", "wer": "where", "hw": "how", "bcos": "because", "cos": "because",
    "cuz": "because", "bc": "because", "tbh": "to be honest", "idk": "i do not know", "imo": "in my opinion",
    "gonna": "going to", "wanna": "want to", "gotta": "have to", "lemme": "let me", "dunno": "do not know",
    "tmrw": "tomorrow", "yday": "yesterday", "morn": "morning", "nite": "night", "info": "information",
    "sup": "what is up", "wassup": "what is up", "thx": "thanks", "ty": "thank you",
}
# Could be slang, could be plain English: never expanded on our own, only reported.
AMBIGUOUS = {"go": "may be the pidgin future marker ('price go retrace')", "dey": "may mean 'is' or 'are'",
             "na": "may mean 'it is' (or n/a)", "abi": "may mean 'right?'", "e": "may mean 'it'", "don": "may mean 'has already'"}
# phrase-level pidgin. Each is reported as an assumption.
PHRASES = (
    (re.compile(r"\be don\b", re.I), "it has"),
    (re.compile(r"\bhow far\b", re.I), "hello"),
    (re.compile(r"\bwetin dey\b", re.I), "what is"),
)

# Words the router cares about; used ONLY to repair a near-miss spelling.
VOCAB = ("thesis", "watching", "changed", "story", "bullish", "bearish", "retrace", "retracement", "support",
         "resistance", "structure", "direction", "happened", "interesting", "looking", "seeing", "reconstruct",
         "consolidation", "momentum", "protected", "reclaim", "rejection", "reversal", "continuation")

REAL_WORDS = {"change", "changes", "changing", "structures", "directions", "supports", "reversals", "watch", "story", "stories"}   # ordinary words, never 'repaired'

# Words the router cares about in QUESTIONS. A typo is repaired only when it is a transposition, a dropped letter or a doubled letter of one of
# these (the three ways fingers actually slip), never a different real word, and the repair is reported like any other.
ROUTE_VOCAB = ("choch", "chochs", "bos", "fvg", "fvgs", "sweep", "sweeps", "last", "latest", "when", "what", "many", "market", "explain", "order", "block",
               "blocks", "value", "fair", "today", "doing", "right", "happened", "liquidity", "character", "break", "price", "inside", "between", "after",
               "before", "first", "recent", "recently", "show", "again", "details", "yesterday", "morning", "afternoon", "gap", "gaps")
_PROTECT = {"lat", "las", "hen", "wen", "whet", "hat", "wha", "lost", "list", "lest", "than", "west", "wheat", "chat", "bloc", "mark", "orde", "ordr", "fir", "firs",
            "afte", "befor", "rest", "pric", "pice", "rice", "insid", "bos", "bo", "ma", "man", "any", "doin", "gp", "ga", "bak", "brea", "sho", "val", "fai", "fare",
            "fail", "may", "black", "lock", "locks", "far", "gas", "gasp", "gain", "ding", "dong", "sow", "vale", "beak", "seep", "seeps", "weep", "weeps", "fist", "ater",
            "recen", "shw", "agai", "deta", "mon", "ask", "bak", "last", "once", "ones", "cho", "ch", "wh", "wa", "ho", "tod", "toda"}


def _typo_map():
    out, clash = {}, set()
    for w in ROUTE_VOCAB:
        if len(w) < 4:
            continue
        v = set()
        for i in range(len(w) - 1):                                 # swapped neighbours
            if w[i] != w[i + 1]:
                v.add(w[:i] + w[i + 1] + w[i] + w[i + 2:])
        for i in range(len(w)):                                     # dropped letter (keep at least 3 letters)
            if len(w) >= 5 or i > 0:
                v.add(w[:i] + w[i + 1:])
            v.add(w[:i] + w[i] + w[i:])                             # doubled letter
        if len(w) >= 5 or w == "many":                              # one wrong vowel (explain -> explaan, many -> muny)
            for i, ch in enumerate(w):
                if ch in "aeiou":
                    for x in "aeiou":
                        if x != ch:
                            v.add(w[:i] + x + w[i + 1:])
        for t in v:
            if len(t) >= 3 and t != w and t not in _PROTECT and t not in SLANG and t not in AMBIGUOUS and t not in ROUTE_VOCAB:
                if out.get(t, w) != w:
                    clash.add(t)
                out[t] = w
    for t in clash:
        out.pop(t, None)
    return out


TYPO_MAP = _typo_map()

_WORD = re.compile(r"[A-Za-z']+|\d+(?::\d+)?|[^\sA-Za-z\d]")

_AUX2 = {"was", "is", "are", "did", "does", "do", "happened", "many", "the", "a", "were", "has", "have"}
_OPENERS = {"so", "ok", "okay", "hey", "yo", "bro", "oya", "please", "well", "and", "hi", "hello", "abeg", "biko", "lol", "quickly", "fast", "also", "then"}
_QUESTION_START = {"was", "were", "has", "have", "had", "wasnt", "isnt", "arent", "werent", "didnt", "doesnt", "dont", "wont", "cant", "couldnt", "shouldnt",
                   "wouldnt", "hasnt", "havent", "hadnt", "wheres", "hows", "explain", "what", "whats", "how", "why", "where", "when", "which", "who", "is", "are", "do", "does", "did",
                   "can", "could", "should", "would", "will", "any", "anything", "remind", "tell", "show", "take", "walk"}

# stance cues (exact words that triggered a reading are returned with it)
_EXPECT = ("i think", "i expect", "looking for", "waiting for", "i'm waiting", "im waiting", "expecting", "hoping",
           "probably", "likely", "might", "should retrace", "going to", "gonna", "will retrace", "will go", "supposed to",
           "price go ", "i feel like", "i reckon", "my plan", "planning")
_REASON = ("because", "since ", "that's why", "thats why", "the reason", "so i ", "which is why", "cause ", "cos ", "cuz ")
_OBSERVE = ("broke", "break of", "swept", "sweep", "rejected", "reject", "reclaimed", "closed above", "closed below",
            "wicked", "tapped", "formed", "i see", "i saw", "i noticed", "noticed", "looks like", "it's at",
            "its at", "printed", "respected", "disrespected", "took out", "taken out", "pulled back", "bounced")

MARKETISH = ("high", "low", "break", "broke", "retrace", "support", "resistance", "zone", "poi", "sweep", "bos", "choch",
             "price", "candle", "trend", "reject", "reclaim", "liquidity", "fvg", "order block", "ob ", "bias", "structure",
             "wick", "pullback", "bounce", "rally", "drop", "momentum", "gbpusd", "cable")


def normalise(text, extra=None):
    """-> dict(raw, text, changes, uncertain). `extra` is the user's own /slang table (word -> meaning)."""
    raw = text or ""
    table = dict(SLANG)
    for k, v in (extra or {}).items():
        table[str(k).lower()] = str(v)
    changes, uncertain = [], []
    work = raw
    dt = re.sub(r"\b(at|by|around|after|before|since|until|till|from|to|and|between|near)\s+([01]?\d|2[0-3])[.h]([0-5]\d)\b(?!\s*(?:pips?|%))", r"\1 \2:\3", work, flags=re.I)
    if dt != work:
        changes.append({"from": "a time written with a dot", "to": "the same time with a colon", "basis": "time format"})
        work = dt
    for rx, to in PHRASES:
        if rx.search(work):
            changes.append({"from": rx.search(work).group(0), "to": to, "basis": "pidgin phrase"})
            work = rx.sub(to, work)
    out = []
    for tok in _WORD.findall(work):
        low = tok.lower()
        if re.fullmatch(r"[A-Za-z']+", tok):
            if low in table:
                out.append(table[low])
                changes.append({"from": tok, "to": table[low], "basis": "your /slang" if low in (extra or {}) else "slang table"})
                continue
            if low in AMBIGUOUS:
                uncertain.append({"word": tok, "note": AMBIGUOUS[low]})
            elif low in TYPO_MAP and low not in REAL_WORDS and low not in VOCAB:
                changes.append({"from": tok, "to": TYPO_MAP[low], "basis": "near-miss spelling"})
                out.append(TYPO_MAP[low])
                continue
            elif len(low) >= 6 and low not in VOCAB:
                m = difflib.get_close_matches(low, VOCAB, n=1, cutoff=0.88)
                if m and low not in REAL_WORDS:   # "change" is a real word, not a typo of "changed"
                    changes.append({"from": tok, "to": m[0], "basis": "near-miss spelling"})
                    out.append(m[0])
                    continue
        out.append(tok)
    s = ""
    for t in out:                                               # re-join without spaces before punctuation
        s += t if (not s or re.fullmatch(r"[^\sA-Za-z\d]", t) and t not in "(\"'") else " " + t
    return {"raw": raw, "text": s.strip(), "changes": changes, "uncertain": uncertain}


def kind_of(text, extra=None):
    """'command' | 'question' | 'statement' | 'empty' — a reading of the shape of the message."""
    raw = (text or "").strip()
    if not raw:
        return "empty"
    if raw.startswith("/"):
        return "command"
    n = normalise(raw, extra)["text"].lower()
    words = [re.sub(r"[^a-z']", "", w).replace("'", "") for w in n.split()]
    words = [w for w in words if w]
    while len(words) > 1 and words[0] in _OPENERS:                # "ok so when was...", "bro what is...", "hey, tell me..."
        words.pop(0)
    first = words[0] if words else ""
    if first and first not in _QUESTION_START and len(first) >= 4 and len(words) > 1 and words[1] in _AUX2:     # "wuen was the choch": a mangled question word
        if difflib.get_close_matches(first, ["when", "what", "where", "which", "whats"], n=1, cutoff=0.74):
            first = "when"
    if raw.endswith("?") or first in _QUESTION_START:
        return "question"
    return "statement"


def classify_stance(text, extra=None):
    """-> list of {"stance", "cue"} in the order the cues appear (a note can hold an observation AND an expectation).
    Empty when nothing in the wording points anywhere. Always a reading of the wording, never a fact."""
    raw = text or ""
    hays = (" " + raw.lower() + " ", " " + normalise(raw, extra)["text"].lower() + " ")
    best = {}                                   # stance -> (position, cue)
    for stance, cues in (("expectation", _EXPECT), ("reasoning", _REASON), ("observation", _OBSERVE)):
        for cue in cues:
            for hay in hays:
                i = hay.find(cue)
                if i >= 0 and (stance not in best or i < best[stance][0]):
                    best[stance] = (i, cue.strip())
    return [{"stance": s, "cue": c} for s, (_i, c) in sorted(best.items(), key=lambda kv: kv[1][0])]


def looks_like_market_talk(text, extra=None):
    low = " " + normalise(text, extra)["text"].lower() + " "
    return any(w in low for w in MARKETISH)
