"""
ledger.py — append-only, bitemporal event ledger. Stdlib only. A leaf: it
imports nothing from this codebase and knows nothing about markets, users,
Telegram or wording. It exists to make two things true by construction:

  1. HISTORY IS IMMUTABLE. The only write is `open(path, "a")`. There is no
     update, no delete. A mistake is fixed by appending a record whose
     `corrects` field names the one it supersedes; the original stays.
  2. NO HINDSIGHT. Every record carries TWO times:
        ts           when the thing happened (event time)
        recorded_at  when the system learned/wrote it (knowledge time)
     `as_of(records, T)` returns only records with BOTH <= T. A trade the
     user logged on Friday about a Monday entry is invisible to a Monday
     reconstruction — correctly, because on Monday the system did not know.

ENVELOPE (schema_version 1)
    schema_version, event_id, event_type, ts, recorded_at, scan_id (optional),
    corrects (optional event_id), payload (JSON object), hash

`hash` is a SHA-256 of the record's own canonical content. It detects an
edited record (see verify()); it is NOT a chain, so one record can't poison
the rest and git merges of a single-writer file stay safe.

SINGLE-WRITER RULE: each ledger file has exactly one writing job (same rule
the live-owned logs follow). Two jobs appending to one file would interleave
under git rebase.

This module is NOT yet used by scanner_live.py. signal_event_log.jsonl keeps
its own format until something needs the envelope; migrating the other ten
logs is explicitly deferred (see BLUEPRINT.md).
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone, timedelta

SCHEMA_VERSION = 1
# An event may not claim to have happened more than this far AFTER the moment
# it was recorded (clock skew only). Anything beyond it is a future-dated
# record, which can only ever be a bug or a hindsight leak.
CLOCK_SKEW_TOLERANCE = timedelta(seconds=60)


class LedgerError(Exception):
    pass


# ---------------------------------------------------------------- time
def to_utc(value):
    """datetime or ISO string -> timezone-aware UTC datetime. Naive input is
    REJECTED: a timestamp without an offset is ambiguous, and ambiguity is
    exactly how hindsight sneaks in."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise LedgerError(f"unparseable timestamp: {value!r}")
    if not isinstance(value, datetime):
        raise LedgerError(f"timestamp must be datetime or ISO string, got {type(value).__name__}")
    if value.tzinfo is None:
        raise LedgerError(f"naive timestamp refused (no UTC offset): {value.isoformat()}")
    return value.astimezone(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------- records
def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _hash_of(record):
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def make_record(event_type, payload, ts=None, recorded_at=None,
                scan_id=None, corrects=None):
    if not event_type or not isinstance(event_type, str):
        raise LedgerError("event_type must be a non-empty string")
    if not isinstance(payload, dict):
        raise LedgerError("payload must be a dict")
    try:
        _canonical(payload)          # must be plain JSON — no silent str() coercion
    except (TypeError, ValueError) as e:
        raise LedgerError(f"payload is not JSON-serializable: {e}")

    rec_at = to_utc(recorded_at) if recorded_at is not None else datetime.now(timezone.utc)
    ev_ts = to_utc(ts) if ts is not None else rec_at
    if ev_ts > rec_at + CLOCK_SKEW_TOLERANCE:
        raise LedgerError(
            f"future-dated event refused: ts {_iso(ev_ts)} is after recorded_at {_iso(rec_at)}")

    rec = {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "ts": _iso(ev_ts),
        "recorded_at": _iso(rec_at),
        "scan_id": scan_id,
        "corrects": corrects,
        "payload": payload,
    }
    rec["hash"] = _hash_of(rec)
    return rec


def append(path, event_type, payload, ts=None, scan_id=None, corrects=None, now=None):
    """Validate, then append ONE line. Returns the record, or None if the
    write itself failed (caller must treat None as 'not recorded'). Validation
    problems raise LedgerError — they are bugs, not I/O weather."""
    rec = make_record(event_type, payload, ts=ts, recorded_at=now,
                      scan_id=scan_id, corrects=corrects)
    try:
        with open(path, "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
    except OSError as e:
        print(f"[LEDGER WRITE ERROR] {path}: {e}")
        return None
    return rec


def read(path):
    """-> (records, bad_line_count). A corrupt line is counted and skipped; it
    never stops the read, and it is never 'repaired'."""
    import os
    records, bad = [], 0
    if not os.path.exists(path):
        return records, bad
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if not isinstance(r, dict) or "event_id" not in r:
                    raise ValueError("not a ledger record")
                records.append(r)
            except Exception:
                bad += 1
    return records, bad


def verify(records):
    """event_ids whose stored hash no longer matches their content, plus
    duplicate event_ids. Empty list == untouched."""
    problems, seen = [], set()
    for r in records:
        if r.get("hash") != _hash_of(r):
            problems.append(("EDITED", r.get("event_id")))
        if r.get("event_id") in seen:
            problems.append(("DUPLICATE_ID", r.get("event_id")))
        seen.add(r.get("event_id"))
    return problems


# ---------------------------------------------------------------- reading as of T
def as_of(records, T):
    """Records the system could have known at time T: BOTH event time and
    knowledge time must be <= T. Order preserved."""
    T = to_utc(T)
    return [r for r in records
            if to_utc(r["ts"]) <= T and to_utc(r["recorded_at"]) <= T]


def view_as_of(records, T=None):
    """as_of(), plus supersession computed ONLY from corrections that were
    themselves known by T. Returns copies; the stored records are untouched.
    A record corrected after T therefore still shows as uncorrected at T —
    which is what the system believed then. T=None means "everything ever
    recorded" (no time filter at all — deliberately NOT wall-clock-bounded, so
    a record with a slightly-ahead clock never silently disappears)."""
    visible = list(records) if T is None else as_of(records, T)
    superseded_by = {}
    for r in visible:                       # file order == knowledge order
        if r.get("corrects"):
            superseded_by[r["corrects"]] = r["event_id"]
    out = []
    for r in visible:
        c = dict(r)
        c["superseded_by"] = superseded_by.get(r["event_id"])
        out.append(c)
    return out


def current(records, T=None):
    """Convenience: the non-superseded records, optionally as of T."""
    return [r for r in view_as_of(records, T) if r["superseded_by"] is None]


def of_type(records, *event_types):
    return [r for r in records if r.get("event_type") in event_types]
