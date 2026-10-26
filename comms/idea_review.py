"""
idea_review.py — talking an idea through with you, over several messages, without inventing a single market fact.

WHAT IT DOES
  Keeps a small running picture of YOUR idea (the setup you describe, the outcome you expect, any cause you claim) and updates it every time you add
  a condition, ask how to test it, offer a mechanism, or bring a case that contradicts it. Each reply is built from that picture plus counts handed in
  from the saved records, so it follows your revisions instead of starting again.

WHAT IT NEVER DOES (the authority boundary)
  * It is not a second interpretation of the market. It never says what price will do, never calls an idea good, bad, strong or weak, and never adds
    a rule of its own. The engine owns the market facts; this module only reads counts it is given and talks about METHOD (what would have to be
    defined, compared and recorded before the idea could be called anything).
  * It keeps three meanings of "supported" apart, always:
      OBSERVED          the saved records contain the thing you described (a count, with times)
      CONSISTENT WITH   nothing recorded contradicts your reasoning (which is not evidence for it)
      SHOWN TO WORK     a fair test, against a baseline, with enough separate cases, says it does better. Nothing here ever claims that.
  * Anything the engine does not save (displacement, a protected level, a higher-timeframe zone, price entering a zone) is said plainly, and the reply
    goes on to say how it could be defined and what would have to be recorded, instead of stopping.

Pure stdlib + plainwords. No engine, no clock (times arrive pre-formatted through facts["when"]).
"""
import hashlib
import re

import plainwords

# ------------------------------------------------------------------ what a message can mention
# key, plain label, pattern. The first match wins for overlapping keys: the specific one removes the general one.
_TERMS = (
    ("sweep_low", "a sweep of a low", r"\bswe(?:ep|pt)(?:s|ed|ing)?\s+(?:a |an |the |that |this )?(?:\w+\s+){0,2}?(?:lows?|sell[- ]side)\b|\bgrab(?:s|bed|bing)?\s+(?:the )?(?:\w+\s+)?lows?\b|\bliquidity (?:below|under)\b"),
    ("sweep_high", "a sweep of a high", r"\bswe(?:ep|pt)(?:s|ed|ing)?\s+(?:a |an |the |that |this )?(?:\w+\s+){0,2}?(?:highs?|buy[- ]side)\b|\bgrab(?:s|bed|bing)?\s+(?:the )?(?:\w+\s+)?highs?\b|\bliquidity (?:above|over)\b"),
    ("sweep", "a liquidity sweep", r"\bswe(?:ep|pt)s?\b|\bswept\b|\bliquidity (?:grab|sweep|run|hunt)|\bstop ?hunt"),
    ("choch_bull", "a bullish change of character (CHoCH)", r"\bbullish (?:chochs?|change(?:s)? of character)\b|\b(?:chochs?|change(?:s)? of character) (?:up|bullish|to the upside|higher)\b"),
    ("choch_bear", "a bearish change of character (CHoCH)", r"\bbearish (?:chochs?|change(?:s)? of character)\b|\b(?:chochs?|change(?:s)? of character) (?:down|bearish|to the downside|lower)\b"),
    ("choch", "a change of character (CHoCH)", r"\bchochs?\b|\bchange(?:s)? of character\b"),
    ("bos", "a break of structure (BOS)", r"\bbos\b|\bbreak(?:s)? of structure\b"),
    ("htf_zone", "a higher-timeframe zone", r"\b(?:higher[- ]?time ?frame|htf|1h|4h|h1|h4|daily|weekly|1-hour|4-hour)\s+(?:demand|supply|zone|support|resistance|order ?block|ob|fvg|area|level)s?\b"
                                           r"|\b(?:demand|supply) zones?\b"),
    ("fvg", "a fair value gap (FVG)", r"\bfvgs?\b|\bfair value gaps?\b|\bimbalances?\b"),
    ("ob", "an order block", r"\border ?blocks?\b|\bobs?\b"),
    ("retrace", "a pullback", r"\bretrac\w+|\bpull(?:ing)? ?backs?\b|\bretests?\b|\bcomes? back (?:to|into)\b"),
    ("displacement", "displacement", r"\bdisplacement\b|\bimpulsive\b|\bimpulse\b"),
    ("bias", "the higher-timeframe bias", r"\bbias\b|\bhigher[- ]?time ?frame (?:trend|direction)\b"),
    ("session", "a session level", r"\b(?:asia|asian|london|new york|ny)\b"),
    ("tier", "one of the bot's tiers", r"\btier ?[123]\b"),
    ("vol_exp", "volatility expansion", r"\bvolatility (?:expansion|expanding|expands|increas\w+)|\bexpansion\b|\bexpanding volatility\b|\bexpand(?:s|ing)\b"),
    ("collect_liq", "'collecting liquidity'", r"\bcollect(?:s|ed|ing)? liquidity\b|\bliquidity collection\b|\bbuilding liquidity\b|\bliquidity farming\b"),
    ("signal", "a signal or entry", r"\bsignals?\b|\bentr(?:y|ies)\b"),
)
_SPECIFIC = {"sweep_low": "sweep", "sweep_high": "sweep", "choch_bull": "choch", "choch_bear": "choch"}
_RECORD_KEY = {"sweep": "sweep", "sweep_low": "sweep", "sweep_high": "sweep", "choch": "choch", "choch_bull": "choch", "choch_bear": "choch", "bos": "bos",
               "fvg": "fvg", "ob": "ob", "retrace": "retracement", "displacement": "displacement", "bias": "bias", "vol_exp": "volatility"}
_OUTCOME = (
    ("up", r"\b(?:continue|continues|continuing|keep going|carry on|go on|extend|extends)\b\s*(?:to go\s*)?(?:up|upward|upwards|higher|to the upside)\b|\b(?:go(?:es)?|move(?:s)?|rall(?:y|ies)|rise(?:s)?|push(?:es)?|run(?:s)?) (?:up|upward|upwards|higher)\b|\brally\b|\bcontinue upward\b|\bit (?:might|may|could|will) continue up"),
    ("down", r"\b(?:continue|continues|keep going|carry on|go on|extend|extends)\b\s*(?:to go\s*)?(?:down|downward|downwards|lower|to the downside)\b|\b(?:go(?:es)?|move(?:s)?|drop(?:s)?|fall(?:s)?|push(?:es)?) (?:down|downward|downwards|lower)\b|\bdrop\b|\bsell[- ]?off\b"),
    ("reverse", r"\brevers\w+|\bturn(?:s)? around\b|\bflip(?:s)?\b"),
    ("hold", r"\bhold(?:s)?\b|\bbounce(?:s)?\b|\brespect(?:s|ed)?\b|\breact(?:s|ed)?\b"),
    ("continue", r"\bcontinu\w+|\bfollow(?:s)? through\b|\bkeep(?:s)? going\b|\bnext move\b"),
)
_OUTCOME_LABEL = {"up": "continues up", "down": "continues down", "reverse": "reverses", "hold": "holds or reacts", "continue": "carries on in the same direction"}
_MODAL = re.compile(r"\b(might|may|could|probably|likely|perhaps|maybe|will|going to|gonna|should|tends? to|usually|always|expect)\b", re.I)
_VAGUE = re.compile(r"\b(lines? up|lined up|too eager|too early|too late|strong(?:er)?|weak(?:er)?|clean|clear|obvious|proper|real|fake|valid|good|bad|decent|solid|significant|"
                    r"important|high[- ]quality|quality|confirm(?:ed|ation)?|reliable|respect(?:ed)?|eager|aggressive|messy|choppy|"
                    r"long|tight|big|small|large|huge|quick(?:ly)?|slow(?:ly)?|usually|often|mostly|keeps? going|a while|some time|breaks? out|compress\w*)\b", re.I)
_HINDSIGHT = re.compile(r"\b(the real (?:low|high)|the actual (?:low|high)|in hindsight|looking back|obviously|after it (?:reversed|turned|dropped|rallied)|the bottom|the top)\b", re.I)
_EXIT = re.compile(r"\b(stop|invalid\w*|wrong|exit|target|cut|out if|fail(?:s|ed|ure)?|abandon)\b", re.I)

# ------------------------------------------------------------------ what kind of turn it is
_NEG_CMD = re.compile(r"\bdon'?t want (?:an? |any )?(?:alert|alarm|trade)s?\b|\bno (?:alert|alarm|trade)s?\b|\bnot (?:an? )?(?:alert|trade)\b|\bwithout (?:an? )?(?:alert|alarm)\b|"
                      r"\bjust (?:thinking|trying to understand|exploring|curious|asking|wondering)\b|\b(?:alert|trade)s?\b[\w\s,]{0,14}\byet\b|\bnot (?:setting|taking|placing)\b", re.I)
_CONTRA = re.compile(r"\bactually\b|\bbut i (?:found|saw|noticed|checked|looked)\b|\bi (?:found|saw|noticed|checked) (?:a|one|another|that)\b|\bcounter ?example\b|"
                     r"\bthat'?s not (?:true|right|always)\b|\bnot always\b|\b(?:came|happened|was|printed|formed) first\b|\bthe other way (?:round|around)\b|\bit was the other way\b", re.I)
_ADD = re.compile(r"\bwhat if\b|\bbut if\b|\band if\b|\bonly (?:when|if)\b|\binside\b|\bwithin\b|\bat (?:a|an|the)\b|\bplus\b|\bin addition\b|\bwhat about\b|\bwith (?:a|an|the)\b|\bnear\b|\binto\b", re.I)
_STRONGER = re.compile(r"\bstronger\b|\bbetter\b|\bmore (?:likely|reliable|convincing|certain|useful)\b|\bimprov\w+|\badd(?:s)? (?:anything|value)\b|\bmatters?\b|\bmake(?:s)? (?:a )?difference\b|\bworth\b|\bhelp(?:s)?\b", re.I)
_HOWTEST = re.compile(r"\bhow (?:would|do|can|could|should|will) (?:we|i|you)\b[\w\s']{0,30}\b(?:test|check|prove|study|measure|verify|find out|know|tell)\b|"
                      r"\bwhat (?:would|do|should|will) (?:we|i) (?:need|have) to (?:record|track|log|store|save|measure|collect|capture|know|do)\b|"
                      r"\b(?:test|study|investigate|backtest) (?:it|this|that|whether|properly)\b|\bhow (?:to|do we) (?:test|check|prove|study)\b|\bto study it\b|"
                      r"\bwhat (?:would|should|do) (?:we|i) (?:record|track|log|measure)\b", re.I)
_MECH = re.compile(r"\bbecause\b[\w\s',]{0,50}\b(?:collect\w*|hunt\w*|grab\w*|tak(?:e|es|ing)|manipulat\w*|accumulat\w*|distribut\w*|trap\w*|clear\w*|induc\w*|liquidity|fill\w*|rebalanc\w*)\b|"
                   r"\b(?:smart money|institutions?|banks?|big players?|the market|market makers?)\b[\w\s']{0,20}\b(?:want|wants|need|needs|hunt|hunting|collect|collecting|trap|trapping|accumulat\w+|distribut\w+)\b", re.I)
_CAUSAL = re.compile(r"\bcaus(?:e|ed|es|ing)\b|\btriggered?\b|\bled to\b|\bresult(?:s|ed)? in\b|\bis why\b|\bbecause (?:of )?(?:the |it\b|that\b)", re.I)
_PREDICT_Q = re.compile(r"\bdo you think\b|\bwhat do you think\b|\bwill (?:it|price|this|that|the)\b[\w\s']{0,25}\b(?:hold|go|reach|break|reverse|fall|rise|drop|work|bounce|respect)\b|"
                        r"\bis (?:it|this|that)\b[\w\s']{0,20}\b(?:going|likely|gonna)\b|\bthoughts on\b|\bwhat are the chances\b", re.I)
_MORE_LIKELY = re.compile(r"\b(?:does|do|did|is|are)\b[\w\s']{0,40}\b(?:make|makes|mean|lead|leads|help|increase|improve)\b[\w\s',]{0,40}\b(?:more likely|likely|continue|follow|work|better|stronger)\b|"
                          r"\bmore likely\b", re.I)
_FORECAST = re.compile(r"\bwill (?:price|it|the market|gbpusd|cable|this)\b|\bgoing to (?:go|rise|fall|drop|rally|reverse|hold|break)\b", re.I)


def terms(text):
    """-> [key] in the order mentioned, the specific key replacing a general one it contains (a sweep of a low is not also 'a sweep')."""
    low = (text or "").lower()
    found = []
    for key, _label, rx in _TERMS:
        m = re.search(rx, low)
        if m:
            found.append((m.start(), key))
    keys = [k for _, k in sorted(found)]
    gone = {_SPECIFIC[k] for k in keys if k in _SPECIFIC}
    return [k for k in keys if k not in gone or (k in _SPECIFIC)]


def label(key):
    return next((lb for k, lb, _ in _TERMS if k == key), key)


def outcome(text):
    low = (text or "").lower()
    for key, rx in _OUTCOME:
        if re.search(rx, low):
            m = _MODAL.search(low)
            return {"dir": key, "modal": (m.group(1).lower() if m else None)}
    return None


def _join(items):
    items = [i for i in items if i]
    return items[0] if len(items) == 1 else (", ".join(items[:-1]) + " and " + items[-1] if items else "")


_CHALLENGE = re.compile(r"\bhow do you know\b|\bhow did you (?:check|know|work)\b|\bwhat (?:did|do) you (?:check|look at|use|base)\b|\bshow me (?:the |your )?(?:evidence|records|scans|data|proof)\b|"
                        r"\bwhere (?:did|does) (?:that|this) come from\b|\bprove it\b|\bhow can you (?:be sure|say|tell)\b|\bare you sure\b|\bwhat is that based on\b", re.I)
_BETWEEN = re.compile(r"\bbetween (?:two |2 |the )?scans?\b|\bmiss(?:ed|ing)?\b[\w\s]{0,25}\bscans?\b|\bin the gap\b|\bscan (?:gap|interval|frequency)\b|\bhow often (?:do you|does it) scan\b", re.I)
_COMPARE = re.compile(r"\bcompare (?:both|the two|them|these|the)\b|\bfairly\b|\bwhich (?:one )?(?:matters|is (?:more )?important|explains)\b|\bboth (?:explanations|ideas|hypotheses)\b|\bseparate (?:the|them|these)\b", re.I)
_REVISE_NEW = re.compile(r"\bactually,? (?:maybe|perhaps)\b|\bmaybe it'?s\b", re.I)
_REVISE = re.compile(r"\bactually,? (?:maybe|perhaps)\b|\bmaybe it'?s \w+\b|\b(?:irrelevant|doesn'?t matter|does not matter|isn'?t important|not important|might not matter|may not matter)\b|\bmaybe (?:the |it'?s )?\w+ (?:isn'?t|is not|doesn'?t|does not)\b|\bmaybe it'?s (?:whether|about|more|the)\b|\bforget (?:the|that)\b|\binstead\b|\brather than\b|\bnot the \w+,? but\b", re.I)


_PROOF = re.compile(r"\b(?:reliable|real|actual|statistical) edge\b|\bis it reliable\b|\bdoes it (?:really )?work\b|\bproven\b|\bproves?\b|\bproof\b|\bguarantee\w*\b|\balways works?\b|\bhas an edge\b|\bgives? (?:an |me an )?edge\b|\b(?:two|2|three|3|\d+) (?:out of|of) (?:two|2|three|3|\d+)\b|\bnever fails?\b|\bconfirmed\b", re.I)
_LEAP = re.compile(r"\bso (?:it|the \w+|that) (?:caused|made|drove|triggered|led to)\b|\b(?:therefore|that means|which means|so that means)\b[\w\s,']{0,30}\b(?:caus\w+|because|drove|triggered)\b|\bso it caused\b|\bthat caused\b", re.I)
_CORRECT = re.compile(r"\b(?:i meant|i mean|sorry,? i meant|no,? (?:it'?s|its|i said)|not (?:bullish|bearish)|the other way)\b", re.I)
_TF = re.compile(r"\b(1 ?h|1[- ]hour|one hour|hourly|h1|4 ?h|4[- ]hour|h4|daily|weekly|5 ?m|5[- ]minute|1 ?m|1[- ]minute|15 ?m|15[- ]minute)\b", re.I)
_BACKTEST = re.compile(r"\bback ?test\b|\bcan you (?:run|do|test)\b|\brun (?:it|the test|a test)\b|\bsimulat\w+\b", re.I)
_SESSQ = re.compile(r"\bwhich session\b|\bwhat session\b|\bwere (?:those|they|these)\b[\w\s]{0,20}\b(?:london|asia\w*|new york|ny|overlap)\b|\bboth in (?:london|asia\w*|new york|ny)\b|\bin (?:london|asia\w*|new york|ny)\?", re.I)
_COMPARATIVE = re.compile(r"(.+?)\b(?:work|works|perform|performs|is|are)?\s*(?:better|stronger|more reliable|more accurate|more useful)\s+than\b(.+)", re.I)


_PROXIMITY = re.compile(r"\b(?:timestamps?|times?)\b[\w\s']{0,15}\b(?:close|near|similar|same)\b|\bclose (?:together|in time)\b|\bso close\b|\bonly (?:a few|\d+) minutes? (?:apart|between|later|earlier)\b", re.I)
_FALSIFY = re.compile(r"\bwhat would (?:disprove|falsify|make (?:it|this|that) wrong|prove (?:me|it) wrong|show (?:it|this|that|me) (?:is |i'?m )?wrong|tell (?:me|us) (?:i'?m|it'?s|this is) wrong)\b|"
                      r"\bhow (?:would|could|do) (?:we|i) (?:know|tell) (?:it'?s|if it'?s|i'?m) wrong\b|\bwhat (?:would|could) (?:change my mind|invalidate)\b", re.I)
_FREE_CLAIM = re.compile(r"\b(?:when|if|after|once|whenever)\b.{4,}?,.{4,}\b(?:usually|tends? to|often|will|keeps?|continues?|goes?|reverses?|works?|holds?|follows?)\b|"
                         r"\bmy (?:view|take|theory|idea|hypothesis)\b|\bi (?:think|believe|reckon|noticed|suspect)\b.{6,}\b(?:usually|tends? to|often|always|keeps?|continues?)\b", re.I)
NEARBY = (   # what the engine already saves that a plain-words idea might be pointing at. Names only; whether it IS the same thing is the user's call.
    (r"\b(?:compress\w*|tight range|squeeze|consolidat\w*|ranging|range)\b", "a 15-minute compression ratio"),
    (r"\b(?:big|large|huge|impulsive|spike|spiking)\b[\w\s]{0,10}\b(?:candle|bar|move)\b|\bspike\b", "a 15-minute spike state"),
    (r"\b(?:volatil\w*|expansion|expanding|atr)\b", "a 15-minute volatility label and an ATR bucket"),
    (r"\b(?:trend\w*|strong move)\b", "a trend-strength reading in ATR multiples"),
    (r"\b(?:pullback|retrace\w*|dip)\b", "a pullback-depth percentage"),
    (r"\b(?:asia\w*|london|new york|ny|session|overlap)\b", "a session label"),
)


def free_claim(text):
    """A claim in your own words that the engine's vocabulary doesn't cover: 'when X, Y' split into its two halves. -> dict or None."""
    t = (text or "").strip().rstrip(".?!")
    t = re.sub(r"^(?:my (?:view|take|theory|idea|hypothesis)\s*[:\-]?\s*|i (?:think|believe|reckon|suspect)\s+(?:that\s+)?)", "", t, flags=re.I)
    m = re.match(r"(?:when|if|after|once|whenever)\s+(.+?)\s*,\s*(?:then\s+)?(.+)$", t, re.I) or re.match(r"(?:when|if|after|once|whenever)\s+(.+?)\s+\bthen\b\s+(.+)$", t, re.I)
    if m:
        return {"when": m.group(1).strip(), "then": m.group(2).strip()}
    return {"whole": t[:240]} if t else None


def hints(text):
    out = []
    for rx, name in NEARBY:
        if re.search(rx, (text or "").lower()) and name not in out:
            out.append(name)
    return out


_RECALL = re.compile(r"\bremind me\b|\bwhat (?:was|is) my (?:original|first|initial|earlier|old) (?:idea|hypothesis|theory|view)\b|\brecap\b|\bsummar(?:y|ise|ize)\b|\bwhere (?:are|were) we\b|\bwhat have we (?:got|covered|said)\b|\bwhat did i (?:say|start with)\b", re.I)
_WHY_CHANGED = re.compile(r"\bwhat (?:made|led|caused) me (?:to )?(?:change|switch|drop|revise)\b|\bwhy did i (?:change|switch|drop|revise)\b|\bwhat (?:evidence|reason) (?:made|led|caused) (?:me|the change)\b|\bwhy (?:the|that) (?:change|switch)\b", re.I)


def classify(text, h):
    """-> one turn type: 'neg_command', 'contradiction', 'how_test', 'mechanism', 'limit_q', 'predict_q', 'stronger', 'add_condition', 'start', 'elaborate'."""
    low = (text or "").lower()
    has = h is not None
    if _NEG_CMD.search(low) and not has:
        return "neg_command"
    if has and _WHY_CHANGED.search(low):
        return "why_changed"
    if has and _RECALL.search(low):
        return "recall"
    if _FALSIFY.search(low):
        return "falsify"
    if has and _PROXIMITY.search(low):
        return "proximity"
    if has and _CHALLENGE.search(low) and re.search(r"\b(?:london|asia\w*|new york|ny|session|overlap)\b", low):
        return "session_challenge"
    if not has and _BACKTEST.search(low) and (ts_ := terms(low)):
        return "backtest"
    if has and _PROOF.search(low):
        return "proof_claim"
    if has and _LEAP.search(low):
        return "causal_leap"
    if has and _CORRECT.search(low) and re.search(r"\b(?:bullish|bearish|up|down|higher|lower)\b", low):
        return "correct_dir"
    if has and _SESSQ.search(low):
        return "fact_session"
    if has and _TF.search(low) and re.search(r"\binstead\b|\bwhat about\b|\bhow about\b|\bon the\b|\bswitch\b|\bwhat if (?:we|i|it)\b", low) and not _REVISE_NEW.search(low):
        return "timeframe"
    if has and _BACKTEST.search(low):
        return "backtest"
    if has and _CHALLENGE.search(low):
        return "challenge"
    if has and _BETWEEN.search(low):
        return "between"
    if has and _COMPARE.search(low):
        return "compare"
    if has and _REVISE.search(low):
        return "revise"
    if "collect_liq" in terms(low) and not _HOWTEST.search(low):
        return "undefined_term"
    if has and _CONTRA.search(low) and (terms(low) or re.search(r"\bcase\b|\btime\b|\bday\b|\bone\b", low)):
        return "contradiction"
    if _HOWTEST.search(low):
        return "how_test"
    ts = terms(low)
    if ts and any(_RECORD_KEY.get(t) == "displacement" for t in ts) and _MORE_LIKELY.search(low):
        return "limit_q"
    if _MECH.search(low) and (has or ts):
        return "mechanism"
    if has and _STRONGER.search(low) and not _ADD.search(low):
        return "stronger"
    if has and (_ADD.search(low) or ts) and not _PREDICT_Q.search(low):
        return "add_condition" if [t for t in ts if t not in (h.get("conds") or [])] else "elaborate"
    if _PREDICT_Q.search(low):
        return "predict_q"
    if not has:
        return "start"
    return "elaborate"


def start(text):
    ts = terms(text)
    cm = _COMPARATIVE.search(text or "")
    comp = None
    if cm and terms(cm.group(1)) and terms(cm.group(2)):
        comp = {"a": terms(cm.group(1))[-1], "b": terms(cm.group(2))[0]}
    return {"comparative": comp, "conds": ts, "outcome": outcome(text), "claim": _claim(text, ts), "turns": 1, "said": [], "types": ["start"], "asked": None,
            "log": [{"at": None, "type": "start", "text": (text or "")[:200]}], "free": (free_claim(text) if not ts and not _NEG_CMD.search(text or "") else None), "text": (text or "")[:300], "texts": [(text or "")[:300]], "vague": [], "mech": ((text or "")[:200] if _MECH.search(text or "") else None)}


def _last_pos(low, key):
    rx = next((r for k, _l, r in _TERMS if k == key), None)
    ms = list(re.finditer(rx, low)) if rx else []
    return ms[-1].start() if ms else -1


def _claim(text, ts):
    low = (text or "").lower()
    m = _CAUSAL.search(low)
    if not m or len(ts) < 2:
        return None
    pre = [(p, t) for t in ts for p in [_last_pos(low[:m.start()], t)] if p >= 0]
    if not pre:
        return None
    a = max(pre)[1]                                   # "the sweep caused it": the thing named just before the verb is the cause
    post = [t for t in terms(low[m.end():]) if t != a]
    b = post[0] if post else next((t for t in ts if t != a), None)
    return {"type": "cause", "a": a, "b": b} if b else None


def update(h, text, ttype, at=None):
    """-> the idea after this message. Conditions only ever grow by what you said; nothing is removed unless you say so."""
    h = dict(h or start(text))
    h["conds"] = list(h.get("conds") or [])
    ts = terms(text)
    if ttype in ("add_condition", "elaborate", "mechanism", "contradiction", "start"):
        for t in ts:
            if t not in h["conds"] and not (t in _SPECIFIC.values() and any(_SPECIFIC.get(c) == t for c in h["conds"])):
                if ttype in ("add_condition", "start"):
                    h["conds"].append(t)
    oc = outcome(text)
    if oc and ttype in ("start", "add_condition", "elaborate"):
        h["outcome"] = oc
    cl = _claim(text, ts)
    if cl:
        h["claim"] = cl
    if ttype == "correct_dir":
        low_t = (text or "").lower()
        want = "bearish" if re.search(r"\bbearish\b|\bdown\b|\blower\b", low_t) else "bullish"
        swap = {"choch_bull": "choch_bear", "choch_bear": "choch_bull"} if want == "bearish" else {"choch_bear": "choch_bull", "choch_bull": "choch_bull"}
        if want == "bearish":
            swap = {"choch_bull": "choch_bear", "choch": "choch_bear"}
        else:
            swap = {"choch_bear": "choch_bull", "choch": "choch_bull"}
        h["history"] = (list(h.get("history") or []) + [{"conds": list(h["conds"]), "outcome": h.get("outcome"), "text": (h.get("text") or "")[:200]}])[-4:]
        h["conds"] = [swap.get(c, c) for c in h["conds"]]
        if h.get("outcome"):
            h["outcome"] = {**h["outcome"], "dir": "down" if want == "bearish" else "up"}
        h["corrected_to"] = want
    if ttype == "mechanism":
        h["mech"] = (text or "")[:200]
    if ttype == "revise":
        h["history"] = (list(h.get("history") or []) + [{"conds": list(h["conds"]), "outcome": h.get("outcome"), "text": (h.get("text") or "")[:200]}])[-4:]
        have = {_RECORD_KEY.get(c, c) for c in h["conds"]}
        newv = [t for t in ts if t not in h["conds"] and _RECORD_KEY.get(t, t) not in have]
        low_t = (text or "").lower()
        dropped = [c for c in h["conds"] if re.search(r"\b" + {"sweep_low": "sweep", "sweep_high": "sweep", "sweep": "sweep"}.get(c, c.replace("_", " ")) + r"\b[\w\s']{0,12}\b(?:isn'?t|is not|doesn'?t|does not)\b", low_t)]
        h["rival"] = {"vars": newv or (h.get("rival") or {}).get("vars", []), "doubted": dropped or (h.get("rival") or {}).get("doubted", []), "text": (text or "")[:200]}
    if ttype == "contradiction" and re.search(r"\b(?:choch|change of character)\b[\w\s,]{0,25}\b(?:came|was|happened|printed) first\b|\bcame before (?:the )?sweep\b", (text or "").lower()):
        h["claim_contradicted"] = True
    h["texts"] = (list(h.get("texts") or []) + [(text or "")[:300]])[-6:]
    if ttype in ("add_condition", "revise", "correct_dir", "contradiction", "mechanism", "elaborate"):
        h["log"] = (list(h.get("log") or []) + [{"at": at, "type": ttype, "text": (text or "")[:200]}])[-12:]
    h["turns"] = int(h.get("turns") or 0) + 1
    h["types"] = (list(h.get("types") or []) + [ttype])[-8:]
    h["vague"] = sorted(set(h.get("vague") or []) | {m.group(0).lower() for m in _VAGUE.finditer(text or "")})
    return h


# ------------------------------------------------------------------ describing the idea back to you
def _setup_phrase(h):
    conds = h.get("conds") or []
    return _join([label(c) for c in conds]) if conds else "the setup you described"


def mirror(h):
    """The idea in one clause, in your own terms. A cause you claimed is said as a claim, not folded into the setup."""
    out = h.get("outcome")
    s = _setup_phrase(h)
    if h.get("free") and not out and not h.get("conds"):
        return _claim_text(h)
    if h.get("free") and h.get("conds"):
        return f"{_claim_text(h)}, now also considering {_setup_phrase(h)}"
    cp = h.get("comparative")
    if cp:
        return f"you're saying {label(cp['a'])} does better than {label(cp['b'])}, though 'better' isn't yet defined in anything measurable"
    cl = h.get("claim")
    if cl and cl.get("b"):
        return f"you're saying {label(cl['a'])} came before {label(cl['b'])} and caused it" if not out else f"you're saying {label(cl['a'])} caused {label(cl['b'])}, and that price then {_OUTCOME_LABEL[out['dir']]}"
    if out:
        mod = out.get("modal")
        hedge = {"might": "might", "may": "may", "could": "could", "probably": "will probably", "likely": "will likely", "maybe": "might", "perhaps": "might"}.get(mod or "", "will" if mod in ("will", "going to", "gonna") else "might")
        return f"after {s}, price {hedge} {('carry on up' if out['dir'] == 'up' else 'carry on down' if out['dir'] == 'down' else _OUTCOME_LABEL[out['dir']])}"
    return f"{s}, with no outcome stated yet"


def recorded_note(key):
    """What the engine saves for a term, from plainwords, or None for one with no entry (a higher-timeframe zone, a session level, a tier)."""
    k = _RECORD_KEY.get(key)
    return plainwords.recorded(k) if k else None


# ------------------------------------------------------------------ the evidence paragraph (observed only)
def _events_for(h, F):
    conds = h.get("conds") or []
    want = "BULLISH" if "choch_bull" in conds else "BEARISH" if "choch_bear" in conds else None
    evs = [e for e in (F or {}).get("events", []) if want is None or e["dir"] == want]
    return evs, want


NOSCANS = "I have no saved scans to count from, so I can't check any part of this against the records."


def observed(h, F, said):
    """Counts from the records for the parts of the idea that are recorded. Never an average, never a verdict. -> list of sentences."""
    out = []
    conds = h.get("conds") or []
    if not F or not F.get("scans"):
        return [] if "noscans" in said else [NOSCANS]
    span = f"{F['days']} day(s) and {F['scans']} scans" if F.get("days") else f"{F['scans']} scans"
    if not any(_RECORD_KEY.get(c) in ("choch", "sweep") for c in conds):
        return out
    evs, want = _events_for(h, F)
    what = {"BULLISH": "bullish ", "BEARISH": "bearish "}.get(want, "")
    n = len(evs)
    if n == 0:
        out.append(f"In my records ({span}) the engine flagged no {what}15-minute change of character, so there is nothing to look at yet.")
        return out
    when = F.get("when") or (lambda d: str(d))
    out.append(f"In my records ({span}) the engine's scans first saw {n} {what}15-minute change{'s' if n != 1 else ''} of character"
               + (f", at {_join([when(e['at']) for e in evs[:5]])}" if n <= 5 else "")
               + " (the time a scan noticed each, usually a minute or two after the candle closed, not the moment it formed).")
    if F.get("unreadable") or F.get("conflicts"):
        out.append(f"Some saved scans couldn't be used ({len(F.get('unreadable') or [])} unreadable, {len(F.get('conflicts') or [])} conflicting), so these counts may be missing events.")
    sweep_cond = [c for c in conds if _RECORD_KEY.get(c) == "sweep"]
    if sweep_cond:
        chk = [e for e in evs if e["checkable"]]
        if not chk:
            out.append("For none of them could I check a sweep: the engine only runs its sweep check in the direction of its bias, and at those times the bias pointed the other way.")
        else:
            b = sum(1 for e in chk if e["sweep_before"]); a = sum(1 for e in chk if e["sweep_after"] and not e["sweep_before"])
            nb = len(chk) - b - a
            out.append(f"The sweep check was possible for {len(chk)} of them. It had come up true in the hour before in {b}, only in the hour after in {a}, and not at all in {nb}."
                       + (f" For the other {n - len(chk)} the check wasn't running in that direction." if n - len(chk) else ""))
    zones = [c for c in conds if _RECORD_KEY.get(c) in ("fvg", "ob")]
    if zones:
        zk = "in_fvg" if _RECORD_KEY.get(zones[0]) == "fvg" else "in_ob"
        k = sum(1 for e in evs if e[zk])
        out.append(f"Price was inside a 15-minute {'fair value gap' if zk == 'in_fvg' else 'order block'} on the scan that first saw {k} of them.")
    return out


def forward(h, F):
    """The 'what happened next' description, labelled as description. Only when an outcome was stated."""
    if not h.get("outcome"):
        return None
    evs, _w = _events_for(h, F or {})
    ok = [e for e in evs if e.get("fwd")]
    if not ok:
        return None
    parts = [f"{(F.get('when') or str)(e['at'])}: {e['fwd']['pips_1h']:+.1f} pips after an hour (best {e['fwd']['best']:+.1f}, worst {e['fwd']['worst']:+.1f})" for e in ok[:5]]
    return ("For what it's worth, here is what price did in the hour after each, measured in the direction of that change of character: " + "; ".join(parts) +
            f". That is a description of {len(ok)} case(s), not evidence of anything.")


# ------------------------------------------------------------------ method notes (about the idea's wording and testability, never the market)
def method_notes(h, text, said):
    notes = []
    asked = set(h.get("asked_vague") or [])
    cands = [m.group(0).lower() for m in _VAGUE.finditer(text or "")] + [v for v in (h.get("vague") or [])]
    vague = []
    for v in cands:
        if v not in asked and v not in vague and v not in ("usually", "often", "mostly"):
            vague.append(v)
    if vague:
        v = vague[0]
        asked.add(v); h["asked_vague"] = sorted(asked)
        notes.append(("vague", f"'{v}' is doing a lot of work in that sentence, and I can't measure it until it's defined. What would count as {v}, in something I could check in the records or the candles?"))
        return notes
    if h.get("outcome") and "outcome" not in said:
        notes.append(("outcome", "'" + _OUTCOME_LABEL[h["outcome"]["dir"]] + "' needs a yardstick before it can be checked: how far, in how long, and measured from when?"))
    if ((h.get("conds") or h.get("free")) and not _EXIT.search(h.get("text", "") + " " + (text or ""))) and "exit" not in said:
        notes.append(("exit", "What would tell you the idea is wrong? Without that, any result can be explained away."))
    if _HINDSIGHT.search(text or "") and "hindsight" not in said:
        notes.append(("hindsight", "Careful with wording like that: 'the real low' or 'the top' is only knowable afterwards, so a rule built on it can't be used live."))
    return notes


_SAMPLE = ("Adding conditions makes the group smaller, and small groups throw up striking-looking results by chance, so every condition has to earn its place "
           "against the same setup without it.")


def _record_gap(h, said):
    """Which parts of the idea the engine does NOT save. -> sentence or None."""
    miss = []
    for c in h.get("conds") or []:
        if c == "htf_zone":
            miss.append("a higher-timeframe zone (the engine saves 15-minute order blocks and fair value gaps, not zones drawn on the 1-hour or above)")
        elif c == "displacement":
            miss.append("displacement (the engine doesn't measure or save it)")
        elif c in ("session", "tier", "signal") and False:
            pass
        elif _RECORD_KEY.get(c) is None and c not in ("tier", "signal", "session"):
            pass
    if "price entering a zone" in " ".join(h.get("said") or []):
        pass
    return ("Not recorded by the engine: " + _join(miss) + ".") if miss else None


# ------------------------------------------------------------------ the replies
def review(h, ttype, text, F, said, uid=None):
    """-> (reply text, ask-key or None). `said` is what earlier replies in this conversation already covered, so nothing is repeated."""
    said = set(said or [])
    fn = {"start": _r_start, "neg_command": _r_start, "add_condition": _r_add, "elaborate": _r_elab, "stronger": _r_stronger, "how_test": _r_howtest,
          "mechanism": _r_mech, "contradiction": _r_contra, "limit_q": _r_limit, "predict_q": _r_predict,
          "proof_claim": _r_proof, "causal_leap": _r_leap, "correct_dir": _r_correct, "fact_session": _r_session, "timeframe": _r_timeframe, "backtest": _r_backtest, "recall": _r_recall, "why_changed": _r_why_changed, "falsify": _r_falsify, "proximity": _r_proximity, "session_challenge": _r_session_challenge, "challenge": _r_challenge, "between": _r_between, "compare": _r_compare, "revise": _r_revise, "undefined_term": _r_undefined}.get(ttype, _r_elab)
    body, ask, new = fn(h, text, F, said, ttype)
    sig = hashlib.md5(body.encode()).hexdigest()[:12]
    if sig == h.get("last_sig"):                     # never the same reply twice running: say what hasn't changed, briefly
        body = (f"That leaves it where it was: {mirror(h)}. Nothing new to add from the records on that. "
                "What would you change, or which part do you want to pin down next?")
        ask = "next"
    h["last_sig"] = hashlib.md5(body.encode()).hexdigest()[:12] if body.startswith("That leaves it") else sig
    if NOSCANS in body:
        new = list(new) + ["noscans"]
    h["said"] = sorted(said | set(new))
    return body, ask


def _tail(h, text, said):
    """One relevant question, the first that hasn't been asked."""
    for key, q in method_notes(h, text, said):
        return key, q
    return None, None


def _r_start(h, text, F, said, ttype):
    p = []
    lead = ""
    if ttype == "neg_command":
        lead = "Understood: no alert, no trade, nothing saved. I'll treat this as thinking out loud.\n\n"
    if h.get("free") and not h.get("conds"):
        fc = h["free"]
        claim = f"when {fc['when']}, then {fc['then']}" if fc.get("when") else fc.get("whole", "")
        hs = hints(text)
        p = [f"{lead}Here's how I'm reading you: {claim}."]
        p.append("That isn't in terms the engine records, so I can't count how often it has happened in my records. "
                 + (f"The nearest things it does save are {_join(hs)}, each scan; whether any of those is what you mean is for you to say, I won't assume it. " if hs else "")
                 + "What it does keep is every candle, so once the words are turned into numbers it can be checked afterwards.")
        k, q = _tail(h, text, said)
        if q:
            p.append(q)
        return "\n\n".join(p), k, ["start", "free"]
    if not h.get("conds") and not h.get("outcome"):
        track = re.search(r"\btrack\b|\bevidence\b|\bstudy\b", (text or "").lower())
        body = (lead + "I haven't got the setup itself yet, and 'this setup' could be any of several things. Tell me its conditions in the engine's terms (a sweep, a change of character, a fair value gap, an order block, the 1-hour bias) "
                "and what you expect to happen after.")
        if track:
            body += ("\n\nOn tracking the evidence: I don't run a study by myself in the background. What I can do is count the setup in the saved records once it's defined, "
                     "keep your definition word for word as a note if you ask, and set an alert on the things the engine does record. The step-by-step research notebook isn't connected to chat yet.")
        return body, "setup_def", ["start", "track"]
    op = ("Let me check I've got it:", "Here's how I'm reading you:", "So, as I understand it:")[len(text or "") % 3]
    p.append(f"{lead}{op} {mirror(h)}.")
    # a cause claim is a different animal from a sequence
    cl = h.get("claim")
    if cl:
        p.append(f"You've also said {label(cl['a'])} {'caused' if True else ''} {label(cl['b'])}. My records can show which came first and how often. They can't show a cause: that would need a comparison with cases where "
                 f"{label(cl['a'])} didn't happen.")
    obs = observed(h, F, said)
    new = []
    if obs:
        p.append(" ".join(obs)); new.append("observed")
    gap = _record_gap(h, said)
    if gap:
        p.append(gap); new.append("gap")
    if not obs and not gap:
        notes = [recorded_note(c) for c in h.get("conds", []) if recorded_note(c)]
        if notes:
            p.append("What the engine saves about that: " + notes[0]); new.append("recorded")
    if re.search(r"\b(?:what happened|what did (?:price|it) do|afterwards?|after (?:each|them|it))\b", (text or "").lower()):
        fw = forward(h, F)
        if fw:
            p.append(fw); new.append("forward")
    if ttype == "neg_command" and re.search(r"\btrack\b|\bkeeps? happening\b|\bevidence\b", (text or "").lower()):
        p.append("On tracking whether it keeps happening: I don't watch in the background and keep a tally by myself. What I can do is recount from the saved records whenever you ask, "
                 "and if you later want a nudge each time one forms, say so and I'll set that alert. Nothing is set now.")
        new.append("track")
    k, q = _tail(h, text, said)
    if q:
        p.append(q); new.append(k)
    return "\n\n".join(p), k, new + ["start"]


def _r_add(h, text, F, said, ttype):
    newc = [c for c in (h.get("conds") or [])[-2:] if c in terms(text)]
    c = newc[-1] if newc else (h.get("conds") or [None])[-1]
    p = [f"So the idea now reads: {mirror(h)}."]
    new = []
    if c == "htf_zone":
        p.append("A higher-timeframe zone isn't something the engine saves. It saves 15-minute order blocks and fair value gaps, plus a yes/no each scan for whether price is inside one. "
                 "So either we use those as the 'zone', or you define the higher-timeframe zone yourself (for example, the range of a 1-hour candle you mark) and it gets recorded by hand for each case.")
        new.append("gap")
    else:
        rn = recorded_note(c)
        if rn:
            p.append(rn); new.append("recorded")
    obs = observed(h, F, said)
    if obs and c != "htf_zone" and "observed" not in said:
        p.append(" ".join(obs)); new.append("observed")
    prev = [x for x in (h.get("conds") or []) if x != c]
    if prev:
        base = _join([label(x) for x in prev])
        p.append(f"The way to find out whether {label(c)} adds anything is to compare two groups that are the same except for that one condition: cases with {base} {('inside ' if c in ('htf_zone', 'fvg', 'ob') else 'with ')}{label(c)}, "
                 f"and cases with {base} but without it. If the first group does no better than the second, the condition added nothing. " + (_SAMPLE if "sample" not in said else "The same small-group caution applies."))
        new.append("sample")
    k, q = _tail(h, text, said)
    if q:
        p.append(q); new.append(k)
    return "\n\n".join(x for x in p if x), k, new


def _r_stronger(h, text, F, said, ttype):
    if "stronger" in said:
        return ("Same three boxes as before, and for your idea only the first has anything in it so far. "
                "Which do you want to work on: how it would be tested, or how the wording could be made precise enough to count?"), "stronger_which", ["stronger"]
    p = ["'Stronger' can mean three different things, and I'd keep them apart:\n"
         "• seen in the records: it happened, and the count is there to look at\n"
         "• consistent with your reasoning: nothing recorded contradicts it, which is not evidence for it\n"
         "• shown to work: a fair test against the same setup without the new condition, over enough separate cases, comes out better"]
    obs = observed(h, F, said) if "observed" not in said else []
    p.append("A reason for why it should matter belongs in the second box at best, not the third. Today I can only help with the first" + (": " + " ".join(obs) if obs else ", and the counts above are all there is to look at") + ". The third needs a test we haven't designed yet.")
    p.append("Which of the three did you mean? If it's the third, say 'how would we test it' and I'll lay out the comparison.")
    return "\n\n".join(p), "stronger_which", ["stronger"]


def _r_howtest(h, text, F, said, ttype):
    conds = h.get("conds") or []
    recording = bool(re.search(r"\brecord|track|log|store|save|collect|capture", (text or "").lower()))
    steps = []
    n = 1
    def add(s):
        nonlocal n
        steps.append(f"{n}. {s}"); n += 1
    setup = _join([label(c) for c in conds]) or "the setup"
    add(f"Define the event exactly, in the engine's own terms: what counts as {setup}, and how close in time the parts have to be (for example, within an hour of each other).")
    if h.get("outcome"):
        add(f"Fix the outcome before you look: '{_OUTCOME_LABEL[h['outcome']['dir']]}' becomes a rule, like 'reaches N pips in that direction before N pips against, within H hours'. Choose N and H now, not after seeing the cases.")
    else:
        add("Fix the outcome before you look: how far, how soon, and in which direction counts as the idea working.")
    extra = [c for c in conds[2:]] or ([conds[-1]] if len(conds) > 1 else [])
    if h.get("comparative"):
        add(f"Choose the comparison: the same moments for both, so every time price met {label(h['comparative']['a'])} or {label(h['comparative']['b'])} is listed once, and each is scored by the same outcome rule. "
            "Cases where price met both at once are reported separately, not credited to either.")
    elif extra:
        add(f"Choose the baseline: the same setup without {label(extra[-1])}. That comparison is the only thing that can show the condition adds something.")
    else:
        add("Choose the baseline: what the outcome rule does after any ordinary moment, so you know whether the setup beats doing nothing special.")
    add("Count separate episodes, not scans: one move spread over six scans is one case. A difference between two small groups can easily be chance.")
    add("Write all of that down before looking at results, and look once. Peeking and adjusting is how a pattern gets found that was never there.")
    have, lack = [], []
    for c in conds:
        rn = recorded_note(c)
        if c in ("htf_zone",):
            lack.append(f"{label(c)} (not saved; you'd record each one by hand)")
        elif c == "displacement":
            lack.append("displacement (not saved; it would have to be computed from the candles with a definition you choose)")
        elif rn and not rn.startswith("No."):
            have.append(label(c))
        elif rn:
            lack.append(label(c))
    if recording and any(_RECORD_KEY.get(c) in ("fvg", "ob", "retracement", "retrace") for c in conds):
        lack.append("the moment price enters a zone (the engine saves a yes/no each scan, so entry is only known to the scan)")
    if h.get("outcome") or recording:
        have.append("the 5-minute, 15-minute and 1-hour candles (so what price did afterwards can be measured later)")
    lines = ["Here's how I'd set it up so it can be tested rather than just argued about:"] + steps
    if have:
        lines.append("What the engine already records for this: " + _join(have) + ".")
    if lack:
        lines.append("What it doesn't: " + _join(lack) + ".")
    if recording:
        lines.append("So per case, we'd want to write down: the time the setup was first seen, the zone's edges, the direction of the 1-hour bias and the engine's phase at that moment, "
                     "where price went over the next fixed horizons, and the point at which you'd call it wrong. Most of that the engine already has; the zone entry and the outcome are the parts we'd add.")
    lines.append("The research notebook in this system is built for exactly this order (hypothesis, what stays fixed, freeze, then compare), but it isn't wired into chat yet, so for now I can only help you word it.")
    return "\n\n".join(lines[:1]) + "\n" + "\n".join(lines[1:1 + len(steps)]) + "\n\n" + "\n\n".join(lines[1 + len(steps):]), "test_defined", ["howtest"]


def _r_mech(h, text, F, said, ttype):
    mm = re.search(r"because (.+?)[.?!]*$", (text or "").strip(), re.I)
    why = mm.group(1) if mm else "there is a reason behind it"
    p = [f"That's a mechanism, not just a sequence: you're saying it happens because {why}. It's worth working out what that would imply, because an explanation that implies nothing checkable can't be tested."]
    p.append("If that's the reason, three things should follow. The move should take out a level that holds obvious orders (equal highs or lows, a session extreme), and the engine doesn't save which level a sweep took, "
             "so I can't check that today. It should precede reversals more often than ordinary pokes through a level do, which needs the sweeps that were NOT followed by anything as the comparison. "
             "And it should show up regardless of how the setup is defined, not just with one particular definition.")
    p.append("Other explanations produce the same sequence too: ordinary volatility poking past a recent extreme and coming back, or the structure break being detected only because price had just moved the other way. "
             "The records show the order of events, not the reason for it.")
    obs = observed(h, F, said)
    if obs and "observed" not in said:
        p.append(" ".join(obs))
    p.append("Which of those three consequences would you most want to check first?")
    return "\n\n".join(p), "mech_which", ["mech"]


def _short(key):
    lb = label(key) if key else "first event"
    return re.sub(r"^(?:a|an|the) ", "", lb)


def _r_contra(h, text, F, said, ttype):
    cl = h.get("claim") or {}
    p = ["Thanks, that changes the picture, and I don't want to defend my earlier answer if it doesn't hold."]
    if cl.get("type") == "cause" or h.get("claim_contradicted"):
        ea, eb = _short(cl.get("a")), _short(cl.get("b"))
        p.append(f"One case with the order reversed doesn't cancel the others, but it does mean '{ea} came first' can't be the whole reason to say the {ea} caused the {eb}. "
                 "Even a perfectly consistent order wouldn't have shown a cause; now it isn't even consistent.")
    else:
        p.append("One case that goes the other way doesn't settle it either direction, but it means the idea needs to say when it applies.")
    evs, want = _events_for(h, F or {})
    chk = [e for e in evs if e.get("checkable")]
    if F and F.get("scans") and chk:
        b = sum(1 for e in chk if e["sweep_before"]); a = sum(1 for e in chk if e["sweep_after"] and not e["sweep_before"]); z = len(chk) - a - b
        p.append(f"In my records, of {len(chk)} change(s) of character where the sweep check was running, it had already come up true before in {b}, came up only after in {a}, and never came up in {z}. "
                 "So 'sweep first' isn't what the records show every time either.")
    p.append("Before trusting either order, check that both of us mean the same events at the same time. I only know when my scan FIRST saw each one, so two events a few minutes apart can look reversed; "
             "and my sweep check is judged in the direction of the bias and stays on across several scans, which may not match the sweep you're picturing.")
    p.append("Which case was it? Give me the day and roughly the time and I'll show what my scans saw around it.")
    return "\n\n".join(p), "which_case", ["contra"]


def _r_limit(h, text, F, said, ttype):
    key = "displacement"
    rec = plainwords.recorded("displacement") or ""
    rec = rec[4:].strip() if rec.startswith("No. ") else rec
    p = [f"I can't answer that from my records: {rec[:1].lower() + rec[1:]}"]
    p.append("That doesn't have to be the end of it. Displacement can be given a measurable definition, for example a candle whose body is at least some multiple of the normal range, "
             "or a run of candles in one direction with almost no overlap. It would be YOUR definition, not the engine's, and the choice changes the answer, so we'd fix it before looking.")
    p.append("'More likely to continue' also needs a comparison: how often price carried on after displacement versus after similar moments without it, over enough separate episodes. "
             "The bot keeps candles, so that could be computed after the fact once the definition and the yardstick for 'continue' are fixed.")
    p.append("Which definition would you use: a size threshold, or no-overlap candles?")
    return "\n\n".join(p), "def_disp", ["limit"]


def _r_predict(h, text, F, said, ttype):
    if "predict" in said:
        return ("Still no forecast from me, however it's put: the records can't give one and I won't make one up. "
                "What I can do is help you pin down what 'it holds' or 'it goes up' means in numbers, so it could be checked on the saved candles. Where would you draw the line: how far past the level still counts?"), "def_hold", ["predict"]
    ts = terms(text)
    what = _join([label(t) for t in ts]) or "that"
    p = [f"I can't tell you whether it will happen: I don't forecast, and nothing the engine records is a forecast."]
    rn = [recorded_note(t) for t in ts if recorded_note(t)]
    if rn:
        p.append("What I can say is what is recorded: " + rn[0])
    p.append(f"'Hold' or 'work' also needs a definition before anything could be said about it: how far price may go through {what} and still count as holding, and over what time. "
             "The engine saves each zone and whether price is inside it each scan, but not how price reacted, so I can't count past reactions yet; that would have to be measured from the candles.")
    p.append("If you tell me how you'd define it, I'll help you set up a way to check it on the saved candles.")
    return "\n\n".join(p), "def_hold", ["predict"]


def _r_elab(h, text, F, said, ttype):
    p = [f"Got it: {mirror(h)}."]
    new = []
    obs = observed(h, F, said)
    if obs and "observed" not in said:
        p.append(" ".join(obs)); new.append("observed")
    k, q = _tail(h, text, said)
    if q:
        p.append(q); new.append(k)
    else:
        p.append("Where do you want to take it next: how to test it, what it would need recorded, or what else could produce the same picture?")
    return "\n\n".join(p), k, new


# ------------------------------------------------------------------ is this message an idea, or a question about the records?
_OPINION = re.compile(r"\bi (?:think|believe|reckon|feel|suspect|wonder|noticed|figure)\b|\bi'?m thinking\b|\bi was thinking\b|\bmy (?:idea|theory|hypothesis|plan)\b|\bwhat if\b|"
                      r"\bmight\b|\bcould (?:it|price|this|that)\b|\bmore likely\b|\bbecause\b|\bwould (?:that|this|it)\b|\bso (?:i think|the)\b|\bidea\b|\btheory\b|\bhypothesis\b|"
                      r"\bwatching (?:for|the)\b|\bhow would we\b|\bwhat would we need\b|\bto study\b|\bcame first\b|\bi found a case\b", re.I)
_RECORDS_Q = re.compile(r"^\s*(?:when|what time|what was|what is|what's|how many|how long|list|show|tell me when)\b|\blast (?:choch|bos|sweep|fvg|order block)\b|\bwhat happened\b", re.I)
_HARD_KINDS = {"command", "smalltalk", "integrity", "act_offer", "act_missing", "need_time", "zone_state", "details", "prev_event", "market_recon", "convo_recon", "compare_prev", "explain_prev"}


def is_idea(text, kind, h=None):
    """Should this message go to the idea discussion? Only a message that voices a thought of yours does; a question about the records, a command or an
    explicit alert request stays where it was. A negated alert ("I don't want an alert yet") is the one command-looking message that is conversation."""
    low = (text or "").lower()
    if h is not None and (_WHY_CHANGED.search(low) or _RECALL.search(low)) and kind in ("need_time", "market_recon", "convo_recon", "unknown", "ask_bus", "followup", "offer_save"):
        return True                                           # "why did I change my mind" is about this conversation, not about a stretch of market
    if kind in _HARD_KINDS:
        return False
    ts = terms(low)
    neg = bool(_NEG_CMD.search(low))
    if neg and (_OPINION.search(low) or re.search(r"\btrack\b|\bstudy\b|\bunderstand\b", low)) and kind not in ("integrity",):
        return True
    if kind in ("alert_unsupported",) or kind == "act_offer":
        return neg and bool(ts)
    if re.match(r"\s*/", text or ""):
        return False
    if _RECORDS_Q.search(low) and not _OPINION.search(low):
        return False
    if h is not None:
        if kind in ("followup", "unknown", "offer_save", "ask_bus", "last_event", "multi", "explain", "cannot_say", "capability"):
            return bool(_OPINION.search(low) or ts or _STRONGER.search(low) or _HOWTEST.search(low) or _CONTRA.search(low) or outcome(low)
                        or _RECALL.search(low) or _WHY_CHANGED.search(low) or _FALSIFY.search(low) or _PROXIMITY.search(low) or _FREE_CLAIM.search(low) or _CHALLENGE.search(low) or _BETWEEN.search(low) or _COMPARE.search(low) or _REVISE.search(low)
                        or _PROOF.search(low) or _LEAP.search(low) or _CORRECT.search(low) or _TF.search(low) or _BACKTEST.search(low) or _SESSQ.search(low))
        return False
    if not (ts or outcome(low)):
        if _FREE_CLAIM.search(low) and not _RECORDS_Q.search(low) and not _PREDICT_Q.search(low):
            return True
        return False
    if "collect_liq" in ts:
        return True
    return bool(_OPINION.search(low) or neg or _MORE_LIKELY.search(low) or _BACKTEST.search(low) or _FREE_CLAIM.search(low))


# ------------------------------------------------------------------ the replies added after the independent review (25 Oct)
VOL_NOTE = ("the engine saves a 15-minute volatility label with each scan (in the records I have it says 'expanding' or 'flat'). That is the engine's own label, "
            "not a definition of expansion that you have chosen")


def _evs(h, F):
    evs, want = _events_for(h, F or {})
    return evs, want


def _r_challenge(h, text, F, said, ttype):
    evs, want = _evs(h, F)
    chk = [e for e in evs if e.get("checkable")]
    if not F or not F.get("scans") or not chk:
        return ("I don't have a counted result to defend here: either there are no saved scans, or the sweep check wasn't running for the events in question. "
                "If you give me a time, I'll show you the scans around it."), "which_case", ["challenge"]
    when = F.get("when") or str
    lines = []
    for e in chk[:6]:
        lines.append(f"• {when(e['at'])}: {e.get('scans_before', 0)} scans in the hour before and {e.get('scans_after', 0)} in the hour after; "
                     f"the sweep flag was {'true on some' if e['sweep_before'] or e['sweep_after'] else 'false on every one'}; the longest stretch with no scan was {e.get('max_hole_min', 0):g} minutes.")
    p = ["Here's what I actually looked at. For each event I read the saved scans from an hour before to an hour after, and the engine's own yes/no sweep flag on each one:\n" + "\n".join(lines)]
    p.append("So 'no sweep' means the engine's flag never came up on those scans, not that no sweep happened. Two limits: I only looked an hour either side, so a sweep earlier than that isn't counted; "
             "and the flag is judged in the direction of the bias, which is why it could only be checked for some events.")
    holes = [e for e in chk if e.get("max_hole_min", 0) >= 15]
    if holes:
        p.append(f"One thing weakens it: around {when(holes[0]['at'])} there was a {holes[0]['max_hole_min']:g}-minute stretch with no scan, long enough for a short sweep to fall inside it unseen.")
    p.append("If you think I've matched the wrong event, tell me which time you mean and I'll show the scans around it.")
    return "\n\n".join(p).replace("\\n", "\n"), "which_case", ["challenge", "observed"]


def _dur(m):
    return f"{m:g} minutes" if m < 120 else f"about {m / 60:.0f} hours (scans weren't running then, and I can't tell you why)"


def _r_between(h, text, F, said, ttype):
    if not F or not F.get("scans"):
        return "I have no saved scans to say anything about scan spacing.", None, ["between"]
    evs, _w = _evs(h, F)
    chk = [e for e in evs if e.get("checkable")]
    p = [f"It could have, and the records can't rule it out. Scans are usually {F.get('scan_median_min'):g} minutes apart; the longest gap anywhere in what I have is {_dur(F.get('scan_max_gap_min'))}"
         + (f", and around the events I checked the longest hole was {max(e.get('max_hole_min', 0) for e in chk):g} minutes." if chk else ".")]
    p.append("Whether a sweep that started and ended between two scans would still show depends on how far back the engine's sweep check looks at each scan. I can't see that from the saved records, so I won't claim it would or wouldn't.")
    p.append("There is a way to close the hole without relying on scans: the 15-minute candles are saved, so a sweep can be defined from them yourself (a candle whose wick goes past the previous swing and closes back inside), "
             "then counted for the same events. That would be your definition checked on the candles, independent of the engine's flag. Want to define it that way?")
    return "\n\n".join(p).replace("\\n", "\n"), "def_sweep", ["between"]


def _vol_line(F, evs):
    labs = [(e.get("vol") or "no reading") for e in evs]
    return labs


def _r_revise(h, text, F, said, ttype):
    r = h.get("rival") or {}
    hist = (h.get("history") or [])[-1:] or [{"conds": h.get("conds")}]
    old = _join([label(c) for c in hist[0].get("conds") or []]) or "your first setup"
    newv = _join([label(c) for c in r.get("vars", [])]) or "a different variable"
    p = [f"That's a different idea rather than an adjustment, so I'll keep both and not overwrite the first. Earlier: {old}. Now: a change of character that happens during {newv}."
         + (f" You've also said the {_join([label(c)[2:] if label(c).startswith('a ') else label(c) for c in r.get('doubted', [])])} may not matter; two events can't show that either way." if r.get("doubted") else "")]
    p.append("Nothing I said about the first idea carries over to the new one: it hasn't been looked at.")
    evs, want = _evs(h, F)
    if "vol_exp" in r.get("vars", []):
        if evs:
            labs = _vol_line(F, evs)
            p.append(f"It is partly checkable, because {VOL_NOTE}. At the {len(evs)} {'bullish ' if want == 'BULLISH' else ''}change(s) of character in my records it read: " + _join(labs) + ".")
        else:
            p.append(f"It is partly checkable, because {VOL_NOTE}.")
        p.append("Do you mean the engine's label, or something you'd define yourself (for instance, the last few candles being larger than the recent average)?")
        return "\n\n".join(p).replace("\\n", "\n"), "vol_def", ["revise"]
    if "displacement" in r.get("vars", []):
        p.append("The engine doesn't record displacement, so there's nothing in my records to line up with the events. It can still be defined from the candles, for example a candle body at least some multiple of the recent average, "
                 "or a run of candles in one direction with almost no overlap. The choice changes the answer, so it would be fixed before looking.")
        p.append("Which definition would you use: a size threshold, or no-overlap candles?")
        return "\n\n".join(p), "def_disp", ["revise"]
    p.append("What would you like to count as the new variable, in something that can be read from the candles or the engine's saved readings?")
    return "\n\n".join(p), "var_def", ["revise"]


def _r_compare(h, text, F, said, ttype):
    cp = h.get("comparative")
    if cp:
        ka, kb = _RECORD_KEY.get(cp["a"]), _RECORD_KEY.get(cp["b"])
        fa, fb = {"fvg": "in_fvg", "ob": "in_ob"}.get(ka), {"fvg": "in_fvg", "ob": "in_ob"}.get(kb)
        p = [f"To compare {label(cp['a'])} with {label(cp['b'])} fairly they need the same moments and the same yardstick: define 'better' first (for example, price reacts by N pips within H hours of touching the zone), "
             "fix it before looking, list every time price met each kind of zone, and compare how often the rule was met. Counting only the cases you remember would favour whichever you remember better."]
        evs = (F or {}).get("events") or []
        if fa and fb and evs:
            na, nb = sum(1 for e in evs if e.get(fa)), sum(1 for e in evs if e.get(fb))
            p.append(f"What the records can say today is thin: at the scan that first saw each of the {len(evs)} changes of character, price was inside an order block on {sum(1 for e in evs if e.get('in_ob'))} and inside a fair value gap on {sum(1 for e in evs if e.get('in_fvg'))}. "
                     "That is about where price was, not how it reacted. The engine saves each zone and whether price is inside one on each scan, but not how price reacted, so reactions would have to be measured from the candles.")
        p.append("Which would you use for 'better': how far price moves away, or whether the zone's far edge holds?")
        return "\n\n".join(p), "def_better", ["compare"]
    r = h.get("rival") or {}
    a = [c for c in (h.get("conds") or []) if _RECORD_KEY.get(c) == "sweep"]
    b = r.get("vars") or []
    na = label(a[0]) if a else "the first condition"
    nb = label(b[0]) if b else "the second condition"
    p = [f"Fairly means giving both ideas the same events and the same yardstick. Take every change of character in the records, fix the outcome rule before looking (how far, how soon, from when), then sort each event into four boxes: "
         f"{na} yes or no, crossed with {nb} yes or no. Each idea is judged by whether the outcome differs between its own yes and no while the other is held fixed. Only then does it become possible to say which variable, if either, explains anything."]
    evs, want = _evs(h, F)
    if evs and F and F.get("scans"):
        sw = [e for e in evs if e.get("checkable")]
        labs = [(e.get("vol") or "no reading") for e in evs]
        exp = sum(1 for x in labs if x == "expanding")
        p.append(f"What the records can fill in today: {len(evs)} {'bullish ' if want == 'BULLISH' else ''}events. For the sweep the check was possible for {len(sw)} and came up true for none, so that variable never varies and can't be tested at all from these records. "
                 f"The engine's volatility label read 'expanding' for {exp} and something else for {len(evs) - exp}. That puts at most one event in any box, and one case per box can't separate anything.")
        p.append("I haven't looked at what price did afterwards for this comparison, on purpose: outcomes stay unseen until both definitions and the outcome rule are fixed, otherwise the rule gets shaped by the answer.")
    p.append("To make it a real test it needs more events and written definitions of both variables. Shall we write those down first?")
    return "\n\n".join(p).replace("\\n", "\n"), "def_both", ["compare"]


def _r_undefined(h, text, F, said, ttype):
    p = ["I'm not going to say whether price was collecting liquidity: the engine has no such reading, and a plausible story is not an observation. It can still be made testable."]
    p.append("The question is what you could see that separates 'collecting liquidity' from ordinary movement. For example: price pushing through an obvious prior high or low (equal highs or lows, a session extreme) and closing back inside; "
             "or several touches of the same level before it breaks. The engine's closest reading is its sweep check against the current swing high and low, which doesn't say which level or why.")
    p.append("Ordinary movement would show the same push without the reversal, or the same push with no obvious level under it. If both look identical on the chart, the idea can't be told apart from noise.")
    if re.search(r"\bbecause\b|\bcaus", (text or "").lower()):
        p.append("Other explanations produce the same sequence too: ordinary volatility poking past a recent extreme and coming back, or the structure break only being detected because price had just moved the other way. "
                 "The records show the order of events, not the reason for it.")
    p.append("Which signs would you point at?")
    return "\n\n".join(p).replace("\\n", "\n"), "obs_signs", ["undefined"]


def _r_proof(h, text, F, said, ttype):
    evs, want = _evs(h, F)
    p = ["No: that isn't established, and a small number of matching cases couldn't establish it, however neatly they line up. Matches like that are what chance produces all the time, and they say nothing about the times it would have failed."]
    sw = [c for c in (h.get("conds") or []) if _RECORD_KEY.get(c) == "sweep"]
    if sw and evs and F and re.search(r"\b(?:two|2|\d+) (?:out of|of)\b", (text or "").lower()):
        chk = [e for e in evs if e.get("checkable")]
        hit = sum(1 for e in chk if e["sweep_before"])
        p.append(f"And in my records the sweep part isn't there at all: of {len(chk)} {'bullish ' if want == 'BULLISH' else ''}event(s) where it could be checked, the sweep had come up before in {hit}. So 'two out of two' doesn't match what I have.")
    p.append("What would count is a test fixed in advance: the outcome rule, a comparison group without the setup, and enough separate episodes. Even then it would be evidence for a pattern in the past, not a guarantee. Want to start by fixing the outcome rule?")
    return "\n\n".join(p), "def_outcome", ["proof"]


def _r_leap(h, text, F, said, ttype):
    subj = [t for t in terms(text) if t not in ("signal",)]
    key = subj[0] if subj else None
    if key is None and (h.get("rival") or {}).get("vars"):
        key = h["rival"]["vars"][0]
    nm = label(key) if key else "that"
    p = ["I can't say it caused it. Records can show which came first; a cause needs more: cases where the first thing happened and the second didn't, cases where the second happened without the first, and some reason the order isn't just a coincidence."]
    if key in ("displacement", "collect_liq"):
        p.append(f"And here I can't even show that {nm} happened: the engine doesn't record it, so any order I gave you would be invented.")
    elif key and _RECORD_KEY.get(key) == "sweep":
        evs, want = _evs(h, F)
        chk = [e for e in evs if e.get("checkable")]
        if chk:
            p.append(f"Here the order itself isn't there: the sweep check came up before in {sum(1 for e in chk if e['sweep_before'])} of {len(chk)} checkable event(s).")
    elif key == "vol_exp":
        p.append("The engine's volatility label can be lined up with the events, but lining up is all it shows.")
    p.append(f"Which comparison could you imagine that would show {nm} wasn't needed: the same move without it?")
    return "\n\n".join(p), "counter_case", ["leap"]


def _r_correct(h, text, F, said, ttype):
    want = h.get("corrected_to", "bullish")
    old = (h.get("history") or [{}])[-1].get("conds") or []
    p = [f"Changed to {want}; the earlier {('bearish' if want == 'bullish' else 'bullish')} version is kept as history, not overwritten. Now: {mirror(h)}."]
    obs = observed(h, F, set())
    if obs:
        p.append(" ".join(obs))
    k, q = _tail(h, text, said)
    if q:
        p.append(q)
    return "\n\n".join(p), k, ["correct", "observed"]


def _r_session(h, text, F, said, ttype):
    evs, want = _evs(h, F)
    if not evs or not F:
        return "I have no saved events to look up a session for.", None, ["session"]
    when = F.get("when") or str
    rows = [f"{when(e['at'])}: {e.get('session') or 'no reading'}" for e in evs[:6]]
    return ("The engine labels the session on each scan. At the scan that first saw each " + ("bullish " if want == "BULLISH" else "") + "change of character it read: " + "; ".join(rows) +
            ". Those are the engine's own labels (I haven't applied hours of my own), and two events is far too few to say anything about London in general."), None, ["session", "observed"]


def _r_timeframe(h, text, F, said, ttype):
    m = _TF.search(text or "")
    tf = m.group(1).lower() if m else "that timeframe"
    hour = bool(re.match(r"(1 ?h|1[- ]hour|one hour|hourly|h1|4 ?h|4[- ]hour|h4)$", tf))
    if hour:
        n = (F or {}).get("flips_1h")
        body = ("That would be a different test from the one so far. The engine saves its change-of-character flag for the 15-minute and the 5-minute only; there is no separate 1-hour flag. "
                "The nearest thing it keeps is the direction of the 1-hour leg, so the question becomes 'when did that leg change direction'. ")
        if n is None:
            body += "I can't read the saved history to count those."
        else:
            body += f"In the history I have (from {((F or {}).get('hist_from') or '?')[:10]}), the 1-hour leg changed direction {n} time(s)" + (", so there is nothing to look at on that timeframe yet." if n == 0 else ".")
    else:
        body = f"The engine saves its change-of-character flag on the 15-minute and the 5-minute; for {tf} I'd have to work from the candles with a definition you choose."
    return body + " I'll keep the earlier idea as it was and treat this as a separate version. Which would you like to pursue first?", "which_version", ["timeframe"]


def _r_backtest(h, text, F, said, ttype):
    n = (F or {}).get("candles")
    body = ("Not from here: I can't run a backtest in this chat, and the research notebook that is built for one isn't connected to it yet. "
            + (f"What exists is the saved 15-minute candles ({n} of them in the records I have), so a test is possible later, once it is written down. " if n else "")
            + "Until then I can help you write it down properly, which is the part that decides whether the result means anything:")
    sub, ask, new = _r_howtest(h, text, F, said, ttype)[0], "test_defined", ["howtest", "backtest"]
    if h.get("comparative"):
        body += ("\n\nOne caution on the saved logs: they hold each zone when it was first seen and a yes/no each scan for whether price was inside one. They don't hold how price reacted, "
                 "so they are not by themselves enough for this comparison; the reactions would have to be measured from the candles, and whether those cover the same period needs checking first.")
    return body + "\n\n" + sub, ask, new


# ------------------------------------------------------------------ added 26 Oct: falsification, proximity, session challenge
def _claim_text(h):
    rv = h.get("rival")
    if rv and rv.get("vars"):
        base = mirror(h) if not h.get("free") else _claim_text({**h, "rival": None})
        return f"{base}; your revised idea is a change of character during {_join([label(v) for v in rv['vars']])}"
    fc = h.get("free")
    if fc:
        return f"when {fc['when']}, then {fc['then']}" if fc.get("when") else fc.get("whole", "your idea")
    return mirror(h)


def _r_falsify(h, text, F, said, ttype):
    p = [f"It would be wrong if cases that meet your own definition of the setup carry on no more often than comparable moments without it. Counting them, not remembering them: "
         "a single case that fails doesn't disprove a tendency ('usually' allows for failures), and a single success doesn't support one. What matters is the share of cases, compared with the baseline."]
    p.append("So it needs three things written down before looking: the definition of the setup, the rule for 'carries on' (how far, how soon), and the share below which you'd drop the idea.")
    p.append("What share would make you drop it?")
    return "\n\n".join(p), "drop_share", ["falsify"]


def _r_proximity(h, text, F, said, ttype):
    p = ["Close in time isn't the same as the same thing, or one causing the other. Two events a few minutes apart can belong to different sessions, can be in either order, and can both be reactions to something else."]
    if F and F.get("scan_median_min"):
        p.append(f"There's a recording limit too: scans are usually about {F['scan_median_min']:g} minutes apart and each event is only stamped when a scan first noticed it, so differences smaller than a scan or two can't be ordered reliably from my records.")
    p.append("If the point matters for the idea, what's the largest gap you'd still call 'the same episode'? Without that number, 'close' can't be tested.")
    return "\n\n".join(p), "gap_rule", ["proximity"]


def _r_session_challenge(h, text, F, said, ttype):
    said_before = "session" in said
    evs, want = _evs(h, F)
    p = []
    if not said_before:
        p.append("I haven't said that in this conversation, so I don't want to take credit for it; if an earlier answer of mine implied it, the basis would be the same as below.")
    if evs and F:
        when = F.get("when") or str
        rows = [f"{when(e['at'])}: {e.get('session') or 'no label saved'}" for e in evs[:6]]
        p.append("What I can actually point to is the engine's own session label on the scan that first saw each event: " + "; ".join(rows) + ". "
                 "Those labels are assigned by the engine; I don't have how it decides, and a label on one scan says what session it was then, not that the whole event formed inside it.")
    else:
        p.append("I have no saved events to point to for a session.")
    p.append("So, from my records the most I can say is what the engine labelled; anything stronger would be me guessing.")
    return "\n\n".join(p), None, ["session", "observed"]


def _stamp(F, at):
    if not at:
        return "earlier in this conversation"
    try:
        from datetime import datetime
        return ((F or {}).get("when") or str)(datetime.fromisoformat(at))
    except Exception:
        return "earlier in this conversation"


def _r_recall(h, text, F, said, ttype):
    log = h.get("log") or []
    lines = []
    names = {"start": "your first version", "revise": "you revised it", "correct_dir": "you corrected the direction", "add_condition": "you added a condition",
             "contradiction": "you raised a case that goes against it", "mechanism": "you offered a reason", "elaborate": "you added"}
    for i, e in enumerate(log[:8], 1):
        lines.append(f"{i}. {_stamp(F, e.get('at')) if e.get('at') else 'at the start'}: \"{e.get('text', '')}\" ({names.get(e.get('type'), e.get('type'))})")
    p = ["Here's the thread so far, in your words and your order:\n" + "\n".join(lines)]
    p.append(f"Where it stands now: {_claim_text(h)}. Earlier versions are kept as they were; nothing I said changed them.")
    return "\n\n".join(p), None, ["recall"]


def _r_why_changed(h, text, F, said, ttype):
    ch = [e for e in (h.get("log") or []) if e.get("type") in ("revise", "correct_dir")]
    if not ch:
        return "You haven't changed it so far, so there's no change to explain.", None, ["why_changed"]
    e = ch[-1]
    p = [f"You changed it with these words: \"{e.get('text', '')}\". That's all I have: you didn't give a reason, and I didn't put anything in front of you that led to it (all I had counted was what the records show for the first version). I won't fill in a reason you didn't say."]
    p.append("If there was one, tell me and I'll keep it with the change as yours.")
    return "\n\n".join(p), None, ["why_changed"]
