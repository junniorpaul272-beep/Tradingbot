"""
guard.py — the last check before anything is said to you. "Think before you talk."

Pure stdlib; imports nothing from this codebase; holds no facts and never invents any. It only REVIEWS the text a reply was already
built from (and, where a message asked several things, checks that every part got an answer). bus_runner passes every reply through
review() before it is stored or sent, and deliver() runs final() on everything that leaves, including alerts and announcements.

WHAT IT STOPS
  hard (the message is replaced by a short honest one; nothing half-built is sent)
    EMPTY          nothing to say
    PLACEHOLDER    an unfilled {field}, None / nan / null / undefined, or a Python error
    DUMP           the plain claim listing ("[market_state] status ANSWER ...", JSON) that the claim-bound audit falls back to
  repaired (fixed in place, and the fix is recorded)
    RAW_CODE       an engine CODE_WORD (AGING_CAMPAIGN, TIER_3_STRUCTURE) became lower-case words
    DOTTED_PATH    an internal field path (htf.bias, thesis.state) became words
    INTEGRITY_FIRST  a system-check note led a market answer: moved to the end
    DUPLICATE_LINE the same line twice
    TOO_LONG       longer than a chat message should be: cut at a line, and it says so
    PART_MISSING   a part of a several-part question produced nothing: said plainly instead of silently dropped
  noted (recorded, not changed; the tests expect these to be zero on normal replies)
    JARGON         internal vocabulary (MIL, tier names, ids)
    LONG_QUOTE     a very long quoted engine sentence

It cannot make a wrong sentence right. Facts stay the business of the claim-bound audit in bus.py; this only makes sure what reaches a
person is readable, complete and clean.
"""
import re
from collections import namedtuple

Result = namedtuple("Result", "text violations blocked repaired")

CAPS = {"answer": 1500, "report": 3600, "short": 900, "other": 3900}
HARD_CAP = 3900                   # Telegram refuses more than 4096 characters; stay clear of it
SAFE = ("I have the information but couldn't put it into words I trust, so I won't guess. "
        "Try asking it a simpler way, or send /story for the full technical read.")
SAFE_EMPTY = "I don't have anything to say to that yet. Try asking it another way, or send /help."

_CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_DOTTED = re.compile(r"\b(?:htf|thesis|leg|case|system|market|volatility|momentum|watch|story|campaign|event|signal|user|research|alerts|engine)"
                     r"\.[a-z][a-z_]*(?:\.[a-z_]+)*\b")
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}|\bNone\b(?! of\b)|\bnan\b|\bNaN\b|\bnull\b|\bundefined\b|Traceback|\b[A-Z][a-z]+Error\b|<class |<function ")
_DUMP = re.compile(r"^\[[a-z_]+\] status [A-Z_]+|\"[a-z_]+\":\s*[\[{\"0-9]|^- (?:MISSING|CONFLICT|NOTE) ", re.M)
_JARGON = re.compile(r"\bMIL\b|\bTIER_?\d\b|\bBUS\b|\bupdate_id\b|\bused_fallback\b|\bscan_id\b|\bthread_id\b|\bclaim_id\b|\bevent_id\b")
_QUOTE = re.compile(r"“([^”]{200,})”")
_INTEGRITY = re.compile(r"^(?:Integrity (?:alarm|inspector)|• (?:LOW|MED|HIGH)\b|• …and more)")


_QUOTED = re.compile(r'"[^"\n]*"|\u201c[^\u201d]*\u201d|\'[^\'\n]{12,}\'')


def _outside_quotes(t):
    """The text with anything in quotes removed: your own words (a note you saved, a message you typed) are shown back to you as you
    wrote them and are never judged as if the bot had written them."""
    return _QUOTED.sub(" ", t)


def _code_words(m):
    return m.group(0).lower().replace("_", " ")


def _path_words(m):
    return m.group(0).replace(".", " ").replace("_", " ")


def _move_integrity(text):
    """Move the leading system-check lines to the end as one short line. -> (text, moved?)"""
    lines = text.split("\n")
    i = 0
    while i < len(lines) and _INTEGRITY.match(lines[i]):
        i += 1
    if i == 0 or i == len(lines):
        return text, False
    moved = lines[:i]
    rest = lines[i:]
    note = "(Housekeeping: the integrity check has " + ("a note" if len(moved) <= 2 else "some notes") + " for you. Send /health to see it.)"
    return "\n".join(rest + ["", note]), True


def _cut(text, cap):
    if len(text) <= cap:
        return text
    keep, size = [], 0
    for line in text.split("\n"):
        if size + len(line) + 1 > cap - 110:
            break
        keep.append(line)
        size += len(line) + 1
    if not keep:
        keep = [text[:cap - 110]]
    return "\n".join(keep).rstrip() + "\n\n(Shortened to fit. Ask me about one part of it and I'll go into that.)"


def review(text, kind="answer", parts=None):
    """-> Result(text, violations, blocked, repaired).
    `parts` is, for a message that asked several things, a list of (label, text): every part must have produced something."""
    v, repaired = [], False
    t = (text or "").strip()
    if not t:
        return Result(SAFE_EMPTY, ("EMPTY",), True, False)
    own = _outside_quotes(t)
    if _DUMP.search(own):
        return Result(SAFE, ("DUMP",), True, False)
    if _PLACEHOLDER.search(own):
        return Result(SAFE, ("PLACEHOLDER",), True, False)
    if parts:
        missing = [lab for lab, body in parts if not (body or "").strip()]
        if missing:
            v.append("PART_MISSING")
            repaired = True
            t += "\n\n" + " ".join(f"I couldn't answer the part about {lab}." for lab in missing)
    if _CODE.search(t):
        t = _CODE.sub(_code_words, t)
        v.append("RAW_CODE"); repaired = True
    if _DOTTED.search(t):
        t = _DOTTED.sub(_path_words, t)
        v.append("DOTTED_PATH"); repaired = True
    if kind in ("answer", "short"):
        t2, moved = _move_integrity(t)
        if moved:
            t = t2
            v.append("INTEGRITY_FIRST"); repaired = True
    seen, out, dup = set(), [], False
    for line in t.split("\n"):
        key = line.strip()
        if len(key) > 24 and key in seen:
            dup = True
            continue
        seen.add(key)
        out.append(line)
    if dup:
        t = "\n".join(out)
        v.append("DUPLICATE_LINE"); repaired = True
    cap = min(CAPS.get(kind, CAPS["other"]), HARD_CAP)
    if len(t) > cap:
        t = _cut(t, cap)
        v.append("TOO_LONG"); repaired = True
    if _JARGON.search(t):
        v.append("JARGON")
    if _QUOTE.search(t):
        v.append("LONG_QUOTE")
    return Result(t, tuple(v), False, repaired)


def final(text):
    """The last gate in deliver(): only the hard rules and the code-word clean-up, for every message that leaves. -> Result."""
    t = (text or "").strip()
    if not t:
        return Result(SAFE_EMPTY, ("EMPTY",), True, False)
    own = _outside_quotes(t)
    if _DUMP.search(own) or _PLACEHOLDER.search(own):
        return Result(SAFE, ("DUMP" if _DUMP.search(own) else "PLACEHOLDER",), True, False)
    v, repaired = [], False
    if _CODE.search(t):
        t = _CODE.sub(_code_words, t)
        v.append("RAW_CODE"); repaired = True
    if _DOTTED.search(t):
        t = _DOTTED.sub(_path_words, t)
        v.append("DOTTED_PATH"); repaired = True
    if len(t) > HARD_CAP:
        t = _cut(t, HARD_CAP)
        v.append("TOO_LONG"); repaired = True
    return Result(t, tuple(v), False, repaired)
