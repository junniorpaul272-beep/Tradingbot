# SPEC - GBPUSD market awareness (hourly candles only). Version awareness-v0.1-provisional. Draft for review.

Status: RESEARCH. Read-only. Not connected to trade decisions, signal qualification, bias, alerts or BUS. Every threshold below is provisional until section 8 says otherwise.

## 1. Purpose
Describe the broader condition GBPUSD's recent movement sits in (for example "a 10-day up-move inside a choppy 40-day stretch"), so a short-term read can be understood in context. It does not predict and does not say buy or sell.

## 2. Data and provenance
- Sources: (a) `ohlc_1h_log.jsonl`, written by the live scanner (Twelve Data); (b) the Research Lab file `observatory_data/raw/GBPUSD_1h.jsonl` (same provider, `timezone=UTC`), 30 Jan 2020 to 4 Oct 2026. Kept as separate datasets, never merged; overlapping candles compared (see audit). No daily candles. A research file's naive timestamps are read as UTC. The Research Lab holdout (from 18 Jun 2026) stays unseen until the end.
- Candle convention: hourly candles stamped by UTC open time, as stored. A candle is a completed hour.
- Coverage: live log, complete days 31 Aug to 23 Oct 2026 = 40 trading days; research file 1,700 trading days (31 Jan 2020 to 2 Oct 2026), 41 blocked. Live-log detail: 40 trading days, no blocked weekdays; 28 Aug and 26 Oct are partial and excluded (see data report from `awareness.py`). The file keeps overlapping copies of the same hour: 2,423 duplicates, none with different prices (checked). If one ever differed, rule 4 below applies.
- It grows by about 5 trading days a week. Longer windows are switched on only when enough days exist. Optional, your choice: a one-off hourly backfill (the provider's per-request cap is about 5,000 candles; actual coverage is to be confirmed from the returned timestamps, not assumed) to reach longer windows sooner. Conditions: record provider, retrieval time, requested range, timestamp convention and limits; store as its own raw file; compare overlapping candles with the live log and report disagreements; never merge silently. More history does not validate a definition.

## 3. Cleaning rules (all counted in a data report, nothing disappears silently)
Convention checked against the real file: every timestamp is "+00:00" (UTC), hourly, stamped by open time. The feed returns all 24 hours on every Mon-Fri (40 of 42 days; the other two were the first and last day of the log). This is what the source does, not a claim about the real FX session.
1. Timestamps are converted to canonical UTC (`YYYY-MM-DDTHH:MM:SS+00:00`) BEFORE weekday filtering, duplicate detection, sorting and day grouping. "Z", "+00:00" and other offsets for the same instant are one candle. A timestamp with no offset is taken as UTC and counted.
2. Malformed rows (missing or non-numeric fields, non-positive prices, high below low or outside open/close, unparseable time) are dropped and counted.
3. Identical duplicates collapse to one candle and are counted.
4. CONFLICTING duplicates (same instant, different prices) are never resolved by picking one. The timestamp is removed, both versions are kept in the report, its whole day is blocked, and any window spanning that day is "unavailable" with the reason. The result does not depend on which copy arrived first.
5. Saturday and Sunday (UTC) candles are dropped and counted. Weekend rule is a simple convention; Sunday-evening activity is excluded for now.
6. A trading day is COMPLETE only when its hourly candles run without a hole from 00:00 UTC and, Monday to Thursday, to 23:00; on Friday to a last candle at or after 19:00 UTC (the audit of 6.7 years shows the real weekly close at 19-22:00 UTC, moving by year, and real trading to 23:00 since Jan 2026). Only complete days are used. Every other weekday between the first and last complete day is listed in `blocked_days` with its reason ('missing', 'incomplete (n bars)', 'conflicting candle data'), and any window spanning it is unavailable (never filled or averaged over). First/last partial days are excluded and not called gaps. A day still in progress is never measured. See AUDIT_RESEARCH_DATASET.md.
7. Missing history gives "not enough history" (None). A number is never estimated.
8. Open: Sunday-evening hours (real open about 22:00 UTC) fall outside the Mon-Fri UTC-date convention; an alternative market-day grouping is deferred. The 19:00 Friday threshold is provisional.

## 4. Measurements (per trading day D, per window of N trading days ending on D; N = 10, 20, 40 now)
Daily values: open = first candle's open, close = last candle's close, high/low = extremes of the day's candles.
| Name | Definition | Unit |
|---|---|---|
| net_pips | close(D) minus close(D-N+1) | pips |
| net_in_mean_daily_ranges | net divided by the window's mean daily high-low range | multiples |
| path_efficiency | abs(net) / sum of abs(day-to-day close changes) | 0 to 1 (1 = straight line) |
| range_width_pips | highest high minus lowest low in the window | pips |
| range_position | (close(D) - window low) / (window high - window low) | 0 to 1 |
| up_day_share | share of day-to-day close changes that are up | 0 to 1 |
Open question for the first review: do all six add distinct information? Overlap (for example net_pips vs net_in_mean_daily_ranges) will be measured, not assumed.

## 4b. Chart-legend view (`enrich`)
Per candle: open, high, low, close, `change_pips` / `change_pct` (close vs the previous candle's close, as a chart legend shows), `body_pips` (close minus own open), `gap_hours` (distance to the previous available candle, never bridged), and EMAs 20/50/100/200 of the close with a `warm` flag (false for the first 3 spans). Causal, so later candles never change earlier rows.

## 5. Classification (CANDIDATES - not implemented, nothing reports a label yet)
Output today is `classification: NOT_DEFINED_YET`. Candidate regime vocabulary: TRENDING_UP, TRENDING_DOWN, RANGING, TRANSITIONING. Candidate rules will be tried side by side (for example efficiency above or below a cut and net move above or below a number of daily ranges) and compared on stability before one is promoted. The five Buy/Sell labels are not used.

### Range events (candidate definitions, boundaries fixed at the moment they are established)
| Event | Candidate rule |
|---|---|
| Range established (day D) | over the last N days: path_efficiency <= E1 and abs(net_in_mean_daily_ranges) <= R1. Upper/lower boundary = that window's highest high / lowest low, stored with established_on = D and never changed afterwards |
| Breakout attempt | a daily close beyond a boundary |
| Breakout confirmed | closes beyond the boundary on at least K consecutive days and no close back inside for M days |
| Failed breakout | a close back inside the range during the confirmation period |
| Range invalidated | confirmed breakout, or the range outlives L days |
E1, R1, K, M, L: several values will be tested; none is chosen. Each event is an appended record with its evidence (dates and prices). A later invalidation is a new event; the original range record is never edited.

## 6. Fixed facts vs revisable interpretation
- Fixed once written: raw cleaned candles, daily values, every measurement as of its date, established range boundaries, event records.
- May legitimately change: the current classification as new days arrive (a new record, with the reason), and the definition version (old records keep their own version; nothing is recomputed under a new version without writing a new set alongside).
- A later candle must never alter an earlier record. Tested in `test_awareness.py` (cut the history at several dates; every earlier observation is identical).

## 6b. Ledger status
Today this is a deterministic rebuild from the raw log, not a persisted append-only record of what was known at each moment. A persisted ledger is a later step; until then 'earlier observations unchanged' means: rebuilding with more or reordered data reproduces them, and any data conflict is surfaced rather than absorbed.

## 7. Output record (one line per trading day, append-only; file name proposed `market_awareness_log.jsonl`, NOT created or wired anywhere)
definition_version, as_of, days_available, measurements per window (None where history is short), classification, limitations. The data-quality report is stored beside it.

## 8. Evidence stages (reported separately; passing one says nothing about the next)
1. Descriptive validity: can each labelled period be explained from its recorded measurements alone?
2. Historical stability: same date, same label when later data is added; how often labels flip; are flips explained by the window advancing.
3. Predictive usefulness: only after 1 and 2 pass, against pre-stated outcomes and a baseline, out of sample.
With 40 trading days, stage 2 has exactly one end date for the 40-day window; real stability testing needs months of hourly history.

## 9. What BUS may do
Present the recorded observation with its date, definition version and limitations ("Classification pending validated definitions" until then). BUS never computes or reinterprets these numbers.

## 10. Scope limits
GBPUSD only: this describes GBPUSD's trajectory, not independent GBP or USD strength. Named "market awareness", not RSI.
