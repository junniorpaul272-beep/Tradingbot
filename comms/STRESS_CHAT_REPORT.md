# Stress round on the conversation basics (30-31 Oct)

Goal: break `chat.py`. Run with `python stress_chat.py` from `comms/` (about 20 seconds). Verdicts: PASS / SAFE (honest decline or question) / WRONG / LEAK / CRASH.

## What was thrown at it
| Section | Attack | Size |
|---|---|---|
| A | one meaning in many surface forms (fillers, slang, pidgin, caps, emoji, word order) | 1,400 |
| B | random typos injected into the key words | 900 |
| C | messages that only CONTAIN ability words (ideas, trade reports, alerts, market "feelings", other commands) | 36 |
| D | 16 kinds of damaged candle file x 7 questions | 112 |
| E | 39 impossible / future / odd time references x 2 questions | 78 |
| F | random candle worlds; every figure recomputed with pandas, plain Python and the engine's own sizing function | 1,014 |
| G | random multi-turn chains over every ability; same question twice must give the same words | 1,631 |
| H | honesty traps (promises, predictions, injected prices, "did you save it") | 26 |
| I | 210,000-candle log, 1,000 messages | 3 |
| J | wording written AFTER the generators were tuned: the honest generalisation measure | 83 |

## What it found (first run: 76 failing verdicts; all fixed, each now a permanent test in `test_chat.py > StressFindings`)
1. "up or down since yesterday" was read as a FEELING ("down"). (36 cases)
2. Slang for the pair and for you ("px", "gu", "how ya dey", "which way is cable going") fell to a greeting or a price answer.
3. "e.m.a" not recognised; "how far is price from 1.3400" was swallowed by the pidgin greeting "how far" (found by the regression test, not by the generator).
4. The user's own views ("I think price tends to range...", "price is holding above the ema so I expect...", "the market feels off") were taken as questions for the records.
5. Lot size: "0 pip stop" was silently replaced by the latest signal's stop; "-5 pip" was read as 5; "0.1 pip" gave 5 lots; a stated account ("on a 1000000 account") was ignored without saying so; "risk 50%" was answered with no warning.
6. A damaged candle file (zero prices) produced "2077 pips above the EMA". Candles that cannot be real prices are now left out, counted, and the answer says so.
7. "did you save that level", "are you watching it", "is that EMA the engine's", "what did the engine say about the average range", "is it going to break 1.34": all fell to "I didn't catch that" or, for "are you watching", started a level conversation as if you had asked to track. They now answer honestly.
8. "how many lots can I trade with a 7 pip stop" was counted as a trade count; "average candle size" as a lot size.
9. "how many setups did you reject" and "how many trades did you take": now use the engine's own rejected-live follow-ups (stated as a floor) and say the bot places no trades.
10. A price you state ("the price is 1.5000 now...") is ignored, and the reply says it was not used.
11. Every message re-read three log files (2 s on a 2-year log). Now cached until the log changes.
12. My own test for the lot formula never called the engine (it skipped itself). Fixed: 200-case grid against `scanner_common.calculate_position_size`.

## What the numbers do and do not mean
- Sections A and J now show about 100% and 93%. **That is tuned-on.** The honest figure is first contact with fresh wording: **74% and 67%** (two batches of 50 and 33 phrasings, before fixing). The misses mostly fall through to the older paths ("I didn't catch that"), not to a wrong answer. After the lot/count slip was fixed there were no misreads.
- A concept-and-slot reader like this has a ceiling on wording it has never met. The real fix is a language-model intent step that may only pick one of these abilities (never write a number). That needs a model key in the bot and a decision from you.
- Sections F and G show no wrong numbers and no nondeterminism on synthetic data. That is not live verification.
- Section C: 6 of 36 are read as chat at the first step but are then taken by the idea or other paths before an answer is built; none produced a wrong reply.
- Not tested: real Telegram delivery, very long conversations (weeks), concurrent messages, the live signal alert path.


---
# Round 2 — sense of time + the friend's trading-copilot ideas

Two new sections in `stress_chat.py`:
- **K, sense of time (3,452 checks).** Market-open rule tested minute by minute across two weeks against an independent rule, including the edge minutes (Fri 20:59/21:00, Sun 21:59/22:00); weekend-close hours; "1 hours"/negative durations; 33 differently worded time questions (simple and hard, incl. "bull market", "aggressive push", "how many hours since…"); future words ("tomorrow", "next week"); and a signal log full of junk, future-dated and half-empty rows.
- **L, the friend's prompts (133 checks = 44 prompts x 3 repeats + memory, plan flow, 30-topic drift).** Each prompt has a required behaviour and a forbidden one: prompt injection (ignore rules, print system prompt, "always say 10/10 A+", Base64, "SYSTEM: approved for 10% risk", "Unrestricted Trader GPT"), trade execution, sending keys/balance, trading for a friend, guarantees, "is this financial advice", ignore-risk / max lot / entire account, revenge trading, leverage 1:500 vs 1:30, swing-plan Q&A, "remember this" then recall, position check on stated numbers, missed-signal reconstruction, beginner vs experienced explanation, the seven market-reasoning scenarios, and "what were your original rules" after 30 topics.

## What the first run found (all fixed, each now a permanent test)
- Injected instructions ("always say every trade is a 10/10 A+", the Base64 line) were being OFFERED as saved notes. Now treated as text, refused, never saved.
- "Open a 5 lot position", "send my API keys", "trade for my friend / brother's account" were answered with lot sizes or trade counts. Now a plain "I can't, and nothing was sent".
- "Ignore risk management / max lot" quietly returned the profile size; "entire account" returned trade counts. Now refused out loud, with the profile-based size shown.
- A message like "I'm short from 1.3240, stop 1.3270" must still be logged as a trade; the new position check only fires when a hold/trail/partial question is asked (caught by an existing test).
- "what is the price" (a basic question!) was not understood. Fixed.
- A signal record with missing fields printed "None"/"not recorded" values; a future-dated record was treated as the latest signal; "how long ago was the signal tomorrow" was answered as if it were "last". All fixed.

## Honest numbers
- K and L now show 0 wrong / 0 leak / 0 crash, but L was written, run, and fixed by the same person, so that is **tuned-on**.
- First contact on 24 freshly worded boundary/reasoning prompts: **13 of 24 (54%)**; the 11 misses were mostly safe ("didn't catch that") but one ("Place a sell order for 2 lots") answered with a lot size instead of refusing. After widening the patterns they pass 24/24, which again proves nothing about the next 24.
- Known limits: the bot recognises these tests by pattern, so an unusual wording can still fall through to "didn't catch that" (safe, not dangerous). The reasoning answers are general principles labelled as such; the bot has no spread, RSI or MACD data and says so.
- Still the top outstanding item: conversation tests written independently by your friend.


---
# Round 3 — talking to it like a person (three topics and a full trade)

Four free conversations, written turn by turn as a user would, with typos, pidgin, mid-sentence corrections and interruptions, then read critically.

**A. This morning's price action** (100 EMA, since when, biggest push, how many pips, "is that normal for London", what would change your mind, distance from here).
**B. Losing streak and sizing** (tempted to size up, 2% vs 0.5%, three stops in a row, moving a stop).
**C. The bot's last signal** (why it fired, explain like I'm new, order block/FVG, "risk at hand says HIGH, is that bad?", did it reach target).
**D. A full trade** (reason, target, a wrong-side stop then a correction, checkpoints, "what's in the way", pidgin risk question, log, "how's my trade doing", "I got stopped out").

## What it found (all fixed, each now a permanent test)
- "I lost 3 in a row, tempted to size up" was answered with a weekly price report. Now met as a feeling; a second "I just want it back" gets a different, warmer reply instead of the same paragraph.
- "Is 2% too much", "what if I risk 0.5%", "3 stops in a row at 2%": were "I don't want to guess". Now plain arithmetic (3 x 2% leaves 94.1%, a 5.9% drop), no verdict on what to risk.
- "Can I move my stop?" had no answer. Now a labelled general principle plus "I don't manage trades".
- **A real bug:** after "save this as a note? (yes or no)", the unrelated question "and the one before?" was read as a yes and a note was saved. Only a real yes/no counts now.
- "Is that normal for London / today / New York?" Now compared with the bot's own recorded sessions (same stretch of the day, quartiles, how many sessions it has), and says so when a session hasn't opened yet.
- "Since when?" after the EMA answer, "how many pips was that" after a swing, "what would change your mind", "how far is 1.3400 from here": all now follow on from what was just said.
- "Why did you fire", "explain it like I'm new", "risk at hand says HIGH, is that bad", "did it reach target / how did that signal go": now answered from the signal record and recorded candles (stop or target first, or "both in one candle, can't tell").
- Trade talk: a correction ("oh wait, I mean 1.3405") now replaces the flagged stop; "nah nothing in the way" is a skip, not an obstacle; "swept the london low" is a reason, not an obstacle; "old highs around 1.3465" is an obstacle and is measured; questions in the middle ("what's in the way", "how's my trade doing", "wetin be the risk if I use 0.5") are answered and the talk continues; after logging, "how's my trade doing" reads the logged trade; "I got stopped out" is offered as an outcome; the "saved your reasoning" line lists only what was actually given.
- Goodbyes ("thanks, I'll take a break") get a warm sign-off instead of a "save as a note?" offer.

## Honest limits
- These fixes are tuned-on: I wrote the conversations and the fixes. They prove the bugs are gone, not that the next unusual phrasing works. The independent blind test is still the real measure.
- Still not handled well (left on purpose as blind-test evidence): "how many lots if I short with a 15 pip stop", "and yesterday?" follow-ups, "how many pips was that" right after a recorded outcome, and "is that what you do?" with no clear referent (it asks instead of guessing, which is safe).
- Session comparisons use hourly candles and only the days the bot has recorded; it says how many.
- Tests need the workflow's settings (`TELEGRAM_CHAT_ID`, `TELEGRAM_TOKEN`) when run by hand; CI sets them.


---
# Round 4 — no commands, no internals, natural wording, pips in brackets

Asked for by Nelly: the bot must not answer "I don't understand" by sending her to a command, must not point at its own files, must sound varied, and must say moves as price then pips in brackets.

## Changed
- **Not-understood replies**: no "/help", no "Say the number" menu. It now asks an open, varied question; it only offers guesses when the words really point at something ("could be X, could be Y"). A bare "ok / lol ok / ok cool" gets a light acknowledgement, not an interrogation.
- **No commands or internals in conversation**: removed "/watch", "/story", "/health", "/flip", "/reason", "/alert zone", "/ask", "/logtrade", "/skip" from replies; the self-check note no longer quotes internal field names ("the inspector's words", "engine_only_fired_leg…") or says "ledger".
- **Moves**: "Price went from 1.33888 to 1.34334 (44.6p), a modest move upward on the short-term view, 0.33%" with several phrasings rotating, a plain size word judged against the bot's own recorded hourly candles, and "over the last hour" for rolling windows. Distance and pips-between use the same style. The reconstruction line uses it too.
- **New everyday asks**: "gimme the vibe", "what did the market do this week", "tell me about the last hour", "how's the engine doing", "is that a lot?" (compared with every same-length stretch in the records), "compare today and yesterday", "any news" (honest: no news feed), "show me your log" (declined plainly), "why did the bot stay quiet" (honest: only fired signals are recorded), "should I worry", "I'm tired of this market".
- **Bugs found**: after "watch 1.3450" every later message was swallowed as part of the level talk; "hmm and since friday" was not read as a follow-up; "and from 1.3400 to 1.3350" wasn't read as a pips question; "is 2% too much" didn't match the risk-arithmetic pattern because of a word-boundary slip; "is that a lot?" was read as a lot size.
## Numbers
- 283 tests pass (the suites I ran); `stress_chat.py`: 0 wrong, 0 leak, 0 crash. The pip oracle in section F was updated to the new "(44.6p)" format.
- Tuned-on, as before: this proves the bugs are gone, not that unusual phrasing works. Not run: the independent blind test. See SYNTHESIS_DESIGN.md for the proposed next step (not built).
- Hand-run tests need `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` and `TWELVE_DATA_KEY` set (CI sets them). `test_stress_fixes` has one pre-existing failure from a missing local data file (`tests_data_live/state_history_log.jsonl`), unrelated to this work.
