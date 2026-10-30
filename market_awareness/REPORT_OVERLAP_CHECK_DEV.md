# Development-period measurement check (2020-2022) - PRELIMINARY, on the ORIGINAL dataset
Status: the 2021 Friday repair has not been run (it needs your GitHub Actions run; I cannot run it from here: no key, no network). So this is a dry run on the original file to prove the check works and to show what the repair decision depends on. **Re-run after the repair is reviewed and a dataset version is approved:**
`python -m market_awareness.overlap_check <approved 1h file> 2020-01-01 2022-12-31 market_awareness/results/overlap_result.json` (about 1 minute).
Dataset used: `GBPUSD_1h.jsonl`, sha256 prefix `c1659eb3c7f8a414`, rows cut at load to 1 Jan 2020 - 31 Dec 2022 (nothing later was loaded, except that the point-in-time test compares against the same period only). Definition `awareness-v0.1-provisional`, Friday floor 19:00 (provisional), Mon-Thu 24 hours. No labels, no thresholds, no outcomes.

## Coverage and failure counts (732 trading days; 29 blocked days)
| Window | End dates | Valid | Not enough history | Unavailable (a blocked day inside) |
|---|---|---|---|---|
| 10 days | 732 | 559 (76%) | 9 | 164 |
| 20 days | 732 | 440 (60%) | 19 | 273 |
| 40 days | 732 | 299 (41%) | 39 | 394 |
Blocked days: 29, all "incomplete": 16 Fridays, 5 Mon, 3 Tue, 3 Wed, 2 Thu; by year 2020: 5, 2021: 19, 2022: 5. Weekend rows dropped 670; duplicates 0; conflicting duplicates 0.
**Main finding: 29 blocked days remove 59% of possible 40-day windows**, because one blocked day invalidates every window spanning it. 2021 alone accounts for most of it. How we treat blocked days is a decision, not a detail (see "Decisions needed").

## The six checks
1. **Window coverage:** table above.
2. **Shared observations:** 0 windows contain a hidden missing weekday; 0 windows whose dates do not match their end date. The 10/20/40-day windows ending on a date use the same underlying daily rows (the shorter is the tail of the longer).
3. **Missing-data effects (comparability):** all three windows are valid together on 299 end dates only. On **236 end dates the windows disagree on availability** (e.g. 10-day valid, 40-day not). Comparing a 10-day reading with a 40-day reading on those dates would silently compare different amounts of history, so they are reported as unavailable, not compared. Breakdown: 10V/20V/40V 299; 10V/20V/40U 127; 10V/20U/40U 109; all unavailable 158; start-up 39.
4. **Overlap between windows:** consecutive end dates reuse 90% / 95% / 97.5% of their days (10/20/40). Maximum non-overlapping windows: 62 / 27 / 9, i.e. only 11% / 6% / 3% of the valid windows are independent evidence. Any stability or "how often" claim has to be sized by those numbers, not by 559 / 440 / 299.
5. **Point in time:** all 732 end dates rebuilt from data stopping at that day give identical measurements (0 changed).
6. **Reproducibility:** rebuild in the same order and rebuild from shuffled input give identical output (same hash `27bc7e0b186dd92d`, same coverage counts).

## Friday-floor sensitivity (the rule affects the result a lot)
| Friday floor (last candle at or after) | Trading days | Blocked Fridays | Valid 40-day windows |
|---|---|---|---|
| 19:00 (current) | 732 | 16 | 299 |
| 20:00 | 721 | 26 | 271 |
| 21:00 | 637 | 110 | 49 |
| 22:00 | 595 | 151 | 0 |
A flat floor is therefore wrong in both directions. The data shows Friday closes follow a pattern by season and era (20:00 in US summer months, 21:00 in winter, 19:00 for Oct 2022-Mar 2023 and Jan-Feb 2024, 13:00 in the 2021 hole, 23:00 in 2026): see `AUDIT_RESEARCH_DATASET.md` section 9. Nothing has been changed.

## Descriptive only: how similar are the six measurements? (Spearman, same window)
- `net_pips` vs `net_in_mean_daily_ranges`: 0.99 in all three windows. They are nearly the same information; keeping both adds almost nothing.
- `net_pips` / `net_in_mean_daily_ranges` vs `range_position` and vs `up_day_share`: 0.76-0.90. Strongly related, but not duplicates.
- Not strongly related to the rest: `path_efficiency`, `range_width_pips`.
Serial dependence (overlap above) means these correlations describe the measurements, they are not significance tests. This says nothing about usefulness.

## Decisions needed before candidate definitions (nothing is chosen)
1. **Repair:** run the workflow; review comparison; then approve a dataset version (original, or original + a separately stored repaired copy).
2. **Blocked-day policy** (options, with trade-offs): (a) current: any window spanning a blocked day is unavailable (strict, loses much data); (b) bridge the blocked day with its partial candles and flag the reading as "degraded"; (c) skip the blocked day and use N available days (windows then span more calendar days, breaking the "same dates" guarantee).
3. **Friday completeness rule by era** (see above); then re-run this check.
4. Whether to keep both `net_pips` and `net_in_mean_daily_ranges`.
5. The 13 hourly cross-timeframe disagreements stay documented, no source chosen.

## Not done
No regime labels, no threshold tuning, no validation-period or holdout outcomes inspected, no prediction, nothing connected to the engine, alerts or BUS. The live EMA100 is unchanged.
