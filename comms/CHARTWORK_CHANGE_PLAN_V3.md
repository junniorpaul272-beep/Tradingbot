# Chart-work: scoped change plan, version 3 (PLAN ONLY, nothing built, frozen build untouched)

Replaces CHARTWORK_CHANGE_PLAN_V2.md (and the first plan). Changes from v2 are marked [v3]. This is a proposal for your review, not permission to build. Everything marked PROPOSED needs your approval before coding.

## 1. Order of work (corrected)
1. Complete and preserve the blind baseline on the frozen build.
2. Separate change proposal for the two frozen-build defects (a ratio such as 0.618 saved as a price; 1.618 read as a price). Own tests, own approval.
3. You approve THIS plan (final version).
4. Hand-computed numerical tests and fixtures, written first.
5. Pure geometry module. 6. Alert conditions. 7. Discussion-path improvements.
8. Fresh-wording blind test, results reviewed before anything is considered for live use.
The prototype on the dev copy is not approved for adoption; it will be reviewed against this plan afterwards and anything out of scope removed.

## 2. Provenance (calculation source and input source are separate)
Every chart-work result carries all of:
- `calc_source`: engine-recorded | bot-calculated
- `anchor_source`: engine-recorded | recorded-candles | user-supplied | hypothetical
- `timeframe`, and `anchors`: the exact values used (e.g. 1.3300 and 1.3400, as you typed them)
Wording follows from these: "a fib the bot calculated from two prices you gave me", never "I found a fib". Your levels and hypothetical levels are
never presented as market facts. chartwork never says what an engine order block is; its own zones are a separate, explicitly defined interpretation, and
engine zones are only ever read through the existing engine records.

## 3. One owner for intention classification
`converse.interpret` owns the three-way classification (asks for the bot's view / brings their own idea or hypothesis / explores a concept), because it is
already the single router. chat.py and chat_policy.py only produce answers once told the intention; neither classifies. This resolves the "chat_policy or converse" ambiguity.

## 4. Discussion path (top priority; interim only)
- Only "asks for the bot's view" may return the engine's thesis.
- "Brings their own idea" covers both the known shape (OB/fib/timeframe relationships) and a general hypothesis about price behaviour ("it reacts
  differently when it approaches a level aggressively"). For a hypothesis the bot acknowledges it as hers, offers possible mechanisms as possibilities,
  separates speculation from anything measured in the recorded candles, names alternative explanations, and says how it could be checked. It never
  falls back to its own thesis and never says "I don't understand" to a statement that is plainly an idea.
- It may disagree and explain why (for example a countertrend setup against the engine's 1H bias, stated as the engine's).
- Stated limit: this is fixed shapes over real numbers. It is an interim improvement. The conversation capability is NOT marked complete after testing a few
  patterns; the language-model step stays parked and the design keeps a seam for it (numbers, provenance and BUS authority enforced outside the model).

## 5. Exact rules, to be approved BEFORE coding (PROPOSED; nothing is to be invented during implementation)
All rules use completed candles only and are point-in-time: a thing exists from the moment the candle that confirms it has closed, never earlier.

**Swing low / high.** A candle is a swing low if its low is strictly lower than the lows of the 2 completed candles before it AND the 2 completed candles after it
(highs mirror). Confirmed only when the second candle after it has closed. Equal lows do not qualify. n = 2 on every timeframe to start; stored as a per-timeframe setting.

**Early-warning rule [v3: exact trigger].** Threshold T = 1.5 x the median high-low range of the last 20 completed candles on that timeframe, calculated once when the alert is armed, shown to you in pips, and fixed at that number. Bullish push: developing high H = the highest high among completed candles since the alert was armed. For each newly completed candle, the warning fires if H minus that candle's LOW is at least T. (H includes that same candle's own high.) Bearish push is the mirror using the lowest low and the candle's high. So a wick can trigger it, an unfinished candle cannot, and a close is not required. NOTE: v2 measured against the candle's close; this version measures against the low, as you and your friend specified. Illustration from recorded data (to 30 Oct): 5m 4.2p, 15m 10.3p, 1h 21.0p. 1.5x is the starting value; 1.0x and 2.0x are to be compared later on enough cases, not tuned on a few examples.

**Structural confirmation.** The relevant swing low is the most recent confirmed swing low at the moment the early warning fires. Fires when a completed candle closes below it. If no confirmed swing low exists yet, the bot says at setup that confirmation can't be evaluated until one forms.

**Own order block (bullish; bearish is the mirror).** Originating candle: the last bearish-bodied candle before a displacement. Displacement: within the next 3 completed candles, one closes above the originating candle's high, and the move from its low to the highest high reached is at least 1.5 times the median range of the 20 candles before it. Canonical zone: the originating candle's full high to low (wicks included); never switched silently. [v3] The body region (open to close) is also stored and may be mentioned as extra information, labelled as the body, never as the zone. Exists once the displacement candle has closed. Touched: a later candle trades into the zone; it stays valid and is labelled "tested". Invalid: a later candle closes beyond the far side. Overlapping zones are kept as separate zones, not merged.

**Own fair value gap.** Three consecutive completed candles where candle 1's high is below candle 3's low (mirror for bearish); zone = candle 1 high to candle 3 low; minimum size 1 pip, a provisional noise floor and not a proven filter [v3]: the actual gap size, timeframe and the three originating candles are always recorded so gaps above and below the floor can be studied later. No extra quality or displacement requirement is added. Partly filled: a later candle trades into it. Filled/invalid: a later candle trades through all of it.

**Overlap versus near [v3].** Two separate statements, never blurred. (1) OVERLAP: the two levels or zones physically intersect; no tolerance. (2) NEAR: they do not intersect but are within a tolerance. The bot says "overlap" or "near", never "overlap" for a near result.
Tolerance for one timeframe = half the median high-low range of the last 20 completed candles on that timeframe, calculated the same way as the early-warning threshold and shown in pips each time (illustration: 5m 1.4p, 15m 3.4p, 1h 7.0p).
Cross-timeframe rule (fixed, PROPOSED): when two things come from different timeframes, "near" uses the FINER (smaller) timeframe's tolerance, the stricter of the two, whichever side the thing was named on. A pair that is within the coarser timeframe's tolerance only is reported as "close only at the coarser timeframe's scale (Xp apart; the finer tolerance is Yp)", in those words, and is not called near. The rule is fixed in code and is never chosen per case to make two things look aligned. Example: a 5m order block against a 15m fib level uses the 5m tolerance.

## 6. Alerts: history and re-arming
- Two separate notifications, early warning then structural confirmation, each fires once, in that order, only from completed candles after the alert opened.
- Created mid-pullback: at setup the bot checks the latest completed candles. If price is already retreating, it says so, says it did not see how that started, and asks whether you still want it armed; the developing high is the highest high AFTER creation, and the bot says that.
- Both fired: the alert closes. Re-arming is manual only. Recurring mode only if you explicitly ask for it later.
- The alert is saved only after the bot states the measurement, threshold in pips, timeframe and reset rule and you say yes. Ambiguity that affects an action is never guessed, whatever the clarification count.

## 7. Clarification
Context first (if the talk was about a 20-pip retracement, "what about 20?" is not asked back). Ask naturally; if genuine ambiguity remains, one final attempt; then admit it and let you move on. The counter applies to genuine ambiguity, not incomplete sentences.

## 8. Pure geometry scope (restrictions)
chartwork: swings, legs, fib and extension levels for any ratio you name, own OB/FVG rules above, overlap. Inputs candles; outputs numbers and records with provenance. No routing, wording, Telegram, clocks, disk, alert creation or market-state tracking. Tracking whether price later breaks a swing is the alert evaluator's job (it calls the geometry). Ratios are never read as prices.
Known issue in the prototype to be removed: it contains wording, a seed-based phrase picker and an n=3 swing, none of which match this plan.

## 9. Files that would change
chartwork.py (new, pure); chat.py (answer wording, source labels); converse.py (the single intention owner, routing, ratio-vs-price guard); clarify.py (context-aware two-attempt rule); bus_runner.py (glue only); bus.py, bus_contract.py, user_catalog.py (two pullback conditions); test files; STRESS_CHAT_REPORT.md. chat_policy.py is not modified. Engine, scanner, trading rules, guard.py, ledger.py, voice.py are not touched.

## 10. Tests
Numerical with hand-computed fixtures (fib/extension ratios both directions, swing confirmation timing including "not yet confirmed", OB/FVG rules on tiny built series, overlap edges, 0.618 / 61.8% / 1.618 never prices). Alert tests: fire once and in order; no use of information before alert creation; created-mid-pullback case; no re-fire; manual re-arm. Provenance tests: user anchors stay user-supplied in the output. Interpretation scored separately on a fresh-wording set, not tuned on.

## 11. Decisions now provisional (from your friend's review; still not permission to build)
Early warning 1.5x median range, last 20 completed candles, trigger = developing high minus a completed candle's low. FVG floor 1 pip. Order block canonical zone = full wick range, body kept separately. Near-tolerance = half median range per timeframe, finer timeframe governs cross-timeframe comparisons.
Still for you: do you approve the cross-timeframe rule (finer timeframe governs), and the change from close-based (v2) to low-based early-warning trigger? After that this plan is complete and I would wait for the baseline and defect-fix review before any code.
