# Market awareness - status report, 30 Oct 2026

## Decision: no new download
The Research Lab already holds 6.7 years of GBPUSD 1h/15m/5m (same provider, UTC). Full audit: `AUDIT_RESEARCH_DATASET.md`. The earlier "backfill" proposal is withdrawn.

## Headline audit findings
- Clean files: 0 malformed, 0 duplicates, 0 conflicts; UTC confirmed from the fetch script.
- Friday is three different things: the normal weekly close (moves 19-22:00 UTC by year; not a fault), a real hole of 13 Fridays Jul-Oct 2021 (data stops 13:59, same in all timeframes), and a later Friday session (to 23:00 UTC with real ranges) since Jan 2026, not filler as I first said. Not the only gap type: Christmas/New Year closures and scattered 1-5 hour gaps (mostly 2020-23).
- Live log vs research: 891 common hourly candles, 1 differs (probably captured just before close). Rebuilt 15m/5m vs 1h: 13 distinct hours differ (12 in 2023; closes 0.5-17.5 pips off; none a day's last candle).
- No 4h or daily files; 1m only Oct 2024 - Jun 2026. Files end on different dates (1h 4 Oct, 5m 11 Oct).
- Usable under the repaired rule: 1,700 trading days (31 Jan 2020 - 2 Oct 2026), 41 blocked, 1,048 dates with a clean 40-day window.

## Code changes this round (all research-only)
- Friday-aware completeness rule (provisional 19:00 UTC); tests 29 passing (was 23).
- `enrich()`: per-candle open/high/low/close, change (pips and %), body, EMAs 20/50/100/200 with a warm-up flag - the chart-legend view, point-in-time.
- `audit_research_dataset.py`: repeatable read-only audit.

## Proposed use of the history (nothing built yet; for review)
1. Optional one targeted 1h re-fetch for 9 Jul - 2 Oct 2021 (about 2 credits) to see whether the provider fills the hole.
2. Develop on data before the 18 Jun 2026 holdout; split earlier years into development and validation periods up front, in writing.
3. Fixed windows (10/20/40 days, later longer) as the consistent context layer, run across all 6.7 years, with how much the readings overlap tested first.
4. Event history (range formed / expansion / failed breakout) as the second layer, using the candidate rules in the spec, boundaries frozen when established, each event stored with its evidence.
5. Descriptive validity, then stability across years, then (much later) predictive tests against baselines, out of sample. Nothing labelled until 3-4 survive.

## Still not done / open
Persisted append-only ledger; Sunday-evening hours (about 2 hours of the real Monday open are excluded by the UTC-date convention); engine's EMA accuracy (see reply: the engine's EMA_100 uses only 120 candles, differing from a full-history EMA by 3 pips typical, up to 27); no connection to the engine, alerts or BUS.
