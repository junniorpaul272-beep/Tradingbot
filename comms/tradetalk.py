"""
tradetalk.py — talking a trade through, in your own words, one turn at a time.

You describe a trade the way you think about it: why you took it, the price you want it to reach, the price where you'd be wrong, the checkpoints price should pass to
prove the idea, what could get in the way. You never have to speak in pips. The bot listens, writes down what you said in YOUR words, does the arithmetic it can on YOUR
prices (pips shown in brackets), points out what the numbers say, asks about what is missing or unclear, and only logs the trade after you say yes.

PURE: imports only re. It reads no files and holds no market facts. The runner hands in the recorded price, the account profile and the engine's leg reading;
anything computed from them is labelled as such. Nothing is saved here: `turn()` returns what to say, the updated draft, and (when asked to log) a confirm action.

HONESTY RULES
  * What you said is quoted back as you said it, never reworded into something you didn't say.
  * A price is never invented: a stop, a target or an entry you haven't given stays blank and the bot asks.
  * Pips are computed, never assumed: 1 pip = 0.0001 on GBPUSD.
  * When a message fits nowhere, the bot asks what you meant. It doesn't pretend to understand and it doesn't answer with a generic line.
"""
import re

PIP = 0.0001
_PX = r"(?<![\d.])(\d\.\d{3,5})(?!\d)"
PX = re.compile(_PX)

SIDE_BUY = re.compile(r"\b(bought|buy|buying|long|went long|going long|longed)\b", re.I)
SIDE_SELL = re.compile(r"\b(sold|sell|selling|short|shorted|went short|going short)\b", re.I)
CUE_STOP = re.compile(r"\b(stop ?loss|stoploss|stop|sl)\b", re.I)
CUE_TARGET = re.compile(r"\b(target|tp|take ?profit|head(?:ing|ed)? (?:to|for|towards?)|go(?:ing)? (?:to|up to|down to)|want(?:ed)? it to|expect(?:ing)? it to|aim(?:ing)?|reach|run (?:to|up to)|rally (?:to|up to)|drop (?:to|down to)|fall (?:to|down to)|take it to|to hit)\b", re.I)
CUE_FAIL = re.compile(r"\b(wrong|invalid\w*|fails?|failed|failure|not valid|abandon\w*|out if|exit if|cut (?:it|the trade|out)|breaks? (?:below|above|under|over)|clos\w+ (?:a |the |an )?(?:\w+ )?(?:\w+ )?(?:candle )?(?:below|above|under|over))\b", re.I)
CUE_MILE = re.compile(r"\b(check ?points?|milestones?|passes|passing|pass|prove[sd]?|proof|confirm\w*|first|second|third|fourth|next|then|along the way|on the way)\b", re.I)
CUE_OBS = re.compile(r"\b(obstacles?|resistance|worr\w+|dangers?|problems?|concerns?|nfp|cpi|fomc|boe|news|could get in the way|in the way|trap|blocking (?:it|price)|(?:old|previous|prior|equal|daily|weekly) (?:highs?|lows?)|ceiling|supply zone|demand zone|round number|big figure)\b", re.I)
CUE_REASON = re.compile(r"\b(because|reason|since|cos|coz|swept|sweep\w*|reclaim\w*|break of structure|bos|choch|order block|fvg|fair value|structure|bias|setup|confluence|rejection|retest\w*|thesis|idea|thinking|think|believe|liquidity grab)\b", re.I)
SKIP = re.compile(r"^\W*(none|nothing|no|nope|nah|skip|skip it|n/?a|not really|nothing (?:else|more|there)|no (?:obstacles?|milestones?|checkpoints?|reason|failure|more)|i (?:don'?t|do not) (?:have|know)(?: any)?|there (?:isn'?t|is none|are none)|pass)\W*$", re.I)
FINISH = re.compile(r"^\W*(?:ok(?:ay)?[, ]*)?(?:(?:that'?s|thats) (?:all|it|everything)|done|i'?m done|log (?:it|that|this)(?: now)?|go ahead(?: and log)?|save (?:it|that|this)|record (?:it|that)|finish|that is all|nothing else|no(?:thing)? more)\W*$", re.I)
YES = re.compile(r"^\W*(yes|yeah|yep|yup|ok(?:ay)?|sure|correct|right|exactly|that'?s right|confirm(?:ed)?|do it)\W*$", re.I)
NO = re.compile(r"^\W*(no|nope|nah|cancel|stop|not yet|wait|change(?: it)?)\W*$", re.I)
START = re.compile(r"\b(i (?:just |have |'ve |already )?(?:bought|sold|went long|went short|took|entered|opened|am in|'m in|got in)|i'?m (?:long|short|in a (?:buy|sell|long|short)|in)\b|"
                   r"(?:log|record|talk (?:about|through)|tell you about|walk you through|explain|discuss|share)\b.{0,30}\b(?:trade|position|setup|buy|sell)|i (?:took|have|opened) (?:a|an|my|the)\b.{0,20}\b(?:trade|buy|sell|position|long|short)|"
                   r"i (?:want|wanna|plan|am planning|'m planning|'m thinking|am thinking) (?:to|of|about) (?:buy|sell|go long|go short|take)|thinking of (?:buying|selling|going long|going short)|about to (?:buy|sell|enter))", re.I)
NEG_SLOT = re.compile(r"^\W*(?:nah|no|nope|not really|hmm)?[, ]*(?:there'?s |there is |i see |i don'?t see |i dont see |i have )?(?:nothing|no|none|not much|nothing much)\b.{0,25}\b(in the way|obstacles?|blocking|resistance|worr\w+|check ?points?|milestones?|reasons?)\W*$", re.I)
QUESTION_FIRST = re.compile(r"^\W*(?:how|what|what's|whats|where|which|when|why|is|are|do|does|did|can|could|will|should)\b", re.I)


def p5(x):
    return f"{x:.5f}"


def pips(x):
    return x / PIP


def pp(x):
    return f"{x:.1f}"


def signed(x):
    return ("+" if x > 0 else ("-" if x < 0 else "")) + pp(abs(x))


def new_draft():
    return {"side": None, "pair": "GBPUSD", "entry": None, "target": None, "stop": None, "milestones": [], "reasons": [], "failure": [], "failure_px": [], "obstacles": [], "obstacle_px": [],
            "milestone_notes": [], "skipped": [], "asked": [], "asking": None, "shown": [], "unplaced": None, "turns": 0, "pip_inputs": {}}


def is_start(raw):
    """A message that begins talking about a trade the user took or means to take. Questions are not starts."""
    t = (raw or "").strip()
    if not t or t.startswith("/") or QUESTION_FIRST.search(t) and t.endswith("?"):
        return False
    return bool(START.search(t)) and bool(SIDE_BUY.search(t) or SIDE_SELL.search(t) or re.search(r"\btrade\b|\bposition\b", t, re.I))


def has_all_three_prices(raw):
    """The one-message form the old logger understood (entry, stop and target all given): left to that path, which already asks for a yes."""
    return bool(re.search(r"\b(?:sl|stop ?loss|stop|stoploss)\b\D{0,12}\d\.\d{2,5}", raw, re.I) and re.search(r"\b(?:tp|take ?profit|target|takeprofit)\b\D{0,12}\d\.\d{2,5}", raw, re.I)
                and re.search(r"(?:\bat\b|@|\bentry\b|\bentered\b|\bfrom\b)\D{0,8}\d\.\d{2,5}", raw, re.I)) and not re.search(r"\b(because|reason|checkpoint|milestone|thesis)\b", raw, re.I)


def _clauses(raw):
    parts = re.split(r"(?<=[.!?;])\s+(?=[A-Za-z])|\n+|\s+(?:but|and then)\s+|,\s+(?=(?:if|because|but|my|the|and|so|i|it|first|then|also|plus)\b)", raw)
    return [p.strip(" ,") for p in parts if p and p.strip(" ,")]


def _prices(s):
    return [float(x) for x in PX.findall(s)]


def _quote(s, n=140):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _add(lst, v):
    if v not in lst:
        lst.append(v)


def absorb(d, raw):
    """Fold one message into the draft. -> list of plain notes about what changed (in the user's words/prices). Never invents a value."""
    notes = []
    low = raw.lower()
    if d["side"] is None:
        b, s = SIDE_BUY.search(raw), SIDE_SELL.search(raw)
        if b and not s:
            d["side"] = "buy"; notes.append("a buy")
        elif s and not b:
            d["side"] = "sell"; notes.append("a sell")
    # entry: a price after "at / from / @ / entry / entered / in at", else the asked slot
    m = re.search(r"(?:\bat\b|@|\bentry\b|\bentered\b|\bfrom\b|\bin at\b|\bgot in\b)\D{0,10}" + _PX, raw, re.I)
    if m and d["entry"] is None:
        d["entry"] = float(m.group(1)); notes.append(f"in from {p5(d['entry'])}")
    for cl in _clauses(raw):
        cp = _prices(cl)
        if d["entry"] is not None:
            cp_ex = [x for x in cp if abs(x - d["entry"]) > 1e-9]
        else:
            cp_ex = cp
        taken = False
        pm = re.search(r"\b(?:stop ?loss|stoploss|stop|sl)\b\D{0,14}(\d+(?:\.\d+)?)\s*pips?\b", cl, re.I)
        tm = re.search(r"\b(?:target|tp|take ?profit)\b\D{0,14}(\d+(?:\.\d+)?)\s*pips?\b", cl, re.I)
        if pm:
            d["pip_inputs"]["stop"] = float(pm.group(1)); taken = True
        if tm:
            d["pip_inputs"]["target"] = float(tm.group(1)); taken = True
        sm = re.search(r"\b(?:stop ?loss|stoploss|stop|sl)\b\D{0,14}" + _PX, cl, re.I)
        tpm = re.search(r"\b(?:target|tp|take ?profit)\b\D{0,14}" + _PX, cl, re.I)
        if sm and tpm:                                                  # "stop 1.3380 target 1.3440" in one breath
            for k, mm in (("stop", sm), ("target", tpm)):
                v = float(mm.group(1))
                notes.append(f"{k} moved from {p5(d[k])} to {p5(v)}" if d[k] is not None and abs(d[k] - v) > 1e-9 else f"{k} {p5(v)}")
                d[k] = v
            cp_ex = [x for x in cp_ex if abs(x - d["stop"]) > 1e-9 and abs(x - d["target"]) > 1e-9]; taken = True
        elif CUE_STOP.search(cl) and cp_ex and not pm:
            new = float(sm.group(1)) if sm else cp_ex[0]
            if d["stop"] is not None and abs(d["stop"] - new) > 1e-9:
                notes.append(f"stop moved from {p5(d['stop'])} to {p5(new)}")
            else:
                notes.append(f"stop {p5(new)}")
            d["stop"] = new; cp_ex = cp_ex[1:]; taken = True
        elif CUE_TARGET.search(cl) and cp_ex and not tm and not CUE_FAIL.search(cl):
            far = max(cp_ex) if (d["side"] != "sell") else min(cp_ex)
            if d["target"] is not None and abs(d["target"] - far) > 1e-9:
                notes.append(f"target moved from {p5(d['target'])} to {p5(far)}")
            else:
                notes.append(f"target {p5(far)}")
            d["target"] = far
            for x in cp_ex:
                if abs(x - far) > 1e-9:
                    _add(d["milestones"], x); notes.append(f"{p5(x)} as a checkpoint on the way")
            cp_ex = []; taken = True
        if CUE_FAIL.search(cl) and not (CUE_STOP.search(cl) and not re.search(r"wrong|invalid|fail", cl, re.I)):
            _add(d["failure"], _quote(cl, 200)); notes.append("where you're wrong: \u201c" + _quote(cl, 80) + "\u201d")
            for x in cp_ex:
                _add(d["failure_px"], x)
            cp_ex = []; taken = True
        if CUE_MILE.search(cl) and cp_ex and not taken:
            for x in cp_ex:
                _add(d["milestones"], x)
            notes.append("checkpoint" + ("s " if len(cp_ex) > 1 else " ") + ", ".join(p5(x) for x in cp_ex)); cp_ex = []; taken = True
        rs = re.search(r"\b(?:because|since|cos|coz|reason(?: is| was)?)\b[: ]*(.+)$", cl, re.I)
        explicit_obs = re.search(r"\b(obstacle|obstacles|worr\w+|in the way|concern\w*)\b", cl, re.I)
        if rs and len(rs.group(1)) > 3 and not explicit_obs:
            _why = re.sub(r"^(?:of|that)\s+", "", rs.group(1).strip(), flags=re.I)
            _add(d["reasons"], _quote(_why, 200)); notes.append("you took it because \u201c" + _quote(_why, 80) + "\u201d")
        elif CUE_OBS.search(cl) and (not taken or explicit_obs):
            _add(d["obstacles"], _quote(cl, 200))
            for x in cp_ex:
                _add(d["obstacle_px"], x)
            notes.append("what could get in the way: \u201c" + _quote(cl, 80) + "\u201d"); taken = True
        elif CUE_MILE.search(cl) and not cp_ex and not taken and re.search(r"\b(pass\w*|prove\w*|confirm\w*|working|valid)\b", cl, re.I) and d["milestones"]:
            _add(d["milestone_notes"], _quote(cl, 200)); notes.append("passing them means \u201c" + _quote(cl, 80) + "\u201d")
        elif CUE_REASON.search(cl) and not taken:
            _add(d["reasons"], _quote(cl, 200)); notes.append("your reasoning: \u201c" + _quote(cl, 80) + "\u201d")
    return notes


def _place_asked(d, raw):
    """The user answered the question just asked without any cue word: put the answer where the question pointed."""
    a, notes = d.get("asking"), []
    if a is None:
        return notes
    px = _prices(raw)
    txt = _quote(raw, 200)
    if a == "entry" and px:
        d["entry"] = px[0]; notes.append(f"in from {p5(px[0])}")
    elif a == "stop" and px:
        d["stop"] = px[0]; notes.append(f"stop {p5(px[0])}")
    elif a == "target" and px:
        d["target"] = (max(px) if d["side"] != "sell" else min(px)); notes.append(f"target {p5(d['target'])}")
        for x in px:
            if abs(x - d["target"]) > 1e-9:
                _add(d["milestones"], x)
    elif a == "milestones" and px:
        for x in px:
            _add(d["milestones"], x)
        notes.append("checkpoints " + ", ".join(p5(x) for x in px))
    elif a == "side":
        if SIDE_BUY.search(raw) and not SIDE_SELL.search(raw):
            d["side"] = "buy"; notes.append("a buy")
        elif SIDE_SELL.search(raw) and not SIDE_BUY.search(raw):
            d["side"] = "sell"; notes.append("a sell")
    elif a == "reason" and len(raw.split()) >= 3:
        _add(d["reasons"], txt); notes.append("why you took it, in your words")
    elif a == "failure" and len(raw.split()) >= 3:
        _add(d["failure"], txt); notes.append("where you're wrong, in your words")
        for x in px:
            _add(d["failure_px"], x)
    elif a == "obstacles" and len(raw.split()) >= 3:
        _add(d["obstacles"], txt); notes.append("what could get in the way, in your words")
        for x in px:
            _add(d["obstacle_px"], x)
    return notes


def _apply_pips(d):
    """She said 'stop 13 pips' / 'target 48 pips': convert from her entry and SAY SO."""
    out = []
    if d["entry"] is None or d["side"] is None:
        return out
    sgn = 1 if d["side"] == "buy" else -1
    for k, mult in (("stop", -1), ("target", 1)):
        v = d["pip_inputs"].pop(k, None)
        if v is not None:
            d[k] = round(d["entry"] + sgn * mult * v * PIP, 5)
            out.append(f"I read {k} {pp(v)} pips as {p5(d[k])} ({pp(v)} pips {'below' if sgn * mult < 0 else 'above'} your entry): tell me if you meant a different price")
    return out


def essentials_missing(d):
    return [k for k in ("side", "entry", "stop", "target") if d[k] is None]


def conflicts(d):
    """Prices on the wrong side of each other. -> list of plain sentences."""
    out = []
    e, s, t, sd = d["entry"], d["stop"], d["target"], d["side"]
    if None in (e, sd):
        return out
    if s is not None and ((sd == "buy" and s >= e) or (sd == "sell" and s <= e)):
        out.append(f"For a {sd}, your stop {p5(s)} is {'above' if s >= e else 'below'} your entry {p5(e)} ({pp(abs(pips(s - e)))} pips), which would mean the stop is in profit territory. One of those prices (or the direction) is mixed up")
    if t is not None and ((sd == "buy" and t <= e) or (sd == "sell" and t >= e)):
        out.append(f"For a {sd}, your target {p5(t)} is {'below' if t <= e else 'above'} your entry {p5(e)} ({pp(abs(pips(t - e)))} pips), which is the wrong way for a {sd}. One of those prices (or the direction) is mixed up")
    return out


def _fmt_levels(d):
    e = d["entry"]
    parts = []
    if e is not None and d["stop"] is not None:
        parts.append(f"stop {p5(d['stop'])} ({pp(abs(pips(d['stop'] - e)))} pips {'below' if d['stop'] < e else 'above'} your entry)")
    if e is not None and d["target"] is not None:
        parts.append(f"target {p5(d['target'])} ({pp(abs(pips(d['target'] - e)))} pips {'above' if d['target'] > e else 'below'} your entry)")
    return parts


def observe(d, price_now=None, profile=None, engine=None, lot_fn=None):
    """Things the numbers say, each shown once. Pure arithmetic on the user's prices plus clearly labelled recorded values."""
    out, shown = [], d["shown"]
    e, s, t, sd = d["entry"], d["stop"], d["target"], d["side"]
    bad = conflicts(d)
    for b in bad:
        k = "conf:" + b[:40]
        if k not in shown:
            shown.append(k); out.append(b + ".")
    if bad:
        return out
    if None not in (e, s, t, sd) and "rr" not in shown:
        shown.append("rr")
        risk, rew = abs(pips(e - s)), abs(pips(t - e))
        out.append(f"On your prices the stop is {pp(risk)} pips away and the target {pp(rew)} pips, so the target pays about {rew / risk:.1f} times what the stop costs" + (" (that's below 1 to 1: you'd lose more than you'd win)" if rew < risk else "") + ".")
    if None not in (e, s) and profile and lot_fn and "lot" not in shown:
        shown.append("lot")
        r = lot_fn(abs(pips(e - s)), profile["balance"], profile["risk_pct"])
        if r and r.get("lots"):
            out.append(f"For size: at your profile's {profile['risk_pct'] * 100:g}% risk (${profile['balance']:,.0f} account) that stop works out to about {r['lots']:.2f} lots, if you want a number to check against what you used.")
    if e is not None and price_now and "entry_vs_now" not in shown and abs(price_now - e) >= 0.0010:
        shown.append("entry_vs_now")
        diff = pips(price_now - e)
        out.append(f"For reference, my newest recorded price is {p5(price_now)}, {pp(abs(diff))} pips {'above' if diff > 0 else 'below'} where you say you got in. That's only a comparison with my candle records, not a judgement.")
    if e is not None and t is not None and d["milestones"] and "mile" not in shown:
        shown.append("mile")
        lo, hi = sorted((e, t))
        inside = [m for m in d["milestones"] if lo <= m <= hi]
        outside = [m for m in d["milestones"] if m not in inside]
        ordered = sorted(inside, reverse=(sd == "sell"))
        if inside:
            out.append("Your checkpoints in the order price would meet them: " + ", ".join(f"{p5(m)} ({signed(pips(m - e))} pips from entry)" for m in ordered) + ".")
        if outside:
            out.append("These checkpoints are outside the stretch between your entry and your target: " + ", ".join(p5(m) for m in outside) + ". Did you mean them as something else?")
    if e is not None and t is not None and d["obstacle_px"] and "obs" not in shown:
        shown.append("obs")
        lo, hi = sorted((e, t))
        for x in d["obstacle_px"]:
            if lo <= x <= hi:
                out.append(f"The obstacle at {p5(x)} sits on the way: {pp(abs(pips(x - e)))} pips from your entry and {pp(abs(pips(t - x)))} pips short of your target.")
            else:
                out.append(f"The obstacle at {p5(x)} is {'beyond' if abs(x - e) > abs(t - e) else 'behind'} your target, so it wouldn't block the route to it.")
    if s is not None and d["failure_px"] and "fail" not in shown:
        shown.append("fail")
        for x in d["failure_px"]:
            if abs(x - s) > 1e-9:
                out.append(f"You said you're wrong around {p5(x)}, but your stop is at {p5(s)}, {pp(abs(pips(x - s)))} pips {'closer to' if (e is not None and abs(x - e) < abs(s - e)) else 'further from'} your entry. Is the stop where you actually get out, or do you leave earlier?")
    if s is not None and d["failure"] and any(re.search(r"\bclos", f, re.I) for f in d["failure"]) and "close_vs_stop" not in shown:
        shown.append("close_vs_stop")
        out.append("One thing to settle: you said you're wrong when a candle closes beyond a price, but a stop order fires the moment price touches it, before any candle closes. "
                   "So price can tag your stop and take you out even though no candle closed there. Do you plan a hard stop order, or to leave by hand when the candle closes?")
    if engine and engine.get("direction") and sd and "engine" not in shown:
        shown.append("engine")
        dr = str(engine["direction"]).lower()
        same = (sd == "buy" and "bull" in dr) or (sd == "sell" and "bear" in dr)
        opp = (sd == "buy" and "bear" in dr) or (sd == "sell" and "bull" in dr)
        if same or opp:
            out.append(f"For what it's worth, the engine's last recorded leg direction is {dr}{engine.get('when', '')}, which is {'the same side as' if same else 'the opposite side to'} your {sd}. That's the engine's reading, not a verdict on your idea.")
    return out


def _next_question(d, seed):
    miss = essentials_missing(d)
    qs = {
        "side": "Are you buying (long) or selling (short)?",
        "entry": "At what price did you get in?",
        "stop": "At what price would you know you're wrong and get out? Just the price is fine.",
        "target": "And what price do you want it to head to?",
    }
    for k in ("side", "entry", "stop", "target"):
        if k in miss:
            return k, qs[k]
    opt = [("reason", "What made you take it? Tell it however you think of it."),
           ("failure", "What would have to happen on the chart for you to say 'my idea is wrong'? Is it a price, or a candle closing somewhere?"),
           ("milestones", "Are there checkpoints price should pass on the way to prove the idea is working? Give me the prices, in order if you can."),
           ("obstacles", "Is anything in the way: a level, a news release, a session change?")]
    for k, q in opt:
        if k in d["skipped"] or k in d["asked"]:
            continue
        have = {"reason": d["reasons"], "failure": d["failure"], "milestones": d["milestones"], "obstacles": d["obstacles"]}[k]
        if not have:
            return k, q
    return "confirm", "That's everything I can think to ask. Anything to add or change, or shall I log it? (say 'log it')"


def summary(d, profile=None, lot_fn=None):
    e, s, t = d["entry"], d["stop"], d["target"]
    L = [f"Here's the trade as you told it: a {d['side']} on {d['pair']} from {p5(e)}."]
    L.append(", ".join(_fmt_levels(d)).capitalize() + ".")
    if e and s and t:
        L[-1] += f" About {abs(pips(t - e)) / abs(pips(e - s)):.1f} to 1."
    if d["reasons"]:
        L.append("Why: " + " / ".join(f"“{r}”" for r in d["reasons"]))
    if d["milestones"]:
        sd = d["side"]
        L.append("Checkpoints: " + ", ".join(f"{p5(m)} ({signed(pips(m - e))} pips)" for m in sorted(d["milestones"], reverse=(sd == "sell")))
                 + ("; in your words: " + " / ".join(f"\u201c{x}\u201d" for x in d["milestone_notes"]) if d["milestone_notes"] else ""))
    if d["failure"]:
        L.append("Where you're wrong: " + " / ".join(f"“{f}”" for f in d["failure"]))
    if d["obstacles"]:
        L.append("In the way: " + " / ".join(f"“{o}”" for o in d["obstacles"]))
    return "\n".join(L)


def story_text(d):
    """The user's own words plus the checkpoints in a fixed, readable form, stored with the trade as her reasoning. Nothing here is mine."""
    bits = []
    if d["reasons"]:
        bits.append("Why I took it: " + " / ".join(d["reasons"]))
    if d["milestones"]:
        bits.append("Checkpoints (price should pass these to prove the idea): " + ", ".join(p5(m) for m in sorted(d["milestones"], reverse=(d["side"] == "sell"))) + (" [" + " / ".join(d["milestone_notes"]) + "]" if d["milestone_notes"] else ""))
    if d["failure"]:
        bits.append("Where I'm wrong: " + " / ".join(d["failure"]) + (" [prices: " + ", ".join(p5(x) for x in d["failure_px"]) + "]" if d["failure_px"] else ""))
    if d["obstacles"]:
        bits.append("In the way: " + " / ".join(d["obstacles"]) + (" [prices: " + ", ".join(p5(x) for x in d["obstacle_px"]) + "]" if d["obstacle_px"] else ""))
    return " | ".join(bits)


def pick(opts, seed):
    s = str(seed)
    return opts[(abs(int(s)) if s.lstrip("-").isdigit() else sum(map(ord, s))) % len(opts)]


def turn(d, raw, price_now=None, profile=None, engine=None, lot_fn=None, seed=0):
    """-> (reply text, draft, action). action is None, or {'confirm': True} when the user asked to log and everything needed is there."""
    d["turns"] += 1
    t = (raw or "").strip()
    asking = d.get("asking")
    # --- answering the "which is it?" question about a message that fitted nowhere
    if asking == "unplaced" and d.get("unplaced"):
        slot = _slot_word(t)
        held = d["unplaced"]; d["unplaced"] = None; d["asking"] = None
        if re.search(r"nothing to do|unrelated|not (?:about|part of|related to) (?:the |this )?trade|ignore (?:that|it)|never ?mind|forget (?:it|that)|just (?:venting|talking|a joke)|\bjoking\b", t, re.I):
            k, q = _next_question(d, seed); d["asking"] = k
            return "Okay, I've left that out of the trade. " + q, d, None
        if slot:
            key = {"reason": "reasons", "obstacle": "obstacles", "failure": "failure"}.get(slot)
            if key:
                _add(d[key], _quote(held, 200))
                return _continue(d, [f"{_label(slot)}, in your words"], price_now, profile, engine, lot_fn, seed)
            if slot == "checkpoint":
                px = _prices(held)
                if px:
                    for x in px:
                        _add(d["milestones"], x)
                    return _continue(d, ["checkpoint " + ", ".join(p5(x) for x in px)], price_now, profile, engine, lot_fn, seed)
                return ("A checkpoint needs a price. Which price should it pass? (I kept your words: “" + _quote(held, 100) + "”)", _hold(d, "milestones"), None)
        if SKIP.match(t):
            return _continue(d, ["I'll leave that out"], price_now, profile, engine, lot_fn, seed)
    ng = NEG_SLOT.match(t)
    if ng and not _prices(t):
        slot = {"obstacle": "obstacles", "milestone": "milestones", "reason": "reasons"}.get(next((k for k in ("obstacle", "milestone", "reason") if re.search({"obstacle": r"in the way|obstacle|block|resistance|worr", "milestone": r"check ?point|milestone", "reason": r"reason"}[k], ng.group(0), re.I)), ""), asking)
        if slot:
            _add(d["skipped"], slot); d["asking"] = None
            k, q = _next_question(d, seed); d["asking"] = k; _add(d["asked"], k)
            return "Okay, nothing there. " + q, d, None
    side = _side_answer(d, t, price_now, profile, lot_fn)
    if side:
        return side, d, None
    fix = _correct_conflict(d, t)
    if fix:
        return _continue(d, [fix], price_now, profile, engine, lot_fn, seed)
    if FINISH.match(t) or (YES.match(t) and asking == "confirm"):
        miss = essentials_missing(d)
        bad = conflicts(d)
        if miss or bad:
            k, q = _next_question(d, seed)
            d["asking"] = k
            lead = ("Before I can log it I still need " + _list([{"side": "whether it's a buy or a sell", "entry": "the price you got in at", "stop": "the price where you're wrong (your stop)", "target": "the price you want it to reach"}[m] for m in miss]) + ". ") if miss else ("I can't log it yet. " + bad[0] + ". Which price should I use?" if False else "I can't log it yet. " + bad[0] + ". ")
            return lead + q, d, None
        d["asking"] = "confirm_log"
        return summary(d, profile, lot_fn) + "\n\nSay yes and I'll log it (the trade, with all of this in your own words), or tell me what to change.", d, {"confirm": True}
    if asking == "confirm_log" and NO.match(t):
        d["asking"] = None
        return "Okay, I won't log it yet. What should I change?", d, None
    if SKIP.match(t) and asking in ("reason", "failure", "milestones", "obstacles"):
        _add(d["skipped"], asking); d["asking"] = None
        k, q = _next_question(d, seed); d["asking"] = k; _add(d["asked"], k)
        return pick(("Okay, leaving that out. ", "Fine, nothing there. ", "No problem. "), seed) + q, d, None
    # --- ordinary content
    notes = absorb(d, t)
    notes += _apply_pips(d)
    if not notes:
        notes = _place_asked(d, t)
    if not notes:
        # nothing fits anywhere: ASK what it was, never pretend
        d["unplaced"] = _quote(t, 300); d["asking"] = "unplaced"
        return ("I don't want to guess where that fits. You said: \u201c" + _quote(t, 120) + "\u201d. Is it (1) why you took the trade, (2) something that could get in the way, "
                "(3) a checkpoint price should pass, or (4) where you'd be wrong? Or something else, or nothing to do with the trade? Just tell me."), d, None
    return _continue(d, notes, price_now, profile, engine, lot_fn, seed)


_FIXCUE = re.compile(r"\b(i mean|i meant|meant|oops|oh wait|wait|sorry|typo|actually|instead|rather|make it|i said|my bad|mistake|lol|correction|change (?:it )?to|no)\b", re.I)


def _correct_conflict(d, t):
    """'oh wait that's above my entry, I mean 1.3405': a lone price after a wrong-side warning replaces the price that was flagged."""
    px = _prices(t)
    e, sd = d["entry"], d["side"]
    if len(px) != 1 or None in (e, sd) or CUE_TARGET.search(t) and not CUE_STOP.search(t):
        return None
    if not (_FIXCUE.search(t) or len(t.split()) <= 3):
        return None
    v, s_, tg = px[0], d["stop"], d["target"]
    if s_ is not None and ((sd == "buy" and s_ >= e) or (sd == "sell" and s_ <= e)):
        if (sd == "buy" and v < e) or (sd == "sell" and v > e):
            d["stop"] = v
            return f"stop moved from {p5(s_)} to {p5(v)}"
    if tg is not None and ((sd == "buy" and tg <= e) or (sd == "sell" and tg >= e)):
        if (sd == "buy" and v > e) or (sd == "sell" and v < e):
            d["target"] = v
            return f"target moved from {p5(tg)} to {p5(v)}"
    return None


_INWAY = re.compile(r"^\W*(?:ok(?:ay)?[, ]*)?(?:what'?s|whats|what is|is there anything|is anything|anything|any)\b.{0,12}\b(in the way|blocking|resistance|obstacles?)\b", re.I)
_RISKQ = re.compile(r"\b(risk|lots?|size|how much)\b.{0,25}\b(if|with|at|using|for)\b.{0,14}?(\d+(?:\.\d+)?)\s?%?", re.I)


def _side_answer(d, t, price_now, profile, lot_fn):
    """Questions asked in the middle of the trade talk are answered, then the talk carries on where it was."""
    if is_check(t):
        e, sd = d["entry"], d["side"]
        out = "You haven't logged this one yet, so all I can read is your draft."
        if price_now and e and sd:
            mv = (price_now - e) * (1 if sd == "buy" else -1)
            out += f" Price is {p5(price_now)}, {pp(abs(pips(mv)))} pips {'in your favour' if mv >= 0 else 'against you'} compared with your entry {p5(e)}."
            hit = [m for m in d["milestones"] if (price_now >= m if sd == "buy" else price_now <= m)]
            if d["milestones"]:
                out += (f" Price is currently beyond {', '.join(p5(x) for x in hit)}" if hit else " Price hasn't reached any of your checkpoints yet") + " (that's where it is now, not whether it got there earlier; say 'log it' and I'll check it against the candles properly)."
            if d["stop"] is not None and ((sd == "buy" and price_now <= d["stop"]) or (sd == "sell" and price_now >= d["stop"])):
                out += " Price is past your stop level right now."
        return out + " Want to carry on with the rest, or log it as it stands?"
    if _INWAY.search(t) and not _prices(t):
        d["asking"] = "obstacles"
        return ("I can't see your chart's levels, only my recorded candles and the engine's reading, so I can't tell you what's in the way. "
                "If you can see something (old highs, a news release, a session change), tell me where and I'll record it and measure the distance. Anything there?")
    m = _RISKQ.search(t)
    if m and profile and lot_fn and d["entry"] is not None and d["stop"] is not None and not _prices(t):
        pct = float(m.group(3))
        pct = pct / 100 if pct >= 0.1 or "%" in m.group(0) else pct
        if 0 < pct < 0.5:
            r = lot_fn(abs(pips(d["entry"] - d["stop"])), profile["balance"], pct)
            if r and r.get("lots"):
                return (f"At {pct * 100:g}% risk on ${profile['balance']:,.0f}, that's ${r['risk_usd']:,.2f} if the stop at {p5(d['stop'])} ({pp(abs(pips(d['entry'] - d['stop'])))} pips) is hit, which works out to {r['lots']:.2f} lots "
                        f"(rounded down). Your own setting is {profile['risk_pct'] * 100:g}%. That's only arithmetic, not a view on what to risk. Shall we carry on with the trade?")
    return None


def _slot_word(t):
    low = t.lower()
    if re.search(r"\b(why|reason|took)\b|^1\b", low):
        return "reason"
    if re.search(r"\b(obstacle|way|block|in the way|get in)\b|^2\b", low):
        return "obstacle"
    if re.search(r"\b(check ?point|milestone|pass)\b|^3\b", low):
        return "checkpoint"
    if re.search(r"\b(wrong|fail|invalid)\b|^4\b", low):
        return "failure"
    return None


def _label(slot):
    return {"reason": "Why you took it", "obstacle": "What could get in the way", "failure": "Where you're wrong"}.get(slot, "That")


def _list(xs):
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def _hold(d, asking):
    d["asking"] = asking
    return d


def _continue(d, notes, price_now, profile, engine, lot_fn, seed):
    notes = [n for n in notes if n]
    lead = pick(("Got it: ", "Okay, so ", "Understood: ", "Right, "), seed) + _list(notes[:6]) + "."
    obs = observe(d, price_now, profile, engine, lot_fn)
    k, q = _next_question(d, seed)
    d["asking"] = k
    _add(d["asked"], k)
    body = lead + ((" " + " ".join(obs)) if obs else "")
    return body + "\n\n" + q, d, None


# ----------------------------------------------------------------------
# CHECKING A LOGGED TRADE AGAINST WHAT HAPPENED (recorded candles; the checkpoints and failure conditions are the user's own)
# ----------------------------------------------------------------------
CHECK = re.compile(r"\bhow('s| is| are)\b.{0,20}\b(my|the|that|this)\b.{0,15}\b(trade|position|buy|sell|setup|case|thesis|idea)\b|"
                   r"\b(trade|position|buy|sell|case|thesis|idea)\b.{0,20}\b(doing|going|status|progress|looking|developing|playing out)\b|"
                   r"\b(has|have|did|is|are)\b.{0,25}\b(check ?points?|milestones?)\b.{0,25}\b(hit|reach\w*|pass\w*|made|happen\w*|ticked)\b|"
                   r"\b(check ?points?|milestones?)\b.{0,30}\b(hit|reached|passed|progress|status)\b|\b(hit|reached|passed|made|ticked)\b.{0,20}\b(check ?points?|milestones?)\b|"
                   r"\bis (my |the )?(thesis|idea|trade|setup) (still )?(valid|working|alive|intact|on|good)\b|\bcheck (on )?(my|the) (trade|position|buy|sell|case)\b", re.I)


def is_check(raw):
    t = (raw or "").strip()
    return bool(t) and not t.startswith("/") and bool(CHECK.search(t))


def parse_story(text):
    """The checkpoints, failure prices and her words back out of the stored reasoning text. -> dict(milestones, failure_px, failure, notes)."""
    out = {"milestones": [], "failure_px": [], "failure": [], "obstacles": []}
    for part in (text or "").split(" | "):
        if part.startswith("Checkpoints"):
            out["milestones"] = _prices(part.split(":", 1)[1].split("[", 1)[0])
        elif part.startswith("Where I'm wrong"):
            body = part.split(":", 1)[1]
            m = re.search(r"\[prices: ([^\]]*)\]", body)
            out["failure_px"] = _prices(m.group(1)) if m else []
            out["failure"] = [body.split(" [prices:")[0].strip()]
        elif part.startswith("In the way"):
            out["obstacles"] = [part.split(":", 1)[1].split(" [prices:")[0].strip()]
    return out


def check(trade, story, candles, now, fmt_when, price_now=None):
    """trade: payload dict (direction, entry, sl, tp, case_id) + 'since' (datetime the trade was logged). candles: list of dicts t,o,h,l,c at or after `since`, oldest first.
    -> plain text. Every statement is a comparison of the user's own prices with recorded candles; nothing about the outcome is assumed."""
    sd = "buy" if str(trade["direction"]).upper() == "BUY" else "sell"
    e, sl, tp = trade["entry"], trade["sl"], trade["tp"]
    since = trade["since"]
    if not candles:
        return (f"I have no recorded candles since you logged {trade['case_id']} ({fmt_when(since)}), so I can't say how it has gone. I won't guess.")
    last = candles[-1]
    cur = price_now if price_now is not None else last["c"]
    pl = pips(cur - e) * (1 if sd == "buy" else -1)
    L = [f"{trade['case_id']}: your {sd} from {p5(e)}. Price is {p5(cur)} ({signed(pl)} pips {'in your favour' if pl > 0 else ('against you' if pl < 0 else 'flat')}), measured from the candles I recorded since you logged it ({fmt_when(since)})."]

    def when(c):
        return fmt_when(c["t"]) + (" (that candle began before you logged the trade, so I can't tell whether it came before or after)" if c["t"] < since else "")

    def first_touch(level, up):
        for c in candles:
            if (up and c["h"] >= level) or ((not up) and c["l"] <= level):
                return c
        return None
    up_dir = sd == "buy"
    hit_stop = first_touch(sl, not up_dir)
    hit_tp = first_touch(tp, up_dir)
    reached = []
    for m in sorted(story.get("milestones") or [], reverse=not up_dir):
        c = first_touch(m, up_dir)
        reached.append((m, c))
    if reached:
        rows = []
        for m, c in reached:
            rows.append(f"{p5(m)} ({signed(pips(m - e))} pips from entry): " + (f"reached, first at {when(c)}" if c else f"not reached yet, {pp(abs(pips(m - cur)))} pips {'above' if m > cur else 'below'} price"))
        L.append("Your checkpoints: " + "; ".join(rows) + ".")
        n_hit = sum(1 for _, c in reached if c)
        L.append(f"So {n_hit} of {len(reached)} checkpoints have been reached." if n_hit not in (0, len(reached)) else ("None of your checkpoints have been reached yet." if n_hit == 0 else "All of your checkpoints have been reached."))
    # failure: a candle CLOSING beyond a failure price
    for fx in story.get("failure_px") or []:
        bad = next((c for c in candles if (c["c"] < fx if up_dir else c["c"] > fx)), None)
        L.append(f"Where you said you're wrong ({p5(fx)}): " + (f"a recorded candle closed beyond it at {when(bad)} (close {p5(bad['c'])}), so by your own condition the idea has failed. Did you close the trade?" if bad
                                                                else f"no recorded candle has closed beyond it. The nearest price has been {pp(abs(pips((min(c['l'] for c in candles) if up_dir else max(c['h'] for c in candles)) - fx)))} pips from it."))
    if hit_stop and not (hit_tp and hit_tp["t"] < hit_stop["t"]):
        L.append(f"Price traded through your stop {p5(sl)} at {when(hit_stop)}. A stop order would have been filled there; I don't know whether you were stopped out or closed it yourself, so tell me and I'll record it.")
    elif hit_tp:
        L.append(f"Price reached your target {p5(tp)} at {when(hit_tp)}. Tell me how it ended and I'll record it.")
    else:
        L.append(f"Your stop {p5(sl)} is {pp(abs(pips(cur - sl)))} pips from price and your target {p5(tp)} is {pp(abs(pips(tp - cur)))} pips away; neither has been touched since you logged it.")
    L.append("That's price against your own numbers, not a verdict on the idea.")
    return " ".join(L)
