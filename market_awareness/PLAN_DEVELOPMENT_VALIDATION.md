# Market awareness - development and validation plan (DRAFT for review; written before any definition is tested)
Nothing here is implemented as a classifier. Thresholds named are candidates, to be chosen on development data only.

## 1. Periods (1h research file; frozen in writing now, before any result exists)
| Period | Dates | Used for |
|---|---|---|
| Development | 31 Jan 2020 - 31 Dec 2022 | try measurements and candidate definitions, choose thresholds |
| Validation | 1 Jan 2023 - 31 Dec 2024 | do the chosen definitions still behave the same? Looked at after the definitions are frozen; any change afterwards means this period is spent |
| Pre-holdout check | 1 Jan 2025 - 17 Jun 2026 | one more out-of-sample look, used once |
| Sealed holdout | 18 Jun 2026 onward (research lab seal) | final evaluation only, no tuning |
Known weak spots: 2020 is an unusual year (Covid) so it is kept in development, not dropped; Jul-Oct 2021 is excluded unless the repair works (windows touching blocked days are "unavailable").

## 2. What has already been seen after 18 Jun 2026 (exposure log - kept up to date)
The holdout is not untouched. Exposure so far, none of it used to set any threshold:
1. Data-audit statistics over the whole files, including holdout dates: counts, gaps, weekday/hour patterns, flat-candle share, comparison with the live log (28 Aug - 4 Oct 2026 overlap).
2. Market-awareness readings from the live log, windows ending 23 and 26 Oct 2026 (the 10/20/40-day windows reach back into the holdout period): net pips, efficiency, range width. Looked at as demonstration, not used to choose cut-offs.
3. Earlier ValleyBot bias/invalidation replay work, which looked at live price behaviour through 26 Oct 2026, so my general picture of recent GBPUSD is not blind.
Excluded from the holdout by design: the EMA comparison (stops before 18 Jun) .
Rule: from now on every look at holdout-period data is logged here with date and purpose; holdout market outcomes are never used to tune. Final evaluation will state this exposure.

## 3. Two layers, developed separately
- **Fixed windows** (10/20/40 trading days now; longer, e.g. 60/120, once tested): net move, efficiency, range width/position, up-day share, plus (new) move relative to typical daily range. First question: do the measures overlap (correlation across all development dates) - drop or merge those that carry the same information.
- **Event history**: range established / breakout attempt / confirmed / failed / range invalidated, using the candidate rules in the spec. Boundaries are fixed on the day the range is established and never edited; invalidation is a later event. Each event stored with its evidence (dates, prices, rule version).
Neither layer may use a candle later than its as-of date (tested).

## 4. Candidate definitions and how they are compared
Several rule sets side by side (for example efficiency cut-offs 0.15/0.25/0.35 combined with net move below/above 1 or 2 daily ranges; range events with confirmation 1/2/3 closes). Chosen on Development only, by these pre-stated criteria:
- **Descriptive validity:** can each labelled period be explained from its recorded numbers alone; sample of periods checked by eye against charts by you or your friend, not by me alone.
- **Stability:** the same date gets the same label as later data arrives; how often the label flips (per month); whether flips are explained by the window moving; how long labels last (very short-lived labels = noise).
- **Cross-year consistency:** share of time in each state and typical durations in Development vs Validation; a definition whose behaviour changes sharply between periods is discarded or flagged.
- **Sensitivity:** small threshold changes must not change the story much.
Keep/discard gates are written before running: e.g. discard if median label duration is under 3 trading days, or if the share of time per state differs by more than a pre-set margin between Development and Validation. Exact margins set in the first development run and recorded before Validation is opened.

## 5. Baselines and controls (for the later predictive stage only)
Predictive tests (later, separate plan) will compare against: no-information baseline (base rates), simple benchmarks (sign of the last N-day move, previous label persists), and shuffled/permuted-label controls. Out of sample only, with outcomes defined in advance. Nothing predictive is claimed in this phase.

## 6. Data handling rules
- Clean with `awareness.clean_hourly` (UTC, duplicates, conflicts quarantined, weekend dropped); complete-day rule per spec (Friday floor 19:00 provisional; a per-era expected close is still to be decided).
- Blocked days make spanning windows unavailable, never filled.
- 15m/5m used only for refinement checks, not mixed into hourly results; the 13 hours where timeframes disagree are listed and not "corrected".
- Sunday-evening hours: excluded for now; no claims about the weekly open until grouping is decided.
- 4h and daily series, if wanted, are built from 1h under a written session convention, not downloaded.

## 7. Separate decisions (not part of this plan)
- 2021 Friday repair: isolated workflow ready (`repair_2021`), output compared before any merge.
- Live EMA100: separate proposed patch with before/after test; method of the comparison is in `ema_window_comparison.py` (trailing 120 vs full history over 380 sample hours before the holdout: median 3.6, 90th percentile 9.5, max 26.8 pips; 500 candles: max 0.01 pips). It only measures start-value sensitivity, not whether 500 candles suits the strategy.
- BUS / signals: not connected.
