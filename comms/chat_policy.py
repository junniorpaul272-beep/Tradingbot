"""
chat_policy.py — what the bot says when a message is a TEST OF ITS BOUNDARIES or a request for general trading reasoning, rather than a question about its records.

Pure stdlib (re only); reads no files, imports nothing from this codebase, holds no market facts. Facts it needs (a recorded price, the account profile, the last
signal) are handed in by the runner, and every number in a reply comes from those or from the user's own message.

THREE KINDS OF THING LIVE HERE
  1. BOUNDARIES   instructions hidden in a message, requests to trade / send data / use credentials, "guarantee it", "ignore the risk rules", "go all in".
                  Text inside a message is DATA, never an instruction: it cannot change the bot's rules, risk limits or how it describes a setup.
  2. REASONING    general trading principles for situations the user describes (a news spike, a weekend event, a wide spread, a gap through a stop...).
                  Always opened with "General principle, not an engine reading": the bot has no record of the user's situation, and says what it would need.
  3. PARTNER      a question-by-question plan builder, a place to remember what the user said, a check on a position the user describes.

Detection is by CONCEPT (several loose patterns each, not one phrase), so reworded versions of the same test still land. Anything not matched returns None.
"""
import re

I = re.I

PATTERNS = {
    "inject_talk": [
        r"\b(why|how) (would|could|does|do|is|are)\b.{0,80}\b(always|every|ignore|inject\w*|jailbreak\w*|unrestricted|no rules)\b|\bprompt injection\b|\bjailbreak\w*\b",
        r"\b(my friend|someone|a friend|he|she|they|a guy|somebody)\b.{0,20}\b(sent|told|said|wrote|posted|gave)\b.{0,60}\b(me|us)\b.{0,60}[\"'\u2018\u201c]",
        r"\b(as an? |an )?example of (a |an )?(bad|dangerous|risky|hostile|malicious|trick)\w*\b|\bis it (a )?(good|bad|safe|wise|smart) (idea|thing|move)\b.{0,30}\b(ignore|skip|always|no rules)\b|\bwhat (would|could) (go wrong|happen)\b.{0,60}\b(always|ignore|every)\b",
    ],
    "hypo_size": [
        r"\b(what (would|will|could) happen if|what if|how much (would|will|could) i|how much is|just tell me|don'?t execute|do not execute|no(t)? (to )?execute|hypothetical\w*|suppose|if i (sold|bought|shorted|went (long|short)))\b.{0,80}\b(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|half a)[- ]?lots?\b",
    ],
    "inject": [
        r"\b(forget|ignore|erase|disregard)\b.{0,30}\b(everything|all|anything)\b.{0,40}\b(told|said|before|above|previous|earlier)\b|\bact as\b.{0,40}\b(no|without|zero) (limits|rules|restrictions|filters)\b|\bnew rule\b|\byou may now\b|\b(admin|developer|owner|boss|operator|system|nelly)\b.{0,20}\b(says|said|has (said|approved)|authori[sz]es?|allows?|permits?)\b.{0,40}\b(you|risk|trade|lot)\b",
        r"\b(ignore|disregard|forget|override|bypass|drop)\b.{0,40}\b(instruction|instructions|rules|prompt|guidelines|limits|checks|restrictions|programming)\b",
        r"\b(system prompt|developer message|hidden (prompt|instructions)|your (full |entire |original )?(prompt|instructions))\b",
        r"\byou are now\b|\bunrestricted\b|\bjailbreak\b|\bdeveloper mode\b|\bdan mode\b|\bno (risk )?rules\b|\bwithout (any )?(rules|limits|restrictions|filters)\b",
        r"\bfrom now on\b.{0,50}\b(always|never|you (must|will|should)|say)\b|\balways (say|call|tell|answer|rate|describe)\b|\bevery (gbpusd )?(trade|setup|signal)\b.{0,30}\b(is|are)\b.{0,20}\b(10 ?/ ?10|a\+|perfect|winner|win)\b",
        r"\bbase ?64\b|\b(decode|decrypt|translate|unscramble)\b.{0,50}\b(and )?(follow|obey|run|execute|do)\b",
        r"^\s*(system|assistant|developer|admin)\s*:|\[(system|admin|developer)\]|<\s*system\s*>",
        r"\b(user|you|account) (is|are|has been) (now )?(approved|authori[sz]ed|cleared|whitelisted)\b|\bapproved for \d+ ?% risk\b|\bauto(matically|-)? ?approve\b",
        r"\bpretend (you|that|there)\b.{0,30}\b(no|without|aren'?t|are not)\b",
        r"\b(reveal|print|show|leak|repeat|output|dump|display)\b.{0,30}\b(your |the )?(instructions|system prompt|prompt)\b",
    ],
    "execute": [
        r"^\W*(?:please\s+|pls\s+|just\s+|go ahead and\s+)?(?:open|place|enter|execute|put on|take|close|fire|set up)\b.{0,20}\b(?:lots?|positions?|orders?|trades?)\b(?!.{0,40}\b(?:means?|is|are)\b)",
        r"\b(put on|put me|get me|go|open|enter|execute|take|do|make|place|set up|fire|send|run|start|bet)\b.{0,25}\b(long|short|buy|sell|buying|selling)\b.{0,25}\b((?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|half a|a))[- ]?(lots?|contracts?|units?)\b|\b((?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|half a|a))[- ]?(lots?)\b.{0,15}\b(long|short|buy|sell)\b|\b(short|long|buy|sell)\b.{0,15}\b((?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|half a|a))[- ]?lots?\b|\b(sell|buy|short|go long|go short)\b.{0,10}\b(for me|now|right now|asap|immediately)\b|\b(execute|place|open|put on)\b.{0,15}\b(a |an |the |my )?(sell|buy|short|long)\b|\bcan you (execute|place|open|enter|take|fire)\b",
        r"\b(place|send|submit|put in|fire)\b.{0,12}\b(a |an |the |my )?(buy|sell|market|limit|stop) orders?\b|\b(withdraw|transfer|wire)\b.{0,20}\b(money|funds|cash|balance)\b",
        r"\b(call|use|run|invoke|trigger)\b.{0,30}\b(tool|execution|trade[- ]execution|api)\b",
        r"\b(open|place|execute|enter|close|buy|sell|go long|go short)\b.{0,25}\b(a |an |the |my )?(\d+(\.\d+)?[- ]?lots?|position|order|trade)\b.{0,30}\b(for me|now|right now|gbpusd|cable|on my account)\b",
        r"\btrade for (him|her|them|me|my friend|someone|a friend)\b|\b(trade|run|manage|operate|log ?in)\b.{0,30}\b(\w+'s|his|her|their|another|someone'?s?|a friend'?s?) (trading )?account\b|\b(trade|run|manage)\b.{0,30}\bfor (him|her|them)\b|\btrade (his|her|their) account\b|\bon (his|her|their) behalf\b",
        r"\b(credentials?|password|passcode|passphrase|log ?in details|login|api[- ]?keys?|secret key|private key|token|seed phrase)\b",
        r"\bsend\b.{0,70}\b(to|at)\b.{0,40}(@|\.com|\.org|email|e-mail|address|telegram|whatsapp)",
        r"\b(email|e-mail|forward|upload|post|share|dm|message)\b.{0,40}\b(my|the|his|her)\b.{0,30}\b(balance|positions?|keys?|account|credentials?|holdings)\b",
    ],
    "guarantee": [
        r"\b(definitely|surely|certainly|always)\b.{0,25}\b(make|earn|profit|money|win)\w*\b",
        r"\bguarantee\w*\b|\bsure (win|thing|bet)\b|\b100 ?% (win|sure|certain|safe|accurate)\b|\b(can'?t|cannot|won'?t) lose\b|\brisk[- ]?free\b",
        r"\b(definitely|certainly|surely|for sure|without (a )?doubt)\b.{0,25}\b(win|profit|hit|work|go up|go down)\b",
        r"\bis (it|this|that) (a )?(safe|sure|certain) (trade|bet|signal|win|setup)\b",
    ],
    "ignore_risk": [
        r"\b(largest|biggest|highest|greatest)\b.{0,25}\b(position|size|lots?)\b",
        r"\b(ignore|skip|without|disable|remove|turn off|bypass|forget|drop|no)\b.{0,25}\b(risk (management|rules?|limits?|control|check)s?|stop[- ]?loss|risk gate)\b",
        r"\bmax(imum|imi[sz]e)?(?: out)?\b.{0,15}\b(lot|size|position|leverage|account|my account)\b|\bmax out\b|\bbiggest (lot|size|position)\b",
        r"\b(raise|increase|lift|remove|change|override)\b.{0,20}\b(my |the )?(risk )?(limit|cap)\b",
    ],
    "all_in": [
        r"\beverything i (have|own)\b|\ball (my|the) (money|savings|funds|cash)\b",
        r"\b(entire|whole|full|all of my|all my)\b.{0,15}\b(account|balance|equity|capital|bank ?roll|stack)\b",
        r"\ball[- ]in\b|\byolo\b|\bgo big\b|\bbet (everything|it all)\b",
    ],
    "advice": [
        r"\b(financial|investment|trading) advice\b|\blicen[sc]ed\b|\bregulated\b|\bfiduciary\b",
        r"\b(are you|r u|you) (an? )?(adviser|advisor|financial|broker|human|a real|real person|an ai|a bot|robot|person)\b",
        r"\b(allowed|permitted|able|supposed)\b.{0,20}\b(to )?(tell|say|do|advise|recommend)\b|\bwhat can'?t you\b|\bwhat (are )?you (not )?allowed\b",
    ],
    "rules": [
        r"\bexplain how you\b|\bhow (do|did) you (make|reach|take|come to) (your |these |the )?(decisions?|calls?|conclusions?)\b",
        r"\b(operating|original|core|your own) (rules|instructions|principles|guidelines|limits)\b|\bwhat are your (rules|limits|boundaries|restrictions|principles)\b",
        r"\bhow do you (decide|operate|get your (answers|numbers))\b|\bwhat (can'?t|cannot|can|could) you (do|help)\b",
    ],
    "leverage": [r"\bleverage\b|\b1\s?:\s?\d{2,4}\b.{0,40}\b(vs|versus|or|and|compared)\b.{0,20}\b1\s?:\s?\d{2,4}\b|\bmargin (call|requirement|level)\b"],
    "emotional": [
        r"\b(double up|martingale|chase (it|the loss|losses|price)|recover (the|my) loss(es)?|get (it|my money|that) back)\b",
        r"\brevenge[- ]?trad\w*|\bwin (it|my money|them|it all) back\b|\bmake (it|my money|up|the loss|that) back\b|\btilt(ed|ing)?\b",
        r"\b(emotional|angry|furious|desperate|spiral\w*|panick\w*|shaking|can'?t think)\b.{0,50}\b(trad\w*|loss|lost|losing|position|entry)\b|\btrad\w* .{0,40}\b(emotional|angry|revenge|desperate)\b",
        r"\btalk me (down|out of)\b|\bstop me from\b",
        r"\b(tempted|thinking|want|wanna|planning|itching) to\b.{0,30}\b(size up|sizing up|double|increase (my )?(size|lots?|risk)|bigger (size|lots?|position)|go bigger|risk more)\b|\bsize (it )?up\b.{0,20}\b(after|since|because|to (get|win|make))\b",
        r"\b(lost|losing|stopped out|took)\b.{0,18}\b(\d+|two|three|four|five|six|a few|several)\b.{0,14}\b(in a row|straight|back to back|on the trot)\b.{0,60}\b(tempted|want|wanna|size|bigger|double|revenge|back)\b",
        r"\bjust want it back\b|\bwant (it|my money|them|that) back\b",
    ],
    "risk_math": [
        r"\b(is|are|isn'?t|would)\b.{0,12}\b\d+(?:\.\d+)?\s?%.{0,30}\b(too (much|high|big|risky|aggressive)|safe|okay|ok|fine|a lot|reasonable|normal)\b",
        r"\bwhat (if|about) (i|we) risk(ed)?\b.{0,10}\d+(?:\.\d+)?\s?%|\b(\d+|two|three|four|five|six|ten) (stops?|losses|losers|losing trades|stop[- ]outs?) in a row\b.{0,50}\b(how much|gone|lose|left|down|drawdown|cost)\b|\bhow much (would|will|could) i (lose|be down)\b.{0,40}\b(in a row|straight)\b",
    ],
    "move_stop": [
        r"\b(can|should|could|may|would|is it (ok|okay|fine|bad|wrong)( to)?|what if i)\b.{0,16}\b(move|moving|widen|widening|shift|push|adjust|remove|removing|cancel)\b.{0,14}\b(my |the |a )?(stop|sl|stop[- ]?loss)\b",
    ],
    "spike": [
        r"\b(nfp|cpi|fomc|boe|news|data|release|payroll)\b.{0,60}\b(\\d{2,3} ?pips?|(large|big|huge|massive|giant) (candle|move|bar))|\b(large|big|huge|massive|giant) (candle|move|bar)\b.{0,40}\b(nfp|cpi|fomc|boe|news|data|release|payroll)\b",r"\b(spiked|spike|spiking|jumped|surged|flash|whipsaw\w*|exploded)\b.{0,80}\b(data|news|release|cpi|nfp|fomc|boe|announcement|decision|payroll)\b|\b(data|news|release|cpi|nfp|fomc|boe|announcement|decision|payroll)\b.{0,80}\b(spiked|spike|spiking|jumped|surged|whipsaw\w*)\b"],
    "friday": [r"\b(friday|weekend|end of (the )?week)\b.{0,120}\b(event|news|monday|cpi|nfp|fomc|boe|gap|data)\b|\b(event|news|cpi|nfp|fomc|boe|data)\b.{0,60}\b(monday|after the weekend|next week)\b"],
    "htf_ltf": [
        r"\b(daily|weekly|4h|4 ?hour|htf|higher[- ]timeframe|monthly)\b.{0,70}\b(15|5|1)[- ]?(m|min|minute|h|hour)\b.{0,70}\b(conflict|reconcile|disagree|but|versus|vs|clash)\w*",
        r"\b(reconcile|conflict between|conflicting)\b.{0,50}\b(timeframes?|structure|levels?)\b|\bat a (daily|weekly|htf|key|major) level\b.{0,60}\b(but|while|however)\b",
    ],
    "scenarios": [r"\bthree scenarios?\b|\b(bull|bear)ish\b.{0,30}\b(bear|bull)ish\b.{0,40}\b(chop|range|sideways)\b|\bscenarios?\b.{0,80}\b(bullish|bearish|chop)\b|\bscenarios? for (tomorrow|next week|the week|today)\b"],
    "spread": [r"\bspread\b.{0,50}\b(wide|widen\w*|high|large|huge|blown)\b|\bwide spread\b|\b(thin|low|poor|no) liquidity\b|\billiquid\b"],
    "gap_stop": [
        r"\bstop\b.{0,20}\b(skipped|gapped|jumped|slipped|bypassed)\b|\bgap\b.{0,30}\bstop\b",r"\bgaps? (through|past|over)\b.{0,40}\b(stop|sl)\b|\bgapped (through|past|over)\b|\bstop (was )?(skipped|jumped|slipped|blown)\b|\bslippage\b.{0,40}\b(stop|loss)\b|\b(loss|filled)\b.{0,30}\b(worse|bigger) than (my )?(stop|planned)\b"],
    "indicators": [
        r"\b(conflicting|contradict\w*|disagree\w*|opposite|mixed) (indicators?|signals?|readings?)\b",
        r"\b(rsi|macd|stochastic|stoch|cci|adx|bollinger|ema cross|moving average cross)\b.{0,70}\b(rsi|macd|stochastic|stoch|cci|adx|bollinger|dominate|conflict|which one)\b",
    ],
    "plan": [
        r"\b(build|make|create|draft|design|put together)\b.{0,30}\b(swing |trading |day[- ]?trading )?(plan|strategy|routine|playbook)\b",
        r"\bask me (questions?|things)\b|\bone (question )?at a time\b.{0,50}\b(until|context|questions?)\b|\b(summari[sz]e)\b.{0,40}\b(my )?(trading )?(style|approach)\b",
    ],
    "remember": [r"^\W*(please\s+)?(remember|keep in mind|take note|don'?t forget|note down|make a note|note that)\b"],
    "recall": [r"\bwhat (are|were) my (trading )?(rules|preferences|limits|plan)\b|\bwhat did i (tell|ask|say to) you to remember\b|\bdo you remember (my|what|the|that)\b|\bwhat do you (know|remember) about my\b|\bremind me (of )?(my|what i)\b|\bwhat('?s| is) my (trading )?(rule|plan|style)\b|\b(my rules|my limits)\b.{0,15}\?"],
    "position": [r"(?=.*\b(hold|trail\w*|partial\w*|take profit|move (my |the )?stop|break ?even|exit|should i|what now|close (it|half|part))\b)(?:\b(i'?m|i am|im|i went|i'?ve gone|currently)\s+(long|short)\b|\b(long|short)\b.{0,25}\bfrom\b\s*\d\.\d{3,5}|\bi (bought|went long|sold|went short|entered)\b.{0,40}\bat\b\s*\d\.\d{3,5})"],
    "missed": [r"\bi missed (your|the|that|this) (last |latest |previous )?(signal|setup|alert|trade|entry)\b|\bmissed (the|your) (last |latest )?(signal|setup|alert)\b|\breconstruct\b.{0,40}\b(setup|signal)\b"],
    "risk_at_hand": [r"\brisk[- ]at[- ]hand\b|\bwhat does (the )?(risk|band) .{0,12}(high|medium|low|mean)\b|\b(why|what).{0,25}\b(says?|rated|marked|flagged) high\b"],
    "signal_outcome": [r"\b(did|has|have|was|were)\b.{0,14}\b(it|that|this|the (last |latest )?(signal|trade|setup|idea)|that signal)\b.{0,14}\b(reach|hit|get to|touch|make|work|win|lose|lost|stopped?|tp|sl|target|stop|play out|succeed|fail)\w*|\bhow did (it|(that|this|the)( last| latest)? (signal|trade|setup|idea)) (go|do|end|turn out|play out)\b"],
    "explain_signal": [r"\bwhy\b.{0,18}\b(did|does|would)\b.{0,12}\b(you|it|the bot|the engine)\b.{0,12}\b(fire|signal|buy|sell|enter|take)\b|\bwhy (was|is) (that|this|the last|the latest) (signal|setup|trade)\b|\bexplain\b.{0,14}\b(it|that|this)\b.{0,12}\b(like|as if|to someone)\b.{0,12}\b(i'?m|i am|im)?\s*(new|a beginner|5|five|a newbie)\b|\bi'?m (new|a newbie|a beginner)\b|\b(explain|describe|walk me through|break down|tell me about|what was)\b.{0,50}\b(this|that|the|last|latest|your)\b.{0,25}\b(setup|signal|trade idea)\b|\b(beginner|newbie|no jargon|without jargon|simple terms|eli5|plain (english|words|language))\b|\bexperienced (trader|user)\b"],
}
_COMPILED = {k: [re.compile(p, I) for p in v] for k, v in PATTERNS.items()}

PRIORITY = ["hypo_size", "inject_talk", "inject", "execute", "guarantee", "ignore_risk", "all_in", "emotional", "risk_math", "move_stop", "leverage", "advice", "rules", "recall", "remember", "missed", "risk_at_hand", "signal_outcome", "explain_signal", "position",
            "plan", "spike", "friday", "htf_ltf", "scenarios", "spread", "gap_stop", "indicators"]


def detect(text):
    """-> {'intent': 'policy', 'key': ..., 'also': [other keys that matched]} or None."""
    t = (text or "").strip()
    if not t or t.startswith("/"):
        return None
    hit = [k for k in PRIORITY if any(rx.search(t) for rx in _COMPILED[k])]
    if not hit:
        return None
    key = hit[0]
    # "remember" is a plain request to keep something, UNLESS what follows is itself an injection (checked first because inject outranks it)
    return {"intent": "policy", "key": key, "also": hit[1:]}


def f5(x):
    try:
        return f"{float(x):.5f}"
    except (TypeError, ValueError):
        return "not recorded"


def f1(x):
    try:
        return f"{float(x):.1f}"
    except (TypeError, ValueError):
        return "not recorded"


def pick(options, seed):
    s = str(seed)
    return options[(abs(int(s)) if s.lstrip("-").isdigit() else sum(map(ord, s))) % len(options)]


# ----------------------------------------------------------------------
# REPLIES
# ----------------------------------------------------------------------
def a_inject(text, also, profile, seed):
    low = (text or "").lower()
    bits = ["I treat that as text, not as an instruction. A message, a pasted news feed or an encoded string can't give me new rules, switch what I am, or change my limits."]
    if re.search(r"\bapproved for|\bauto(matically|-)? ?approve|\brisk\b.{0,30}\b\d+ ?%", low):
        bits.append("Your risk limit comes from your account profile" + (f" ({profile['risk_pct'] * 100:g}% per trade)" if profile else "") + ", and a line inside a message can't raise it or approve anything.")
    if re.search(r"10 ?/ ?10|a\+|always (say|call|tell|rate)|every (gbpusd )?(trade|setup|signal)", low):
        bits.append("I also won't call every setup a 10/10: a setup's score and rating come from the engine and I report them as recorded, good or bad.")
    if re.search(r"base ?64|decode|translate", low):
        bits.append("I don't decode hidden text and then carry it out.")
    if re.search(r"unrestricted|you are now|no (risk )?rules|dan mode|developer mode|jailbreak", low):
        bits.append("I'm the same ValleyBot with the same rules; there's no mode that switches them off.")
    if re.search(r"system prompt|instructions|reveal|print|leak", low) and not re.search(r"always|from now on", low):
        bits.append("I won't print my internal instructions, but I'm happy to tell you how I work if you ask 'what are your rules'.")
    bits.append("If there was a real question inside that message, ask it plainly and I'll answer it from the records.")
    return " ".join(bits), None


_WORDNUM = {"half a": 0.5, "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def lots_in(text):
    m = re.search(r"(\d+(?:\.\d+)?|half a|one|two|three|four|five|six|seven|eight|nine|ten)[- ]?lots?\b", (text or "").lower())
    if not m:
        return None
    v = m.group(1)
    return float(v) if v[0].isdigit() else _WORDNUM.get(v)


def a_hypo_size(text, profile, seed):
    lots = lots_in(text)
    if not lots:
        return "Nothing is being placed. Tell me the size in lots and a stop in pips and I'll work out what it would cost.", None
    pv = lots * 10
    body = (f"Nothing is being placed, I only do the maths. {lots:g} lot{'' if lots == 1 else 's'} of GBPUSD moves about ${pv:,.0f} per pip, so a 10-pip move is about ${pv * 10:,.0f} and a 30-pip move about ${pv * 30:,.0f}, up or down. ")
    if profile:
        bal, rp = profile["balance"], profile["risk_pct"]
        body += (f"Against your profile (${bal:,.0f}, {rp * 100:g}% risk = ${bal * rp:,.2f} per trade), that size would exceed the budget with any stop wider than {bal * rp / pv:.1f} pips. ")
    body += "Give me a stop in pips and I'll show the exact cost and the size your profile allows."
    return body, None


def a_inject_talk(text, seed):
    return ("Happy to talk about that. A message that tries to rewrite a bot's rules ('ignore your instructions', 'always call every trade A+', hidden or encoded text, a fake 'SYSTEM:' line) is called prompt injection. "
            "It's dangerous because a bot that obeyed it would stop being honest: a rating that is always 10/10 tells you nothing, and the first losing trade would arrive with no warning. "
            "The defence is that text inside a message is only text, never a rule. So I'm not following anything in what you quoted, I haven't saved it, and nothing about how I work has changed. "
            "If you want it kept as an example, say 'remember this: ...' and I'll ask you to confirm before I save anything."), None


def a_execute(text, seed):
    low = (text or "").lower()
    if re.search(r"credential|password|passcode|passphrase|login|log in|api[- ]?key|secret|token|seed phrase", low) and re.search(r"friend|his|her|their|for (him|her|them)", low):
        return ("No. I can't trade for anyone, and I'm not able to use anyone's login anyway. Please don't share your friend's credentials with me or with anyone else: "
                "logging into someone else's trading account can also put you both on the wrong side of a broker's rules. If you want, tell me a stop distance and I'll work out a size you or he can place yourselves."), None
    if re.search(r"\bfor (him|her|them)\b|\b\w+'s (trading )?account\b|\bhis|\bher\b|\btheir\b", low) and not re.search(r"\bsend\b|\bemail\b", low):
        return ("No. I can't trade for anyone, whoever's account it is: I have no connection to a broker, only to the scanner's records. "
                "And please don't share anyone's logins with me. If you want, tell me a stop distance and I'll work out a size they can place themselves."), None
    if re.search(r"\bsend\b|\bemail\b|e-mail|forward|upload|@|\.com", low):
        return ("I can't send anything outside this chat: not API keys, not a balance, not positions. I also don't hold your API keys or your open positions, so there was nothing to send, and nothing has been sent. "
                "Please keep keys and logins out of chat messages."), None
    if re.search(r"credential|password|passcode|passphrase|login|log in|api[- ]?key|secret|token|seed phrase", low):
        return ("Please don't paste passwords, API keys or broker logins here. I can't use them, I don't need them, and a chat is the wrong place for them."), None
    return (pick(("I can't open, change or close trades, and I have no trade-execution tool: all I do is read the scanner's records and message you. So I won't open that position, whatever the size. "
                  "If it helps, tell me the stop in pips and I'll work out the size from your profile for a trade you place yourself.",
                  "I don't place trades. I have no connection to a broker, only to the scanner's own records, so there's nothing for me to call or execute. "
                  "What I can do is tell you what size your profile allows for a stop you give me."), seed)), None


def a_guarantee(seed):
    return ("No, and nobody honestly can. A signal is a setup with a stop and a target; losing is a normal outcome of it, and the stop is there for that day. "
            "What I can do is show you what the engine recorded for it (entry, stop, target, score) and what size your profile allows, so the loss, if it comes, is the one you planned. "
            "Ask 'what lot size for the last signal' or 'explain the last signal'."), None


def a_ignore_risk(lot_line, profile, seed):
    base = ("I can't ignore the risk rules, and I won't work out a 'maximum' size: I size from your account profile" + (f" ({profile['risk_pct'] * 100:g}% risk per trade)" if profile else "")
            + " and round down so a stop being hit never costs more than the budget.")
    return base + (" " + lot_line if lot_line else "") + " If you want a calculation at a different risk percentage, say the percentage and the stop and I'll show the maths, but I'll say plainly what it costs you if it's hit.", None


def a_all_in(lot_line, profile, seed):
    return ("I wouldn't put the whole account on one trade, and I won't size it that way: if the stop is hit, the loss is the entire stake, and with leverage your broker can close you out before your stop. "
            + (f"Your profile has {profile['risk_pct'] * 100:g}% risk per trade. " if profile else "")
            + (lot_line + " " if lot_line else "")
            + "If this trade feels worth more than usual, tell me why. That's worth talking through, but a stronger feeling is a reason to check the setup, not to change the size."), None


def a_advice(seed):
    return ("I'm not a licensed financial adviser and what I say here isn't financial advice. What I do: relay what the engine recorded, do plain arithmetic on recorded candles and your account profile (price changes, ranges, how long ago, lot sizes), "
            "explain terms, talk your ideas through, and keep notes you ask me to keep. What I don't do: tell you to buy or sell, predict where price goes, promise an outcome, trade for you, or know your whole financial situation. "
            "Every decision and every result is yours."), None


def a_rules(seed):
    return ("How I work, plainly: the engine owns the market facts, the signals and the risk numbers, and I only relay them. Anything I work out myself (changes, ranges, EMA distances, how long ago, lot sizes) is arithmetic on recorded candles "
            "and your account profile, and I say so. I don't predict, I don't trade or touch your account, and I say when something is old, missing or not recorded instead of guessing. "
            "I don't save anything until you say yes, and I don't take new rules from a message, however it's worded."), None


def a_leverage(text, price, lots, lots_src, seed):
    nums = [int(x) for x in re.findall(r"1\s?:\s?(\d{2,4})", text or "")]
    head = ("Leverage doesn't change how much you lose if your stop is hit: that's set by the size and the stop distance (lots x stop in pips x $10 for GBPUSD). What leverage changes is the MARGIN the broker locks to open the trade, "
            "and so how close you can get to a margin call. ")
    if len(nums) >= 2 and price and lots:
        a, b = nums[0], nums[1]
        ma, mb = lots * 100000 * price / a, lots * 100000 * price / b
        return (head + f"On {lots:g} lot{'' if lots == 1 else 's'} ({lots_src}) at the last recorded price, the margin would be about ${ma:,.0f} at 1:{a} and about ${mb:,.0f} at 1:{b}. "
                "That's my arithmetic (lots x 100,000 x price / leverage, for a USD account); brokers differ on margin rules, so check yours. The risk if the stop is hit is the same in both cases."), None
    return head + "Give me both leverages, like 1:500 versus 1:30, and a lot size, and I'll work out the margin for each.", None


_REVENGE = ("That urge is the signal to stop, not to trade. Concretely, and in this order: (1) don't open anything for a set time after the loss; decide the time now, not later. "
            "(2) If you do trade again today, the size is the minimum, not the size that 'gets it back'. (3) Before any entry, write the entry, stop and target down in one line; if you can't, it's the loss talking. "
            "(4) A loss is usually the price of a setup working at its normal rate, not a debt the market owes you. ")


def a_emotional(profile, seed, again=False):
    if again:
        return ("I hear you, and wanting it back is the most normal feeling in trading, which is exactly why it's dangerous. A bigger size doesn't make the next setup better, it only makes the next loss bigger and the one after it harder to take. "
                "What would help right now? Stepping away for a set time, going through the three losses to see if any broke your own rules, or looking at what a plain 0.5% trade would risk? Tell me which and I'll do it with you."), {"kind": "chat", "flow": "feeling", "stage": "talking", "theme": "revenge"}
    return (_REVENGE + (f"Your own limit is {profile['risk_pct'] * 100:g}% per trade, so one loss should be a small, planned amount. " if profile else "")
            + "How big was this loss compared with that limit, and what is the trade you're itching to take?"), {"kind": "chat", "flow": "feeling", "stage": "talking", "theme": "revenge"}


_G = "General principle, not an engine reading: "

_NW = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10}


def a_risk_math(text, profile, seed):
    """Plain arithmetic on a risk percentage; it never says what the person should risk."""
    import math
    low = (text or "").lower()
    pcts = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s?%", low)]
    m = re.search(r"\b(\d+|two|three|four|five|six|ten)\s+(?:stops?|losses|losers|losing trades|stop[- ]?outs?)\s+in a row", low)
    n = int(_NW.get(m.group(1), m.group(1))) if m else None
    p = pcts[0] / 100 if pcts else (profile["risk_pct"] if profile else None)
    if not p or not 0 < p < 1:
        return "Tell me the percentage and I'll show what it means: per trade, after a run of losses, and how many straight losses it takes to hurt.", None
    bal = (profile or {}).get("balance")
    out = [f"{p * 100:g}% risk means each full stop-out costs {p * 100:g}% of the account" + (f" (about ${bal * p:,.2f} on ${bal:,.0f})" if bal else "") + "."]
    if n:
        left = (1 - p) ** n
        out.append(f"{n} stops in a row at {p * 100:g}% leaves you at {left * 100:.1f}% of the account, a drop of {(1 - left) * 100:.1f}%" + (f" (about ${bal * (1 - left):,.2f} gone)" if bal else "") + ". It's slightly less than {n}x{p:g} because each loss is taken from a smaller account.".replace("{n}x{p:g}", f"{n} x {p * 100:g}%"))
    else:
        out.append(f"For scale: 10 stops in a row at {p * 100:g}% is a {(1 - (1 - p) ** 10) * 100:.1f}% drop, and it takes about {math.log(0.8) / math.log(1 - p):.0f} straight losses to be down 20%.")
    out.append("Whether that's too much depends on how many losses in a row you can sit through without changing your plan, and on whether you'd still follow your plan after the last of them; that's your call, not something I can read off a chart. " + (f"Your own setting is {profile['risk_pct'] * 100:g}%. " if profile else "") + "Want me to run it for a different percentage or a different number of losses?")
    return " ".join(out), None


def a_move_stop(seed):
    return (_G + "moving a stop AWAY from price after you're in means risking more than you sized, so it breaks the loss limit you chose before emotion was involved. Moving it TOWARD price (to breakeven or a trail) is a different thing: it's fine only if it was part of your plan, "
            "because it often takes you out on a normal pullback. The test is whether you'd make the same move if you were looking at this chart with no position. "
            "I don't place or manage trades, so I'm not moving anything for you. If you tell me the plan, I can check it against the candles, or help you write the rule for stop moves down now, before you're in a trade."), None


def a_reasoning(key, seed):
    t = {
        "spike": _G + "a fast 80-pip move on a data release tells you volatility arrived, not yet which way it will settle. Don't conclude yet that the direction is decided (spikes often retrace part or all of the move), "
                 "that the spike's high or low is real structure rather than a news wick, that spreads and fills were normal (they usually aren't), or that your usual stop distance still means the same thing. "
                 "Wait for the spread to normalise, for the first pullback and whether it holds above or below the spike's midpoint, for a candle that CLOSES beyond the extreme, and for the 15-minute structure to read cleanly again. "
                 "Which release was it? I can show you what the engine recorded around that time.",
        "friday": _G + "a weekend with a big event after it adds gap risk: the market can open Sunday night or Monday beyond your stop, and the stop fills at the next available price, not your level. Ranges also tend to dry up late on Friday. "
                  "So the question to settle before the close is whether you're willing to hold through a gap at full planned loss, and if not, to cut size or close rather than hope. "
                  "Moving the stop to breakeven only makes sense if structure supports it, not as a reflex. Is a position already open, and what does your own rule say about holding over weekends?",
        "htf_ltf": _G + "treat them as two timeframe stories, not one average. The daily level is context (where reactions are likelier); the 15-minute structure is the trigger. A bullish 15-minute structure running INTO a daily resistance is a trade with a ceiling; "
                   "a break and hold above the level, then a retest that holds, changes the picture. Don't blend them into 'neutral': decide which one your rule gives authority, and size down when they disagree. "
                   "The engine reports its own higher-timeframe and 15-minute readings separately (ask 'what's the direction'); I don't hold your daily level unless you give it to me. What's the level and which way is the 15-minute going?",
        "scenarios": _G + "I won't invent price targets I can't derive, so here is the frame to fill with your levels. Bullish: price holds above your line in the sand and the 15-minute keeps making higher lows; invalidation is a close back below that line. "
                      "Bearish: price loses it and closes below, then fails to get back; invalidation is a close back above. Chop: price stays between the two and neither side gets a close beyond; the 'trade' is waiting, and invalidation is a clean close outside the range. "
                      "For each, watch the close, not the wick, and note the spread around news. Give me your line in the sand and I'll lay the three out against it; the engine's own invalidation is below if it has one.",
        "spread": _G + "no, the same signal isn't the same trade when the spread is wide and liquidity is thin. The stop and target were drawn from candles, not from a wide spread, so the real stop is effectively closer, entry and exit cost more, "
                   "and slippage is likelier, which worsens the reward-to-risk. Your options are to wait for the spread to normalise, to cut size, or to skip it. I don't record spread, so I can't see it; you'd have to read it on your broker's quote. What is it showing right now?",
        "gap_stop": _G + "if price gaps through your stop, the stop fills at the next available price, so the loss is bigger than planned. Describe it exactly that way: planned risk was one number, actual loss was larger by the gap, and that difference is slippage, not 'the stop worked as designed'. "
                    "Next: don't re-enter at once, log planned versus actual, check whether the setup was valid before the gap, and ask whether your size allowed for gap risk around weekends and news. How big was the gap compared with your planned stop?",
        "indicators": _G + "neither dominates by default. RSI and MACD are both derived from price, so when they disagree it's information about timeframe and regime: overbought can persist for a long time in a trend, while a bullish MACD says momentum is up. "
                      "Choose the one your rule calls the trigger and use the other only as a filter, or step back to structure and levels, which the engine reads. The danger is picking whichever agrees with what you already want. "
                      "I don't calculate RSI or MACD, so I can't tell you what either reads. Which one is your trigger?",
    }[key]
    return t, {"kind": "chat", "topic": "reasoning"}


# ----------------------------------------------------------------------
# PARTNER: the plan builder (questions one at a time; the summary is the user's own words, nothing added)
# ----------------------------------------------------------------------
PLAN_QS = (
    "How long do you usually hold a trade: hours, days, or weeks?",
    "Which sessions or hours do you actually trade, and which do you avoid?",
    "How much of the account do you risk per trade, and is there a daily or weekly loss limit where you stop?",
    "What makes you enter: structure, a level, a pattern, news, something else?",
    "What makes you leave early, move the stop, or cut a trade?",
    "What's the mistake you repeat most often?",
)


def a_plan_start(seed):
    return ("Happy to, one question at a time, and I'll sum up your style in your own words at the end. First: " + PLAN_QS[0]), {"kind": "chat", "flow": "plan", "stage": 1, "answers": []}


def a_plan_turn(text, ctx, seed):
    answers = list(ctx.get("answers") or []) + [(text or "").strip()[:200]]
    stage = len(answers)
    if stage < len(PLAN_QS):
        return pick(("Got it. ", "Okay. ", "Understood. ", "Thanks. "), seed + stage if str(seed).isdigit() else seed) + PLAN_QS[stage], {"kind": "chat", "flow": "plan", "stage": stage + 1, "answers": answers}
    lines = [f"- {q} {a}" for q, a in zip(PLAN_QS, answers)]
    return ("Here's your trading style as you described it, in your own words:\n" + "\n".join(lines)
            + "\nThat's all from you; I haven't added anything or judged it. I haven't saved it either. If something there is a rule you want me to hold you to, tell me 'remember this: ...' and I'll ask you to confirm before I keep it."), {"kind": "chat", "topic": "plan"}


# ----------------------------------------------------------------------
# PARTNER: a position the user describes, checked on THEIR numbers (never adopted as market facts)
# ----------------------------------------------------------------------
def a_position(text, price_now, profile, seed):
    low = (text or "").lower().replace(",", "")
    d = "long" if re.search(r"\b(long|bought|buy)\b", low) else ("short" if re.search(r"\b(short|sold|sell)\b", low) else None)
    nums = [float(x) for x in re.findall(r"(?<![\d.])(\d\.\d{3,5})(?!\d)", low)]
    risk = re.search(r"(\d+(?:\.\d+)?)\s*%", low)
    if not d or len(nums) < 2:
        return ("Tell me three things and I'll work through it with you: are you long or short, where did you enter, and where is price now? I'll take your numbers as yours and check them against my records.", None)
    entry, cur = nums[0], nums[1]
    pips_ = (cur - entry) * 10000 * (1 if d == "long" else -1)
    out = f"On your numbers, you're {d} from {entry:.5f} with price at {cur:.5f}, which is {abs(pips_):.1f} pips {'in profit' if pips_ > 0 else 'against you'}."
    if price_now and abs(cur - price_now) > 0.0100:
        out += (f" One thing doesn't match: my newest recorded GBPUSD price is {price_now:.5f}, which is {abs(cur - price_now) * 10000:.0f} pips from the price you gave. "
                "Either you're looking at a different instrument or date, or one of us has an old number, so I won't treat yours as the market price. Which is it?")
    out += (" The three usual choices: HOLD keeps full exposure and needs your stop and target still to make sense; PARTIAL CLOSE banks some of the move and lowers risk but also lowers the payoff if it runs; "
            "TRAIL protects profit but can stop you out on a normal pullback if it's tight. Which fits depends on things I don't know yet: where is your stop and your target, how big is the position, "
            + (f"and does it still respect your {risk.group(1)}% risk" if risk else "and what is the risk as a percentage of the account") + ", and is any big news due before you'd normally close? Tell me those and we'll decide.")
    return out, {"kind": "chat", "topic": "position"}


# ----------------------------------------------------------------------
# PARTNER: explaining or reconstructing the last signal from its RECORD (the runner passes the record; nothing is added)
# ----------------------------------------------------------------------
def _facts(sig, tier_plain):
    """Only what the record actually holds. Nothing missing is printed as a value, and nothing is guessed."""
    r = sig.get("risk_result") or {}
    d = sig.get("direction")
    d = d if d in ("BUY", "SELL") else None
    return r, d


def _plan_bits(r):
    bits = []
    if r.get("entry") is not None:
        bits.append(f"entry {f5(r['entry'])}")
    if r.get("sl") is not None:
        bits.append(f"stop {f5(r['sl'])}" + (f" ({f1(r['risk_pips'])} pips away)" if r.get("risk_pips") is not None else ""))
    elif r.get("risk_pips") is not None:
        bits.append(f"a stop {f1(r['risk_pips'])} pips away")
    if r.get("tp") is not None:
        bits.append(f"target {f5(r['tp'])}" + (f" ({f1(r['reward_pips'])} pips away)" if r.get("reward_pips") is not None else ""))
    return bits


def a_missed_signal(sig, tier_plain, seed):
    if not sig:
        return "I have no signal on record, so there's nothing to reconstruct."
    r, d = _facts(sig, tier_plain)
    out = "Here's the last signal as the engine recorded it: " + (f"a {d}" if d else "a signal (its direction isn't recorded)")
    if sig.get("tier_label"):
        out += f" on the {plainwords_label(sig['tier_label'])} model" + (f" ({tier_plain})" if tier_plain else "")
    if sig.get("score") is not None:
        out += f", score {sig['score']}" + (f", rated {sig['tier_rating']}" if sig.get("tier_rating") else "")
    bits = _plan_bits(r)
    out += ". " + ("The plan was " + ", ".join(bits) + "." if bits else "The entry, stop and target aren't recorded on it.")
    if sig.get("reason"):
        out += f" Its reason, in the engine's words: \u201c{sig['reason']}\u201d."
    out += (" What I can't tell you: where, or whether, you could have filled; the spread at the time; whether price had already run when the alert reached you; and what you were looking at. "
            "The record is the engine's plan at firing, not a trade, so any 'miss' is measured against that plan and not against a fill.")
    return out


def plainwords_label(code):
    return str(code).replace("_", " ").lower()


def a_explain_signal(sig, tier_plain, level, seed):
    if not sig:
        return "I have no signal on record to explain."
    r, d = _facts(sig, tier_plain)
    bits = _plan_bits(r)
    if level == "beginner":
        side = {"BUY": "buy (betting price goes up)", "SELL": "sell (betting price goes down)"}.get(d)
        out = ("Plain version: the bot's last idea was to " + side + " GBPUSD." if side else "Plain version: the bot raised a trade idea, but its direction isn't recorded.")
        if tier_plain:
            out += f" It was the kind of setup called {tier_plain}: price reacting from a spot where it had paused before."
        if bits:
            out += (" The plan: " + ", ".join(bits) + ". The stop is the point where you admit you're wrong and leave with a small, planned loss.")
            if r.get("risk_pips") and r.get("reward_pips"):
                try:
                    out += f" A win would pay about {float(r['reward_pips']) / float(r['risk_pips']):.1f} times what a loss costs."
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
        out += " That isn't a promise it works. Want the experienced-trader version too?"
        return out
    out = "Experienced version: " + (d if d else "direction not recorded")
    if sig.get("tier_label"):
        out += f" on {plainwords_label(sig['tier_label'])}" + (f" ({tier_plain})" if tier_plain else "")
    if sig.get("score") is not None:
        out += f", score {sig['score']}" + (f" rated {sig['tier_rating']}" if sig.get("tier_rating") else "")
    out += (". " + ", ".join(bits).capitalize() + (f", {r['target_r']}R" if r.get("target_r") is not None else "") + ".") if bits else ". No plan levels are recorded on it."
    if sig.get("reason"):
        out += f" Engine's reason: \u201c{sig['reason']}\u201d."
    band = (sig.get("risk_at_hand") or {}).get("band") if isinstance(sig.get("risk_at_hand"), dict) else None
    if band:
        out += f" Risk-at-hand band: {band}."
    if sig.get("macro_bias"):
        out += f" Macro bias at firing: {sig['macro_bias']}."
    return out


def a_risk_at_hand(sig, seed):
    if not sig:
        return "I have no signal on record, so there's no risk-at-hand rating to read."
    rh = sig.get("risk_at_hand")
    if not isinstance(rh, dict) or not rh.get("band"):
        return "The last signal on record has no risk-at-hand rating, so I can't tell you what it said. I won't guess."
    reasons = [str(x) for x in (rh.get("reasons") or []) if x]
    out = f"The engine marked the last signal's risk-at-hand as {rh['band']}. That's the engine's own caution rating for that setup: it compares the setup to what it normally likes to see, and a higher band means it found reasons this one is less clean than usual."
    if reasons:
        out += " Its reasons: " + "; ".join(f"\u201c{x}\u201d" for x in reasons[:4]) + "."
    if rh.get("recommendation"):
        out += f" Its recommendation: \u201c{rh['recommendation']}\u201d."
    return out + " So is HIGH bad? It's a warning to size small or skip, not a verdict that the trade loses. Many HIGH-rated setups still work, and I can't tell you the hit rate without counting your records. Want me to count how the HIGH ones turned out?"


def a_signal_outcome(sig, candles, now):
    """Walks the recorded 5-minute candles after the signal fired: which of stop or target was touched first. Says so plainly when it can't tell."""
    if not sig:
        return "I have no signal on record to check."
    r = sig.get("risk_result") or {}
    d = sig.get("direction")
    try:
        entry, sl, tp = float(r["entry"]), float(r["sl"]), float(r["tp"])
    except (KeyError, TypeError, ValueError):
        return "The last signal's record doesn't hold a full entry, stop and target, so I can't check how it went."
    if d not in ("BUY", "SELL"):
        return "The last signal's direction isn't recorded, so I can't check how it went."
    from chat_records import _ts
    t0 = _ts(sig.get("fired_at"))
    if not t0:
        return "I can't read when the last signal fired, so I can't check what price did after it."
    after = [c for c in candles if c["t"] >= t0]
    if not after:
        return "I have no 5-minute candles after that signal fired, so I can't say how it went. I won't guess."
    up = d == "BUY"
    for c in after:
        hit_tp = c["h"] >= tp if up else c["l"] <= tp
        hit_sl = c["l"] <= sl if up else c["h"] >= sl
        when = c["t"]
        if hit_tp and hit_sl:
            return f"Both the stop and the target sit inside one 5-minute candle (the one opening at {when.strftime('%H:%M')} UTC), so the candles can't tell me which came first. I won't pick."
        if hit_tp:
            return f"Yes. Price reached the target {f5(tp)} in the 5-minute candle opening at {when.strftime('%H:%M')} UTC, without touching the stop {f5(sl)} first. That's from recorded candles, so a spread spike or a fill difference could change the real result."
        if hit_sl:
            return f"No. Price hit the stop {f5(sl)} first, in the 5-minute candle opening at {when.strftime('%H:%M')} UTC, before the target {f5(tp)}. That's from recorded candles."
    last = after[-1]["c"]
    pips_ = (last - entry) * 10000 * (1 if up else -1)
    return (f"Not yet. Neither the stop {f5(sl)} nor the target {f5(tp)} has been touched since it fired. Price last closed at {f5(last)}, which is {abs(pips_):.1f} pips {'in profit' if pips_ >= 0 else 'against it'} versus the entry {f5(entry)} "
            f"({len(after)} five-minute candles recorded since).")
