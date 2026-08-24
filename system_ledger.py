"""
SYSTEM OVERVIEW LEDGER — per system-overview-ledger-schema.md (design doc,
same directory/conversation). Builds DailyLedger/WeeklyLedger/
MonthlyLedger records from the bot's EXISTING permanent logs. Adds no new
sensors, no new detection, no market interpretation — purely aggregation
over what's already written to disk by min_scanner.py/scanner_live.py.

DELIBERATE DEPARTURE FROM min_scanner.py's OWN READER HELPERS: every
_read_*() function there (_read_shadow_trade_log, _read_bank_transactions,
_read_leg_obs_log, _read_failure_cases) silently filters out any record
whose methodology_version doesn't match the CURRENT constant — a record
from before a methodology bump, or one that was written without the
field at all (a bug, not a version bump), simply vanishes from every
report that uses these helpers. That's the exact mechanism behind the
known "12 of 22 EXP7_TIER_ATR trades invisible" gap, and per chat this
ledger is supposed to be as close to "absolute truth" as the bot can
produce — so it reads the RAW files directly here, with NO methodology
filtering, and reports legacy/missing-methodology records as their own
explicit count rather than silently dropping them. This is intentional
and should not be "fixed" to match the other readers' behavior.

Nothing in this file is wired into the live scan path. It's meant to be
run on its own schedule (end of day / Friday / month end) OR via the
one-off backfill entry point at the bottom, per chat, 2026-08-23.
"""

import os
import json
from datetime import datetime, timezone, timedelta

from scanner_common import (
    BASE_DIR, SHADOW_TRADE_LOG_FILE, LEG_OBS_LOG_FILE, FAILURE_CASE_LOG_FILE,
    BANK_TRANSACTIONS_FILE, SHADOW_METHODOLOGY_VERSION,
    LEG_OBS_METHODOLOGY_VERSION, BANK_LEDGER_METHODOLOGY_VERSION,
)

SYSTEM_LEDGER_SCHEMA_VERSION = 1  # bump on MEANING change only — see schema doc

# Single source of truth for "what's current" per subsystem — both the
# live per-pass builder and the one-off backfill import this, instead of
# each keeping their own copy that could silently drift out of sync with
# scanner_common.py's actual constants.
CURRENT_METHODOLOGY_VERSIONS = {
    "shadow":  SHADOW_METHODOLOGY_VERSION,
    "bank":    BANK_LEDGER_METHODOLOGY_VERSION,
    "leg_obs": LEG_OBS_METHODOLOGY_VERSION,
    "failure": SHADOW_METHODOLOGY_VERSION,  # failure cases share shadow's versioning
}

DAILY_LEDGER_FILE   = os.path.join(BASE_DIR, "daily_ledger.jsonl")
# Same path scanner_live.py computes independently — matching this
# project's existing convention (REJECTED_LIVE_QUEUE_FILE is likewise
# redefined locally in both scanner_live.py and min_scanner.py, rather
# than centralized and imported) rather than creating a new cross-module
# dependency for one constant.
SCAN_LOG_FILE = os.path.join(BASE_DIR, "scan_log.jsonl")
WEEKLY_LEDGER_FILE  = os.path.join(BASE_DIR, "weekly_ledger.jsonl")
MONTHLY_LEDGER_FILE = os.path.join(BASE_DIR, "monthly_ledger.jsonl")
LEDGER_CORRECTIONS_FILE = os.path.join(BASE_DIR, "ledger_corrections.jsonl")

# market_phase_history lives inside state.json, not its own log file — the
# caller passes it in (see build_daily_ledger's `phase_history` param)
# rather than this module reaching into load_state() itself, so this stays
# a pure function of its inputs and is easy to test/backfill offline.


# ---- generic raw reader — NO methodology filtering, unlike min_scanner's
#      own _read_*() helpers (see module docstring for why) -----------------
def _read_raw_jsonl(path):
    """
    Reads every valid JSON line in `path`, unfiltered. Malformed lines are
    skipped (same tolerance as every _read_*() helper elsewhere) but
    counted, so a corrupt-line problem is visible rather than silently
    absorbed. Returns (records, skipped_count).
    """
    records = []
    skipped = 0
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    skipped += 1
    except FileNotFoundError:
        return records, skipped
    return records, skipped


def _date_of(record, *fields):
    """
    Returns the YYYY-MM-DD this record belongs to, trying each field name
    in order (records across subsystems don't share one timestamp field
    name — see the docstring's field-name notes below). Returns None if
    no usable timestamp is found, so the caller can bucket it as
    'undated' instead of guessing a day for it.
    """
    for field in fields:
        val = record.get(field)
        if not val:
            continue
        try:
            return datetime.fromisoformat(val).date().isoformat()
        except Exception:
            continue
    return None


def _bucket_by_day(records, *timestamp_fields):
    """Groups records into {date_str: [records]}, plus an 'undated' bucket
    for anything with no usable timestamp (should be empty in practice —
    surfacing it rather than hiding it is the point)."""
    buckets = {}
    for r in records:
        d = _date_of(r, *timestamp_fields) or "undated"
        buckets.setdefault(d, []).append(r)
    return buckets


def _methodology_split(records, current_version_field="methodology_version",
                        current_version_value=None):
    """
    Splits records into (current, legacy_or_missing) — this is the whole
    point of reading raw instead of using min_scanner's filtered readers.
    `current_version_value` is the CURRENT methodology constant for this
    subsystem (e.g. SHADOW_METHODOLOGY_VERSION) — pass None to skip the
    split entirely (some subsystems, like phase_history, don't version).
    """
    if current_version_value is None:
        return records, []
    current, legacy = [], []
    for r in records:
        if r.get(current_version_field) == current_version_value:
            current.append(r)
        else:
            legacy.append(r)
    return current, legacy


def _count_coverage(day_scan_records):
    """
    Coverage — per chat, 2026-08-23: answers 'did the bot observe
    nothing, or did it not observe' at the day level. Sourced from
    scan_log.jsonl, which scanner_live.py writes unconditionally as the
    FIRST thing _scan_once() does, before any early return — so this is
    a true count of scan attempts, not inferred from side effects that
    only fire on some exit paths (like the rejected-live queue, which
    skips days with an open trade).

    scan_log.jsonl did not exist before 2026-08-23 — days before that
    have NO coverage field at all (absent, never fabricated as zero),
    same discipline as every other absent-subsystem case in this file.
    """
    if not day_scan_records:
        return None
    timestamps = sorted(r["scanned_at"] for r in day_scan_records if r.get("scanned_at"))
    if not timestamps:
        return None
    return {
        "scans": len(day_scan_records),
        "first_scan_at": timestamps[0],
        "last_scan_at": timestamps[-1],
    }


# ---- per-subsystem counters -------------------------------------------
# Each function takes ONE day's records for its subsystem and returns a
# flat {event_type: count} dict, plus a legacy_count if applicable. None
# of these guess at fields that might not exist — a missing field is
# skipped from that specific count, not treated as zero for the whole
# subsystem (same "don't fabricate" discipline as the rest of the bot).

def _count_shadow_experiments(day_records_current, day_records_legacy):
    """
    Generalized across EVERY experiment name found in the data — EXP1_
    STRUCTURE, EXP2_FIB, EXP7_TIER_ATR, EXPE_REJECTED_LIVE, whatever else
    exists today or gets added later. Deliberately NOT a fixed list of
    experiment names (per chat: generalize, don't hardcode per-tier/per-
    experiment logic) — the ledger reports whatever experiment names
    actually appear, nothing more.
    """
    per_experiment = {}
    for r in day_records_current:
        exp = r.get("experiment") or "UNKNOWN"
        per_experiment.setdefault(exp, {"logged": 0, "resolved": 0})
        per_experiment[exp]["logged"] += 1
        if r.get("resolved_at"):
            per_experiment[exp]["resolved"] += 1

    legacy_per_experiment = {}
    for r in day_records_legacy:
        exp = r.get("experiment") or "UNKNOWN"
        legacy_per_experiment[exp] = legacy_per_experiment.get(exp, 0) + 1

    return {
        "by_experiment": per_experiment,
        "legacy_or_missing_methodology": legacy_per_experiment,
    }


def _count_bank(day_records_current, day_records_legacy):
    per_source = {}
    for r in day_records_current:
        src = r.get("source") or "UNKNOWN"
        per_source.setdefault(src, {"transactions_settled": 0})
        per_source[src]["transactions_settled"] += 1
    legacy_per_source = {}
    for r in day_records_legacy:
        src = r.get("source") or "UNKNOWN"
        legacy_per_source[src] = legacy_per_source.get(src, 0) + 1
    return {
        "by_source": per_source,
        "legacy_or_missing_methodology": legacy_per_source,
    }


def _count_forward_observation(day_records_current, day_records_legacy):
    """Forward Observation (leg_obs) — per-leg records, one per resolved
    H1 leg. `fate` is whatever _close_leg_obs() actually wrote — not
    assumed here, just tallied by whatever value shows up."""
    by_fate = {}
    for r in day_records_current:
        fate = r.get("fate") or "UNKNOWN"
        by_fate[fate] = by_fate.get(fate, 0) + 1
    return {
        "legs_resolved": len(day_records_current),
        "by_fate": by_fate,
        "legacy_or_missing_methodology": len(day_records_legacy),
    }


def _count_failure_investigation(day_records_current, day_records_legacy):
    """Failure Investigation Bureau — failure_case_log.jsonl."""
    return {
        "cases_opened": len(day_records_current),
        "legacy_or_missing_methodology": len(day_records_legacy),
    }


def _count_phase(day_phase_events):
    """
    market_phase_history entries — passed in already-filtered-to-day by
    the caller (build_daily_ledger), since this lives in state.json, not
    its own log file. Each entry is expected to carry a transition_cause
    (per classify_transition_cause() in scanner_observation.py); counted
    by whatever cause value actually appears, same generalized approach
    as the experiment counter above.
    """
    by_cause = {}
    for ev in day_phase_events:
        cause = ev.get("transition_cause") or "UNKNOWN"
        by_cause[cause] = by_cause.get(cause, 0) + 1
    return {"transitions": len(day_phase_events), "by_cause": by_cause}


# ---- top-level daily builder --------------------------------------------
def build_daily_ledger(target_date, phase_history_events=None,
                        current_methodology_versions=None):
    """
    Builds ONE DailyLedger dict for `target_date` (a date object or
    'YYYY-MM-DD' string).

    `phase_history_events` — the day's slice of state["market_phase_
    history"], passed in by the caller (this module doesn't read
    state.json itself, to stay a pure function — easy to unit-test and
    easy to reuse for the backfill, which has no live `state` to read).
    Pass None/omit if not available this run; the phase subsystem key is
    simply absent then (not zeroed — see schema doc's absent≠zero rule).

    `current_methodology_versions` — optional dict like
    {"shadow": SHADOW_METHODOLOGY_VERSION, "bank": BANK_LEDGER_
    METHODOLOGY_VERSION, "leg_obs": LEG_OBS_METHODOLOGY_VERSION,
    "failure": SHADOW_METHODOLOGY_VERSION} — pass the LIVE constants from
    min_scanner.py at call time so this module never hardcodes a
    methodology version that could drift out of sync with the source of
    truth. If omitted, no legacy/current split is attempted (every
    record just gets counted as "current").
    """
    if hasattr(target_date, "isoformat"):
        target_date = target_date.isoformat()
    cmv = current_methodology_versions or {}

    subsystems = {}

    # --- coverage (top-level, not a subsystem — describes THIS ledger's
    #     own completeness, not something the bot "did") ------------------
    all_scans, _ = _read_raw_jsonl(SCAN_LOG_FILE)
    day_scans = _bucket_by_day(all_scans, "scanned_at").get(target_date, [])
    coverage = _count_coverage(day_scans)

    # --- shadow experiments (covers EXP2_FIB and every other experiment) ---
    all_shadow, _ = _read_raw_jsonl(SHADOW_TRADE_LOG_FILE)
    day_shadow = _bucket_by_day(all_shadow, "resolved_at", "opened_at").get(target_date, [])
    cur, legacy = _methodology_split(day_shadow, current_version_value=cmv.get("shadow"))
    if day_shadow:
        subsystems["shadow_experiments"] = {
            "source": "shadow_trade_log.jsonl",
            "counts": _count_shadow_experiments(cur, legacy),
        }

    # --- bank ---
    all_bank, _ = _read_raw_jsonl(BANK_TRANSACTIONS_FILE)
    day_bank = _bucket_by_day(all_bank, "resolved_at", "logged_at").get(target_date, [])
    cur, legacy = _methodology_split(day_bank, current_version_value=cmv.get("bank"))
    if day_bank:
        subsystems["bank"] = {
            "source": "bank_transactions.jsonl",
            "counts": _count_bank(cur, legacy),
        }

    # --- forward observation (leg_obs) ---
    all_legobs, _ = _read_raw_jsonl(LEG_OBS_LOG_FILE)
    day_legobs = _bucket_by_day(all_legobs, "resolved_at").get(target_date, [])
    cur, legacy = _methodology_split(day_legobs, current_version_value=cmv.get("leg_obs"))
    if day_legobs:
        subsystems["forward_observation"] = {
            "source": "leg_obs_log.jsonl",
            "counts": _count_forward_observation(cur, legacy),
        }

    # --- failure investigation bureau ---
    all_failures, _ = _read_raw_jsonl(FAILURE_CASE_LOG_FILE)
    day_failures = _bucket_by_day(all_failures, "opened_at").get(target_date, [])
    cur, legacy = _methodology_split(day_failures, current_version_value=cmv.get("failure"))
    if day_failures:
        subsystems["failure_investigation"] = {
            "source": "failure_case_log.jsonl",
            "counts": _count_failure_investigation(cur, legacy),
        }

    # --- phase (from state.json's market_phase_history, passed in) ---
    if phase_history_events:
        day_phase = [
            ev for ev in phase_history_events
            if _date_of(ev, "timestamp", "occurred_at") == target_date
        ]
        if day_phase:
            subsystems["phase"] = {
                "source": "state.json:market_phase_history",
                "counts": _count_phase(day_phase),
            }

    return {
        "ledger_date": target_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": SYSTEM_LEDGER_SCHEMA_VERSION,
        "coverage": coverage,  # None (absent) for any day before scan_log.jsonl existed
        "subsystems": subsystems,
    }


# ---- rollups: PURE ADDITION, nothing recomputed ----------------------------
def _sum_counts(dicts):
    """Recursively sums a list of {event_type: int} (or nested dict-of-
    dicts) structures. Non-numeric leaves (e.g. a 'source' string) are
    kept from the first occurrence, not summed."""
    if not dicts:
        return {}
    result = {}
    for d in dicts:
        for k, v in d.items():
            if isinstance(v, dict):
                result[k] = _sum_counts([result.get(k, {}), v])
            elif isinstance(v, (int, float)):
                result[k] = result.get(k, 0) + v
            else:
                result.setdefault(k, v)
    return result


def build_period_ledger(daily_ledgers, period_start, period_end):
    """
    Sums a list of already-built DailyLedger dicts into one period record
    (weekly or monthly — same function, the caller decides the range).
    `days_covered` is len(daily_ledgers), NOT (period_end - period_start)
    — a gap (bot down, no scan) must show up as days_covered < the
    calendar span, per the schema doc's coverage requirement. This
    function does not know or care WHY a day is missing; it only reports
    that it is.
    """
    all_subsystem_names = set()
    for d in daily_ledgers:
        all_subsystem_names.update(d.get("subsystems", {}).keys())

    subsystems = {}
    for name in all_subsystem_names:
        entries = [d["subsystems"][name] for d in daily_ledgers if name in d.get("subsystems", {})]
        if not entries:
            continue
        subsystems[name] = {
            "source": entries[0].get("source"),
            "counts": _sum_counts([e["counts"] for e in entries]),
        }

    # Coverage rolls up differently from a subsystem count dict — scans
    # SUM, but first/last_scan_at need min/max, not addition. Days with
    # no coverage (before scan_log.jsonl existed) are simply excluded
    # from this, same absent-not-zero rule as everywhere else — a
    # period covering some pre-coverage days and some post- days
    # reports coverage for only the days that actually have it.
    coverages = [d["coverage"] for d in daily_ledgers if d.get("coverage")]
    period_coverage = None
    if coverages:
        period_coverage = {
            "scans": sum(c["scans"] for c in coverages),
            "first_scan_at": min(c["first_scan_at"] for c in coverages),
            "last_scan_at": max(c["last_scan_at"] for c in coverages),
            "days_with_coverage": len(coverages),  # may be < days_covered below
        }

    return {
        "period_start": period_start,
        "period_end": period_end,
        "days_covered": len(daily_ledgers),
        "coverage": period_coverage,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": SYSTEM_LEDGER_SCHEMA_VERSION,
        "subsystems": subsystems,
    }


# ---- append (immutable — corrections are separate records, per Option A) --
def _append_jsonl(path, record):
    """Same append-only discipline as every other permanent log in this
    project (min_scanner.py's _append_shadow_trade / _append_bank_
    transaction / etc.) — write, flush, fsync, never truncate."""
    line = json.dumps(record, default=str) + "\n"
    with open(path, "a") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def append_ledger_correction(ledger_date, subsystem, event_type, delta, reason):
    """
    Option A from the friend's review: the ledger is immutable once
    written. A late-arriving record that would change an already-written
    day's counts becomes its OWN correction record here, never a rewrite
    of the original DailyLedger line. The effective count for that day/
    subsystem/event is (original + sum of its corrections) — computed at
    READ time, not by mutating the original file.
    """
    _append_jsonl(LEDGER_CORRECTIONS_FILE, {
        "ledger_date": ledger_date,
        "subsystem": subsystem,
        "event_type": event_type,
        "delta": delta,
        "reason": reason,
        "logged_at": datetime.now(timezone.utc).isoformat(),
    })


def write_daily_ledger(ledger):
    _append_jsonl(DAILY_LEDGER_FILE, ledger)


def write_weekly_ledger(ledger):
    _append_jsonl(WEEKLY_LEDGER_FILE, ledger)


def write_monthly_ledger(ledger):
    _append_jsonl(MONTHLY_LEDGER_FILE, ledger)


# ---- shared date-discovery — used by BOTH the live per-pass trigger below
#      AND backfill_system_ledger.py, so there is exactly one implementation
#      of "what dates exist" and "what's already been ledgered" -------------
def all_dates_present_in_source_logs():
    """Every calendar date that appears in ANY of the five source logs —
    the true historical span, not assumed from today backward. Includes
    scan_log.jsonl so a day with scan activity but zero subsystem
    activity (every scan that day hit an early return before reaching
    any subsystem write — rare, but the whole point of coverage is to
    catch exactly this) still gets a DailyLedger built, not silently
    skipped for lacking subsystem data."""
    dates = set()
    for path, ts_fields in [
        (SHADOW_TRADE_LOG_FILE, ("resolved_at", "opened_at")),
        (BANK_TRANSACTIONS_FILE, ("resolved_at", "logged_at")),
        (LEG_OBS_LOG_FILE, ("resolved_at",)),
        (FAILURE_CASE_LOG_FILE, ("opened_at",)),
        (SCAN_LOG_FILE, ("scanned_at",)),
    ]:
        records, _ = _read_raw_jsonl(path)
        for r in records:
            d = _date_of(r, *ts_fields)
            if d:
                dates.add(d)
    return sorted(dates)


def already_ledgered_dates():
    existing, _ = _read_raw_jsonl(DAILY_LEDGER_FILE)
    return {rec.get("ledger_date") for rec in existing if rec.get("ledger_date")}


def already_ledgered_weeks():
    existing, _ = _read_raw_jsonl(WEEKLY_LEDGER_FILE)
    return {rec.get("period_end") for rec in existing if rec.get("period_end")}


def already_ledgered_months():
    existing, _ = _read_raw_jsonl(MONTHLY_LEDGER_FILE)
    return {(rec.get("period_start") or "")[:7] for rec in existing if rec.get("period_start")}


# ---- live per-pass scheduling — called from min_scanner.py's run_min_pass(),
#      same "no fresh candles needed, only replays already-resolved permanent
#      log entries" reasoning as settle_bank_transactions()/drain_rejected_
#      live_queue() there. Self-bootstrapping: reads the ledger files
#      themselves to find what's missing, rather than needing a separately-
#      persisted cursor — cheap when there's nothing new (one small file
#      read per source, no-op loop), and correctly continues from wherever
#      the one-off backfill left off with zero manual handoff step. --------
def maybe_build_daily_ledgers(now_utc):
    """
    Builds a daily ledger for every calendar day that (a) has fully
    completed — i.e. is strictly before today, UTC — and (b) isn't in
    daily_ledger.jsonl yet. Loops rather than only checking "yesterday",
    so a multi-day gap (bot down, no scans) gets caught up in one pass
    rather than silently skipped. Returns the list of dates it built
    (empty list = nothing new, the common case).
    """
    yesterday = (now_utc.date() - timedelta(days=1)).isoformat()
    already = already_ledgered_dates()
    to_build = [d for d in all_dates_present_in_source_logs()
                if d <= yesterday and d not in already]
    built = []
    for date_str in to_build:
        ledger = build_daily_ledger(date_str, current_methodology_versions=CURRENT_METHODOLOGY_VERSIONS)
        write_daily_ledger(ledger)
        built.append(date_str)
    return built


def maybe_build_weekly_ledger(now_utc):
    """
    Builds ONE weekly ledger once a trading week's Friday has fully
    completed (i.e. today is Saturday or later relative to that Friday)
    AND every daily ledger for that week (Mon-Fri) already exists. Only
    ever builds the most recently completed week per call — a multi-week
    gap catches up one week per pass, over successive 5-minute passes,
    rather than all at once (kept deliberately simple; weekly boundaries
    don't need the same single-pass urgency as daily). Returns the
    period_end date string if built, else None.
    """
    # Most recent Friday on/before yesterday.
    d = now_utc.date() - timedelta(days=1)
    while d.weekday() != 4:  # 4 = Friday
        d -= timedelta(days=1)
    period_end = d.isoformat()
    period_start = (d - timedelta(days=4)).isoformat()  # that week's Monday

    if period_end in already_ledgered_weeks():
        return None

    daily_all, _ = _read_raw_jsonl(DAILY_LEDGER_FILE)
    week_dailies = [r for r in daily_all if period_start <= r.get("ledger_date", "") <= period_end]
    if not week_dailies:
        return None  # week's dailies aren't built yet — nothing to sum

    weekly = build_period_ledger(week_dailies, period_start, period_end)
    write_weekly_ledger(weekly)
    return period_end


def maybe_build_monthly_ledger(now_utc):
    """
    Builds ONE monthly ledger once a calendar month has fully completed
    (today is in a later month) AND at least one daily ledger exists for
    that month (per the schema's days_covered honesty rule — a month
    with zero daily records simply isn't built, not built-as-zero).
    Returns the YYYY-MM string if built, else None.
    """
    today = now_utc.date()
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - timedelta(days=1)
    period_start = last_month_end.replace(day=1).isoformat()
    period_end = last_month_end.isoformat()
    month_key = period_start[:7]

    if month_key in already_ledgered_months():
        return None

    daily_all, _ = _read_raw_jsonl(DAILY_LEDGER_FILE)
    month_dailies = [r for r in daily_all if period_start <= r.get("ledger_date", "") <= period_end]
    if not month_dailies:
        return None

    monthly = build_period_ledger(month_dailies, period_start, period_end)
    write_monthly_ledger(monthly)
    return month_key


def run_scheduled_ledger_maintenance(now_utc):
    """
    Single call-site for min_scanner.py's run_min_pass() — builds
    whatever daily/weekly/monthly ledgers have newly become due, in that
    order (weekly/monthly depend on that period's dailies already being
    written). Never raises — any per-period failure is caught so one bad
    period can't block the others or crash the calling pass; the caller
    is expected to wrap this call in its own try/except too, same as
    every other MIN-pass step, per project convention.
    """
    result = {"daily_built": [], "weekly_built": None, "monthly_built": None, "errors": []}
    try:
        result["daily_built"] = maybe_build_daily_ledgers(now_utc)
    except Exception as e:
        result["errors"].append(f"daily: {e}")
    try:
        result["weekly_built"] = maybe_build_weekly_ledger(now_utc)
    except Exception as e:
        result["errors"].append(f"weekly: {e}")
    try:
        result["monthly_built"] = maybe_build_monthly_ledger(now_utc)
    except Exception as e:
        result["errors"].append(f"monthly: {e}")
    return result
