"""
converse.py — reading a plain message ("wah is price doing rn?", "what happened by 8:30", "what did we talk about
tuesday", "buh it broke the high tho") and deciding WHAT IT IS. Pure: informal + timeref + voice. It never answers
anything itself and never stores anything; it returns a decision, and bus_runner (the I/O edge) acts on it.

DECISIONS
  command        starts with "/": none of this applies, the existing commands run as they always did.
  ask_bus        a question the BUS can answer: returns the CANONICAL command to hand it (for example "/now").
  market_recon   a stretch of the market: returns the time Window.
  convo_recon    "what did we talk about ...": returns the Window (and the words, for matching a named topic).
  act_offer      "i sold gbpusd at 1.3240 sl 1.3270 tp 1.3180", "my trade hit tp, 2R", "trade 1 i was thinking ...": the bot ASKS, then does it.
  act_missing    a trade described without its entry, stop or target: the bot asks for what is missing.
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
_RECON2 = re.compile(r"\bhow (was|did|were)\b.*\b(market|price|gbpusd|cable|london|new york|ny|overlap|session|asian|asia|day|morning|night|yesterday|today)\b|"
                     r"\b(summary|summarise|summarize|recap|rundown|run down) (of )?(the )?(market|price|session|day|london|new york|ny|overlap)\b|"
                     r"\bhow('?s| has| have)\b.*\b(market|price)\b.*\b(been|gone)\b", re.I)
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
_STOP = set("the a an and or but of to in on at for with about that this it is are was were my me you your please yes no "
            "what how why when where which who whats think thinks see seeing saw look looking like did does do done we our us talk talked "
            "discuss said say tell show give want need can could would should".split())


# --- plain-language acts: logging a trade, recording how it ended, saving your reasoning ---------------------------------------
_PRICE = r"(\d\.\d{2,5})"
_SIDE = re.compile(r"\b(buy|bought|buying|long|went long|going long|sell|sold|selling|short|shorted|went short|going short)\b", re.I)
_SL = re.compile(rf"\b(?:sl|stop ?loss|stop|stoploss)\b\D{{0,12}}{_PRICE}", re.I)
_TP = re.compile(rf"\b(?:tp|take ?profit|target|takeprofit)\b\D{{0,12}}{_PRICE}", re.I)
_ENTRY = re.compile(rf"(?:\bat\b|@|\bentry\b|\bentered\b|\bfrom\b)\D{{0,8}}{_PRICE}", re.I)
_PAIR = re.compile(r"\b(gbpusd|eurusd|cable|gu)\b", re.I)
_REF = re.compile(r"\b(?:trade|case|position|setup)\s*(?:#|no\.?|number)?\s*(?:case-0*)?(\d+)\b|#(\d+)\b|\bcase-0*(\d+)\b", re.I)
_MY_TRADE = re.compile(r"\b(my (last |latest |recent )?(trade|position|setup|sell|buy|short|long)|the trade|that trade|this trade|trade|case|position)\b", re.I)
_RES = (
    (re.compile(r"\b(break ?even|breakeven|be)\b|\bscratch(ed)?\b|\bclosed (at )?(entry|even)\b", re.I), "be"),
    (re.compile(r"\b(lost|loss|losing|stopped out|stop(ped)? out|hit (my )?(sl|stop)|stop got hit|sl hit|got stopped)\b", re.I), "loss"),
    (re.compile(r"\b(won|win|winner|winning|profit|in profit|hit (my )?(tp|target)|tp hit|target hit|took profit|got tp|hit tp)\b", re.I), "win"),
)
_RR = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*r\b", re.I)
_REASON_FOR = re.compile(r"^(?:(?:for|on) )?(?:i took |i entered |i opened |why i took |my reason(?:ing|s)? (?:for|on) )?(?:trade|case|position)\s*(?:#|no\.?|number)?\s*(?:case-0*)?(\d+)\s*[,:\-]?\s*"
                         r"(?:i )?(?:(?:was thinking|thought|reasoned|reasoning(?: was)?|because|cos|coz|since|my reason(?: was)?|the reason(?: was)?|why)\b[: ]*)(.+)$", re.I | re.S)


def _act(raw, low):
    """-> a decision dict or None. Only reads; the runner asks before anything is stored."""
    m = _REASON_FOR.search(raw.strip())
    if m:
        return {"kind": "act_offer", "act": "reason", "case_no": int(m.group(1)), "text": m.group(2).strip()}
    sd = _SIDE.search(raw)
    if sd and (_ENTRY.search(raw) or _SL.search(raw) or _TP.search(raw)) and not re.search(r"\b(should|would|could|might|maybe|thinking of|planning|plan to|if)\b", raw, re.I):
        word = sd.group(1).lower()
        side = "buy" if word in ("buy", "bought", "buying", "long", "went long", "going long") else "sell"
        en, sl, tp = _ENTRY.search(raw), _SL.search(raw), _TP.search(raw)
        if not en:                                           # the first price that isn't the stop or the target
            taken = {x.group(1) for x in (sl, tp) if x}
            en = next((x for x in re.finditer(_PRICE, raw) if x.group(1) not in taken), None)
        pr = _PAIR.search(raw)
        pair = "GBPUSD"
        if pr and pr.group(1).lower() == "eurusd":
            pair = "EURUSD"
        missing = [n for n, v in (("the price you entered at", en), ("your stop loss", sl), ("your target", tp)) if not v]
        if missing:
            return {"kind": "act_missing", "act": "logtrade", "missing": missing}
        return {"kind": "act_offer", "act": "logtrade", "pair": pair, "side": side, "entry": en.group(1), "sl": sl.group(1), "tp": tp.group(1)}
    if _MY_TRADE.search(raw) and not raw.strip().endswith("?"):
        res = next((r for rx, r in _RES if rx.search(raw)), None)
        if res:
            ref = _REF.search(raw)
            no = next((int(g) for g in ref.groups() if g), None) if ref else None
            rr = _RR.search(raw)
            return {"kind": "act_offer", "act": "outcome", "case_no": no, "result": res, "r": rr.group(1).lstrip("+") if rr else None}
    return None


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
    a = _act(raw, low)
    if a:
        return {**base, **a}
    if _CONVO.search(low):
        w = timeref.parse(low, now)
        return {**base, "kind": "convo_recon", "window": w, "words": low}
    if _RECON.search(low) or _RECON2.search(low):
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


def resolve(options, raw, extra=None, strict=False):
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
    if toks and not strict:                  # strict: the message is itself a clear new request, so only a number, yes/no or ordinal counts
        scored = sorted(((len(toks & {t for t in re.findall(r"[a-z']{3,}", o["label"].lower()) if t not in _STOP}), i) for i, o in enumerate(options)),
                        reverse=True)
        if scored[0][0] >= 1 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return "option", options[scored[0][1]]
        if scored[0][0] >= 1:
            return "ambiguous", None
    return None, None
