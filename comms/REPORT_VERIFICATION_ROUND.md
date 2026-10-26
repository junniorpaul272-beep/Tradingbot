# Verification round (26 Oct) — answering the friend's fourth review

Every finding below has: the input, what was expected, what happened, where, and whether it is a **confirmed defect**, an **unverified assumption** or a **limitation**. Test types are kept apart: *independent* (none yet), *builder-written after seeing the scenarios*, *builder regression*, *real saved data*, *synthetic*.

## 1. Conversation scenarios A–D (friend's text; run by me, so NOT independent)
| Input | Expected | Actual before | Verdict | Fix | Test |
|---|---|---|---|---|---|
| "…sweeps before bullish CHoCHs might matter" | direction kept | plural "CHoCHs" lost "bullish" → counted all 5 events | confirmed defect | plural patterns | test_a |
| "Actually maybe the sweep isn't important. Maybe it's volatility expansion" | new idea kept beside the old, not overwritten | treated as a vague tweak | confirmed defect | revision turn keeps history, uses engine's own 15-min volatility label | test_a |
| "Can we compare both explanations fairly?" | fair comparison | "didn't catch that" | confirmed defect | four-box comparison, outcomes deliberately not shown | test_a |
| "How do you know?" / "What if the sweep happened between two scans?" | show the scans, admit the gap | generic how-to-test text | confirmed defect | shows scans per event, the real 24.9-minute hole, and says it can't see how far back the engine's check looks | test_b |
| "price was collecting liquidity" | not endorsed, ask for observables | offered to save as a note | confirmed defect | undefined-term turn | test_c |
| "don't create an alert yet, help me track the evidence" | no alert, say what tracking is possible | "didn't catch that" | confirmed defect | said plainly no background study exists | test_d |
| "set an alert when price enters that zone" | check zone defined | generic "couldn't read" | confirmed defect | "no zone fixed yet" + engine can't record zone entry | test_d |

## 2. Extra probes I wrote (builder-written, not independent)
Proof claims ("two out of two, proven?") — now refused and contradicted by the records; causation leaps — refused; "no I meant bearish" — direction swapped, old version kept; "what about the 1h" — says there is no 1-hour CHoCH flag and counts the 1-hour leg flips (0 in the history); "X works better than Y" — asks to define "better"; "were those both in London?" — answers from the engine's own session label; idea + alert in one message — both answered; "can you backtest it" — honestly "not from here", then how to set it up. Each was a **confirmed defect** before.

## 3. Inspector coverage (who owns what)
- **Source data** (NaN, zero/negative prices, spikes): the engine's `data_looks_sane` (scanner_common). Not changed. **Gap, limitation:** it does not check high<low or close outside the bar's range, and a whole series ten times too large passes (the spike test is relative).
- **Saved-state schema, direction, time, identity, growth, version:** the inspector (unchanged).
- **Domain invariants (new, in the inspector):** *impossible* (HIGH): negative average range, a high below its low, a leg whose extreme is on the wrong side of its origin. *Suspicious* (MED, wording "worth investigating", never corrected): a price about 3× or more off the others in the same state. Real saved state: zero domain findings.
- Attack results: claimed faults 25/25 (was 21/21); beyond-scope 1/5 (was 1/9). Still **not** caught: scan id going backwards, a day-old data time, an absurd break count, "LOW risk with no reasons", and any price error close to the other levels.
- "Hidden alarms" when two faults coincide: 3 of 104 pairs (two flipped-direction pairs, one origin-zero + flip). Counting changed to *fields no longer flagged* (merged alarms are not hiding). **Limitation.**

## 4. Real 1-hour flip
`audit_real_1h_flip.py` traces a flip from the saved history to the bot's wording and checks first-seen time, the previous-scan gap, one report only, and the wording (a leg direction change, not the engine's change-of-character flag). **On the real history (322 snapshots, 16–20 Oct) there is NO 1-hour flip: the leg stayed bullish.** So the real path is **unverified**; the script passes on a planted flip and will check the first real one the moment it exists.

## 5. Chat isolation
- **Confirmed defect (fixed):** the runner answered any chat id that reached the inbox. It now answers only the configured owner; missing, malformed, numeric-lookalike or different ids are dropped without a reply or a record; no owner configured means nobody is answered.
- **Confirmed defect (fixed):** in the poller, an empty chat id matched when the owner setting was empty. Now an empty id never matches.
- **Robustness (fixed):** a null message/chat crashed the poller's preamble (caught, but noisy).
- **Limitation, not fixed:** the system has ONE ledger and ONE conversation context. Strangers are shut out, but two users could not share it safely. Before anyone else uses it: per-chat ledgers/contexts and per-chat alerts would have to be built and tested.
- Group chat messages never reach the conversation (tested); the group only gets /watch and /help.

## 6. Test classes (not one pass rate)
- Independent tests: **none yet. Outstanding.** The friend should write conversations I never see.
- Builder-written after seeing the scenarios: test_idea.py (A–D and extra probes).
- Builder regression: test_idea, test_chat_isolation, test_inspector, batteries, stress_hard, stress_idea.
- Real saved data (19–20 Oct, 208 scans): all conversation and evidence tests.
- Synthetic: planted inspector faults; planted 1-hour flip.
- Remaining live risks: the cause of scan #6033; any behaviour after 20 Oct; scan gaps outside the sample; real Telegram delivery.

---
# Addendum (26 Oct, after the friend's fifth review)

## Evidence status, by class (not a pass rate)
| Class | What | Status |
|---|---|---|
| Independent testing | conversations written by someone who did not build the rules | **NONE YET. Outstanding.** |
| Builder-written, after seeing the scenarios | test_idea.py: scenarios T1–T5, A–D, A–E of reviews 3–5, plus my extra probes | pass, **not independent** |
| Builder regression / stress | stress_idea.py (random idea chains incl. unfamiliar wording), stress_hard.py, batteries | 0 wrong, 0 leaks, 0 crashes on the seeds run |
| Real saved data (19–20 Oct) | every counted fact in the idea replies (2 bullish / 3 bearish CHoCH, sessions, volatility labels, the 24.9-minute scan hole) | observed, two days only |
| Synthetic only | inspector faults, planted 1-hour flip | pass |
| UNVERIFIED ON REAL DATA | the 1-hour flip path (no flip in the history) | **UNVERIFIED** |
| Known limitations | single owner (one ledger); inspector scope; rule-based discussion; two days of data | stated |

## Defects found by the fifth review's examples (all confirmed, fixed, pinned in test_idea.py)
- "Maybe it's displacement that matters" after "the sweep might be irrelevant" was treated as a contradiction case; it is now a revision that keeps the first idea, says displacement isn't recorded, and offers definitions.
- "So the displacement caused the rally?" answered about the sweep (the old variable). It now answers about displacement and says it can't even show it happened.
- "You said both events happened in London. How do you know?" — the bot had never said that. It now says so, and shows the engine's own session labels with what they do and don't establish.
- "But the timestamps are close." fell to "didn't catch that". It now says nearness is not sameness or cause, names the scan-spacing limit, and asks for the largest gap that counts as one episode.
- "Can you backtest whether order blocks work better than FVGs?" fell to "didn't catch that". It now declines honestly, says the saved logs hold zones and a yes/no inside flag but not price reactions, so they are not enough on their own.
- "Don't alert me yet; help me track whether it keeps happening" understood the no-alert part but ignored tracking. It now says there is no background tally and offers recounting on request or an alert later.
- **The unfamiliar idea** (compression, then breakout, "usually keeps going") was offered back as a note to save. It is now held as the user's own words across turns; the bot names nearby saved readings (compression ratio, spike state, session label) without assuming a match, asks what each vague word means one at a time, says what would disprove it, and refuses to call it an edge.
- Event times: replies used to say events were "flagged at" a time. They now say the time a scan first saw each, usually a minute or two after the candle closed, not when it formed.
- Damaged scan lines: idea counts now say some saved scans couldn't be used and the counts may be missing events.

## Still open
- Stale-timestamp protection in the inspector (a day-old data time passes it; staleness is reported by the chat layer separately). Not changed this round.
- The three two-fault pairs that hide one alarm are documented, not fixed.
- Deployment: `tests.yml` now lists test_plainwords, test_stress_fixes, test_chat_isolation, test_idea. `scanner_live.py` must be in the push, and the `.github/workflows/tests.yml` copy must match the root one.

## Addendum 2 (26 Oct, after the sixth review): recall without invention
The friend asked for one more test: explain the original idea several turns after a revision, then ask what made the user change their mind. **Confirmed defect:** "remind me what my original idea was" returned only the current version, and "what made me change my mind?" was not understood. Fixed: the thread is now kept as a log of the user's own words with the time each was said; "remind me" lists every stage in order, and "what made me change my mind?" quotes the change and says no reason was given and none will be invented. Pinned in test_idea.py (TestRecall). All open items are in DEFECT_REGISTER.md.
