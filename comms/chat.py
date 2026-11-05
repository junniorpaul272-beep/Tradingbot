"""
chat.py — the conversation BASICS: understanding an everyday question about the market or about the bot itself, and answering it
from the bot's own recorded candles and ledgers, in a partner's voice.

WHAT THIS IS NOT: a list of allowed sentences. A message is read as CONCEPTS (price, direction, EMA, change, range, count, level, feeling...)
plus SLOTS (a time, a timeframe, an EMA length, a number), with typos and slang tolerated. Any wording that carries the same concepts
lands on the same ability. Nothing is looked up from a phrase list.

AUTHORITY: this module computes nothing about the engine's view of the market. Direction/bias belongs to the engine (the runner hands that
question to the BUS). What is computed here is plain arithmetic on RECORDED candles and counts from RECORDED ledgers, and every answer says
which records and which definition it used. Anything it cannot establish is said plainly. It never invents a price, a count or a cause.

Pure: facts in, text out. The functions that read the candle logs and ledgers live in chat_records.py, so this file touches no disk.
"""
import difflib
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import timeref

PIP = 0.0001
LAGOS = ZoneInfo("Africa/Lagos")
UTC = timezone.utc
TF_MIN = {"5m": 5, "15m": 15, "1h": 60}
TF_FILE = {"5m": "ohlc_5m_log.jsonl", "15m": "ohlc_15m_log.jsonl", "1h": "ohlc_1h_log.jsonl"}
TF_WORDS = {"5m": "5-minute", "15m": "15-minute", "1h": "hourly"}
DEFAULT_EMA = 100

# ----------------------------------------------------------------------
# CONCEPTS: word families, not sentences. A word counts when it matches closely (typos), so "wahs", "direcion", "avrage" still land.
# ----------------------------------------------------------------------
FAMILIES = {
    "price": "price prices cable gbpusd gbp pair quote rate currently px gu",
    "where": "where wheres wher whre whr",
    "direction": "direction directions bias trend trending leaning lean bullish bearish headed heading bull bear",
    "ema": "ema emas",
    "relation": "relationship relation relate relates relating relative versus vs against position positioned sitting compared compare",
    "above": "above below over under beneath upper lower",
    "change": "change changed changes moved move gained lost dropped rose risen fallen difference gone climbed performance performed",
    "range": "range ranges volatility volatile avg average mean typical",
    "count": "many count number total often times",
    "scan": "scan scans scanned scanning",
    "setup": "setup setups signal signals signalled setting",
    "shadow": "shadow shadows experiment experiments",
    "trade": "trade trades traded trading",
    "alert": "alert alerts announcement announcements",
    "case": "case cases investigation investigations",
    "message": "message messages chat chats talked",
    "level": "level levels zone zones line",
    "track": "track tracking watch watching monitor monitoring follow",
    "feel": "feeling feel feels felt somehow tired stressed anxious anxiety worried upset frustrated angry sad drained overwhelmed confused lost",
    "advice": "advice advise opinion guidance help suggestion",
    "report": "report summary summarise summarize writeup breakdown recap rundown",
    "greet": "hey hi hello yo sup howdy hiya wassup morning afternoon evening",
    "wellbeing": "doing dey going fare fairing",
    "lot": "lot lots sizing volume",
    "stop": "stop sl stoploss",
    "market": "market markets",
    "new": "new anything latest news",
    "since": "since from after starting",
    "reject": "reject rejected rejects rejection rejections dropped skipped filtered blocked",
    "thanks": "thanks thank thx ty",
    "bot": "you your ur u ya yu yah bot valleybot",
}
_FAMILY_OF = defaultdict(set)
for _k, _v in FAMILIES.items():
    for _w in _v.split():
        _FAMILY_OF[_w].add(_k)
_VOCAB = sorted(_FAMILY_OF)


def _fam(word):
    """The concept families a word belongs to; near-misses (typos) count when the word is long enough to be sure."""
    if word in _FAMILY_OF:
        return _FAMILY_OF[word]
    if len(word) >= 5:
        m = difflib.get_close_matches(word, _VOCAB, n=1, cutoff=0.82)
        if m and abs(len(m[0]) - len(word)) <= 2:
            return _FAMILY_OF[m[0]]
    return set()


def concepts(text):
    """-> set of concept names present in the text."""
    out = set()
    for w in re.findall(r"[a-z']+", (text or "").lower()):
        out |= _fam(w)
    return out


_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_PRICE_RX = re.compile(r"(?<![\d.])(1\.\d{2,5})(?!\d)")


# ----------------------------------------------------------------------
# SLOTS
# ----------------------------------------------------------------------
def slot_tf(text):
    low = (text or "").lower()
    if re.search(r"\b(1|one)\s*(h|hr|hour|hourly)\b|\bhourly\b|\b1h\b|\bh1\b", low):
        return "1h"
    if re.search(r"\b15\s*(m|min|mins|minute|minutes)?\b(?!\s*(pips|:))|\b15m\b", low) and re.search(r"15\s*-?\s*(m|min)|fifteen", low):
        return "15m"
    if re.search(r"\b5\s*-?\s*(m|min|mins|minute|minutes)\b|\bfive minute\b|\b5m\b", low):
        return "5m"
    if re.search(r"\b(4|four)\s*(h|hr|hour)\b|\b4h\b", low):
        return "4h"
    if re.search(r"\bdaily\b|\bd1\b|\b1d\b", low):
        return "1d"
    return None


def slot_ema_len(text):
    m = re.search(r"\b(\d{1,3})\s*-?\s*(?:period\s*)?ema\b|\bema\s*-?\s*(?:of\s*)?(\d{1,3})\b", (text or "").lower())
    if m:
        n = int(m.group(1) or m.group(2))
        if 2 <= n <= 500:
            return n
    return None


def _fix_clock(text):
    """'since 8 56' -> 'since 8:56' (people drop the colon)."""
    return re.sub(r"\b(since|from|after|at|around|by)\s+(\d{1,2})\s+(\d{2})\b", r"\1 \2:\3", text or "", flags=re.I)


def slot_since(text, now):
    """-> (start_utc, label, assumptions) for 'since ...' in natural words, or None. END is always 'now'.
    The reference is the START of what was named (since friday = friday 00:00 Lagos; since 8:56 = 08:56 Lagos), and it is said aloud."""
    low = _fix_clock(text).lower()
    low = re.sub(r"\b(last|past)\s+(hour|hr|minute|min)\b", r"\1 1 \2", low)       # "the last hour" = the last 1 hour
    nl = now.astimezone(LAGOS)
    m = re.search(r"\b(?:last|past|previous)\s+(\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s*(minute|min|hour|hr|day|week)s?\b|"
                  r"\b(\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s*(minute|min|hour|hr|day|week)s?\s+ago\b", low)
    if m:
        g = [x for x in m.groups() if x]
        n = int(g[0]) if g[0].isdigit() else _NUM_WORDS.get(g[0], 1)
        unit = g[1][:3]
        delta = {"min": timedelta(minutes=n), "hou": timedelta(hours=n), "hr": timedelta(hours=n), "day": timedelta(days=n), "wee": timedelta(weeks=n)}.get(unit)
        if delta:
            return now - delta, (f"the last {g[1]}" if n == 1 and g[1] in ("hour", "hr", "day", "week", "minute", "min") else f"the last {n} {g[1]}{'s' if n != 1 else ''}"), []
    m = re.search(r"\blast week\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+last week\b", low)
    if m:
        d = _DAYS.index(m.group(1) or m.group(2))
        monday = (nl - timedelta(days=nl.weekday())).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)
        day = monday + timedelta(days=d)
        mine = day + timedelta(days=7)
        alt = f"{mine:%A %d %b}" if mine <= nl else None
        note = f"I took 'last week {_DAYS[d].title()}' as {day:%A %d %b}, in the week before this one" + (f"; if you meant the most recent {_DAYS[d].title()}, {alt}, say so" if alt else "")
        return day.astimezone(UTC), f"{day:%A %d %b} 00:00 Lagos time", [note]
    if re.search(r"\bthis week\b", low):
        monday = (nl - timedelta(days=nl.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return monday.astimezone(UTC), f"Monday {monday:%d %b} 00:00 Lagos time (the start of this week)", []
    if re.search(r"\blast week\b", low):
        monday = (nl - timedelta(days=nl.weekday())).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)
        return monday.astimezone(UTC), f"Monday {monday:%d %b} 00:00 Lagos time (the start of last week)", ["I took 'last week' as starting last Monday"]
    w = timeref.parse(low, now)
    if w is not None and w.start is not None:
        notes = [a[0].upper() + a[1:] for a in w.assumptions if "followed it for" not in a and "went up to now" not in a]
        start = w.start
        # timeref widens a clock time to a 2-hour stretch; the point we want is its START. For a clock time it is exactly T.
        if re.search(r"\d{1,2}[:.]\d{2}|\b\d{1,2}\s*(am|pm)\b", low):
            if start > now:
                start -= timedelta(days=1)
            return start, f"{timeref.hm(start)} Lagos time on {start.astimezone(LAGOS):%A %d %b}", notes
        return start, f"{start.astimezone(LAGOS):%A %d %b} 00:00 Lagos time" if start.astimezone(LAGOS).hour == 0 else f"{timeref.hm(start)} Lagos time", notes
    return None


# ----------------------------------------------------------------------
# UNDERSTANDING: concepts + slots -> an intent. Returns None when it isn't something this module understands (the rest of the bot gets it).
# ----------------------------------------------------------------------
_STATEMENT = re.compile(r"\b(i|we) (think|believe|expect|notice|noticed|reckon|suspect|feel like|see|saw|assume)\b|\bmy (view|take|idea|read|theory|opinion|bet)\b|\bimo\b|\bi'?m (feeling|seeing|noticing|getting) (bullish|bearish)\b|"
                        r"\b(feels|looks|seems) (heavy|off|weak|strong|bullish|bearish|stretched)\b|\b(tends|tend) to\b|\busually\b|\bso i expect\b", re.I)
_ASKS = re.compile(r"^\W*(how|what|what's|whats|where|which|when|why|is|are|am|do|does|did|can|could|will|should|tell|show|give|any)\b", re.I)



_AGO = re.compile(r"\bhow long (ago|since)\b|\bhow long (has|have) (it|that|this) been since\b|\bhow (many|much) (minutes?|mins?|hours?|hrs?|days?|time|candles|bars) (ago|since|has it been|have passed|has passed)\b|\btime since\b|\bbeen since\b|"
                  r"\bhow (old|recent|fresh|stale|current|up[- ]?to[- ]?date) (is|was|are|were)\b|\bwhen (was|did|is|were)\b.*\b(last|latest|previous|most recent|first)\b|\bwhen did\b|\bhow recently\b|\bwhat time (was|did)\b|\bago\b", re.I)
_DUR = re.compile(r"\bhow long (did|has|have|does|was|were|is|will|do|would|can)\b|\bfor how long\b|\bhow (many|much) (minutes?|mins?|hours?|hrs?|days?|weeks?|bars|candles|time) (did|has|have|does|was|were|is|will|do|would|can)\b|\b(lasted|lasting|duration)\b|\bhow (many|much) (minutes?|hours?|days?)\b.*\b(last|lasted|running|been|took|take)\b", re.I)
_UNTIL = re.compile(r"\b(how long|how much time|how many (minutes|hours|days))\b.*\b(until|till|before|to go|left|more|away)\b|\b(until|till) (the )?(london|ny|new york|market|overlap|session|scanner|close|open|asia|asian|monday|weekend)\b|\btime (left|remaining)\b|"
                    r"\bwhen (does|do|will|is) (the )?(london|ny|new york|market|markets|overlap|scanner|session|next|asia|asian|you)\b.*\b(open|opens|close|closes|start|starts|begin|begins|end|ends|run|runs|scan|scans|next)\b", re.I)
_CLOCKQ = re.compile(r"\bwhat('?s| is) the time\b|\bwhat time is it\b|\bwhat day is it\b|\bwhat('?s| is) (the )?(date|day)( today)?\b|\bwhat'?s today\b|\bis (the )?(forex |fx )?(market|markets) (open|closed)\b|\bare (we|the markets?) (open|closed)\b|"
                     r"\bis (it|the scanner|the bot) (open|running|scanning|awake|on)\b|\bare (you|u) (scanning|running|awake|on|active)\b|\bis it (the )?weekend\b", re.I)
_S_SIG = re.compile(r"\b(signal|signals|setup|setups|alert|alerts|fired|trade idea|entry)\b", re.I)
_S_SCAN = re.compile(r"\b(scan|scans|scanned|scanner|scanning|data|feed|candle|update|updated|reading|readings|price you have|quote)\b", re.I)
_S_EVENT = re.compile(r"\b(choch|change of character|bos|break of structure|fvg|fair value gap|order block|ob|structure|sweep|liquidity grab)\b", re.I)
_S_MSG = re.compile(r"\b(message|messaged|msg|texted|wrote|write|told|talked|spoke|chat|chatted|replied|answered|pinged|heard from)\b", re.I)
_S_TRADE = re.compile(r"\b(my trade|my last trade|trade i took|i took|i traded|i logged|i entered)\b", re.I)
_S_EXTREME = re.compile(r"\b(high|low|top|bottom|peak|trough|extreme)\b", re.I)
_S_MOVE = re.compile(r"\b(push|pushes|move|moves|rally|rallied|drop|dropped|spike|run|impulse|rise|rose|fall|fell|sell-?off|selloff|breakout|pump|dump|surge|slide|leg|swing|expansion|retrace\w*|pullback|decline|climb\w*|advance|plunge)\b", re.I)
_S_REGIME = re.compile(r"\b(bull|bear)(ish)? ?(market|run|cycle|phase|regime)\b|\b(bull|bear) (market|run)\b|\bbull ?run\b|\b(cycle|regime|secular)\b|\bmacro trend\b", re.I)
_S_LEG = re.compile(r"\b(current|this|the) (leg|trend|phase|campaign|bias|thesis|structure|bullish trend|bearish trend|uptrend|downtrend|expansion)\b|\b(bullish|bearish) (trend|leg|phase)\b|\b(uptrend|downtrend)\b", re.I)
_S_BAND = re.compile(r"\b(ranging|consolidat\w*|sideways|chop\w*|flat|range[- ]?bound|stuck|inside a|within)\b", re.I)


def _time_intent(low, cs, pr, since, n):
    if _CLOCKQ.search(low):
        return {"intent": "clock"}
    if _UNTIL.search(low) and not _S_SIG.search(low) and not _AGO.search(low):
        return {"intent": "until"}
    ago, dur = bool(_AGO.search(low)), bool(_DUR.search(low))
    if not (ago or dur):
        return None
    if _S_REGIME.search(low):
        return {"intent": "duration_regime"}
    if (ago or dur) and _S_SIG.search(low) and not _S_MOVE.search(low) and not re.search(r"\bhow many (signals|setups|alerts)\b(?! (ago|since))", low):
        return {"intent": "ago_signal", "which": "previous" if re.search(r"\b(previous|before (that|the last)|one before|second last|2nd last|prior)\b", low) else ("first" if re.search(r"\bfirst\b", low) else "last")}
    if _S_EVENT.search(low):
        return {"intent": "ago_event"}
    if _S_TRADE.search(low):
        return {"intent": "ago_trade"}
    if _S_MSG.search(low) and not _S_MOVE.search(low) and not _S_EXTREME.search(low):
        return {"intent": "ago_msg", "mine": bool(re.search(r"\b(did|have|has) (you|u)\b|\byou (last )?(message|write|wrote|text|reply|replied|answer|ping)", low)) and not re.search(r"\b(did|have) i\b|\bi (last )?(wrote|messaged|texted|said)\b", low)}
    if pr and ("price" in cs or re.search(r"\b(we|it|cable|gbpusd|gu)\b", low)) and re.search(r"\b(was|were|at|touch\w*|hit|traded|trading|there|above|below|reach\w*|see|saw)\b", low):
        return {"intent": "ago_level" if ago and not dur else "duration_side", "level": float(pr[0]), "side": "above" if re.search(r"\babove|over\b", low) else ("below" if re.search(r"\bbelow|under\b", low) else None)}
    if "ema" in cs and (dur or ago):
        return {"intent": "ema", "reclaim": False, "holding": False, "ema_len": None}
    if _S_SCAN.search(low) and not _S_MOVE.search(low) and not _S_EXTREME.search(low):
        return {"intent": "ago_scan"}
    if _S_EXTREME.search(low) and ago:
        return {"intent": "ago_extreme", "which": "low" if re.search(r"\b(low|bottom|trough)\b", low) and not re.search(r"\bhigh\b", low) else "high"}
    if _S_LEG.search(low) and (dur or ago):
        return {"intent": "duration_leg"}
    if _S_BAND.search(low):
        m = re.search(r"(\d+(?:\.\d+)?)\s*-?\s*pips?\b", low)
        return {"intent": "duration_band", "band": float(m.group(1)) if m else None}
    if _S_MOVE.search(low) and (dur or ago):
        return {"intent": "duration_swing", "words": low}
    if re.search(r"\b(trend|bullish|bearish|uptrend|downtrend|market)\b", low) and dur:
        return {"intent": "duration_leg"}
    return None


def _is_question_to_bot(text):
    low = (text or "").lower().strip()
    q = low.endswith("?") or re.match(r"^\W*(?:ok\W+|so\W+|and\W+|hmm\W+)?(?:what|what's|whats|why|how|how's|when|where|which|who|is|are|do|does|did|can|could|will|would|should|any|anything|tell me|show me|explain)\b", low)
    return bool(q) and not _PRICE_RX.search(low) and not re.search(r"\b(because|since|cos|coz|reckon|expect|i think|my reason)\b", low)


def understand(text, now, ctx=None):
    """-> dict(intent=..., slots...) or None.  `ctx` is the last chat context the bot stored (a follow-up may lean on it).
    While a conversation about a level or a feeling is open, a message that is not clearly some other question continues that conversation."""
    r = _understand(text, now, ctx)
    flow = ctx.get("flow") if isinstance(ctx, dict) and ctx.get("kind") == "chat" else None
    if flow == "level" and (r is None or r["intent"] in ("level_start", "feeling_start", "greet")) and _is_question_to_bot(text):
        return r                                   # a fresh question about something else is not part of the level talk
    if flow in ("level", "feeling") and (r is None or r["intent"] in ("level_start", "feeling_start", "greet")) and (text or "").strip() and not (text or "").lstrip().startswith("/"):
        base = r or {"tf": None, "ema_len": None, "since": None, "concepts": sorted(concepts(text)), "words": len((text or "").split())}
        return {**base, "intent": flow + "_turn", "flow_ctx": ctx}
    return r


_FILLER = re.compile(r"^\W*(?:hmm+|hm+|ok(?:ay)?|so|well|and|yeah|yep|but|then|oh|ah|alright|right|aight|ehh+|erm|um+|lol|haha|btw)\b[\s,.!-]*", re.I)


def _understand(text, now, ctx=None):
    for _ in range(3):                                   # "hmm and since friday" is "since friday"
        t2 = _FILLER.sub("", text or "", count=1)
        if t2 == (text or "") or not t2.strip():
            break
        text = t2
    t = re.sub(r"(\d)\.\s+(\d)", r"\1.\2", _fix_clock(text or "")).strip()          # the typo-cleaner can split "1.3350" into "1. 3350"
    low = t.lower()
    if not low or low.startswith("/"):
        return None
    low = re.sub(r"\be\.\s?m\.\s?a\b", "ema", low)
    cs = concepts(low)
    if re.search(r"\bmoving average", low):
        cs.add("ema")
    words = re.findall(r"[a-z']+", low)
    n = len(words)
    tf, span, since = slot_tf(low), slot_ema_len(low), slot_since(low, now)
    base = {"tf": tf, "ema_len": span, "since": since, "concepts": sorted(cs), "words": n}
    pr = _PRICE_RX.findall(low)

    # --- a STATEMENT of the user's own view is not a question for the records ("I think price tends to range...", "price is holding above the ema so I expect...")
    if _STATEMENT.search(low) and not _ASKS.search(low) and not low.rstrip().endswith("?") and not ("track" in cs and re.search(r"\b(want|need|can|could|please|pls)\b", low)):
        return None
    # --- SENSE OF TIME: what time/day is it, how long until, how long ago, how long did / has something lasted
    t_int = _time_intent(low, cs, pr, since, n)
    if t_int:
        return {**base, **t_int, "prices": pr}
    # --- questions ABOUT the bot's own behaviour and answers (must come before the level/price branches that share their words)
    if re.search(r"\b(are|r|do|will) (you|u) (watching|tracking|monitoring|following|keeping an eye|looking at)\b.*\b(it|that|this|level|1\.\d+|for me)\b|\b(are|r) (you|u) (watching|tracking|monitoring)\b", low):
        return {**base, "intent": "meta_watching", "prices": pr}
    if re.search(r"\b(did|have|has|will|do) (you|u) (save|saved|record|recorded|log|logged|note|noted|store|stored|add|added|remember|keep)\b|\bis (that|it|this) (saved|recorded|logged|stored)\b|\bwhat (did|have) (you|u) (save|saved|record|log)\b", low):
        return {**base, "intent": "meta_saved"}
    if re.search(r"\b(is|was) (that|this|it)\b.*\b(engine|yours|from you|your own)\b|\bwhere (did|does) (that|this|it) come from\b|\bhow did (you|u) (get|calculate|work out|compute|come up)\b|\bwho (calculated|computed|says)\b|\bwhat did the engine say\b|\b(engine'?s?|engine) (own|number|figure|reading|ema)\b|\bwhat('s| is) (the )?(basis|source)\b", low) and "level" not in cs:
        return {**base, "intent": "meta_source"}
    if re.search(r"\b(will|would|gonna|going to|is it going to|are we going to|do you think (it|price)|you think (it|price) (will|goes|go))\b", low) and re.search(r"\b(go|goes|rise|fall|drop|break|breaks|bounce|reverse|reach|hit|rally|continue|up|down|higher|lower|pump|dump|hold)\b", low) and not re.search(r"\b(since|ago|yesterday|last)\b", low):
        return {**base, "intent": "predict", "prices": pr}
    if len(pr) == 1 and "ema" not in cs and re.search(r"\bhow far\b|\bdistance\b|\bhow many pips\b|\bpips (from|away|to)\b", low) and re.search(r"\b(from|to) (here|now|where we are|current(ly)?|price|cable|gu)\b|\bfrom here\b|\baway\b", low):
        return {**base, "intent": "distance", "prices": pr}
    if len(pr) >= 2 and "ema" not in cs and re.search(r"\bpips?\b|\bhow far\b|\bdistance\b|\bdifference\b|\bgap\b|\bbetween\b|\bapart\b|\bhow (much|many)\b", low) and not re.search(r"\b(stop|target|entry|sl|tp|bought|sold|long|short)\b", low):
        return {**base, "intent": "pips_between", "prices": pr}
    if cs & {"price"} and "ema" not in cs and pr and re.search(r"\b(how far|distance|away from|far from|pips (from|away|to)|from (here|price|now)|to reach|until)\b", low):
        return {**base, "intent": "distance", "prices": pr}
    if re.search(r"\b(how many lots?|what lot|position size|lot size|size me|calculate .*size)\b", low) and (re.search(r"\bpips?\b", low) or "stop" in cs):
        return {**base, "intent": "lot", "prices": pr}
    if isinstance(ctx, dict) and ctx.get("kind") == "chat" and (ctx.get("topic") in ("change", "duration", "range", "price") or ctx.get("swing")) and \
       re.search(r"^\W*(?:is |was |so |that |that's |thats )*(?:that|this|it)(?:'s| is| was)? (?:a lot|a big|big|large|small|huge|much|tiny|normal|unusual|usual|a small|a large|a huge|significant)\b|\b(?:is|was) (?:that|this|it) (?:a lot|big|large|small|normal|unusual|usual|a big move)\b", low) and n <= 9:
        return {**base, "intent": "move_size", "flow_ctx": ctx}
    # --- about the bot itself: counts of its own records
    if ("count" in cs or re.search(r"\bhow (many|often|much)\b", low)) and cs & {"scan", "setup", "shadow", "trade", "alert", "case", "message", "reject"}:
        things = [x for x in ("scan", "setup", "shadow", "trade", "alert", "case", "message", "reject") if x in cs]
        return {**base, "intent": "count", "things": things, "asks_bot": bool(re.search(r"\b(did|do|have|has) (you|u)\b", low))}
    # --- lot size
    if ("lot" in cs and ("stop" in cs or re.search(r"\bpips?\b", low) or "risk" in low or "trade" in cs or n <= 6)) or \
       (re.search(r"\b(how big|how much|what size|how large)\b", low) and re.search(r"\b(trade|position|risk|size|lots?)\b", low) and (re.search(r"\bpips?\b", low) or "stop" in cs)):
        return {**base, "intent": "lot", "prices": pr}
    # --- a level to track
    if ("level" in cs or pr) and ("track" in cs or re.search(r"\bkeep an eye\b|\beye on\b", low)) and not re.search(r"\b(alert me|notify)\b", low):
        return {**base, "intent": "level_start", "prices": pr, "text": t}
    if re.search(r"\b(need|want) to talk\b|\bnot (in a good|feeling|doing) (place|good|great|well|okay|ok|myself)\b|\bhaving a (hard|rough|bad|tough|long) (day|time|week)\b|\bcan i (talk|vent)\b", low) and not cs & {"price", "change", "range", "market", "direction", "ema"}:
        return {**base, "intent": "feeling_start", "text": t}
    if "feel" in cs and (("advice" in cs) or re.search(r"\b(i|im|i'm|me|my|myself)\b", low)) and not cs & {"price", "change", "range", "market", "direction", "ema"} and not re.search(r"\b(bullish|bearish|heavy|light)\b", low):
        return {**base, "intent": "feeling_start", "text": t}
    if "advice" in cs and re.search(r"\b(i|me|my)\b", low) and n <= 8 and not cs & {"price", "market", "direction", "ema", "change"}:
        return {**base, "intent": "feeling_start", "text": t}
    if re.search(r"\b(any|latest|recent|what'?s the|whats the|got any) news\b|\bnews today\b|\beconomic (calendar|data|events?)\b|\bcalendar\b", low) and not pr:
        return {**base, "intent": "news"}
    if re.search(r"\b(show|send|give|gimme|let me see|can i see)( me)? (your|the|ur) (logs?|files?|records?|json|data ?base|database|internals?)\b", low):
        return {**base, "intent": "no_files"}
    if re.search(r"\bwhy\b.{0,24}\b(quiet|silent|no (signals?|alerts?|trades?|setups?)|haven'?t|hasn'?t|didn'?t|not (fire|firing|signal|send|alert|trade|trading)|nothing)\b", low) and not re.search(r"\b(fire|fired|take|took)\b.{0,8}\b(last|that|the)\b", low):
        return {**base, "intent": "why_quiet"}
    if re.search(r"\bshould i (worry|be worried|be concerned|panic|be scared)\b|\bdo you think i should (worry|panic)\b|\bam i (in trouble|in danger|screwed|cooked)\b", low):
        return {**base, "intent": "worry"}
    if re.search(r"\bcompare\b.{0,25}\b(today|yesterday|this week|last week)\b.{0,25}\b(and|with|to|vs|versus)\b.{0,15}\b(today|yesterday|last week|this week)\b|\b(today|yesterday|this week) (vs|versus|compared (to|with)) (today|yesterday|last week|this week)\b", low):
        return {**base, "intent": "compare_days"}
    if re.search(r"\b(i'?m|im|i am|feeling|feel|so|really|getting)\b.{0,15}\b(tired|fed up|sick|exhausted|drained|frustrated|stressed|annoyed|upset|discouraged|burnt out|burned out|over it)\b", low):
        return {**base, "intent": "feeling_start", "text": t}
    if len(pr) >= 2 and n <= 9 and not re.search(r"\b(stop|target|tp|sl|lot|lots|risk|alert|buy|sell|long|short)\b", low) and (re.search(r"\bfrom\b.{0,12}\bto\b|\bbetween\b|\band\b|\bto\b", low)):
        return {**base, "intent": "pips_between", "prices": [float(x) for x in pr]}
    if n <= 12 and re.search(r"\b(take a (break|rest|walk|breather)|going (to )?(rest|sleep|bed|offline)|go (rest|sleep|to bed|offline)|call it (a day|a night)|log(ging)? off|head(ing)? out|sign(ing)? off|bye|goodnight|good night|catch you later|talk later|ttyl|see you (later|tomorrow|soon))\b", low) and not pr and not re.search(r"\b(price|signal|trade|stop|alert|ema)\b", low):
        return {**base, "intent": "farewell"}
    if re.search(r"\b(normal|usual|typical|unusual|common|ordinary|standard|expected|average|big|large|huge|small|quiet|slow|strong|weak)\b", low) and re.search(r"\b(is|was|are|were|does|did)\b.{0,18}\b(that|this|it|today|the (?:push|move|range|session))\b|\b(that|this|it|today)\b.{0,12}\b(normal|usual|typical|unusual|common)\b", low) \
       and re.search(r"\b(london|new york|ny|asia\w*|overlap|friday|monday|tuesday|wednesday|thursday|today|this time|morning)\b", low) and not re.search(r"\bema\b|candles?|average|\bavg\b|\bmean\b", low):
        sess = next((k for k, rx in (("overlap", r"overlap"), ("london", r"london"), ("ny", r"new york|\bny\b"), ("asia", r"asia")) if re.search(rx, low)), None)
        return {**base, "intent": "normal", "session": sess}
    m_n = re.search(r"\b(over|above|under|below|beneath|on top of)\s+the\s+(\d{2,3})\b(?!\s*(pips?|%))", low)
    if m_n and (cs & {"price"} or re.search(r"\bwe\b|\bit\b", low)) and 5 <= int(m_n.group(2)) <= 500:
        return {**base, "intent": "ema", "ema_len": int(m_n.group(2)), "reclaim": False, "holding": False}
    # --- EMA relationship (above/below/relationship are the same question)
    if "ema" in cs and (cs & {"relation", "above", "price", "where", "direction"} or re.search(r"\b(reclaim\w*|hold\w*|cross\w*|touch\w*|broke|break|away|far|close)\b", low)):
        return {**base, "intent": "ema", "reclaim": bool(re.search(r"reclaim|cross|broke|break", low)), "holding": bool(re.search(r"\bhold\w*|\bstay\w*|\bsustain", low))}
    # --- change over a stretch (before range: "how much has price moved since X")
    if since and ("change" in cs or re.search(r"\bhow (much|far)\b", low) or "up or down" in low or re.search(r"\bpips?\b|\bgain\w*|\bloss\w*|\bwhat'?s the move\b|\bthe move\b", low)) and "range" not in cs and "lot" not in cs:
        return {**base, "intent": "change", "report": "report" in cs}
    if re.search(r"\bcandle size\b|\bhow big are (the|these)? ?candles\b|\bhow (big|large|tall) (is|are) (the )?(average|typical|usual)", low) or re.search(r"\bhow volatile\b", low):
        return {**base, "intent": "range"}
    if "range" in cs and ("average" in low or "avg" in low or "mean" in low or "typical" in low or re.search(r"\bper\b", low)):
        return {**base, "intent": "range"}
    if "range" in cs and (since or "price" in cs or "market" in cs):
        return {**base, "intent": "range"}
    if "report" in cs and (since or "market" in cs or "price" in cs or "happened" in low):
        return {**base, "intent": "change", "report": True}
    if since and ("price" in cs or "market" in cs) and ("change" in cs):
        return {**base, "intent": "change", "report": "report" in cs}
    # --- direction (the engine's call; the runner hands it to the BUS)
    if (re.search(r"\bup or down\b|\bwhich side is in control\b|\bin control\b|\bdirectional\w*|\btrending\b", low) and not since and not cs & {"change", "range", "count"}) or ("direction" in cs and cs & {"market", "price", "where", "bot"}) or ("direction" in cs and n <= 6) or \
       (re.search(r"\bwhich way\b|\bwhere (is|are|'s)\b.*\b(going|headed|heading|leaning)\b", low) and cs & {"price", "market"} and not cs & {"change", "range", "since"}):
        return {**base, "intent": "direction"}
    # --- price now
    if re.search(r"\bwhere (are we|is it|is cable|are we at|we at) (trading|sitting|at)\b|\bwhere are we trading\b", low) and not cs & {"change", "range", "ema", "since"}:
        return {**base, "intent": "price"}
    if ("price" in cs and n <= 8 and re.search(r"\bhow much (is|are)\b|\b(give|show|gimme|send)( me)?\b|\b(what|whats|wats|wat)('?s)?( is| are)?( the| its| gbpusd| gu| cable| gbp)*( current| latest| last| live)? (price|quote|rate)\b", low) and not cs & {"change", "range", "ema", "direction", "count", "lot", "level", "feel", "since"}) or ("where" in cs and "price" in cs) or (cs & {"price"} and re.search(r"\b(now|rn|currently|at|trading|sitting)\b", low) and n <= 8) or ("price" in cs and n <= 3 and not cs & {"change", "range", "ema", "direction", "count", "lot", "level", "feel"}):
        return {**base, "intent": "price"}
    # --- which market is it watching
    if "market" in cs and "track" in cs and ("bot" in cs or "you" in low or "u" in words) and "level" not in cs:
        return {**base, "intent": "which_market"}
    # --- how are you
    if re.search(r"\bhow('s| is| was) (your|ur) (day|night|morning|evening|week)\b|^\W*(you|u|ya) (good|ok|okay|alright|fine|well)\W*$|\beverything (ok|okay|good|fine|alright)\b.*\b(you|u|with you)\b|\bhow (you|u) been\b|\bhow are things\b|\b(are|r) (you|u) (alright|ok|okay|good|well|fine)\b|\bhow'?s it hanging\b", low):
        return {**base, "intent": "wellbeing", "greeted": False}
    if not cs & {"market", "price", "direction", "ema"} and ("wellbeing" in cs and "bot" in cs and n <= 8 and re.search(r"\b(how|hw|wah|what)\b", low)) or re.search(r"\bhow (are|r) (you|u|ya|yu)\b|\bhow (you|u|ya|yu) (dey|doing|been)\b|\bhow('s| is) it going\b|\bhow far (are|r) (you|u)\b|\bhow far\b(?!.{0,25}(?:\d\.\d|price|here|pips|away|is|are|did|has|have|was|will))", low):
        return {**base, "intent": "wellbeing", "greeted": "greet" in cs}
    if cs & {"greet"} and n <= 4:
        return {**base, "intent": "greet"}
    if ctx and ctx.get("kind") == "chat" and ctx.get("topic") == "band":
        m = re.search(r"(\d+(?:\.\d+)?)\s*-?\s*(?:pips?)?\b", low)
        if m and n <= 6:
            return {**base, "intent": "duration_band", "band": float(m.group(1))}
    # --- a short follow-up that only supplies a slot ("what about the 15m one?", "and since friday?")
    if isinstance(ctx, dict) and ctx.get("kind") == "chat" and ctx.get("topic") == "ema" and n <= 7 and re.search(r"^\W*(?:and )?(?:since when|for how long|how long(?: has (?:it|that|this|price))?(?: been)?|since|when did (?:it|that|this) (?:start|begin|cross)|how long)\W*(?:has (?:it|that|this|price) been (?:there|that way|like that))?\W*$", low):
        return {**base, "intent": "ema_since", "flow_ctx": ctx}
    if isinstance(ctx, dict) and ctx.get("kind") == "chat" and ctx.get("swing") and n <= 9 and re.search(r"\bhow (many pips|big|large|much|far|strong)\b|\bhow many\b.{0,12}\bpips\b|\bsize\b|\bpips\b", low) and not pr:
        return {**base, "intent": "swing_pips", "flow_ctx": ctx}
    if ctx and ctx.get("kind") == "chat" and ctx.get("topic") in ("ema", "change", "range", "price") and n <= 7 and (tf or span or since or "since" in cs):
        return {**base, "intent": ctx["topic"], "followup": True, "report": bool(ctx.get("report")), "reclaim": False, "holding": False,
                "tf": tf or ctx.get("tf"), "ema_len": span or ctx.get("ema_len"), "since": since or None}
    return None


# ----------------------------------------------------------------------
# NUMBERS FROM RECORDED CANDLES  (pure; candles = list of dicts t(datetime utc), o, h, l, c sorted by t)
# ----------------------------------------------------------------------
def pips(x):
    return x / PIP


def p5(x):
    return f"{x:.5f}"


def pp(x, signed=False):
    s = f"{abs(x):.1f}"
    return (("+" if x >= 0 else "-") + s) if signed else s


def lagos_hm(dt):
    return f"{dt.astimezone(LAGOS):%H:%M}"


def lagos_when(dt, now):
    l, n = dt.astimezone(LAGOS), now.astimezone(LAGOS)
    if l.date() == n.date():
        return f"{l:%H:%M} today"
    if l.date() == (n - timedelta(days=1)).date():
        return f"{l:%H:%M} yesterday"
    return f"{l:%H:%M} on {l:%a %d %b}"


def ema_series(closes, span):
    """Standard EMA (same recursion as TradingView): seed = first close, alpha = 2/(span+1)."""
    a = 2.0 / (span + 1)
    out, e = [], None
    for c in closes:
        e = c if e is None else a * c + (1 - a) * e
        out.append(e)
    return out


def last_cross(candles, emas):
    """-> (index of the first candle on the current side of the EMA, side) or (None, side)."""
    side = lambda i: candles[i]["c"] > emas[i]
    i = len(candles) - 1
    cur = side(i)
    while i > 0 and side(i - 1) == cur:
        i -= 1
    return (i if i > 0 else None), cur


def window(candles, start, end):
    return [c for c in candles if start <= c["t"] < end]


def candle_at(candles, tf, start):
    """The candle that CONTAINS `start`, else the first one after it (-> (candle, exact))."""
    step = timedelta(minutes=TF_MIN[tf])
    for c in candles:
        if c["t"] <= start < c["t"] + step:
            return c, True
        if c["t"] > start:
            return c, False
    return None, False


def gaps(candles, tf, start, end):
    """Stretches inside the window with no candles, EXCLUDING the normal weekend close (Fri 21:00 UTC -> Sun 22:00 UTC)."""
    step = timedelta(minutes=TF_MIN[tf])
    out = []
    ws = window(candles, start, end)
    for a, b in zip(ws, ws[1:]):
        if b["t"] - a["t"] > step * 1.5:
            gap_start, gap_end = a["t"] + step, b["t"]
            wk = (a["t"].weekday() == 4 and a["t"].hour >= 21) or a["t"].weekday() in (5, 6) or (b["t"].weekday() == 6 and b["t"].hour <= 23 and a["t"].weekday() >= 4)
            if not wk:
                out.append((gap_start, gap_end))
    return out


def pick_tf(span, have, want=None):
    """Which candle size to use for a window of this length. `have` is the dict tf -> candles. The user's own choice wins if we have it."""
    if want in have and have[want]:
        return want
    order = ["5m"] if span <= timedelta(hours=3) else (["15m", "5m"] if span <= timedelta(days=2) else ["1h", "15m"])
    for tf in order + ["1h", "15m", "5m"]:
        if have.get(tf):
            return tf
    return None


def _age_note(last_t, tf, now):
    close_t = last_t + timedelta(minutes=TF_MIN[tf])
    age = now - close_t
    if age <= timedelta(minutes=20):
        return ""
    mins = int(age.total_seconds() // 60)
    ago = f"{mins} minutes" if mins < 120 else (f"{mins // 60} hours" if mins < 2880 else f"{mins // 1440} days")
    return (f" That's the latest I have, from about {ago} ago. The scanner only runs 08:00 to 16:55 Lagos time, Monday to Friday, "
            "so outside those hours I'm reading my last record, not a live quote.")


# ----------------------------------------------------------------------
# VOICE: glue only (no digits, no facts). Seeded so a retry says the same thing and neighbours don't sound templated.
# ----------------------------------------------------------------------
def pick(options, seed):
    s = str(seed)
    return options[(abs(int(s)) if s.lstrip("-").isdigit() else sum(map(ord, s))) % len(options)]


GLUE = {
    "price": ("Last price I have for GBPUSD is", "GBPUSD is sitting at", "Price is at", "Right now I have GBPUSD at"),
    "price_q": ("Want me to compare it with the EMA?", "Want to see how far it's moved today?", "Should I check where it is against the EMA?", "Want the move since a certain time?"),
    "greet": ("Hey, good to hear from you.", "Hey!", "Hi, I'm here."),
    "well": ("Doing well, thanks for asking.", "I'm good, scanning away.", "All good on my side.", "Can't complain, I'm running fine."),
    "well_q": ("How are you doing?", "And you, how's your day going?", "What's on your mind?", "How are you?"),
    "ema_q": ("Want me to check it on another timeframe?", "Want to look at how long it's been on that side?", "Should I check another EMA length?"),
    "change_q": ("Want me to walk you through how it got there?", "Want a proper write-up of that stretch?", "Should we go through the biggest move in it?"),
    "range_q": ("Want it on a different candle size?", "Want me to compare it with a quieter stretch?", "Should I split it by day?"),
    "which": ("I only watch GBPUSD. That's the one market the scanner covers.", "GBPUSD, only that one. It's the single pair the scanner covers."),
    "unclear": ("I want to get this right rather than guess.", "Before I answer, one thing I need from you.", "Quick question first so I don't guess."),
}


# ----------------------------------------------------------------------
# ANSWERS (each returns (text, context_to_store))
# ----------------------------------------------------------------------
def a_price(cs, now, seed, sig=None):
    tf = next((t for t in ("5m", "15m", "1h") if cs.get(t)), None)
    if not tf:
        return "I don't have any recorded candles to read a price from right now, so I can't tell you where price is. I won't guess.", None
    c = cs[tf][-1]
    body = f"{pick(GLUE['price'], seed)} {p5(c['c'])}, from the {TF_WORDS[tf]} candle that opened at {lagos_hm(c['t'])} Lagos time.{_age_note(c['t'], tf, now)}"
    day = [x for x in cs[tf] if x["t"].astimezone(LAGOS).date() == c["t"].astimezone(LAGOS).date()]
    if day:
        o, hi, lo = day[0]["o"], max(x["h"] for x in day), min(x["l"] for x in day)
        body += f" Since the day started in my records ({lagos_hm(day[0]['t'])}) it has moved {pp(pips(c['c'] - o), True)} pips, between {p5(lo)} and {p5(hi)}."
    return body + " " + pick(GLUE["price_q"], seed), {"kind": "chat", "topic": "price"}


def a_ema(cs, now, seed, tf=None, span=None, reclaim=False, holding=False):
    tf = tf if tf in TF_MIN else "1h"
    span = span or DEFAULT_EMA
    if tf not in TF_MIN or not cs.get(tf):
        return f"I only keep 5-minute, 15-minute and hourly candles, so I can't work out an EMA on that timeframe. I can do any of those three.", None
    cand = cs[tf]
    if len(cand) < span:
        return f"I only have {len(cand)} {TF_WORDS[tf]} candles recorded, which isn't enough to calculate a {span}-period EMA properly, so I won't give you a number.", None
    closes = [c["c"] for c in cand]
    em = ema_series(closes, span)
    d = pips(closes[-1] - em[-1])
    side = "above" if d > 0 else "below"
    idx, cur = last_cross(cand, em)
    lead = f"On the {TF_WORDS[tf]} chart, price (last completed candle close {p5(closes[-1])}) is {pp(abs(d))} pips {side} the {span} EMA ({p5(em[-1])})."
    if idx is None:
        hist = f" It hasn't crossed it in all the {len(cand)} candles I have, so it has been on that side the whole recorded stretch."
    else:
        n_since = len(cand) - idx
        hist = (f" It has been on that side since the candle at {lagos_when(cand[idx]['t'], now)}, which is {n_since} {TF_WORDS[tf]} candle{'s' if n_since != 1 else ''} counting the latest "
                f"(it crossed the EMA at that candle's close).")
    extra = ""
    if reclaim:
        extra = " That's the last crossing I can see; whether it counts as a real reclaim is a judgement about what came before it, which these numbers alone don't tell."
    elif holding:
        extra = " Holding means staying there over time, so the stretch above is the evidence I have: it says how long, not whether it will keep holding."
    note = (f" Worth knowing: this is my own EMA, worked out from the {len(cand)} recorded {TF_WORDS[tf]} closes the way a chart does it. "
            "It is not the engine's own reading, and the two can differ by a few pips." )
    return lead + hist + extra + note + " " + pick(GLUE["ema_q"], seed), {"kind": "chat", "topic": "ema", "tf": tf, "ema_len": span, "ema_side": side, "ema_since_t": (cand[idx]["t"].isoformat() if idx is not None else None), "ema_candles": (len(cand) - idx) if idx is not None else None}


def a_ema_since(ctx, now, seed):
    """'since when?' after an EMA answer: the same fact, said as a duration, not the whole answer again."""
    t0 = ctx.get("ema_since_t")
    tf, span, side = ctx.get("tf", "1h"), ctx.get("ema_len", DEFAULT_EMA), ctx.get("ema_side", "above")
    if not t0:
        return (f"It hasn't crossed the {span} EMA on the {TF_WORDS[tf]} chart anywhere in the candles I have, so it has been {side} it for the whole stretch I can see.", ctx)
    t = datetime.fromisoformat(t0)
    n = ctx.get("ema_candles")
    ch = closed_hours(t, now)
    return (f"Price has been {side} the {span} EMA on the {TF_WORDS[tf]} chart since the candle at {lagos_when(t, now)}: {humanize(now - t)} ago"
            + (f" (about {round(ch)} of those hours the market was closed for the weekend)" if ch >= 1 else "") + f", {n} {TF_WORDS[tf]} candles. That's my own EMA from recorded closes, not the engine's.", ctx)


SESSIONS = {"london": ("London", 7, 16), "ny": ("New York", 12, 21), "overlap": ("London/New York overlap", 12, 16), "asia": ("Asian", 21, 7), None: ("day", 0, 24)}


def a_move_size(cs, ctx, now, seed):
    """'Is that a lot?' after a move: the move set against every same-length stretch in the bot's own hourly candles."""
    c1 = cs.get("1h") or []
    if ctx.get("swing"):
        w = ctx["swing"]; net = abs(w["pips"]); h = max(1, int(round(w.get("span_h") or 1))); what = "that swing"
    elif ctx.get("start") and ctx.get("tf") and cs.get(ctx["tf"]):
        from datetime import datetime as _dt
        st = _dt.fromisoformat(ctx["start"]); s = stats(cs, ctx["tf"], st, now)
        if not s:
            return "I can't re-measure that move to compare it, sorry.", ctx
        net = abs(pips(s["last"] - s["ref"])); h = max(1, int(round((now - st).total_seconds() / 3600))); what = "that move"
    else:
        return "Which move do you mean? Ask me how far price has gone over a stretch and I'll tell you if it's big.", None
    h = min(h, 72)
    nets = []
    for i in range(0, len(c1) - h):
        if c1[i + h - 1]["t"] - c1[i]["t"] == timedelta(hours=h - 1):
            nets.append(abs(pips(c1[i + h - 1]["c"] - c1[i]["o"])))
    if len(nets) < 40:
        return f"I only have {len(nets)} comparable {h}-hour stretches recorded, too few to say whether {what} is big. I won't guess.", ctx
    below = sum(1 for x in nets if x < net) / len(nets)
    med = sorted(nets)[len(nets) // 2]
    verdict = ("smaller than most" if below < 0.25 else "about normal" if below < 0.75 else "bigger than usual" if below < 0.9 else "one of the bigger ones")
    return (pick((f"Against the {len(nets)} other {h}-hour stretches in my records, {what} ({pp(net)}p) is {verdict}: it beat {below * 100:.0f}% of them, and the typical one moved about {pp(med)}p.",
                  f"{what.capitalize()} was {pp(net)}p. Over {h} hours that's {verdict} for my records: the typical {h}-hour stretch moved about {pp(med)}p, and {what} was bigger than {below * 100:.0f}% of {len(nets)} stretches."), seed)
            + " That's only my recorded hourly candles, so it says nothing about why."), ctx


def a_news(seed):
    return pick(("I don't have a news or economic-calendar feed, so I can't tell you what's scheduled or what just came out. What I can see is whether price has moved sharply lately: ask me if it's jumped in the last few hours and I'll check the candles.",
                 "News isn't something I can see: I only have price candles and what the engine records. If something big dropped, it would show up as a jump in price, and I can check for that: just ask me how big the biggest recent move was."), seed), {"kind": "chat", "topic": "news"}


def a_no_files(seed):
    return pick(("I keep my records to myself rather than handing them out, but I can tell you what's in them. What do you want to know: signals, how a session went, something you told me before?",
                 "I'd rather not dump my raw records on you, they're not much fun to read. Tell me what you're after and I'll pull it out in plain words."), seed), {"kind": "chat", "topic": "records"}


def a_worry(seed):
    return pick(("I can't tell you whether to worry, because that depends on what you're holding and what your plan says. If something's bothering you, tell me what it is and I'll go through it with you. I can also tell you what the engine currently flags as cautions.",
                 "Hard for me to say without knowing what's on your mind. Is it a position you're in, or the market in general? Tell me more and I'll help you think it through, without pretending to know where price goes."), seed), {"kind": "chat", "flow": "feeling", "stage": "talking", "theme": "worry"}


def a_compare_days(cs, now, seed):
    c1 = cs.get("1h")
    if not c1:
        return "I don't have hourly candles recorded, so I can't compare the days.", None
    nl = now.astimezone(LAGOS)
    d0 = nl.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    for k, name in ((0, "today"), (1, "yesterday")):
        a = d0 - timedelta(days=k); b = a + timedelta(days=1)
        ws = [c for c in c1 if a <= c["t"].astimezone(LAGOS) < b]
        if not ws:
            return f"I have no hourly candles for {name}, so I can't compare.", None
        o, cl = ws[0]["o"], ws[-1]["c"]; hi = max(c["h"] for c in ws); lo = min(c["l"] for c in ws)
        rows.append((name, o, cl, hi, lo, pips(cl - o), pips(hi - lo), ws[-1]["t"], len(ws)))
    t, y = rows
    def line(r):
        return f"{r[0].capitalize()}: {p5(r[1])} to {p5(r[2])} {pb(r[5])}, {'up' if r[5] > 0 else 'down' if r[5] < 0 else 'flat'}, with a low-to-high range of {pp(r[6])}p ({r[8]} hourly candles)."
    bigger = "today" if t[6] > y[6] else "yesterday"
    part = " Today isn't over, so that's the range so far." if (now - t[7]) < timedelta(hours=24) and nl.hour < 23 else ""
    return line(t) + "\n" + line(y) + f"\nSo {bigger} has had the wider range." + part + " Measured on my recorded hourly candles.", {"kind": "chat", "topic": "compare"}


def a_why_quiet(last_t, now, seed):
    when = f" The last signal it did fire was {lagos_when(last_t, now)}." if last_t else ""
    return ("I can't see why the engine passed on anything. It only records the signals it fires, not the setups it looked at and rejected, so a quiet stretch has no reason attached." + when +
            " What I can show you is what it's cautious about right now, which often explains a lack of setups: ask me what it's cautious about and I'll go through it."), {"kind": "chat", "topic": "quiet"}


def a_farewell(seed):
    opts = ["Good call. Stepping away after a rough patch is a real part of the job, not a weakness. I'll be here when you're back.",
            "Sounds right. Rest well, and we pick it up whenever you want.",
            "Take it easy. I'll keep the records, and the history will all be here when you're back."]
    return opts[(seed or 0) % len(opts)], None


def a_normal(cs, now, session):
    """'Is that normal for London?' answered with the bot's own records: today's session range so far against the same stretch of earlier sessions."""
    cand = cs.get("1h")
    if not cand or len(cand) < 48:
        return "I don't have enough hourly candles recorded to compare against, so I can't say what's normal. I won't guess.", None
    name, h0, h1 = SESSIONS.get(session, SESSIONS[None])
    dur = (h1 - h0) % 24 or 24
    nu = now.astimezone(UTC)
    day = nu.replace(hour=0, minute=0, second=0, microsecond=0)
    start = day + timedelta(hours=h0)
    if start > nu:
        if session:
            return f"The {name} session hasn't opened yet today (it opens at {lagos_hm(start)} Lagos time), so there's no range to compare. Ask me again once it's under way.", None
        start -= timedelta(days=1)
    end_full = start + timedelta(hours=dur)
    cur_end = min(nu, end_full)
    elapsed = cur_end - start
    hrs = int(elapsed.total_seconds() // 3600)
    if hrs < 1:
        return f"The {name} session has barely started, so there's nothing to compare yet.", None
    live = nu < end_full

    def rng(st):
        ws = [c for c in cand if st <= c["t"] < st + timedelta(hours=hrs)]
        if len(ws) < max(1, int(hrs * 0.9)):
            return None
        return pips(max(c["h"] for c in ws) - min(c["l"] for c in ws))
    cur = rng(start)
    if cur is None:
        return f"I don't have the hourly candles for this {name} session, so I can't measure it.", None
    past = []
    for k in range(1, 90):
        st = start - timedelta(days=k)
        if st.weekday() >= 5 or (st + timedelta(hours=hrs)).weekday() >= 5 and h0 < h1:
            continue
        r = rng(st)
        if r is not None:
            past.append(r)
    if len(past) < 5:
        return f"This {name} session has covered {pp(cur)} pips from low to high so far, but I only have {len(past)} comparable earlier sessions, too few to call anything normal.", None
    ps = sorted(past)
    q = lambda f: ps[min(len(ps) - 1, int(f * (len(ps) - 1) + 0.5))]
    q1, med, q3 = q(0.25), q(0.5), q(0.75)
    bigger = sum(1 for x in past if x > cur)
    verdict = "below" if cur < q1 else ("above" if cur > q3 else "within")
    word = {"below": "on the quiet side", "above": "bigger than usual", "within": "inside the usual range"}[verdict]
    return (f"{'So far, ' if live else ''}{('today has' if session is None else 'this ' + name + ' session has')} covered {pp(cur)} pips from low to high ({lagos_hm(start)} to {lagos_hm(cur_end)} Lagos time). "
            f"For the same stretch of the {len(past)} earlier {('days' if session is None else name + ' sessions')} I have, the middle half ran from {pp(q1)} to {pp(q3)} pips (typical {pp(med)}; smallest {pp(ps[0])}, largest {pp(ps[-1])}). "
            f"So {pp(cur)} is {word}: {bigger} of those {len(past)} were bigger. That's 'normal' only against my {len(past)} recorded days, measured on hourly candles; it says nothing about why, and a news day can sit anywhere in that range."), {"kind": "chat", "topic": "normal"}


def stats(cs, tf, start, end):
    cand = cs[tf]
    ws = window(cand, start, end)
    if not ws:
        return None
    first, exact = candle_at(cand, tf, start)
    ref = first["o"] if first else ws[0]["o"]
    last = ws[-1]
    hi = max(ws, key=lambda x: x["h"])
    lo = min(ws, key=lambda x: x["l"])
    rng = [pips(x["h"] - x["l"]) for x in ws]
    return {"ref": ref, "ref_t": first["t"] if first else ws[0]["t"], "exact": exact, "last": last["c"], "last_t": last["t"], "hi": hi["h"], "hi_t": hi["t"],
            "lo": lo["l"], "lo_t": lo["t"], "n": len(ws), "avg_candle": sum(rng) / len(rng), "biggest": max(ws, key=lambda x: x["h"] - x["l"]), "ws": ws}


def _coverage_note(cs, tf, start, now):
    first = cs[tf][0]["t"]
    if start < first:
        return f" (My {TF_WORDS[tf]} records only begin on {first.astimezone(LAGOS):%a %d %b at %H:%M} Lagos time, so I measured from there, not from the time you named.)"
    return ""


def pb(x, sign=False):
    """Pips in brackets, the way Nelly reads them: (181p), (0.8p), (+12p). Whole numbers lose the '.0'."""
    v = abs(x)
    t = f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:.1f}"
    return "(" + (("+" if x >= 0 else "-") if sign else "") + t + "p)"


def _push_word(cs, net_p, start, now):
    """A rough plain-words size for a move, judged against how big the bot's own recorded hourly candles usually are over the same number of hours."""
    h = max(1.0, (now - start).total_seconds() / 3600)
    c1 = cs.get("1h") or []
    rs = [pips(c["h"] - c["l"]) for c in c1[-300:]]
    if len(rs) < 24:
        return "move"
    avg = sum(rs) / len(rs)
    r = abs(net_p) / (avg * h ** 0.5)
    return "small drift" if r < 0.5 else ("modest move" if r < 1.2 else ("solid push" if r < 2.2 else "big push"))


def signed(x):
    return ("+" if x > 0 else ("-" if x < 0 else "")) + pp(abs(x))


def a_change(cs, now, seed, since, tf=None, report=False):
    if not since:
        return ("Since when? Give me a time or a day, like 'since yesterday', 'since Friday', 'since 8:56' or 'the last 3 hours', and I'll work out how far price has moved.", None)
    start, label, notes = since
    tf = pick_tf(now - start, cs, tf)
    if not tf:
        return "I have no recorded candles to measure from, so I can't work this out.", None
    s = stats(cs, tf, start, now)
    if not s:
        return f"I have no {TF_WORDS[tf]} candles recorded between {label} and now, so I can't measure it. I won't guess.", None
    net = pips(s["last"] - s["ref"])
    pct = (s["last"] - s["ref"]) / s["ref"] * 100
    direction = "up" if net > 0 else ("down" if net < 0 else "flat")
    sg = "+" if net > 0 else ("-" if net < 0 else "")
    k = seed if isinstance(seed, int) else 0
    sl = ('over ' + label) if label.startswith('the last') else ('since ' + label)
    sl_c = sl[0].upper() + sl[1:]
    word = _push_word(cs, net, start, now)
    term = "short-term" if (now - start) <= timedelta(days=1) else "multi-day"
    up = direction == "up"
    if direction == "flat":
        head = pick((f"{sl_c} price has basically gone nowhere: {p5(s['ref'])} then, {p5(s['last'])} now {pb(net)}.",
                     f"Not much to report {sl}: it started at {p5(s['ref'])} and sits at {p5(s['last'])} {pb(net)}."), k)
    else:
        mv = "upward" if up else "downward"
        head = pick((
            f"{sl_c}, price went from {p5(s['ref'])} to {p5(s['last'])} {pb(net)}. That's a {word} {mv} on the {term} view, a change of {abs(pct):.2f}%.",
            f"Price is at {p5(s['last'])} now, {'up' if up else 'down'} from {p5(s['ref'])} {pb(net)} {sl}: a {term} {word} {mv}, about {abs(pct):.2f}%.",
            f"It's gone from {p5(s['ref'])} to {p5(s['last'])} {sl} {pb(net)}, which reads as a {word} {mv} ({abs(pct):.2f}%).",
            f"From {p5(s['ref'])} to {p5(s['last'])} {pb(net)} {sl}. A {word} {mv}, {abs(pct):.2f}% on the {term} view."), k)
        head += f" (Latest candle: {lagos_when(s['last_t'], now)}.)"
    hi_d, lo_d = pips(s["hi"] - s["ref"]), pips(s["lo"] - s["ref"])
    path = pick((
        f" On the way it touched a high of {p5(s['hi'])} {pb(hi_d, True)} at {lagos_when(s['hi_t'], now)} and a low of {p5(s['lo'])} {pb(lo_d, True)} at {lagos_when(s['lo_t'], now)}, so the full swing from low to high was {pp(pips(s['hi'] - s['lo']))}p. That's different from the net move, because it's the distance it actually covered.",
        f" The high was {p5(s['hi'])} {pb(hi_d, True)} at {lagos_when(s['hi_t'], now)} and the low {p5(s['lo'])} {pb(lo_d, True)} at {lagos_when(s['lo_t'], now)}. Low to high that's {pp(pips(s['hi'] - s['lo']))}p, which is not the same as the net move.",
        f" It ranged from {p5(s['lo'])} {pb(lo_d, True)} at {lagos_when(s['lo_t'], now)} up to {p5(s['hi'])} {pb(hi_d, True)} at {lagos_when(s['hi_t'], now)}: {pp(pips(s['hi'] - s['lo']))}p between the extremes, which isn't the same thing as the net move."), k + 1)
    cov = _coverage_note(cs, tf, start, now) + _age_note(s["last_t"], tf, now)
    assume = (" " + "; ".join(notes) + ".") if notes else ""
    if not report:
        return head + path + cov + assume + " " + pick(GLUE["change_q"], seed), {"kind": "chat", "topic": "change", "tf": tf, "start": start.isoformat(), "report": False}
    return report_text(cs, tf, s, start, label, now, head, path, cov, assume), {"kind": "chat", "topic": "change", "tf": tf, "start": start.isoformat(), "report": True}


def report_text(cs, tf, s, start, label, now, head, path, cov, assume):
    out = [f"Alright, let's go through GBPUSD since {label} together.", "", "**The headline**", head, "", "**The path**", path.strip()]
    # the single biggest hour (always from hourly candles when we have them: comparable between reports)
    big_tf = "1h" if cs.get("1h") else tf
    ws = window(cs[big_tf], start, now)
    if ws:
        b = max(ws, key=lambda x: abs(x["c"] - x["o"]))
        move = pips(b["c"] - b["o"])
        out += ["", "**The move that stands out**",
                f"The biggest single {TF_WORDS[big_tf]} candle body in this stretch was the one at {lagos_when(b['t'], now)}: {pp(abs(move))} pips {'up' if move > 0 else 'down'} "
                f"({p5(b['o'])} to {p5(b['c'])}). I can see that it happened and how big it was; the records don't say why, and I won't guess a reason from a timestamp."]
    # weekend / big overnight gaps
    cand = cs[tf]
    ws2 = window(cand, start, now)
    jumps = []
    step = timedelta(minutes=TF_MIN[tf])
    for a, b in zip(ws2, ws2[1:]):
        if b["t"] - a["t"] > step * 1.5:
            jumps.append((a, b, pips(b["o"] - a["c"])))
    wk = [j for j in jumps if abs(j[2]) >= 5][:3]
    if wk:
        out += ["", "**Gaps and jumps**"]
        for a, b, g in wk:
            out.append(f"Between {lagos_when(a['t'], now)} and {lagos_when(b['t'], now)} there's a break in the candles, and price reopened {pp(abs(g))} pips {'higher' if g > 0 else 'lower'} than it last closed. "
                       "A break like that is usually the weekend or a pause in the feed; I can't tell which from the candles alone.")
    gp = gaps(cand, tf, start, now)
    out += ["", "**What I can't see**"]
    if gp:
        longest = max(gp, key=lambda g: g[1] - g[0])
        hrs = (longest[1] - longest[0]).total_seconds() / 3600
        out.append(f"There are {len(gp)} stretch{'es' if len(gp) != 1 else ''} inside this window where I have no {TF_WORDS[tf]} candles (the longest is about {hrs:.1f} hours from {lagos_when(longest[0], now)}), "
                   "so anything that happened in those gaps isn't in these numbers.")
    else:
        out.append(f"I found no holes in my {TF_WORDS[tf]} candles for this stretch ({s['n']} candles), apart from normal weekend closes.")
    if cov.strip():
        out.append(cov.strip())
    if assume.strip():
        out.append(assume.strip())
    out += ["", "Want me to dig into the biggest move, or lay out what the engine itself recorded (structure changes, signals) in this stretch?"]
    return "\n".join(out)


def a_range(cs, now, seed, since, tf=None):
    if not since:
        return (f"{pick(GLUE['unclear'], seed)} Average range since when? Give me a time or day ('since 8:56', 'since Friday', 'the last 3 hours'). "
                "Unless you say otherwise I'll use the average candle range (high minus low) in pips, on the candle size that suits the stretch.", {"kind": "chat", "topic": "range"})
    start, label, notes = since
    tf = pick_tf(now - start, cs, tf)
    if not tf:
        return "I have no recorded candles to measure from.", None
    s = stats(cs, tf, start, now)
    if not s:
        return f"I have no {TF_WORDS[tf]} candles between {label} and now, so I can't work out a range. I won't guess.", None
    body = (f"Since {label}, the average {TF_WORDS[tf]} candle range (high minus low) is {pp(s['avg_candle'])} pips, across {s['n']} candles. "
            f"The largest single candle was {pp(pips(s['biggest']['h'] - s['biggest']['l']))} pips (the one at {lagos_when(s['biggest']['t'], now)}).")
    days = defaultdict(list)
    for c in s["ws"]:
        days[c["t"].astimezone(LAGOS).date()].append(c)
    full = {d: v for d, v in days.items() if len(v) >= (0.9 * 1440 / TF_MIN[tf]) and d.weekday() < 5}
    if len(full) >= 2:
        dr = [pips(max(x["h"] for x in v) - min(x["l"] for x in v)) for v in full.values()]
        body += f" Across the {len(full)} complete weekdays in that stretch, the average full-day range (day's high minus low) was {pp(sum(dr) / len(dr))} pips."
    body += (" Candle size matters a lot here: an average hourly candle is much bigger than an average 5-minute one, so if you meant a different size, tell me."
             + _coverage_note(cs, tf, start, now) + _age_note(s["last_t"], tf, now))
    if notes:
        body += " " + "; ".join(notes) + "."
    return body + " " + pick(GLUE["range_q"], seed), {"kind": "chat", "topic": "range", "tf": tf, "start": start.isoformat()}


# --- the bot's own records ---------------------------------------------------
def a_count(src, now, seed, things, since, asks_bot=False):
    """src: dict of record lists (see load_counts_source). Counts keep their definitions apart: a scan is not a setup, a setup is not a trade you took."""
    if since:
        start, label, notes = since
        span = f"since {label}"
    else:
        nl = now.astimezone(LAGOS)
        start = nl.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        label, notes, span = "today", [], "today (since 00:00 Lagos time)"
    inwin = lambda ts: [t for t in ts if t is not None and start <= t <= now]
    lines = []
    first = lambda ts: min(ts) if ts else None
    want = set(things) or {"scan", "setup"}
    if "reject" in want:
        want.discard("setup")
        n = len(inwin(src.get("rejected", [])))
        lines.append(f"Setups the live engine turned down that I follow up afterwards (the 'rejected live' shadow experiments): {n} {span}. That only counts ones that have already finished playing out; "
                     "I don't keep a record of every candidate the engine looked at and dismissed before that point, so treat it as a floor, not the full number.")
    if "scan" in want:
        ts = src.get("scans", [])
        n = len(inwin(ts))
        lines.append(f"Scans: {n} {span}. (A scan is one run of the scanner that wrote its time in the scan record; my records start {first(ts).astimezone(LAGOS):%a %d %b}.)" if ts
                     else "Scans: I have no scan record to count from.")
        if ts and n == 0:
            lines[-1] += " Zero is real here if the scanner wasn't running: it only runs 08:00 to 16:55 Lagos time, Monday to Friday."
    if "setup" in want:
        sigs = src.get("signals", [])
        n = len(inwin([s["t"] for s in sigs]))
        lines.append(f"Signals that fired and passed the risk gate: {n} {span}." + (f" (Across everything recorded, from {first([s['t'] for s in sigs]).astimezone(LAGOS):%a %d %b}, that's {len(sigs)}.)" if sigs else " I have none recorded at all."))
        ann = len(inwin(src.get("announced", [])))
        lines.append(f"Signal alerts I have a record of sending you: {ann} {span}.")
        lines.append("I don't keep a separate record of setups the engine looked at and dropped before firing, so 'setups' here means fired signals only."
                     + (f" The engine also opened {len(inwin(src.get('shadow', [])))} shadow experiments {span}; those are quiet tests, not signals." if src.get("shadow") else ""))
    if "shadow" in want and "setup" not in want:
        lines.append(f"Shadow experiments (tests the bot runs quietly, not signals): {len(inwin(src.get('shadow', [])))} opened {span}.")
    if "trade" in want:
        lines.append(("I don't place trades myself, I only send signals. " if asks_bot else "") + f"Trades you logged as yours: {len(inwin(src.get('my_trades', [])))} {span}. Bot signals and your own trades are counted separately; a signal only becomes a trade of yours when you tell me you took it.")
    if "alert" in want and "setup" not in want:
        lines.append(f"Messages I sent you on my own (signal alerts and announcements): {len(inwin(src.get('announced', [])))} {span}.")
    if "case" in want:
        lines.append(f"Failure/loss analysis cases the engine opened: {len(inwin(src.get('cases', [])))} {span}.")
    if "message" in want:
        lines.append(f"Messages you've sent me: {len(inwin(src.get('received', [])))} {span}.")
    body = "\n".join(lines)
    if notes:
        body += "\n" + "; ".join(notes) + "."
    return body + "\nI can count scans, fired signals, shadow experiments, your logged trades, loss cases and messages for any stretch you name.", {"kind": "chat", "topic": "count"}


# --- lot size -----------------------------------------------------------------
def lot_for(stop_pips, balance, risk_pct, pip_value=10.0, step=0.01, min_lot=0.01, max_lot=5.0):
    """The engine's formula restated (a test pins this to scanner_common.calculate_position_size): risk $ = balance x risk%, lots = risk $ / (stop pips x $10),
    rounded DOWN to the step so the risk never goes above budget. Below the minimum lot it returns None rather than rounding up."""
    if not stop_pips or stop_pips <= 0 or balance <= 0 or risk_pct <= 0:
        return None
    raw = balance * risk_pct / (stop_pips * pip_value)
    lots = int(raw / step + 1e-9) * step
    lots = round(lots, 2)
    if lots < min_lot:
        return {"lots": None, "risk_usd": balance * risk_pct, "raw": raw}
    return {"lots": min(lots, max_lot), "risk_usd": balance * risk_pct, "raw": raw, "capped": lots > max_lot}


def parse_lot_args(text):
    """-> dict(stop, balance, risk) from the user's words; each is None when not given. Negative and zero values are KEPT so they can be refused, not silently dropped."""
    low = (text or "").lower().replace(",", "")
    stop = bal = risk = None
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*-?\s*pips?\b", low) or re.search(r"\b(?:stop(?: ?loss)?|sl)\s*(?:of|is|at|=)?\s*(-?\d+(?:\.\d+)?)", low)
    if m:
        stop = float(m.group(1))
    m = re.search(r"\$?\s*(\d+(?:\.\d+)?)\s*(k|m|thousand|million)?\s*(?:usd|dollars?)?\s*(?:account|acct|balance|equity)\b", low) or \
        re.search(r"\b(?:balance|account|equity)\s*(?:of|is|at|=|with)?\s*\$?\s*(\d+(?:\.\d+)?)\s*(k|m|thousand|million)?", low)
    if m:
        bal = float(m.group(1)) * {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}.get(m.group(2) or "", 1)
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", low)
    if m:
        risk = float(m.group(1)) / 100
    return {"stop": stop, "balance": bal, "risk": risk}


def a_lot(profile, args, last_signal, seed):
    """args: parse_lot_args(). Refuses nonsense instead of quietly substituting something else; says out loud whenever it uses something other than the profile."""
    stop, bal_o, risk_o = args.get("stop"), args.get("balance"), args.get("risk")
    if not profile and not bal_o:
        return "I can't find the account profile, so I don't know the balance and risk percentage to size from. I won't guess them.", None
    if stop is not None and stop <= 0:
        return "A stop has to be above zero pips, otherwise there's nothing to size from. Give me the stop distance in pips, like 'lot size for a 10 pip stop'.", {"kind": "chat", "topic": "lot"}
    if stop is not None and stop < 1.0:
        return "A stop under one pip is tighter than GBPUSD's normal spread, so I won't size a trade around it. Give me a realistic stop distance in pips.", {"kind": "chat", "topic": "lot"}
    if bal_o is not None and bal_o <= 0:
        return "That balance doesn't work for a size calculation. Give me an account balance above zero.", None
    if risk_o is not None and not (0 < risk_o <= 1):
        return "A risk percentage has to be above 0% and no more than 100%. Tell me the percentage of the account you want to risk.", None
    bal = bal_o if bal_o is not None else profile["balance"]
    rp = risk_o if risk_o is not None else profile["risk_pct"]
    name = profile.get("name") if profile else None
    used = []
    if bal_o is not None:
        used.append(f"the ${bal:.0f} balance you gave me instead of the profile's ${profile['balance']:.0f}" if profile else f"the ${bal:.0f} balance you gave me")
    if risk_o is not None and profile:
        used.append(f"{rp * 100:g}% risk as you asked, not the profile's {profile['risk_pct'] * 100:g}%")
    if stop is None:
        if last_signal and last_signal.get("risk_pips"):
            stop = last_signal["risk_pips"]
            src = f"the stop on the latest signal ({pp(stop)} pips)"
        else:
            return ("Sure. What's the stop distance in pips? For example 'lot size for a 10 pip stop'. I size from your account profile"
                    + (f" ({name}: ${profile['balance']:.0f} balance, {profile['risk_pct'] * 100:g}% risk)" if profile else "") + ".", {"kind": "chat", "topic": "lot"})
    else:
        src = f"a {pp(stop)} pip stop"
    r = lot_for(stop, bal, rp)
    if r is None:
        return "That doesn't work for a size calculation. Give me the stop in pips, above zero.", None
    who = "the " + name + " account" if (name and bal_o is None) else "that account"
    head = f"For {src} on {who} (${bal:.0f}, {rp * 100:g}% risk = ${r['risk_usd']:.2f})"
    if r["lots"] is None:
        return (f"{head}: the budget only buys {r['raw']:.3f} lots, which is under the smallest lot of 0.01. I won't round it up, because that would risk more than your budget."), None
    risk_actual = r["lots"] * stop * 10
    cap = f" That is the engine's 5-lot cap, so it only uses ${risk_actual:.2f} of the budget; the size you'd actually need is more than the cap allows." if r.get("capped") else ""
    warn = ""
    if rp > 0.02:
        warn = f" Heads up: {rp * 100:g}% is a lot of the account to put on one trade; your profile is set to {profile['risk_pct'] * 100:g}%." if profile else f" Heads up: {rp * 100:g}% is a lot of the account to put on one trade."
    note = (" I used " + " and ".join(used) + ".") if used else ""
    return (f"{head}: {r['lots']:.2f} lots. At $10 a pip per standard lot, that risks about ${risk_actual:.2f}. I round down to the 0.01 step so it never goes over the risk budget.{cap}{warn}{note}",
            {"kind": "chat", "topic": "lot"})


def a_distance(cs, now, prices, seed):
    tf = next((t for t in ("5m", "15m", "1h") if cs.get(t)), None)
    if not tf:
        return "I have no recorded candles, so I can't measure how far price is from that level.", None
    c = cs[tf][-1]
    lv = float(prices[0])
    d = pips(c["c"] - lv)
    side = "above" if d > 0 else "below"
    k = seed if isinstance(seed, int) else 0
    body = pick((f"Price is {p5(c['c'])} (from the {TF_WORDS[tf]} candle at {lagos_hm(c['t'])} Lagos time), so {p5(lv)} is {'below' if d > 0 else 'above'} it {pb(d)}.",
                 f"{p5(lv)} sits {'under' if d > 0 else 'over'} where price is now {pb(d)} ({p5(c['c'])}, {TF_WORDS[tf]} candle at {lagos_hm(c['t'])} Lagos time).",
                 f"Right now it's {p5(c['c'])} ({lagos_hm(c['t'])} Lagos time), which puts {p5(lv)} {'below' if d > 0 else 'above'} it {pb(d)}."), k)
    return (body + _age_note(c['t'], tf, now) + pick(("", " Want me to ping you if it gets there?", " I can alert you when it touches it, if you like."), k + 2)), {"kind": "chat", "topic": "price"}


def a_pips_between(prices, seed):
    """Plain arithmetic on two prices the user gave: no records needed. 1 pip = 0.0001 on GBPUSD."""
    a, b = float(prices[0]), float(prices[1])
    d = pips(b - a)
    if abs(d) < 1e-9:
        return f"{p5(a)} and {p5(b)} are the same price, so zero pips apart.", {"kind": "chat", "topic": "pips"}
    k = seed if isinstance(seed, int) else 0
    return (pick((f"From {p5(a)} to {p5(b)} {pb(d)}, with {p5(b)} {'above' if d > 0 else 'below'}. A pip is 0.0001 on GBPUSD, so it's the difference with the decimal moved four places.",
                  f"{p5(a)} to {p5(b)} {pb(d)}: {p5(b)} is the {'higher' if d > 0 else 'lower'} one. (A pip is 0.0001 here.)",
                  f"That's {pp(abs(d))} pips between them, {p5(a)} to {p5(b)} {pb(d)}, with {p5(b)} {'above' if d > 0 else 'below'} {p5(a)}."), k)), {"kind": "chat", "topic": "pips"}


def a_predict(cs, now, prices, seed):
    lead = pick(("I can't tell you what price will do next, and I won't pretend to.", "Nobody can promise that, me included, so I won't guess.",
                 "I don't predict, because a guess dressed up as a forecast would just mislead you."), seed)
    extra = ""
    if prices:
        d = a_distance(cs, now, prices, seed)[0]
        extra = " What I can tell you: " + d
    else:
        extra = " I can't call where it goes. What I can do is tell you what the engine is reading right now and what it's waiting to see. Just ask me what it's waiting for."
    return lead + extra, {"kind": "chat", "topic": "price"}


def a_meta_watching(prices, ctx, seed):
    lv = f" {p5(float(prices[0]))}" if prices else ""
    return (f"No, not on my own. I only watch something for you when an alert is set: say 'alert me when price touches{lv or ' <the level>'}' (or 'goes above' / 'goes below') and I'll set it. "
            "Until then I'm only keeping what you tell me in this chat, and I haven't set anything."), {"kind": "chat", "flow": "level", "stage": "need_level", "level": float(prices[0])} if prices else None


def a_meta_saved(ctx, seed):
    lvl = ctx.get("level") if isinstance(ctx, dict) else None
    if lvl:
        return (f"No. I haven't saved {p5(lvl)} or started watching it; so far we've only talked about it. Say 'alert me when price touches {p5(lvl)}' and I'll set a real alert."), ctx
    return ("Nothing from this chat has been saved. I only save something when you say yes to a question that asks to save it, or when you set an alert; looking things up "
            "and talking it through don't write anything."), None


def a_meta_source(ctx, seed):
    topic = ctx.get("topic") if isinstance(ctx, dict) else None
    how = {
        "ema": "The EMA is my own arithmetic on the candles I've recorded (the standard EMA formula on closing prices). The engine doesn't publish one, so it isn't an engine reading, and it can differ from a chart's by a few pips.",
        "change": "That's my own arithmetic on recorded candles: the opening price of the candle at your start time against the latest recorded close, plus the highest and lowest prices in between. It's not an engine reading.",
        "range": "That's my own arithmetic on recorded candles: the average of (high minus low) for each candle in the stretch. The engine doesn't compute it, so it isn't an engine reading.",
        "price": "That's the closing price of the newest candle in my candle records, not a live quote.",
        "count": "Those counts come straight from the engine's and the bot's own logs, each counted by its own definition, which I state with the number.",
        "lot": "The size comes from your account profile (balance and risk percentage) and the engine's sizing rule: risk dollars divided by stop pips times ten dollars a pip, rounded down to 0.01.",
    }.get(topic)
    return (how or "Anything I calculate myself (EMA distances, price changes, ranges, lot sizes) is plain arithmetic on recorded candles and your account profile, and I say so when I give it. Anything about direction or structure is the engine's own reading, which I only relay."), ctx if topic else None


# ----------------------------------------------------------------------
# SENSE OF TIME: how long ago, how long did it last, how long until, what time is it. Pure calendar and candle arithmetic; sources named every time.
# FX week: closed Friday 21:00 UTC to Sunday 22:00 UTC (the same closure gaps() treats as normal). Scanner: 07:00-15:55 UTC (08:00-16:55 Lagos), Monday-Friday.
# ----------------------------------------------------------------------
def fx_open(t):
    wd, h = t.weekday(), t.hour
    return not ((wd == 4 and h >= 21) or wd == 5 or (wd == 6 and h < 22))


def closed_hours(a, b):
    """Hours between a and b (UTC) when the forex market was closed for the weekend."""
    if b <= a:
        return 0.0
    mon = (a - timedelta(days=a.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    total, k = 0.0, 0
    while mon + timedelta(weeks=k) < b + timedelta(days=7):
        m = mon + timedelta(weeks=k)
        lo, hi = max(a, m + timedelta(days=4, hours=21)), min(b, m + timedelta(days=6, hours=22))
        if hi > lo:
            total += (hi - lo).total_seconds() / 3600
        k += 1
    return total


def humanize(delta):
    """timedelta -> '7 minutes', '2 hours 15 minutes', '3 days 4 hours'. Never negative."""
    sec = abs(delta.total_seconds())
    if sec < 45:
        return "less than a minute"
    if sec < 90:
        return "about a minute"
    mins = int(round(sec / 60))
    if mins < 60:
        return f"{mins} minutes"
    hrs, m = divmod(mins, 60)
    if hrs < 3:
        return f"{hrs} hour{'s' if hrs != 1 else ''}" + (f" {m} minutes" if m else "")
    if hrs < 48:
        return f"{hrs} hours" + (f" {m} minutes" if m >= 5 and hrs < 10 else "")
    days, h = divmod(hrs, 24)
    if days < 60:
        return f"{days} days" + (f" {h} hour{'s' if h != 1 else ''}" if h and days < 10 else "")
    return f"about {round(days / 30.4)} months"


def rel(t, now):
    """'3 hours ago (at 09:05 today)' with a note when the weekend close is inside the gap, so a Monday answer isn't read as trading time."""
    d = now - t
    if d < timedelta(seconds=-60):
        return f"{humanize(d)} in the FUTURE (at {lagos_when(t, now)}), which shouldn't be possible, so the record or the clock is off"
    out = f"{humanize(d)} ago (at {lagos_when(t, now)})" if d >= timedelta(seconds=45) else f"just now (at {lagos_when(t, now)})"
    ch = closed_hours(t, now)
    if ch >= 1:
        out += f", and about {round(ch)} of those hours the forex market was closed for the weekend"
    return out


def ago_only(t, now):
    """'25 hours ago' (+ weekend note) for sentences that already say the clock time."""
    r = rel(t, now)
    return r.split(" (at ")[0] + (", and about" + r.split(", and about", 1)[1] if ", and about" in r else "") if " (at " in r else r


def next_utc(now, hh, mm, weekdays=(0, 1, 2, 3, 4)):
    for k in range(0, 9):
        c = (now + timedelta(days=k)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if c > now and c.weekday() in weekdays:
            return c
    return None


def a_clock(now, last_scan=None):
    nl, nu = now.astimezone(LAGOS), now.astimezone(UTC)
    body = f"It's {nl:%A %d %B %Y}, {nl:%H:%M} in Lagos ({nu:%H:%M} UTC)."
    if fx_open(nu):
        close = next_utc(nu, 21, 0, (4,))
        body += f" The forex market is open; it closes for the weekend in {humanize(close - nu)} (Friday {close.astimezone(LAGOS):%H:%M} Lagos time)."
    else:
        opn = next_utc(nu, 22, 0, (6,))
        body += f" The forex market is closed for the weekend; it reopens in {humanize(opn - nu)} (Sunday {opn.astimezone(LAGOS):%H:%M} Lagos time)."
    scan_on = nu.weekday() < 5 and (7, 0) <= (nu.hour, nu.minute) < (15, 55) + (0,) * 0 and (nu.hour, nu.minute) < (15, 55)
    if scan_on:
        body += " The scanner is in its running hours (08:00 to 16:55 Lagos time, Monday to Friday)" + (f"; my last scan was {rel(last_scan, now)}." if last_scan else ".")
    else:
        nxt = next_utc(nu, 7, 0)
        body += f" The scanner is outside its running hours (08:00 to 16:55 Lagos time, Monday to Friday); it next runs in {humanize(nxt - nu)}."
    return body, {"kind": "chat", "topic": "clock"}


def a_until(low, now, last_scan=None):
    nu = now.astimezone(UTC)
    tg = None
    if re.search(r"\bscanner|\bscan\b|\byou (start|run|scan)", low):
        scanning = nu.weekday() < 5 and nu.hour * 60 + nu.minute < 15 * 60 + 55 and nu.hour >= 7
        if scanning:
            return ("The scanner is running right now (every five minutes or so, 08:00 to 16:55 Lagos time)" + (f"; my last scan was {rel(last_scan, now)}." if last_scan else ".")), {"kind": "chat", "topic": "clock"}
        t = next_utc(nu, 7, 0)
        return f"The scanner next starts in {humanize(t - nu)}, at {lagos_when(t, now)}. It only runs 08:00 to 16:55 Lagos time, Monday to Friday.", {"kind": "chat", "topic": "clock"}
    table = (("overlap close|overlap end|end of the overlap", "the London/New York overlap ends", 16, 0), ("overlap", "the London/New York overlap starts", 12, 0),
             ("london close|london end|close of london", "the London session ends", 16, 0), ("london", "London opens", 7, 0),
             ("(ny|new york) (close|end)|close of (ny|new york)", "New York closes", 21, 0), ("ny|new york", "New York opens", 12, 0), ("asia|asian|tokyo", "the Asian session starts", 21, 0))
    for rx, label, hh, mm in table:
        if re.search(rf"\b({rx})\b", low):
            t = next_utc(nu, hh, mm, (0, 1, 2, 3, 4) if hh != 21 else (0, 1, 2, 3, 6))
            if t is None:
                break
            return (f"{label[0].upper()}{label[1:]} in {humanize(t - nu)}, at {lagos_when(t, now)}. (These are the engine's own session hours in UTC; Lagos is one hour ahead.)"
                    + (f" That includes the weekend close." if closed_hours(nu, t) >= 1 else "")), {"kind": "chat", "topic": "clock"}
    if re.search(r"\b(market|markets|forex|fx)\b.*\b(open|opens|opening)\b|\bopen\b.*\bmarket\b|\bmonday\b|\bnext week\b", low):
        if fx_open(nu):
            return "The forex market is open right now.", {"kind": "chat", "topic": "clock"}
        t = next_utc(nu, 22, 0, (6,))
        return f"The forex market reopens in {humanize(t - nu)}, on Sunday at {t.astimezone(LAGOS):%H:%M} Lagos time.", {"kind": "chat", "topic": "clock"}
    if re.search(r"\b(market|markets|forex|fx|week|weekend)\b.*\b(close|closes|closing|end|ends)\b|\bweekend\b|\bclose\b", low):
        if not fx_open(nu):
            t = next_utc(nu, 22, 0, (6,))
            return f"It's already closed for the weekend; it reopens in {humanize(t - nu)}.", {"kind": "chat", "topic": "clock"}
        t = next_utc(nu, 21, 0, (4,))
        return f"The forex market closes for the weekend in {humanize(t - nu)}, on Friday at {t.astimezone(LAGOS):%H:%M} Lagos time.", {"kind": "chat", "topic": "clock"}
    return "Until what? I can count down to the London open, the New York open, the overlap, the Asian session, the market's weekend close and reopening, or the scanner starting.", {"kind": "chat", "topic": "clock"}


def a_ago_signal(src, now, which="last"):
    sg = sorted(src.get("signals", []), key=lambda x: x["t"])
    if not sg:
        return "I have no signal on record at all, so there's nothing to measure from.", None
    idx = {"last": -1, "previous": -2, "first": 0}[which]
    if len(sg) < 2 and which == "previous":
        return "There's only one signal on record, so there's no earlier one.", None
    x = sg[idx]
    word = {"last": "The last signal", "previous": "The signal before the last one", "first": "The first signal on record"}[which]
    dirn = f", a {x['direction']}" if x.get("direction") else ""
    out = f"{word} fired {rel(x['t'], now)}{dirn}."
    if which == "last" and len(sg) > 1:
        gap = sg[-1]["t"] - sg[-2]["t"]
        out += f" The one before it was {humanize(gap)} earlier."
    out += f" I have {len(sg)} on record, the earliest from {lagos_when(sg[0]['t'], now)}."
    return out, {"kind": "chat", "topic": "ago"}


def a_ago_scan(cs, src, now):
    sc = [t for t in src.get("scans", []) if t]
    nu = now.astimezone(UTC)
    if not sc:
        return "I have no scan record, so I can't say when the scanner last ran.", None
    last = max(sc)
    out = f"My last scan on record was {rel(last, now)}."
    tf = next((t for t in ("5m", "15m", "1h") if cs.get(t)), None)
    if tf:
        c = cs[tf][-1]
        out += f" The newest {TF_WORDS[tf]} candle I hold opened {rel(c['t'], now)}."
    running = nu.weekday() < 5 and 7 <= nu.hour and nu.hour * 60 + nu.minute < 15 * 60 + 55
    gap = now - last
    if running and gap > timedelta(minutes=20):
        out += " The scanner should be running at this hour, and a gap that long is not normal, so it's worth a look."
    elif not running:
        out += " The scanner only runs 08:00 to 16:55 Lagos time, Monday to Friday, so a gap outside those hours is normal."
    return out, {"kind": "chat", "topic": "ago"}


def a_ago_msg(src, now, mine=False):
    key, who = ("replied", "I last replied to you") if mine else ("received", "you last wrote to me")
    ts = sorted(t for t in src.get(key, []) if t)
    if not mine and ts and now - ts[-1] <= timedelta(minutes=10):
        ts = ts[:-1]                                      # the newest one is this very message
        who += " before this message"
    if not ts:
        return f"I have no record of when {who.split(' before')[0]}.", None
    return f"Counting back through my ledger, {who} {rel(ts[-1], now)}.", {"kind": "chat", "topic": "ago"}


def a_ago_trade(src, now):
    ts = sorted(t for t in src.get("my_trades", []) if t)
    if not ts:
        return "You haven't logged a trade of your own with me, so there's nothing to measure from. (Bot signals are counted separately; ask 'how long ago was the last signal'.)", None
    return f"The last trade you logged as yours was {rel(ts[-1], now)}.", {"kind": "chat", "topic": "ago"}


def a_ago_level(cs, now, level, seed=0):
    tf = next((t for t in ("5m", "15m", "1h") if cs.get(t)), None)
    if not tf:
        return "I have no recorded candles to look back through.", None
    cand = cs[tf]
    if level is None:
        return "Which price level? Give it as a number like 1.3300.", None
    last_touch = None
    for i in range(len(cand) - 1, -1, -1):
        c = cand[i]
        if c["l"] <= level <= c["h"]:
            last_touch = c; break
        if i > 0 and (cand[i - 1]["c"] - level) * (c["o"] - level) < 0:
            last_touch = c; break                                    # a gap straight through it
    if last_touch is None:
        return (f"Price hasn't traded at {p5(level)} anywhere in my {TF_WORDS[tf]} candles, which run from {lagos_when(cand[0]['t'], now)}. "
                "I can't say how long ago before that."), {"kind": "chat", "topic": "ago"}
    if last_touch is cand[-1]:
        return f"Price is trading at {p5(level)} right now (the latest {TF_WORDS[tf]} candle's range includes it).", {"kind": "chat", "topic": "ago"}
    away = pips(cand[-1]["c"] - level)
    return (f"The last time price traded at {p5(level)} was the {TF_WORDS[tf]} candle at {lagos_when(last_touch['t'], now)}, which is {ago_only(last_touch['t'], now)}. "
            f"Since then it has stayed {'above' if away > 0 else 'below'} it, and it's now {pp(abs(away))} pips away."), {"kind": "chat", "topic": "ago"}


def a_ago_extreme(cs, now, since, which="high", tf=None):
    start = since[0] if since else now.astimezone(LAGOS).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    label = since[1] if since else "today (since 00:00 Lagos time)"
    tf = pick_tf(now - start, cs, tf)
    if not tf:
        return "I have no recorded candles to look through.", None
    ws = window(cs[tf], start, now)
    if not ws:
        return f"I have no {TF_WORDS[tf]} candles between {label} and now.", None
    c = max(ws, key=lambda x: x["h"]) if which == "high" else min(ws, key=lambda x: -x["l"])
    val = c["h"] if which == "high" else c["l"]
    first = [x for x in ws if (x["h"] == val if which == "high" else x["l"] == val)][0]
    tail = " That is the latest candle, so the extreme may still be in the making." if first is ws[-1] else ""
    return (f"Since {label}, the {'high' if which == 'high' else 'low'} is {p5(val)}, first reached on the {TF_WORDS[tf]} candle at {lagos_when(first['t'], now)}, {ago_only(first['t'], now)}.{tail}"
            + _coverage_note(cs, tf, start, now)), {"kind": "chat", "topic": "ago"}


def _swing(ws):
    """Biggest single swing inside the candles: low of an earlier candle to high of a later one (up), or high of an earlier to low of a later (down). -> (kind, i, j, pips)"""
    best = (None, 0, 0, 0.0)
    lo_i = hi_i = 0
    for j in range(1, len(ws)):
        if ws[j]["l"] < ws[lo_i]["l"] and False:
            pass
        up = pips(ws[j]["h"] - ws[lo_i]["l"])
        dn = pips(ws[hi_i]["h"] - ws[j]["l"])
        if up > best[3]:
            best = ("up", lo_i, j, up)
        if dn > best[3]:
            best = ("down", hi_i, j, dn)
        if ws[j]["l"] < ws[lo_i]["l"]:
            lo_i = j
        if ws[j]["h"] > ws[hi_i]["h"]:
            hi_i = j
    return best


def a_duration_swing(cs, now, since, tf=None):
    if since:
        start, label, notes = since
    else:
        start, label, notes = now - timedelta(hours=24), "the last 24 hours (you didn't name a stretch)", []
    tf = pick_tf(now - start, cs, tf)
    if not tf:
        return "I have no recorded candles to measure a move from.", None
    ws = window(cs[tf], start, now)
    if len(ws) < 3:
        return f"I have too few {TF_WORDS[tf]} candles in {label} to find a move.", None
    kind, i, j, sz = _swing(ws)
    if not kind:
        return f"I couldn't find a directional swing in {label}.", None
    a, b = ws[i], ws[j]
    ext_a = a["l"] if kind == "up" else a["h"]
    ext_b = b["h"] if kind == "up" else b["l"]
    span = b["t"] - a["t"]
    live = " Its end is the latest candle, so it may still be running." if b is ws[-1] else ""
    ch = closed_hours(a["t"], b["t"])
    wk = f" (about {round(ch)} of that the market was closed for the weekend, so the trading time is shorter)" if ch >= 1 else ""
    avg = pips(sum(x["h"] - x["l"] for x in ws) / len(ws))
    fast = ""
    if cs.get("1h"):
        hs = [x for x in window(cs["1h"], a["t"] - timedelta(minutes=59), b["t"] + timedelta(minutes=TF_MIN[tf]))]
        if hs:
            h = max(hs, key=lambda x: abs(x["c"] - x["o"]))
            mv = pips(h["c"] - h["o"])
            if abs(mv) > 0 and (mv > 0) == (kind == "up"):
                fast = f" The fastest part was the hourly candle at {lagos_when(h['t'], now)}: {pp(abs(mv))} pips {'up' if mv > 0 else 'down'} in one hour ({pp(abs(mv) / sz * 100)[:-2] if sz else 0}% of the swing)."
    return (f"The biggest single swing {'since' if since and not label.startswith('the ') else 'in'} {label} was {'UP' if kind == 'up' else 'DOWN'} {pp(sz)} pips: from {p5(ext_a)} on the {TF_WORDS[tf]} candle at {lagos_when(a['t'], now)} to {p5(ext_b)} on the one at "
            f"{lagos_when(b['t'], now)}. The two candles' open times are {humanize(span)} apart{wk}. It began {ago_only(a['t'], now)}.{fast}{live} "
            f"That's the largest low-to-high (or high-to-low) swing in the window measured on {TF_WORDS[tf]} candles, whose average range here is {pp(avg)} pips; it's my measurement, not the engine's own definition of a push or impulse."
            + "".join(" " + x[0].upper() + x[1:] + "." for x in notes) + _coverage_note(cs, tf, start, now)), {"kind": "chat", "topic": "duration", "swing": {"a": ext_a, "b": ext_b, "pips": sz, "up": kind == "up", "a_t": a["t"].isoformat(), "b_t": b["t"].isoformat(), "span_h": span.total_seconds() / 3600}}


def a_swing_pips(ctx, now):
    w = ctx["swing"]
    per = w["pips"] / w["span_h"] if w["span_h"] else None
    return (f"It went from {p5(w['a'])} to {p5(w['b'])} {pb(w['pips'])}, which averages about {pp(per)}p an hour over the {humanize(timedelta(hours=w['span_h']))} it took." if per else
            f"It went from {p5(w['a'])} to {p5(w['b'])} {pb(w['pips'])}.") + " That's the straight-line move from its low to its high; the path in between was longer.", ctx


def a_duration_side(cs, now, level, side=None, tf=None):
    tf = tf if tf in cs and cs.get(tf) else ("15m" if cs.get("15m") else next((t for t in ("5m", "1h") if cs.get(t)), None))
    if not tf:
        return "I have no recorded candles to measure from.", None
    if level is None:
        return "Which level? Give me a price like 1.3300.", None
    cand = cs[tf]
    cur = cand[-1]["c"] > level
    if cand[-1]["c"] == level:
        return f"The latest {TF_WORDS[tf]} close is exactly at {p5(level)}, so it's on neither side right now.", None
    i = len(cand) - 1
    while i > 0 and (cand[i - 1]["c"] > level) == cur and cand[i - 1]["c"] != level:
        i -= 1
    since_t = cand[i]["t"]
    whole = i == 0
    word = "above" if cur else "below"
    n_c = len(cand) - i
    if side and (side == "above") != cur:
        pre = f"No, price is {word} {p5(level)}, not {side} it. "
    else:
        pre = ""
    if whole:
        return (pre + f"Every {TF_WORDS[tf]} close in my records (from {lagos_when(cand[0]['t'], now)}) has been {word} {p5(level)}, so it has been on that side for at least {humanize(now - cand[0]['t'])}"
                f" and I can't see further back."), {"kind": "chat", "topic": "duration"}
    d = now - since_t
    ch = closed_hours(since_t, now)
    wk = f" (about {round(ch)} of those hours the market was closed)" if ch >= 1 else ""
    return (pre + f"Price has closed {word} {p5(level)} on all {n_c} {TF_WORDS[tf]} candles since the one at {lagos_when(since_t, now)}: that's {humanize(d)}{wk}. "
            f"Before that the {TF_WORDS[tf]} close was on the other side. This counts closes, so a wick through the level that closed back doesn't break the streak."), {"kind": "chat", "topic": "duration"}


def a_duration_band(cs, now, band):
    if band is None:
        return ("'Ranging' needs a number before I can measure it. How tight do you mean: within how many pips, high to low? Tell me something like '20 pips' and I'll measure how long price has stayed inside a band that size.",
                {"kind": "chat", "topic": "band"})
    if band <= 0:
        return "A band has to be wider than zero pips.", {"kind": "chat", "topic": "band"}
    tf = "5m" if cs.get("5m") else next((t for t in ("15m", "1h") if cs.get(t)), None)
    if not tf:
        return "I have no recorded candles to measure from.", None
    cand = cs[tf]
    hi, lo, i = cand[-1]["h"], cand[-1]["l"], len(cand) - 1
    if pips(hi - lo) > band:
        return f"The latest {TF_WORDS[tf]} candle alone spans {pp(pips(hi - lo))} pips, more than a {pp(band)}-pip band, so price isn't inside one right now.", {"kind": "chat", "topic": "band"}
    while i > 0:
        h2, l2 = max(hi, cand[i - 1]["h"]), min(lo, cand[i - 1]["l"])
        if pips(h2 - l2) > band or cand[i]["t"] - cand[i - 1]["t"] > timedelta(minutes=TF_MIN[tf] * 1.5):
            break
        hi, lo, i = h2, l2, i - 1
    d = now - cand[i]["t"]
    whole = i == 0
    tail = " That is as far back as my records go, so it may have been inside it for longer." if whole else ""
    ch = closed_hours(cand[i]["t"], now)
    return (f"Price has stayed inside a {pp(band)}-pip band (high to low) for {humanize(d)}, since the {TF_WORDS[tf]} candle at {lagos_when(cand[i]['t'], now)}; "
            f"in that time it ranged {p5(lo)} to {p5(hi)} ({pp(pips(hi - lo))} pips).{tail}" + (f" (A break in the candles inside that stretch ends it, so weekends reset it.)" if ch >= 1 else "")
            + " Whether you'd call that 'ranging' depends on the band you chose; I only measured the band."), {"kind": "chat", "topic": "band"}


def a_duration_leg(state, now):
    if not state or not state.get("origin"):
        return "I don't have the engine's current-leg reading, so I can't say how long it has been running. I won't work it out from candles and present it as the engine's view.", None
    o, ts = state["origin"], state.get("ts")
    d = state.get("direction")
    head = f"The engine's current leg ({d.lower() if d else 'direction not given'}) began at {lagos_when(o, now)}."
    if ts and now - ts > timedelta(minutes=45):
        out = head + (f" But the engine's last reading is from {rel(ts, now)}, so I can only say it had run {humanize(ts - o)} as of that reading, not as of now.")
    else:
        ch = closed_hours(o, now)
        out = head + f" That's {humanize(now - o)} ago" + (f", of which about {round(ch)} hours the market was closed for the weekend" if ch >= 1 else "") + "."
    if state.get("phase"):
        out += f" The engine labels the phase '{state['phase']}'" + (f" and counts it at {state['age_bars']} bars" if state.get("age_bars") is not None else "") + " (its own bar count; I don't convert it to hours because the bar size isn't recorded)."
    return out + " That's the engine's leg, not a separate measure of mine.", {"kind": "chat", "topic": "duration"}


def a_duration_regime(cs, now, state=None):
    tf = "1h" if cs.get("1h") else next((t for t in ("15m", "5m") if cs.get(t)), None)
    out = ("A bull or bear market is a months-to-years idea, usually judged on daily or weekly data and a definition I don't use, so I can't tell you whether this is one or how long it has lasted. ")
    if tf:
        c = cs[tf]
        span = c[-1]["t"] - c[0]["t"]
        net = pips(c[-1]["c"] - c[0]["o"])
        out += (f"What I can tell you: my {TF_WORDS[tf]} records only cover {humanize(span)} (from {lagos_when(c[0]['t'], now)}), and over that stretch price moved {pp(net, True)} pips "
                f"({p5(c[0]['o'])} to {p5(c[-1]['c'])}). That's far too short to call a market regime.")
    if state and state.get("origin"):
        out += f" The engine's current leg began {lagos_when(state['origin'], now)}; ask 'how long has the current leg been running' for that."
    return out, {"kind": "chat", "topic": "duration"}


# --- people things: greetings, feelings, a level to track ---------------------
def a_greet(facts, seed, wellbeing=False):
    bits = [pick(GLUE["greet"], seed)]
    if wellbeing:
        bits.append(pick(GLUE["well"], seed + 1 if str(seed).lstrip("-").isdigit() else seed))
    ls = facts.get("last_scan")
    if ls:
        bits.append(f"My last scan on record was at {lagos_when(ls, facts['now'])}.")
    ns = facts.get("signals_today")
    if ns is not None:
        bits.append("No signal has fired today." if ns == 0 else f"{ns} signal{'s' if ns != 1 else ''} fired today.")
    bits.append(pick(GLUE["well_q"], seed))
    return " ".join(bits), {"kind": "chat", "topic": "greet"}


_FEEL = {
    "missed": (r"\bmiss(ed|ing)?\b.*\b(setup|trade|move|entry)|\bfomo\b|\bwatch(ed|ing) it (go|run)", "Missing setups is a hard one, because it feels like money you watched walk past. Was it a signal from me you couldn't take, or a move you saw yourself?"),
    "loss": (r"\b(lost|losing|loss|blown|stopped out|sl hit|got stopped)\b", "Losses sting, especially when you did the work. Is it one trade that's sitting on you, or has it been a run of them?"),
    "doubt": (r"\b(doubt|unsure|confus\w*|don'?t know|not sure|second[- ]guess\w*)\b", "Doubt is usually the system telling you something isn't clear yet. What specifically is it that you can't decide on?"),
    "tired": (r"\b(tired|drained|exhaust\w*|burn\w*|sleep\w*|overwhelm\w*)\b", "That sounds like you've been carrying a lot. Is this about trading, or is it more than trading?"),
    "angry": (r"\b(angry|mad|frustrat\w*|annoy\w*|upset|pissed)\b", "Frustration usually has a specific thing behind it. What set it off?"),
    "anxious": (r"\b(anxious|anxiety|worried|nervous|scared|stress\w*|pressure)\b", "Okay, that's worth slowing down for. Is it a trade that's open, or a feeling about the whole thing?"),
    "revenge": (r"\b(revenge|win it back|make it back|recover|chase|chasing)\b", "I'm glad you said that out loud. That urge to win it back is exactly when a pause helps. Are you about to put a trade on?"),
}
_CRISIS = re.compile(r"\b(kill myself|end it all|don'?t want to (live|be here)|suicid\w*|hurt myself|self[- ]harm)\b", re.I)


def a_feeling_start(text, seed):
    if _CRISIS.search(text or ""):
        return crisis_text(), None
    return (pick(("I'm here. What's going on? Is it the market, a trade, or something outside trading? You can just talk.",
                  "Okay, I'm listening. Is it something with the market or a trade, or something else entirely?",
                  "Talk to me. What's sitting on you: trading, or life?"), seed), {"kind": "chat", "flow": "feeling", "stage": "listening"})


def crisis_text():
    return ("I'm really glad you told me, and I want you to have a real person with you for this, not just me. Please reach out to someone you trust right now, "
            "or a local emergency number or crisis line where you are. I'm here to keep talking with you too, but please don't carry this alone.")


def a_feeling_turn(text, ctx, seed):
    if _CRISIS.search(text or ""):
        return crisis_text(), None
    low = (text or "").lower()
    for k, (rx, reply) in _FEEL.items():
        if re.search(rx, low):
            tail = pick((" There's no rush, and I won't turn this into a trade idea unless you want that.", " Take your time. I'm not going to push a trade at you.", " I'm just here to talk it through with you."), seed)
            return reply + tail, {"kind": "chat", "flow": "feeling", "stage": "talking", "theme": k}
    if ctx.get("stage") == "talking" and re.search(r"\b(yes|yeah|yep|no|nah|it|that|because)\b|\w+", low):
        pass
    return (pick(("Thanks for telling me. Say a bit more? What's the part that's weighing on you most?",
                  "I hear you. What do you think is behind it?",
                  "Okay. Is there something specific that happened, or is it more of a general feeling?"), seed + (ctx.get("stage") == "talking" and 1 or 0) if isinstance(seed, int) else seed),
            {"kind": "chat", "flow": "feeling", "stage": "talking", "theme": ctx.get("theme")})


def a_level_start(text, prices, price_now, seed, now_cs=None):
    if prices:
        return a_level_turn(text, {"kind": "chat", "flow": "level", "stage": "need_level"}, price_now, seed)
    return (pick(("Sure, which level are you watching? And is there a particular reason it caught your eye? I'd like to hear the idea.",
                  "Alright. Give me the level, and tell me what you're thinking about it. Is there a reason you picked that one?",
                  "Okay, which level? And what's the story with it, is there something you expect to happen there?"), seed),
            {"kind": "chat", "flow": "level", "stage": "need_level"})


def a_level_turn(text, ctx, price_now, seed):
    low = (text or "").lower()
    prices = [float(x) for x in _PRICE_RX.findall(low)]
    level = prices[0] if prices else ctx.get("level")
    reason = ctx.get("reason")
    side = ctx.get("side")
    if re.search(r"\b(break|breaks|broke|above|through|continue higher|go higher|up)\b", low) and not re.search(r"\b(reject|bounce|hold|resist)\b", low):
        side = side or "up"
    if re.search(r"\b(reject|rejects|bounce|hold|holds|resist|support|reversal|reverse)\b", low):
        side = side or "bounce"
    if not level:
        return ("I didn't catch a price in that. Which level do you mean? Something like 1.3400.", {"kind": "chat", "flow": "level", "stage": "need_level"})
    new_reason = len(low.split()) >= 4 and (not prices or len(low.split()) >= 7)
    if new_reason:
        reason = text.strip()
    where = ""
    if price_now:
        d = pips(price_now - level)
        where = f" For reference, price is at {p5(price_now)}, which is {pp(abs(d))} pips {'above' if d > 0 else 'below'} it right now."
    base = {"kind": "chat", "flow": "level", "level": level, "reason": reason, "side": side}
    if not reason:
        return (f"Got it, {p5(level)}.{where} Is there a particular reason you're watching that one? Something that happened there before, or something you expect?",
                {**base, "stage": "need_reason"})
    ask = {"up": "What would make you believe a break there is real: a candle closing beyond it, a retest that holds, or something else?",
           "bounce": "What would tell you it's rejecting properly: a wick and a close back inside, or a specific candle? And what makes you think it holds this time?"}.get(side, "What would you need to see there to act on it, and what would make you drop the idea?")
    return (f"That makes sense, thanks for explaining.{where} {ask} "
            f"One honest thing: I haven't saved or started watching {p5(level)} yet. If you want me to ping you at it, say 'alert me when price touches {p5(level)}' "
            f"(or 'goes above' / 'goes below') and I'll set that up.", {**base, "stage": "discussing"})
