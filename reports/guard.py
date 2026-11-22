"""guard.py - the last gate before anything is sent. Failing any check means the report is NOT sent."""
import re
BANNED = [r"\blegs?\b", r"\bengine\b", r"\bsetups?\b", r"\bentry\b", r"\bstop[- ]?loss\b", r"\btake[- ]profit\b", r"\bsignals?\b", r"\btiers?\b",
          r"\bbreak count\b", r"\bbreaks in\b", r"\baged?\b", r"\baging\b", r"\bstretched\b", r"\bself-check\b", r"\bthesis\b", r"\bconviction\b",
          r"\bhigher timeframe\b", r"\blower timeframe\b", r"\bHTF\b", r"\bLTF\b", r"\balign(ed|ment)?\b", r"\bconflict", r"\bcatch(es)? up\b", r"\bvs\.?\b",
          r"\bagainst the (1H|15M|5M)\b", r"[“”\"]", r"\bI think\b", r"\bprobably\b", r"\bshould (buy|sell)\b", r"\b(buy|sell) now\b"]
NUM = re.compile(r"\d+(?:\.\d+)?")
TAG = re.compile(r"<(/?)(b|i|code|pre)>")
MAX_MESSAGE = 3500          # Telegram's limit is 4096
MAX_LINE = 230              # no wall-of-text lines
MAX_BLOCK = 800             # no wall-of-text blocks (a table in <pre> is exempt)


class GuardError(Exception): pass


def check(text, renderer, trace):
    plain = re.sub(r"<[^>]+>", "", text)
    plain = re.sub(r"/(thesis|thesis_[a-z0-9]+)\b", "/cmd", plain)      # the fixed command names are allowed; the word is not allowed in sentences
    problems = []
    bad = [p for p in BANNED if re.search(p, plain, re.I)]
    if bad: problems.append(f"banned wording {bad}")
    stray = sorted({t for t in NUM.findall(plain) if t not in renderer.tokens})
    if stray: problems.append(f"numbers not from facts {stray}")
    # format
    if len(text) > MAX_MESSAGE: problems.append(f"message too long ({len(text)})")
    if re.search(r"\n{3,}", text): problems.append("3+ blank lines")
    if re.search(r"[ \t]+\n", text): problems.append("trailing spaces")
    if "  " in re.sub(r"<pre>.*?</pre>", "", text, flags=re.S): problems.append("double spaces outside a table")
    stack = []
    for m in TAG.finditer(text):
        if m.group(1) == "": stack.append(m.group(2))
        elif not stack or stack.pop() != m.group(2): problems.append("unbalanced tags"); break
    if stack: problems.append("unclosed tags")
    for para in re.split(r"\n\s*\n", re.sub(r"<pre>.*?</pre>", "", text, flags=re.S)):
        if len(re.sub(r"<[^>]+>", "", para)) > MAX_BLOCK: problems.append("a block is too long to read comfortably")
        if any(len(re.sub(r"<[^>]+>", "", ln)) > MAX_LINE for ln in para.split("\n")): problems.append("a line is too long to read comfortably")
    if re.search(r"[<>&](?!/?(b|i|code|pre)>)", re.sub(r"<(/?)(b|i|code|pre)>", "", text)): problems.append("unescaped markup character")
    if not trace or any(not s for _, s in trace): problems.append("a paragraph has no source")
    if problems: raise GuardError("; ".join(problems))
    return text
