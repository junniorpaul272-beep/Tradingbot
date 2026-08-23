"""
ONE-OFF BACKFILL — per chat, 2026-08-23: builds the FULL historical
daily_ledger.jsonl from every existing permanent log, in one pass, so the
System Overview Ledger doesn't start empty on 2026-08-24 — history before
that date isn't lost, it's just backfilled in bulk instead of arriving
one day at a time going forward.

Run ONCE, manually (see .github/workflows/ledger-backfill.yml — a
workflow_dispatch, not a schedule, deliberately: this should never fire
on its own). Safe to re-run: it reads the existing DAILY_LEDGER_FILE
first and skips any date already present, rather than appending
duplicates — but it does NOT overwrite a date that's already there, per
the ledger's own immutability rule (see system_ledger.py / schema doc).
If a date needs correcting after this runs, use
system_ledger.append_ledger_correction(), not a re-run of this script.

Also produces a plain-text diagnostic report (NOT written into the
ledger itself — this is a one-time observation, not a permanent
per-day count) of every experiment/date combination found with a
missing or legacy methodology_version, since building this required
reading every record raw anyway. This directly surfaces the "12 of 22
EXP7_TIER_ATR trades invisible" gap and any siblings of it, without
fabricating or silently repairing the underlying records — read-only,
changes nothing on disk except the new ledger files.
"""

from datetime import datetime, timezone, timedelta

from scanner_common import (
    SHADOW_METHODOLOGY_VERSION, LEG_OBS_METHODOLOGY_VERSION,
    BANK_LEDGER_METHODOLOGY_VERSION, SHADOW_TRADE_LOG_FILE, LEG_OBS_LOG_FILE,
    FAILURE_CASE_LOG_FILE, BANK_TRANSACTIONS_FILE,
)
from system_ledger import (
    _read_raw_jsonl, _date_of, build_daily_ledger, write_daily_ledger,
    DAILY_LEDGER_FILE, CURRENT_METHODOLOGY_VERSIONS,
    all_dates_present_in_source_logs, already_ledgered_dates,
)


def _legacy_methodology_report():
    """
    Read-only diagnostic — does not touch the ledger. Groups every
    record missing/mismatching its subsystem's current methodology
    version by (subsystem, experiment-if-applicable, date), so the gap
    is visible and countable instead of silently absorbed the way
    min_scanner.py's own _read_*() helpers absorb it.
    """
    report_lines = ["LEGACY / MISSING METHODOLOGY_VERSION — diagnostic only, "
                    "nothing written to disk by this section", ""]

    shadow, _ = _read_raw_jsonl(SHADOW_TRADE_LOG_FILE)
    legacy_shadow = [r for r in shadow
                     if r.get("methodology_version") != SHADOW_METHODOLOGY_VERSION]
    if legacy_shadow:
        by_exp_date = {}
        for r in legacy_shadow:
            key = (r.get("experiment") or "UNKNOWN",
                   _date_of(r, "resolved_at", "opened_at") or "undated")
            by_exp_date[key] = by_exp_date.get(key, 0) + 1
        report_lines.append(f"shadow_trade_log.jsonl: {len(legacy_shadow)} of "
                             f"{len(shadow)} total records legacy/missing methodology_version")
        for (exp, date), count in sorted(by_exp_date.items()):
            report_lines.append(f"  {exp} on {date}: {count} record(s)")

    for label, path, current_version in [
        ("leg_obs_log.jsonl", LEG_OBS_LOG_FILE, LEG_OBS_METHODOLOGY_VERSION),
        ("bank_transactions.jsonl", BANK_TRANSACTIONS_FILE, BANK_LEDGER_METHODOLOGY_VERSION),
        ("failure_case_log.jsonl", FAILURE_CASE_LOG_FILE, SHADOW_METHODOLOGY_VERSION),
    ]:
        records, _ = _read_raw_jsonl(path)
        legacy = [r for r in records if r.get("methodology_version") != current_version]
        if legacy:
            report_lines.append(f"{label}: {len(legacy)} of {len(records)} "
                                 "total records legacy/missing methodology_version")

    if len(report_lines) == 2:
        report_lines.append("None found — every record on disk matches its "
                             "subsystem's current methodology_version.")
    return "\n".join(report_lines)


def run_backfill():
    already = already_ledgered_dates()
    all_dates = all_dates_present_in_source_logs()
    to_build = [d for d in all_dates if d not in already]

    print(f"Backfill: {len(all_dates)} calendar dates found in source logs, "
          f"{len(already)} already ledgered, {len(to_build)} to build.")

    for date_str in to_build:
        ledger = build_daily_ledger(
            date_str,
            phase_history_events=None,  # state.json history isn't a permanent
                                         # append-only log the way the other
                                         # four sources are — if a phase
                                         # history backfill is wanted later,
                                         # it needs state.json's actual
                                         # retained history checked first,
                                         # not assumed here
            current_methodology_versions=CURRENT_METHODOLOGY_VERSIONS,
        )
        write_daily_ledger(ledger)
        print(f"  wrote {date_str}: "
              f"{list(ledger['subsystems'].keys()) or '(no subsystems reported)'}")

    print("\n" + _legacy_methodology_report())
    print(f"\nBackfill complete. {len(to_build)} day(s) written to "
          f"{DAILY_LEDGER_FILE}. Live daily builder takes over from here.")


if __name__ == "__main__":
    run_backfill()
