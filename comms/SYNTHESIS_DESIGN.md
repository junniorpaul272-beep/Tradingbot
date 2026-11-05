# Synthesis: proposed design (NOT built)

Status: proposal only, approved in principle by the friend with the refinements below (section 11). Nothing is implemented. It waits for the blind baseline on a frozen build (see FREEZE_RECORD.md).

## 1. What problem it solves, in one line
The bot should understand what Nelly means when her wording changes, and should know (and say) when it hasn't understood, instead of guessing or falling back to a canned line.

## 2. What already exists (so we reuse it, not duplicate it)
| Need | Already in the build |
|---|---|
| Read a message, pick a capability | `converse.interpret` (ordered rules) and `chat.understand` (concept families + slots) |
| Carry meaning across turns | stored `context` on each reply (`_last_context`): topics `change`, `ema`, `price`, `duration`/`swing`, flows `level`, `feeling`, `trade`, `plan` |
| Ask when unsure | `clarify.py`: offers only choices the words point at, otherwise an open question |
| Learn a phrase | `PHRASE_TAUGHT` in `user_catalog` (only after a yes, only used when the bot would otherwise not understand) |
| Corrections | `_strip_correction` in converse, and trade-talk edits ("stop moved from X to Y") |
| Authority boundaries | BUS/engine owns facts, `guard.final` last gate, safety policy keys that pre-empt everything |
| Measuring | `blind_eval.py` (six categories + critical flag) |

So Synthesis is not a new bot. It is a better-defined step inside `converse.interpret`.

## 3. Where it sits
message -> (existing safety policy, unchanged, runs first) -> **Synthesis** -> route to an existing capability -> existing authority answers -> `guard.final`.
Synthesis never answers, never computes a market fact, never records anything.

## 4. What it receives
- the message as typed (stored untouched) and a cleaned copy (fillers like "hmm and" removed)
- the last few turns' stored context (topic, slots such as timeframe, since, level, EMA length, the signal or case in play)
- the list of capabilities with their required slots (a fixed list, like `clarify.CHOICES`)
- taught phrases (only as a tie-breaker)

## 5. What it produces: an interpretation record
```
{ candidates: [ {capability, slots, evidence: [why], missing: [slots]} ... max 3 ],
  carried: {slots inherited from earlier turns, each tagged with the turn it came from},
  changed: {slots the new message overrides},
  consequence: "read_only" | "calculation" | "records_or_alerts",
  verdict: "clear" | "likely" | "unclear" }
```
## 6. How ambiguity is represented
Not a confidence number. Verdict comes from evidence:
- **clear**: one candidate, every required slot supplied by the message or by a single unambiguous earlier turn.
- **likely**: one candidate is better supported but a material assumption remains. The bot answers AND states the assumption in a few words so a wrong guess is visible and cheap to correct. (Revised: a slot inherited from an earlier turn is NOT by itself uncertain. "What about 20?" after one range calculation inherits the measure and changes the period, and just answers. Inherited context is judged on relevance, recency and uniqueness; it is suspicious only when it could point at two different things, e.g. two levels.)
- **unclear**: competing readings remain plausible, or a needed detail can't be resolved. Candidates are weighed by the strength of their evidence, not counted: three weak readings do not outweigh one strongly supported by the conversation. The bot asks the smallest question that separates them ("which level, 1.3400 or the 1.3380 stop?").
Required evidence rises with consequence: `records_or_alerts` (log a trade, set an alert, record an outcome) is never actioned on a "likely" reading; it always gets an explicit yes (this already holds today). `calculation` on a "likely" reading shows its assumptions.

## 7. Corrections
- Immediate: a correction replaces the slot in the carried context and the corrected reading stays for the rest of the topic (same mechanism trade-talk already uses for prices).
- Persistent learning: a phrase->meaning mapping is stored only after the SAME meaning was confirmed on at least 3 separate occasions IN THE SAME KIND OF CONTEXT (mixed contexts, e.g. "the move" meaning a bullish leg once and a bearish one another time, do not count together), is scoped to that context, is a hint only (status "user-confirmed preference", authority none: it never overrides the message or the conversation), is tied to the conversation topic it occurred in, every change to it is logged, is listed on request ("what have you learned from me?"), and can be removed. It can only point at an existing capability; it can never change a rule, a risk setting or a record.

## 8. Failure classes (added to `blind_eval` so we stop patching phrases blindly)
intent, context, routing, capability, authority, response, safety. Each failed case in the blind run gets one label. The fix is chosen from the label (a missing route is not fixed by another phrase).

## 9. Smallest useful version (only after the baseline)
1. Return the interpretation record from `interpret` (logging only, no behaviour change). Compare its verdicts with the blind results.
2. Turn on carried/changed slots for the follow-up families the blind run actually fails ("what about 20", "and yesterday?", "the same for friday").
3. Add the "likely" behaviour (answer + state the reading).
4. Only then consider the 3-confirmation learning.
Option for later, her decision: a language-model step that may only choose among the fixed capability list and fill slots (never write a number or an answer). It would generalise far better than rules but needs a model key in the bot and makes behaviour less predictable; it should be judged by the same blind test.

## 10. Testing
- Replay the original blind cases plus NEW unseen prompts written by someone else. Prompts used to build it do not count as evidence.
- Two scores, always reported together: understood correctly, and correctly asked when it couldn't (a false-confidence count: cases with two plausible meanings where it silently picked one).
- Cases: multi-turn follow-ups, slang and pidgin, indirect requests, references to earlier levels or signals, ambiguous references, corrections, combined requests, and requests that look like authorised actions but aren't.
- Pass bar is not "answers more"; it is fewer wrong answers AND no rise in silent guesses.

## 11. Refinements from the friend's review (accepted)
1. **Freeze with proof.** Build fingerprint (hash), repository state (commit, dirty or not, uncommitted files), settings that affect behaviour (no secrets), confirmation that the test run cannot send real Telegram messages or do live actions, and the first run kept unchanged with full replies and side effects. The frozen version must be a coherent, runnable whole, tested together. No fixes after the baseline starts until its results are captured. (FREEZE_RECORD.md is the first part of that.)
2. **Verdict rules**: as revised in section 6.
3. **Language-model step, if it is ever tested**: may only pick registered capabilities; must return each slot with its provenance (said in this message / inherited from turn N / unresolved); must leave uncertain slots unresolved rather than invent them; must name materially competing readings; never supplies a market fact, computes the final answer or executes anything; and its output goes through the same deterministic checks and authorisation as every other route. Right capability with a wrongly filled slot (entry, exit or direction in a "what would I make" question) still counts as a failure. Decision deferred until the baseline shows what actually fails: a few broken context transitions are cheaper to fix than a model; mostly-unfamiliar-wording failures would strengthen the case for one.
4. **Learning** as in section 7. Corrections apply to the current conversation at once; three confirmations are only for persistent learning.
5. **Order**: (1) capture baseline, (2) classify failures, (3) interpretation logging only (no behaviour change; check the record really reflects why the bot decided), (4) context handling for the follow-up failures the baseline demonstrates, (5) likely/unclear behaviour, (6) persistent learning, (7) decide on a model by comparing rule-based and model versions on the same held-out cases. Keep the original baseline at every stage and watch for regressions hidden by gains.
6. **Three diagnostics, always reported together**: correctly understood; correctly asked when it couldn't; **silent misinterpretation rate** (picked one of several materially different readings without stating the assumption or asking). Also track **unnecessary clarification** (asked when context already made it clear), so it can't look safe by becoming hesitant. These are diagnostics, not a pass percentage.
