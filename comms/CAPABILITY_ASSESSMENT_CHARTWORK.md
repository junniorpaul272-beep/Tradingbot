# Chart-work, alerts and trading conversation: capability assessment (review before any build)

Status: ASSESSMENT ONLY. Nothing here is in the frozen build (fingerprint 906a90f2…). A throwaway prototype exists on a separate copy and is
described in section 5; it is not delivered and should not be adopted unless you approve it after this review.

## 1. What I ran against the frozen build (read-only, your own phrases)

| You said | What the bot did | Verdict |
|---|---|---|
| alert me at the first sign of a pull back on the 5M | "couldn't read that one", lists what it can watch | not supported (honest) |
| help me draw a fib on the 5M from 1.3300 to 1.3400 | asked "how far price has moved today or whether the market is open" | misrouted: fib is not a known concept |
| where is the 1.618 extension on the 15m from 1.3280 to 1.3400 | read "1.618" as a price: "1.61800 to 1.32800 (2900p)" | wrong answer, confidently |
| set an alert when price taps the 0.618 on the 15m | **set a real price alert at 0.61800** (can never fire) | **defect**: a ratio was taken for a price and saved |
| the 5M OB is aligning with a 15M fib, what do you think? | dumped the engine's main thesis (bullish, supported…) | ignored what you said |
| does that strengthen the bullish idea? / what if the 1H is bearish? | same thesis dump | no reasoning about the question |
| what would make you change your mind about this? | listed the engine's invalidation and conflicts | closest to right, but only because the words matched |

Two of these are defects in the frozen build independent of any new feature: the 0.618 alert and the 1.618 "price". Neither should be fixed
inside the frozen tree; they belong in the first change proposal after the blind baseline.

## 2. Map of what exists

| Responsibility | Where it lives today | State |
|---|---|---|
| Understand wording | informal.py (typo/slang), converse.interpret (rules), chat._understand (intents), clarify.py | works for known intents; unknown concepts fall to the nearest intent or a generic question |
| Conversation memory | BUS_REPLY context + `_last_context`, flows (level, feeling, plan, trade) | works; no flow for "an idea under discussion" |
| Market facts | engine records (15m only for OB, FVG, CHoCH, BOS), candle archives 5m/15m/1h | OB/FVG/CHoCH/BOS exist on 15m only |
| Own calculations | chat.py: EMA, ranges, pips, swing size, normal-for-session, durations | no fibs, extensions, own OB/FVG, pullback |
| Alerts | converse._alert → bus.parse_alert → catalog threads → catalog.evaluate_condition | levels, closes, EMA, pips-from-now, new 15m CHoCH/BOS/OB/FVG; no pullback, no zone entry |
| Drawing | nothing; the bot cannot draw on a chart | genuinely does not exist (it can only give prices to plot) |
| Reasoning over evidence | thesis/evidence/conflict explanation of the ENGINE's own thesis | exists for the engine's view only, not for ideas you bring |
| Presentation / authority | BUS, guard.final | works |

## 3. What is missing, in your three groups

A. **Monitor/alert.** Missing: a measurable pullback condition; price entering a zone (stored zones carry no "entered" record, but candles do, so a zone-entry alert on stated prices already works via /alert zone).
B. **Calculate/draw.** Missing entirely: fib retracement/extension maths, swing selection for "the last swing", own OB/FVG on 5m/1h. It can never draw; it can give the prices.
C. **Discuss/reason.** Missing: a path for "here is my idea, what do you think". Today anything near it routes to the engine thesis. This is the largest gap and the one that matters most to you.

## 4. Decisions I need from you (not assumptions)

1. **What "first sign of a pullback" means.** Options: (a) first closed candle against the push (what my prototype silently used, and what your friend says must NOT be silent); (b) price gives back N pips from the highest high since the alert; (c) a break of the short-term swing low; (d) a return to an EMA or the last broken level. My suggestion: (c) as the default with (b) available, but this is your call. The bot should state the definition and wait for your yes before saving.
2. **Own calculations on 5m/1h** are labelled "my own working-out, not an engine reading". Acceptable? The engine's 15m zones stay the only "engine" zones.
3. **Clarification rule** (your friend's proposal): first a natural targeted question, second one final attempt, then admit it and move on; never guess on an action (alert, trade log), but keep chatting. Today there is no attempt counter; I'd add one in clarify.py.
4. **Discussion without an LLM.** A rule router can recognise "OB on 5m aligns with fib on 15m" and fetch the facts, then reason in fixed shapes (what reinforces, what is redundant, what contradicts, what is unknown). It cannot hold a free conversation. If you want real free conversation, that is the LLM step already parked until after the blind baseline.

## 5. Does anything new need to exist?

Honest answer: the arithmetic (fib levels, swing legs, own OB/FVG, overlap) has no existing home. Options:
- Extend chat.py's calculation section (already 1,500 lines; one more concern in a file that is already the biggest).
- One small pure module that owns ONLY level/zone geometry from candles (no routing, no wording, no alerts), which chat.py and the alert evaluator both call so there is one definition of "swing", "OB", "pullback".
I lean to the second only because the alert evaluator needs the same pullback definition as the chat answer; two copies would be exactly the duplicate-authority problem you described. A prototype of that module (chartwork.py, ~400 lines, pure, tested by hand on 20 phrasings) exists on the dev copy. It also contains a routing hook in converse and a pullback alert type; **those go beyond the assessment and are parked**. I would discard them or rebuild them after your decisions above.

What I will NOT create: a confluence brain, a separate conversation subsystem, or a score that counts indicators. Confluence is just two geometry results compared, and the reasoning about it (independence, contradiction, what would change my mind) belongs in the discuss path, using the same facts.

## 6. Avoiding duplicate authority / previous integrity problems
- One definition each for swing, OB, FVG, pullback; the alert evaluator and the chat answer import the same function.
- Engine facts stay the engine's; own-calc results are always labelled and never mixed into engine claims or the thesis.
- No answer states market facts it did not compute from recorded candles; any ratio in your message is never treated as a price.
- Alerts are only saved after the bot states the exact rule and you say yes.
- The frozen tree is untouched; changes go through a separate review.

## 7. Suggested order (after your answers)
1. Fix the two defects found in section 1 as the first proposal after the blind baseline (ratio taken as price).
2. Add the shared geometry and the pullback definition you choose.
3. Add the discuss path for ideas you bring, with the two-attempt clarification rule.
4. Blind-test the new phrasings with fresh wording, not the ones above.
