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
  smalltalk      "heyyy", "thanks": a short friendly reply; nothing is stored.
  alert asks     "alert me when price goes above 1.3400" / "moves 10 pips in any direction": offered as an alert, never saved as a note;
                 a wish for an alert the bot has no way to watch (alert_unsupported) is said plainly, never filed as a note.
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
_WHY_CHANGE = re.compile(r"\bwhy did (?:the |my )?\w[\w\s]{0,40}?\b(change|changed|flip|flipped|switch|switched|turn|turned|go|went|drop|dropped|rise|rose|move|moved|break|broke)\b", re.I)
_FOLLOW = re.compile(r"^(and )?(so )?(what (happened|about|did it do|did price do|did price do after) )?(after (that|it|this|then)|next|then|later|and then|what next)\??$|^(and )?what (happened|did price do|came) (after|next)\b[\w\s]{0,6}\??$|"
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
                r"\bwhat'?s happening\b|\bwhat is going on\b|\bhow does (it|price|the market) look\b|\bhow is price\b|\bwhat'?s up with (?:gbpusd|cable|price|the market|gbp)\b|\bwhat is up with (?:gbpusd|cable|price|the market|gbp)\b|"
                r"\bhow far\b|\bwhat'?s the market (?:doing|like)\b|\bwhat'?s good with (?:gbpusd|cable|price|the market|gbp)\b|\bwhats good with (?:gbpusd|cable|price|the market|gbp)\b", re.I), "/now"),
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


_GREET_RX = re.compile(r"^(?:hey+|hi+|hello+|helo|yo+|sup|howdy|hiya|heyy+|good (?:morning|afternoon|evening)|wassup|wass?up|what'?s up|whats up|how far(?: na| now| bot)?|how are you|how you dey|oya)"
                       r"(?:\s+(?:there|bot|valleybot|boss|man|abeg))?\s*[!.?]*$", re.I)
_THANKS_RX = re.compile(r"^(?:thanks+|thank you|thank u|thx|ty|tnx|nice one|nice|cool|great|good job|well done|got it|noted|lol|lmao|ok thanks|okay thanks|alright thanks)\s*[!.]*$", re.I)
_ALERT_ASK = re.compile(r"\b(alert|notify|ping|message|text|buzz|wake|warn|remind|tell|let) (me|us)\b|\balert\b|\bnotification\b", re.I)
_ALERT_STRONG = re.compile(r"\balert\b|\bnotif(y|ication)\b|\bping\b|\bbuzz\b|\bwake me\b|\bremind me\b|\blet me know\b|\b(message|text) me\b", re.I)
_ALERT_WHEN = re.compile(r"\b(when|if|once|as soon as|whenever)\b", re.I)
# a message about the PAST is never a request to be told about the FUTURE ("check the last choch and after that tell me what happened")
_PAST = re.compile(r"\b(last|latest|previous|previously|was|were|had|did|happened|yesterday|earlier|ago|since|so far|recent|recently|already)\b", re.I)
_LEVEL = r"(\d\.\d{2,5})"
_TFS = re.compile(r"\b(5m|5 ?min(?:ute)?s?|15m|15 ?min(?:ute)?s?|1h|1 ?hour|hourly|60 ?min(?:ute)?s?)\b", re.I)


def _tf(raw):
    m = _TFS.search(raw)
    if not m:
        return None
    t = m.group(1).lower().replace(" ", "")
    return "15m" if t.startswith("15") else "5m" if t.startswith("5") else "1h"


def _tf_any(low):
    """Any timeframe the user wrote, normalised ("1hr", "1H", "H4", "30 min", "hourly", "daily" -> 1h, 1h, 4h, 30m, 1h, 1d), else None.
    Used to catch a request for a timeframe the engine does not record, so it is said and never silently swapped for another."""
    m = re.search(r"\b(\d{1,3})\s*(?:m|min|mins|minute|minutes)\b", low)
    if m:
        n = int(m.group(1)); return "1h" if n == 60 else f"{n}m"
    m = re.search(r"\b(\d{1,2})\s*(?:h|hr|hrs|hour|hours)\b", low)
    if m:
        return f"{int(m.group(1))}h"
    m = re.search(r"\b([hm])(\d{1,3})\b", low)
    if m:
        return f"{int(m.group(2))}{m.group(1)}"
    if re.search(r"\bhourly\b", low):
        return "1h"
    if re.search(r"\b(daily|1d|d1)\b", low):
        return "1d"
    if re.search(r"\bweekly\b", low):
        return "1w"
    return None


def _alert(raw):
    """-> decision dict for an alert request, else None. Levels map to the existing /alert forms; 'moves N pips' is anchored to the latest price by the runner."""
    if not _ALERT_ASK.search(raw):
        return None
    if not (_ALERT_WHEN.search(raw) or re.match(r"\s*alert\b", raw, re.I) or re.search(r"\b(next|every|each)\b", raw, re.I)):
        return None
    if not _ALERT_STRONG.search(raw) and (_PAST.search(raw) or not re.search(r"\b(next|when|once|whenever|as soon as|if)\b", raw, re.I)):
        return None                                   # "tell me" alone, or a question about the past, is not a request to set an alert
    if _PAST.search(raw) and not re.search(r"\b(next|when|once|whenever|as soon as)\b", raw, re.I) and not re.match(r"\s*alert\b", raw, re.I):
        return None
    low = raw.lower()
    choch = re.search(r"\b(choch|change of character|change in character|changes? character|character change|shift in character)\b", low)
    bos = re.search(r"\b(bos|break of structure|structure break|breaks? structure|break in structure)\b", low)
    if choch or bos:
        if re.search(r"\b(bullish|bearish|upside|downside|up|down|long|short|buy|sell)\b", low) and not re.search(r"any direction|either|both|whichever|up or down", low):
            return {"kind": "alert_unsupported", "why": "direction"}
        tf_asked = _tf_any(low)
        return {"kind": "act_offer", "act": "alert", "alert": {"type": "level", "cmd": "/alert choch" if choch else "/alert bos",
                                                                "tf_asked": tf_asked if tf_asked and tf_asked != "15m" else None,
                                                                "said": "tell you the next time the engine's saved reading shows a new change of character on the 15-minute"
                                                                        if choch else "tell you the next time the engine's break-of-structure direction changes"}}
    fvg = re.search(r"\b(fvg|fvgs|fair value gaps?)\b", low)
    ob = re.search(r"\b(order ?blocks?|obs?)\b", low)
    if fvg or ob:
        if re.search(r"\b(enter|enters|entered|into|inside|retest|retests|tap|taps|touch|touches|mitigat\w*|fill|fills|filled|revisit\w*)\b", low):
            return {"kind": "alert_unsupported", "why": "zone_entry"}
        tf_asked = _tf_any(low)
        if tf_asked and tf_asked != "15m":
            return {"kind": "alert_unsupported", "why": "zone_tf", "tf": tf_asked}
        bull = re.search(r"\bbullish\b", low); bear = re.search(r"\bbearish\b", low)
        d = "" if (bull and bear) or not (bull or bear) else (" bullish" if bull else " bearish")
        what = "fvg" if fvg else "ob"
        return {"kind": "act_offer", "act": "alert", "alert": {"type": "level", "cmd": f"/alert {what}{d}", "tf_asked": None, "said": "set that zone alert"}}
    if re.search(r"\bzones?\b", low) and re.search(r"\b(enter|enters|entered|into|inside|retest|retests|tap|taps|touch|touches|mitigat\w*|revisit\w*)\b", low):
        return {"kind": "alert_unsupported", "why": "zone_entry"}               # "that zone" with no prices is the same request: price entering a zone
    pips = re.search(r"(\d+(?:\.\d+)?)\s*(?:pips?|pip)\b", low)
    if pips and re.search(r"\b(move|moves|moved|moving|run|runs|travel|travels|go|goes|drop|drops|rise|rises|rally|rallies|fall|falls|swing|swings)\b", low):
        up = re.search(r"\b(up|higher|rise|rises|rally|rallies|upward|upside)\b", low)
        down = re.search(r"\b(down|lower|drop|drops|fall|falls|downward|downside)\b", low)
        both = re.search(r"any direction|either (way|direction)|up or down|down or up|both ways|whichever", low)
        d = "any" if (both or (up and down) or not (up or down)) else ("up" if up else "down")
        return {"kind": "act_offer", "act": "alert", "alert": {"type": "move", "pips": float(pips.group(1)), "dir": d}}
    lv = re.search(_LEVEL, low)
    if lv:
        level = lv.group(1)
        tf = _tf(low)
        if re.search(r"\bclos(e|es|ed|ing)\b", low):
            side = "below" if re.search(r"\b(below|under|lower)\b", low) else "above"
            return {"kind": "act_offer", "act": "alert", "alert": {"type": "level", "cmd": f"/alert close {side} {level}" + (f" {tf}" if tf else "")}}
        if re.search(r"\b(touch|touches|touched|hit|hits|reach|reaches|get to|gets to|tag|tags|retest|retests|tap|taps)\b", low):
            return {"kind": "act_offer", "act": "alert", "alert": {"type": "level", "cmd": f"/alert touch {level}"}}
        if re.search(r"\b(above|over|higher|break|breaks|breaking|cross|crosses|rise|rises|up)\b", low):
            return {"kind": "act_offer", "act": "alert", "alert": {"type": "level", "cmd": f"/alert {level} above"}}
        if re.search(r"\b(below|under|lower|drop|drops|fall|falls|down|breakdown)\b", low):
            return {"kind": "act_offer", "act": "alert", "alert": {"type": "level", "cmd": f"/alert {level} below"}}
    if re.search(r"\balert\b|\b(price|gbpusd|cable|candle|level|pips?|break|breaks|touch|touches|ema|zone|sweep|swept|bos|choch|structure|bias)\b", low):
        return {"kind": "alert_unsupported"}
    return None


# --- "no I mean ...": a correction is just the real question, said again -----------------------------------------------------
_CORRECTION = re.compile(r"^\s*(?:(?:no+|nope|nah|wait|hold on|sorry|oops|actually|ok(?:ay)?|hmm+|well|ah+|oh+|not that|that'?s not what i (?:meant|asked)|"
                         r"i meant|i mean|what i mean(?: is)?|i said|let me rephrase|rephrase|let me ask again|ask again|try again|again)\b[\s,.:;!-]*)+", re.I)


def _strip_correction(raw):
    """-> the message without a leading 'no, I mean' (kept as-is when nothing real would be left)."""
    m = _CORRECTION.match(raw)
    if not m:
        return raw
    rest = raw[m.end():].strip()
    return rest if len(rest) >= 6 else raw


# --- the last time something happened: "when was the last choch", "any bos today", "last bullish fvg" -----------------------------
_EV = {"choch": r"chochs?|change of character|change in character|character change|leg flip(?:ped)?|flip(?:ped)? to (?:bullish|bearish)|flipped",
       "bos": r"bos|break of structure|structure break|break in structure",
       "fvg": r"fvgs?|fair value gaps?",
       "ob": r"order ?blocks?",
       "sweep": r"sweeps?|swept|liquidity sweeps?|liquidity grabs?|stop ?hunts?"}
_EV_ANY = "|".join(_EV.values())
_LAST = re.compile(rf"\b(?:last|latest|most recent|previous|recent|newest|first|earliest)\b[\w\s]{{0,18}}?\b(?:{_EV_ANY})\b|"
                   rf"\bhow many\b[\w\s]{{0,15}}?\b(?:{_EV_ANY})\b|"
                   rf"\bwhen\b[\w\s']{{0,30}}?\b(?:{_EV_ANY})\b|"
                   rf"\b(?:{_EV_ANY})\b[\w\s]{{0,12}}\b(?:last|latest|happen(?:ed)?|form(?:ed)?|occur(?:red)?|print(?:ed)?)\b|"
                   rf"\b(?:any|were there|was there|has there been|have there been|did we (?:get|have|see)|did price (?:make|have|print))\b[\w\s]{{0,15}}\b(?:{_EV_ANY})\b", re.I)
_TERM_ANY = (r"choch|change of character|bos|break of structure|fvgs?|fair value gaps?|order ?blocks?|liquidity sweeps?|sweeps?|htf|higher.?timeframe|bias|atr|"
             r"phase|exhaust\w*|displace\w*|protected (?:highs?|lows?)|strong (?:highs?|lows?)|retrace\w*|pullbacks?")
_CONCEPT_ONLY = r"displace\w*|protected (?:highs?|lows?|levels?)|strong (?:highs?|lows?)"
_EXPLAIN = re.compile(rf"^(?:eli ?5|explain like (?:i'?m|im|i am) (?:5|five|new)|explain simply|simply explain|in simple (?:words|terms))[:,]?\s+(?:an? |the )?(?:{_TERM_ANY})\b|"
                      rf"^(?:what|wat|wah)(?:'s| is| be)? (?:dat|that|this|the|a) (?:{_TERM_ANY}) (?:thing|stuff|word|term)(?: mean| is)?\??$|"
                      rf"^(?:remind me|tell me|teach me|show me) (?:what|wat) (?:an? |the )?(?:{_TERM_ANY}) (?:is|means)\??$|"
                      rf"^(?:explain|define|what is|what's)\s+(?:an? |the )?(?:{_TERM_ANY})(?:\s+(?:to me|simply|like (?:i'?m|im) (?:5|five|new)))\??$|"
                      rf"^(?:and |so |ok |okay )?(?:what(?:'s|s| is| be| are| does| do)?|wat(?:'s| is| be| does)?|wah|explain|define|pls explain|please explain|can you explain|"
                      rf"what do you mean by|meaning of)\s+(?:an? |the )?(?:{_TERM_ANY})\b(?:\s+(?:mean|means|stand for|actually mean|really mean))?(?:\s+(?:like|as if|pls|please|simply|briefly|in simple (?:words|terms)|in plain (?:words|english)|for me|im|i'm|i am)\b[\w\s']{0,12})?\s*\??$|"
                      rf"^(?:{_TERM_ANY})\s+(?:meaning|means|definition)\??$|^(?:an? |the )?(?:{_TERM_ANY})\s*\??$|^(?:explain|define)\s+(?:an? |the )?(?:{_TERM_ANY})\b|^(?:what do|wat do) (?:you|u) mean by\s+(?:an? |the )?(?:{_TERM_ANY})", re.I)
_TRACK = re.compile(rf"\b(?:do|does|can|could|will|did)\s+(?:you|u|it|the bot|the engine|valleybot)\s+(?:also\s+)?(?:track|record|detect|see|log|store|know|measure|save|flag|identify|spot|keep)\b"
                    rf"[\w\s]{{0,30}}?\b(?:{_TERM_ANY})\b", re.I)
_CAUSAL = re.compile(r"\b(?:cause[ds]?|causing|because of|led to|lead to|triggered?|resulted? in|made (?:it|price)|responsible for)\b", re.I)
_VERDICT = re.compile(r"\b(?:is|was|are|were) (?:the |that |this |a |an )?[\w\s]{0,14}?\b(?:real|fake|valid|genuine|legit|true|false|confirmed|reliable|good|strong|weak)\b|"
                      r"\b(?:real|fake|valid|genuine|legit)\b(?: or (?:real|fake|valid|genuine|legit))?\??$", re.I)
_PREDICT = re.compile(r"\b(?:will|would|gonna|going to) (?:price|it|gbpusd|cable|the market|we)\b[\w\s]{0,12}\b(?:go|rise|fall|drop|dump|pump|break|reverse|retrace|hit|reach)\b|"
                      r"\bshould i (?:buy|sell|enter|trade|long|short|take)\b|\bwhere (?:will|is) (?:price|it) (?:going|go|headed)\b|\bwhat will (?:price|it) do\b|"
                      r"\bis it (?:safe|a good time) to (?:buy|sell|enter|trade)\b", re.I)
_INJECT = re.compile(r"\b(?:ignore|forget|disregard|bypass|override)\b[\w\s]{0,20}\b(?:rules?|instructions?|guard|checks?|limits?|restrictions?)\b|\bpretend\b[\w\s]{0,20}\b(?:you|there)\b|\bjust (?:say|tell me|confirm)\b[\w\s]{0,25}\b(?:was|is|will)\b", re.I)
_ZONE_Q = re.compile(r"(?:\b(?:was|is|were|did|has|had)\b[\w\s']{0,24}?|^)\b(?:price|it|we|cable|gbpusd)\b[\w\s']{0,18}?\b(?:inside|in|within|into|touch(?:ed)?|enter(?:ed)?|tap(?:ped)?|mitigat\w*)\b[\w\s']{0,14}?\b(?:fvgs?|fair value gaps?|gaps?|order ?blocks?|obs?)\b", re.I)
_ZONE_IT = re.compile(r"\b(?:was|is|were|did|has|had)\b[\w\s']{0,16}?\b(?:price|it|we|cable|gbpusd)\b[\w\s']{0,14}?\b(?:inside|in|within|into|touch(?:ed)?|enter(?:ed)?|tap(?:ped)?)\b\s+(?:it|that|this|there|the (?:same )?(?:one|zone|gap))\b", re.I)
_ZONE_NAME = re.compile(r"\b(fvgs?|fair value gaps?|gaps?)\b|\b(order ?blocks?|obs?)\b", re.I)
_DETAILS = re.compile(r"^(?:and )?(?:please |pls )?(?:give me |show me |send )?(?:the )?(?:details?|full (?:timeline|detail|story|version|thing)|everything|more detail|go into (?:it|detail)|break it down|the long version)\s*(?:please|pls)?[.!?]*$", re.I)
_PREV_REF = re.compile(r"\b(?:the )?(?:one|event|one just|choch|bos|sweep|fvg|gap|order ?block)\s+(?:right )?(?:before|prior to)\s+(?:that|it|this|the last)\b|\bthe one (?:prior|earlier|before)\b|\bthe (?:previous|prior|earlier) (?:one|choch|bos|sweep|fvg|gap|order ?block)\b", re.I)
_ZONE_REF = re.compile(r"\b(?:that|this|the same|the last|the latest)\s+(?:\w+\s+)?(?:fvg|fair value gap|gap|order ?block|ob)\b", re.I)
_COMPARE = re.compile(r"\b(?:compare|comparing|versus|vs\.?|how (?:does|did) (?:it|that|this) (?:compare|differ|stack up)|same as|different (?:from|to|than))\b", re.I)
_PREV_WORDS = re.compile(r"\b(?:previous|prior|one before|before (?:it|that|this)|earlier one|last one|other one|the one before)\b", re.I)
_AFTER = re.compile(r"^(?:and )?(?:then )?(?:after that|afterwards?|after it|after this|since then|since that|from there|following that|what next)\b", re.I)
_WHAT_HAPPENED = re.compile(r"\b(?:what (?:happened|did price do|went on|did it do|was price doing|has happened|happens|came next)|how did (?:it|price) (?:go|play out|move|behave|react|trade)|"
                            r"tell me what happened|show me what happened|what'?s? (?:has )?been (?:happening|going on))\b", re.I)


_M_FIRST = re.compile(r"\b(?:first|earliest|opening)\b", re.I)
_M_LIST = re.compile(r"\bhow many\b|\bevery\b|\ball (?:the |of the )?\w*\s?(?:chochs?|bos|sweeps?|fvgs?|order ?blocks?|changes? of character)|\blist\b|\bso far\b|\btoday\b|\bthis (?:morning|afternoon|session)\b|\bsince (?:the )?(?:open|morning)\b", re.I)
_M_COUNT = re.compile(r"\bhow many\b", re.I)


def _event_mode(low, now):
    """-> (mode, window) for a last-event style question: 'last' (default), 'first', 'count', or 'list'; the window is Lagos-today by default for
    first/count/list, the morning or afternoon of today, or an explicit time the message gave."""
    mode = "count" if _M_COUNT.search(low) else "first" if _M_FIRST.search(low) else "list" if _M_LIST.search(low) else "last"
    w = None
    if re.search(r"\bthis morning\b", low):
        w = "morning"
    elif re.search(r"\bthis afternoon\b", low):
        w = "afternoon"
    elif re.search(r"\b(?:at|around|about|between|by)\b\s*\d", low):
        w = timeref.parse(low, now)
        if w is not None and mode == "last":
            mode = "list"
    if mode == "last" and re.search(r"\b(?:did we get|any|was there|were there|has there been|have there been)\b", low) and re.search(r"\b(?:today|this morning|this afternoon)\b", low):
        mode = "list"
    return mode, w


def _events_in(low):
    return [k for k, pat in _EV.items() if re.search(rf"\b(?:{pat})\b", low, re.I)]


_SPLIT = re.compile(r"(\s*[;?]+\s*|\s*,\s*|\s+\band then\b\s+|\s+\band after that\b\s+|\s+\bafter that\b\s+|\s+\bthen\b\s+|\s+\band also\b\s+|"
                    r"\s+\balso\b\s+|\s+\bplus\b\s+|\s+\band\b\s+(?=(?:what|whats|what's|when|how|why|where|who|is|are|did|do|does|tell|show|give|"
                    r"check|look|any|has|have|can|wat|wah|i|im|i'm|it|price|my)\b))", re.I)
_DEPENDENT_SEP = re.compile(r"\bthen\b|\bafter that\b", re.I)


def _clauses(low):
    """-> [(clause, depends_on_previous)]. Splits at ?, ;, commas, 'and what/when/...', 'then', 'after that', 'also'."""
    bits = _SPLIT.split(low)
    out, dep = [], False
    for i, b in enumerate(bits):
        if i % 2 == 1:
            dep = dep or bool(_DEPENDENT_SEP.search(b))
            continue
        b = b.strip(" .!")
        if len(b) >= 2:
            out.append((b, dep))
            dep = False
    return out


_TERM_ONLY = re.compile(r"^(?:and |ok |so )?(?:what about |how about |and what about )?(?:the )?(?:last |latest |recent )?((?:chochs?|bos|sweeps?|liquidity sweeps?|fvgs?|fair value gaps?|order ?blocks?))\s*\??$", re.I)


def _concept(low, now=None):
    """Questions about a trading TERM rather than the market: what it means, whether the engine records it, or when it last happened for a
    thing the engine does not record. -> a decision or None. converse holds no definitions: the runner looks them up in plainwords."""
    s = low.strip(" .!?")
    if re.match(r"^(?:and )?(?:so )?(?:what|wat|wah)(?:'s| is| does| do)? (?:it|that|this|those)(?: all)? mean\??$|^(?:and )?(?:what|wat) (?:is|does) (?:that|it)\??$", s, re.I):
        return {"kind": "explain_prev"}
    if _ZONE_IT.search(s) and not _ZONE_Q.search(s):
        then = bool(re.search(r"\b(?:then|back then|at that time|when it (?:formed|appeared|printed)|at the time)\b", s))
        return {"kind": "zone_state", "zone": None, "then": then, "window": timeref.parse(s, now) if now is not None else None, "words": s}
    if _ZONE_Q.search(s):
        m = _ZONE_NAME.search(s)
        kind = "fvg" if (m and m.group(1)) else "ob"
        then = bool(re.search(r"\b(?:then|back then|at that time|when it (?:formed|appeared|printed)|at the time)\b", s))
        return {"kind": "zone_state", "zone": kind, "then": then, "ref": bool(_ZONE_REF.search(s)), "window": timeref.parse(s, now) if now is not None else None, "words": s}
    if _INJECT.search(s):
        return {"kind": "cannot_say", "why": "inject", "words": s}
    if _PREDICT.search(s):
        return {"kind": "cannot_say", "why": "predict", "words": s}
    if _CAUSAL.search(s) and len(re.findall(rf"\b(?:{_TERM_ANY})\b", s, re.I)) >= 1:
        return {"kind": "cannot_say", "why": "cause", "words": s}
    if _VERDICT.search(s) and re.search(rf"\b(?:{_TERM_ANY})\b", s, re.I):
        return {"kind": "cannot_say", "why": "verdict", "words": s}
    if _COMPARE.search(s) and (_PREV_WORDS.search(s) or re.search(r"\bcompare (?:it|that|this)\b", s)):
        return {"kind": "compare_prev", "events": _events_in(s)}
    if _EXPLAIN.search(s):
        return {"kind": "explain", "words": s}
    if _TRACK.search(s):
        return {"kind": "capability", "words": s}
    if re.search(rf"\b(?:{_CONCEPT_ONLY})\b", s) and re.search(r"\b(?:when|was there|were there|any|last|latest|did|has|had|where|show|check)\b", s):
        return {"kind": "capability", "words": s}
    return None


def _read_one(clause, dep, now, extra):
    """One clause of a several-part message -> a decision for a READ-ONLY kind, or None. Actions (alerts, trades, notes) are never run from
    inside a compound message: one change at a time."""
    c = clause.strip()
    if _GREET_RX.match(c) or _THANKS_RX.match(c):
        return {"kind": "noise"}
    if re.match(r"^(?:and )?(?:then )?(?:what|wat|wah) (?:happened|did price do|came|did it do)(?: next| after| afterwards?| then| after that| after it)?$", c, re.I):
        return {"kind": "after_prev"}
    if dep and (_WHAT_HAPPENED.search(c) or not re.sub(r"\b(tell|show|give|me|us|what|the|please|pls|plz)\b", "", c).strip()) or _AFTER.match(c):
        return {"kind": "after_prev"}
    if re.match(r"^(?:and )?(?:what|wat|wah)(?:'s| is| does| do)? (?:it|that|this|those)(?: all)? mean\??$|^(?:and )?(?:what|wat) (?:is|does) (?:that|it)\??$", c, re.I):
        return {"kind": "explain_prev"}
    cc = _concept(c, now)
    if cc and cc["kind"] in ("explain", "capability", "cannot_say", "zone_state"):
        return cc
    if _LAST.search(c):
        mode, w = _event_mode(c, now)
        return {"kind": "last_event", "events": _events_in(c) or ["choch"], "mode": mode, "window": w}
    mt = _TERM_ONLY.match(c)
    if mt and _events_in(mt.group(1)):
        return {"kind": "last_event", "events": _events_in(mt.group(1)), "mode": "last", "window": None}
    if _alert(c) or _act(c, c):
        return {"kind": "action"}
    if _CONVO.search(c):
        return {"kind": "convo_recon", "window": timeref.parse(c, now), "words": c}
    why = bool(_WHY_CHANGE.search(c))
    if _RECON.search(c) or _RECON2.search(c) or why:
        w = timeref.parse(c, now)
        return {"kind": "market_recon", "window": w} if w is not None else {"kind": "need_time"}
    canon = present_question(c, extra)
    if canon:
        return {"kind": "ask_bus", "canonical": canon}
    return None


def _multi(low, now, extra):
    """-> {"kind": "multi", "parts": [...]} when the message asks for more than one thing, else None. Every part is either understood or
    said back as not understood: nothing is silently dropped."""
    cl = _clauses(low)
    if len(cl) < 2:
        return None
    parts, known = [], 0
    for c, dep in cl:
        d = _read_one(c, dep, now, extra)
        if d is None:
            if informal.looks_like_market_talk(c, extra) and len(c.split()) >= 4:
                if (c, dep) != cl[-1]:
                    return None                              # a note in the middle: the whole message is handled the old way
                d = {"kind": "offer_save"}                   # a note at the END of the questions: answer them, then ASK before saving it
                parts.append({**d, "clause": c})
                continue
            d = {"kind": "unknown"}
        elif d["kind"] == "action":
            return None
        elif d["kind"] == "noise":
            continue
        else:
            known += 1
        parts.append({**d, "clause": c})
    if known < 1 or len(parts) < 2:
        return None
    if known < 2 and not any(p["kind"] in ("last_event", "ask_bus", "market_recon") for p in parts):
        return None
    return {"kind": "multi", "parts": parts}


def present_question(text, extra=None):
    """-> canonical command for a 'right now' question, else None."""
    n = informal.normalise(text, extra)["text"].strip()
    for rx, cmd in _PRESENT:
        if rx.search(n):
            return cmd
    return None


_INT_SUBJ = re.compile(r"\b(?:integrity|inspector|inspection|all[- ]?clear|diagnostics?|self[- ]?check|health ?check|checker)\b", re.I)
_INT_ALARM = re.compile(r"\b(?:alarms?(?:ed|ing)?|flagged|all[- ]?clear)\b", re.I)
_INT_LIMITS = re.compile(r"\bsame as\b|\bmean (?:nothing|everything|no)\b|\bcan'?t (?:it|you|the \w+) (?:catch|see|detect|miss|check)\b|\bblind ?spots?\b|\bloopholes?\b|\bguarantee|"
                         r"\bnot cover|\bdoesn'?t (?:cover|check|catch|see)|\bwould (?:it|you) (?:miss|catch)\b|\bhow (?:could|can) it (?:miss|fail)|\bwhat (?:can'?t|cannot|doesn'?t|does(?: not)?n'?t)\b|"
                         r"\bwhat (?:does|do|would) it (?:miss|not)\b|\blimits?\b|\bweak(?:ness|nesses| spots?)\b|\bnot (?:see|catch|check)\b", re.I)
_INT_CHECKS = re.compile(r"\bwhat (?:does|do|is|are|did)\b[\w\s']{0,30}?\b(?:check|inspect|look(?:s|ing)? (?:for|at)|monitor|do|watch)\b|\bwhat (?:is|are) (?:the )?(?:integrity|inspector|checker)\b|"
                         r"\bexplain\b|\bhow (?:does|do) (?:it|the \w+|you) work\b|\bwhat (?:would|will) (?:make|trigger|raise|cause)\b|\bwhen would (?:it|you) (?:raise|alarm|alert)|\bwhat triggers\b|\btell me about\b", re.I)
_INT_WHYCLEAR = re.compile(r"\bwhy\b[\w\s',]{0,40}?\b(?:clear|fine|ok|okay|quiet|nothing|silent|green|no (?:alarm|finding|problem))\b|\b(?:says|saying|shows?|showing|reads?) (?:all )?clear\b|\bhow come\b[\w\s']{0,30}?\bclear\b", re.I)
_INT_HISTORY = re.compile(r"\b(?:earlier|before|previous(?:ly)?|yesterday|ever|since|last time)\b[\w\s',]{0,40}?\b(?:alarm\w*|alert\w*|flag\w*|found|finding|rais\w+)\b|"
                          r"\b(?:alarm\w*|alert\w*|flag\w*|found|finding|rais\w+)\b[\w\s',]{0,40}?\b(?:earlier|before|previous(?:ly)?|yesterday|ever|last time)\b|"
                          r"\bhas (?:it|the inspector) (?:found|raised|flagged|caught|seen)\b|\banything (?:found|open|flagged)\b|\bany (?:findings?|alarms?|problems?|issues?)\b|"
                          r"\bhow many (?:alarms?|findings?)\b|\bwhy did it (?:alarm|raise|flag)\b|\bwhy (?:the )?alarm\b", re.I)
_INT_TRUST = re.compile(r"\bcan i trust\b|\bhow (?:do|can) you know\b[\w\s']{0,30}?\b(?:right|correct|true|accurate|ok|okay|fine)\b|\bhow (?:sure|reliable)\b|"
                        r"\bare you sure\b[\w\s']{0,30}?\b(?:engine|data|state|system|bot|ok|okay|fine|working)\b|"
                        r"\bis (?:the )?(?:data|engine|state|bot|system|scanner) (?:ok|okay|fine|right|correct|good|healthy|working|accurate)\b|"
                        r"\beverything (?:is )?(?:fine|ok|okay|good|working)\b|\ball good\b|\bis anything (?:broken|wrong)\b|\bhealthy\b", re.I)


def _integrity(s, strong):
    """An integrity-inspector question: what it checks, why it says clear, what it has found, what it cannot see, whether to trust it.
    strong = the message names the inspector / integrity / all clear; otherwise only an unmistakable 'alarm' or trust phrasing counts."""
    subj = bool(_INT_SUBJ.search(s))
    if strong != subj and strong:
        return None
    if not strong and not (_INT_ALARM.search(s) or _INT_TRUST.search(s) or subj):
        return None
    if _INT_LIMITS.search(s) and (subj or _INT_ALARM.search(s)):
        ask = "limits"
    elif _INT_WHYCLEAR.search(s) and (subj or _INT_ALARM.search(s) or re.search(r"\bclear\b", s, re.I)):
        ask = "why_clear"
    elif _INT_HISTORY.search(s) and (subj or _INT_ALARM.search(s) or re.search(r"\bit\b|\bthe inspector\b", s, re.I)):
        ask = "history"
    elif _INT_CHECKS.search(s) and (subj or _INT_ALARM.search(s)):
        ask = "checks"
    elif _INT_TRUST.search(s) and (subj or not re.search(r"\b(?:market|price|trend|thesis|structure|bias|setup|trade|signal|chart|gbpusd|cable|bullish|bearish)\b", s, re.I)):
        ask = "trust"
    elif subj:
        ask = "status"
    else:
        return None
    return {"kind": "integrity", "ask": ask, "words": s}


_FILLERS = {"though", "please", "biko", "bro", "lol", "oya", "quickly", "fast", "abeg", "pls", "plz", "sharp", "ok", "okay", "hey", "yo", "so", "boss", "man", "guy"}


def _strip_fillers(s):
    """'what is a fair value gap though' -> 'what is a fair value gap'. Only words that carry no question, only at the ends, plus trailing emoji / !!! / ...
    Used for matching only; the raw message is what gets stored."""
    s = re.sub(r"[^\w\s?:'./%,;-]+", " ", s)                       # emoji, !!!, ... -> space (a '?' is kept: it means something)
    s = re.sub(r"\s+", " ", s).strip()
    ws = s.split(" ")
    clean = lambda w: re.sub(r"[^a-z]", "", w.lower())
    while len(ws) > 1 and clean(ws[0]) in _FILLERS:
        ws.pop(0)
    while len(ws) > 1 and clean(ws[-1]) in _FILLERS:
        ws.pop()
    out = " ".join(ws)
    return re.sub(r"\s+\?", "?", out)


def interpret(raw, now, extra=None):
    raw = raw or ""
    n = informal.normalise(raw, extra)
    k = informal.kind_of(raw, extra)
    base = {"raw": raw, "normalised": n["text"], "changes": n["changes"], "uncertain": n["uncertain"]}
    if k in ("command", "empty"):
        return {**base, "kind": "command" if k == "command" else "unknown"}
    low = _strip_fillers(n["text"])
    if _GREET_RX.match(raw.strip()):
        return {**base, "kind": "smalltalk", "tone": "greet"}
    if _THANKS_RX.match(raw.strip()):
        return {**base, "kind": "smalltalk", "tone": "thanks"}
    stripped = _strip_correction(raw)
    if stripped != raw:                                      # "no I mean ...": read the real question, remember it was a correction
        r2 = interpret(stripped, now, extra)
        r2["raw"], r2["corrected"] = raw, True
        return r2
    ig = _integrity(low, strong=True)
    if ig:                                                      # "why does the integrity alarm say clear?" is about the inspector, not about an alert to set
        return {**base, **ig}
    a = _alert(raw) or _act(raw, low)
    if a:
        return {**base, **a}
    ig = _integrity(low, strong=False)
    if ig:
        return {**base, **ig}
    if _PREV_REF.search(low) and not _COMPARE.search(low):               # "no, I mean the one before that. what happened after it?"
        ev = _events_in(low)
        parts = [{"kind": "prev_event", "events": ev}]
        if _WHAT_HAPPENED.search(low) or re.search(r"\bafter\b", low):
            parts.append({"kind": "after_prev", "clause": low})
        return {**base, "kind": "multi", "parts": parts} if len(parts) > 1 else {**base, "kind": "prev_event", "events": ev}
    mu = _multi(low, now, extra) if len(_clauses(low)) >= 2 else None       # a message with several parts is split first: one part must not hijack the rest
    if mu:
        return {**base, **mu}
    cc = _concept(low, now)
    if cc:
        return {**base, **cc}
    mt = _TERM_ONLY.match(low.strip())
    if mt and _events_in(mt.group(1)):
        return {**base, "kind": "last_event", "events": _events_in(mt.group(1)), "mode": "last", "window": None}
    if _LAST.search(low) and re.search(r"\b(after|since)\b", low) and _WHAT_HAPPENED.search(low):       # "what happened after the last bos"
        return {**base, "kind": "multi", "parts": [{"kind": "last_event", "events": _events_in(low) or ["choch"], "clause": low}, {"kind": "after_prev", "clause": low}]}
    if _LAST.search(low):
        mode, w = _event_mode(low, now)
        return {**base, "kind": "last_event", "events": _events_in(low) or ["choch"], "mode": mode, "window": w}
    if _FOLLOW.search(low.strip()):
        return {**base, "kind": "followup"}
    if _DETAILS.match(low.strip()):
        return {**base, "kind": "details"}
    if _CONVO.search(low):
        w = timeref.parse(low, now)
        return {**base, "kind": "convo_recon", "window": w, "words": low}
    why = bool(_WHY_CHANGE.search(low))
    if _RECON.search(low) or _RECON2.search(low) or why:
        w = timeref.parse(low, now)
        if w is None:
            return {**base, "kind": "need_time"}
        if why:
            w.assumptions.append("you asked why. My records show what changed and when, not the reason, so I'll lay out the sequence and not guess a cause")
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
