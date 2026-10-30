# Audit of the existing Research Lab candle data (30 Oct 2026)
Read-only. Nothing downloaded, nothing in the repo changed. Script: `audit_research_dataset.py`; full numbers: `results/audit_result.json`.

## 1. What exists (observatory_data/raw, same Twelve Data source as the live bot)
| Timeframe | Candles | First | Last | Notes |
|---|---|---|---|---|
| 1h | 43,506 | 30 Jan 2020 | 4 Oct 2026 09:00 | 6.7 years |
| 15m | 173,356 | 30 Jan 2020 | 4 Oct 2026 10:30 | |
| 5m | 510,939 | 18 Mar 2020 | 11 Oct 2026 | ends a week later than 1h/15m |
| 1m | monthly files | Oct 2024 | 17 Jun 2026 | stops at the sealed holdout |
| 4h, daily | none | | | would have to be built from 1h under a written session convention |
The three files end on different dates, so cross-timeframe work should stop at the earliest (4 Oct 2026). The research lab seals everything from **18 Jun 2026** as a holdout (2,602 hourly candles): definitions should be developed before that date and the holdout looked at once, at the end.

## 2. Conventions (verified from the fetch script and the files)
- The fetcher requests `timezone=UTC`; files store naive text `YYYY-MM-DD HH:MM:SS`, so they are UTC. Hourly stamp = open time. Same source and timezone setting as the live feed (the live log writes `+00:00`).
- Prices are stored as text; all parse. The fetcher drops the still-forming candle.
- 0 malformed rows, 0 duplicates, 0 conflicting duplicates, files in time order, in all three timeframes.

## 3. Friday: what is actually going on (not one problem, three)
1. **Normal weekly close, which moves.** Friday's last real candle is 19:00-22:00 UTC depending on the year (last non-flat candle: 2020-21 at 20-21:00, 2022-24 at 19-21:00, 2025 at 20-21:00, 2026 at 23:00). That is the market closing, not missing data. My earlier "all 24 hours" rule wrongly blocked these; fixed (below).
2. **A real hole: 13 Fridays, 9 Jul to 1 Oct 2021**, data stops at 13:59 UTC. It is the same in 1h, 15m and 5m, so it is not a rebuild error; it looks provider-side but that is unproven. Closing prices for those weeks are wrong by up to ~8 hours of trading.
3. **From late 2025 / Jan 2026 the Friday session runs later, to 23:00 UTC, with real price movement.** 39 of 40 Fridays in 2026 have candles at 22:00 and 23:00 with a median range of about 8-10 pips (a typical Friday 20:00 candle is about 11 pips), and none of the 23:00 candles is flat. Before that, candles at 22:00 on Fridays were flat (high = low). I first described the 2026 late-Friday candles as flat filler; that was wrong. What is flat is the weekend: zero-range candles are 4.4% of Sunday hourly candles vs about 0.0-0.8% on weekdays. Whether the provider changed its session or its source is unknown; the live log shows the same 24-hour Fridays.
Other short weekdays (about 24): Christmas/New Year (2024-01-01, 24-25 Dec, 31 Dec each year), plus isolated days such as 2023-04-17 (18 bars), 2021-04-15 and 2022-11-16/17 (gaps of 4-5 hours). Not Friday-only, as your friend suspected.

## 4. Gaps inside the trading week (candles missing between two present ones)
1h: 47 gaps, 28 of them a single hour; 20 of the 47 in 2021. 15m: 241. 5m: 801. Longest: Christmas/New Year closures, and 7-8 Feb 2026 (a weekend, not a trading gap). Heaviest years for small gaps: 2020-2023; 2024 and 2026 are cleanest.

## 5. Consistency checks
- Live log vs research file, same hour: 891 common 1h candles, **1 differs** (8 Sep 09:00: close 1.35302 live vs 1.35315 research; same hour's 15m candle also differs). The live log probably captured that candle a moment before it closed. 1 of 891; worth keeping an eye on, not alarming. Neither file was changed.
- 15m and 5m rebuilt into hours vs the 1h file: 43,171 (15m) and 42,021 (5m) comparable hours, 8 differ for each, **13 distinct hours combined** (closes 0.5 to 17.5 pips off; see section 8).

## 6. Repairs (proposed; raw data stays as is)
1. **Done in `awareness.py` (not in the data):** day-completeness is now session-aware. Mon-Thu need all 24 hours with no hole; Friday needs hours running without a hole from 00:00 to at least 19:00 UTC. Open point: a 2026 Friday that stopped at 19:00 would pass although 2026 Fridays normally run to 23:00; the rule is a floor, and a per-era expected close is still to be decided. Every blocked day is listed with its reason; a window that spans one is "unavailable", never averaged over. Result on 6.7 years: **1,700 trading days (31 Jan 2020 to 2 Oct 2026), 41 blocked**: 19 of the 41 are in 2021 (13 are the truncated Fridays); 1,048 of 1,700 dates have a usable 40-day window (the 2021 hole removes nearly all windows touching Jul-Oct 2021).
2. **Optional, targeted, your call:** re-fetch only 1h for 9 Jul - 2 Oct 2021 with the existing Research Lab fetch workflow (start 2021-07-09, end 2021-10-02, tfs 1h; about 2 credits, outside the live scan hours). If the provider returns full Fridays, the hole is repaired; if it still stops at 13:59, record it as a permanent known gap. Do not fetch anything else.
3. Not repaired: 8 rebuild disagreements and the small gaps are just listed.

## 7. Things the data cannot tell us
It tells us what the market did, not what ValleyBot knew at the time. Weekend/Sunday-evening hours (the real market opens about Sun 22:00 UTC) are currently excluded by the Mon-Fri UTC-date convention, so Monday's open ignores about 2 hours of real trading; an alternative "market day" grouping is a decision for later.

## 8. The 13 distinct hours where 1h and the rebuilt 15m/5m disagree (inspected)
Earlier I said "8"; that was 8 per rebuild. Combined it is **13 distinct hours: 12 in 2023 and 1 in 2022.** The hours (UTC): 2 Mar 2022 14:00; 22 Mar 2023 09, 11, 14, 15, 19:00; 23 Mar 2023 01, 06:00; 28 Jul 2023 15:00; 8 Aug 2023 08, 13, 16:00; 15 Aug 2023 18:00. Findings: the hour's **open is always identical**; the **close differs in all 13 (0.5 to 17.5 pips; the 17.5 is 22 Mar 2023 19:00)**; a high or low differs in 4 (up to 4.5 pips). Patterns: in 3 hours the 15m and 5m rebuilds agree with each other but not with the 1h file; in 5 the 15m rebuild matches 1h and only 5m differs; in 5 the 5m file has no complete hour to compare, so only the 15m difference is known. No consistent direction. It looks like different last-price snapshots at the hour boundary between separately served series, but I have not proved that and have not chosen which series is right. Impact on awareness (which uses the 1h file): none of the 13 is a day's last candle (all between 01:00 and 19:00), so no daily close changes; the hourly high/low effect is at most 4.5 pips in 4 hours.

## 9. Friday close by era (last non-flat hourly candle on Fridays with a normal-length day; count of Fridays)
The pattern is regular, not random, and it is what a Friday rule has to be built on:
- **US summer-time months (about Mar/Apr to Oct/early Nov): last candle 20:00 UTC.** **Winter months: 21:00 UTC.** Seen every year 2020-2025 (transition months mix the two). Consistent with a New York evening close shifting with daylight saving; I have not confirmed that with the provider.
- **Exception 1: Oct 2022 to Mar 2023 and Jan-Feb 2024: last candle 19:00 UTC** (all Fridays in those months), i.e. the session closed an hour earlier than the usual pattern. Cause unknown.
- **Exception 2: Jul-Oct 2021 (13 Fridays): last candle 13:00 UTC** = the repair target.
- **Late 2025 to Oct 2026: 23:00 UTC** on 39 of 40 Fridays in 2026 (mostly 21:00 through autumn 2025). A later session, with real price movement.
So a floor of 19:00 accepts a Friday that stops an hour or two early in an era where Fridays run to 20-21:00 or 23:00, and a floor of 20 or 21 rejects genuine 19:00-close eras. The effect is large (see the overlap report, Friday-floor sensitivity). No change has been made; a rule by era (expected close for that date, a stated tolerance, exceptions listed with their dates) needs your decision. Table is generated from the data with the snippet in the overlap report notes.
