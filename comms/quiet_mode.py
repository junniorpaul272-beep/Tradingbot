"""
quiet_mode.py — the user's "mute the automated pushes for a while" switch. Pure: stdlib + ledger. No Telegram, no
files, no clock (the caller passes `now`), no engine, BUS-wording or catalog imports. Only bus_runner uses it.

WHAT QUIET MODE MUTES (and what it never mutes)
  muted, HELD and summarised once when quiet ends:
    - automated fired-signal announcements (BUS_SIGNALS)
    - automated integrity notifications (MED / HIGH)
  never muted:
    - replies to the user's own messages
    - price alerts the user personally set (/alert)
    - a CRITICAL integrity finding (see is_critical): sent at once with a plain note that quiet mode is on

  Quiet mode changes WHEN the user hears something. It does not change what the engine does, what is
  recorded, or what any other component sees: nothing is dropped, every held item is stored and summarised.

HOW IT IS STORED (user_ledger.jsonl, append-only, written only by bus_runner)
  QUIET_SET       {until, set_at, minutes, update_id}    a quiet period with an explicit end time
  QUIET_CLEARED   {update_id, quiet_id}                  /loud
  BUS_HELD        {kind, quiet_id, ...}                  one automated message held instead of sent
  BUS_CRITICAL_SENT {quiet_id, fingerprints}             a critical finding that bypassed quiet
  BUS_QUIET_SUMMARY {quiet_id, held_ids, text}           the ONE summary for a quiet period

FAIL-SAFE (the direction of every error is "the user hears about it")
  - quiet is active only while now < until; expiry is computed from `until`, never from a clear event
    that might not have been written;
  - a QUIET_SET whose `until` is missing, unparseable or absurd is treated as NOT quiet;
  - an `until` further than MAX_QUIET past the moment it was recorded is clamped to that bound;
  - there is no open-ended mute: every period has an end, and the longest is MAX_QUIET.
"""

import re
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import ledger

QUIET_SET = "QUIET_SET"
QUIET_CLEARED = "QUIET_CLEARED"
BUS_HELD = "BUS_HELD"
BUS_CRITICAL_SENT = "BUS_CRITICAL_SENT"
BUS_QUIET_SUMMARY = "BUS_QUIET_SUMMARY"

MAX_QUIET = timedelta(hours=24)
MIN_QUIET = timedelta(minutes=1)
DEFAULT_TZ = "Africa/Lagos"          # `until 18:00` means this clock unless the user writes `utc`
QUIET_COMMANDS = ("/quiet", "/loud")

USAGE = (
    "Quiet mode holds my automated messages (signals and integrity notices) and sends ONE summary when it ends.\n"
    "/quiet 3h — quiet for 3 hours (also 45m, 1h30m)\n"
    "/quiet until 18:00 — until 18:00 Lagos time (add utc to use UTC: /quiet until 17:00 utc)\n"
    "/loud — end quiet mode now\n"
    "The longest is 24h. Replies to you and the alerts you set yourself always come through, and so does a "
    "critical integrity finding. I only read messages when the scanner runs, so timing follows that."
)

Parsed = namedtuple("Parsed", "kind until minutes zone_note message")
QuietState = namedtuple("QuietState", "active until set_at quiet_id reason")


def command_of(text):
    first = (text or "").strip().split(" ", 1)[0].lower().split("@")[0]
    return first if first in QUIET_COMMANDS else None


_DUR = re.compile(r"^(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?$", re.I)
_UNTIL = re.compile(r"^until\s+(\d{1,2})[:.](\d{2})\s*(utc|gmt|lagos|wat)?$", re.I)


def parse(text, set_at, now):
    """-> Parsed. kind in: loud, usage, set, error. `set_at` is when the user SENT the message (a duration counts
    from there, not from whenever the scanner got round to reading it); `now` is processing time."""
    raw = (text or "").strip()
    cmd = command_of(raw)
    rest = raw.split(" ", 1)[1].strip() if " " in raw else ""
    if cmd == "/loud":
        return Parsed("loud", None, None, None, None)
    if cmd != "/quiet":
        return Parsed("error", None, None, None, "not a quiet command")
    if not rest:
        return Parsed("usage", None, None, None, None)
    set_at = ledger.to_utc(set_at) if set_at is not None else now
    m = _UNTIL.match(rest)
    if m:
        hh, mm, tz = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
        if hh > 23 or mm > 59:
            return Parsed("error", None, None, None, "That isn't a time of day. Try /quiet until 18:00")
        zone = timezone.utc if tz in ("utc", "gmt") else ZoneInfo(DEFAULT_TZ)
        label = "UTC" if tz in ("utc", "gmt") else "Lagos time"
        local = set_at.astimezone(zone)
        target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= local:
            target += timedelta(days=1)
        until = target.astimezone(timezone.utc)
        return _bounded(until, set_at, now, f"{hh:02d}:{mm:02d} {label}")
    m = _DUR.match(rest.replace(",", " ").strip())
    if m and any(m.groups()):
        d, h, mi = (int(x) if x else 0 for x in m.groups())
        delta = timedelta(days=d, hours=h, minutes=mi)
        return _bounded(set_at + delta, set_at, now, None)
    return Parsed("error", None, None, None, "I couldn't read that duration. Try /quiet 3h or /quiet until 18:00")


def _bounded(until, set_at, now, zone_note):
    length = until - set_at
    if length < MIN_QUIET:
        return Parsed("error", None, None, None, "That is shorter than a minute, so I set nothing.")
    if length > MAX_QUIET:
        return Parsed("error", None, None, None,
                      "The longest quiet period is 24h, so I set nothing. Send /quiet 24h for the longest.")
    return Parsed("set", until, int(length.total_seconds() // 60), zone_note, None)


def _until_of(rec):
    """-> (until or None). Never raises: a malformed record is 'no valid until'."""
    try:
        until = ledger.to_utc((rec.get("payload") or {}).get("until"))
        recorded = ledger.to_utc(rec["recorded_at"])
    except Exception:
        return None
    return min(until, recorded + MAX_QUIET)


def state(records, now):
    """The quiet state at `now`, from the latest QUIET_SET / QUIET_CLEARED record only."""
    last = None
    for r in records:
        if r.get("event_type") in (QUIET_SET, QUIET_CLEARED):
            last = r
    if last is None:
        return QuietState(False, None, None, None, "never set")
    if last["event_type"] == QUIET_CLEARED:
        return QuietState(False, None, None, None, "cleared")
    until = _until_of(last)
    if until is None:
        return QuietState(False, None, None, last.get("event_id"), "malformed record, treated as not quiet")
    now = ledger.to_utc(now)
    if now >= until:
        return QuietState(False, until, None, last.get("event_id"), "expired")
    return QuietState(True, until, (last.get("payload") or {}).get("set_at"), last["event_id"], "active")


# A finding may break through quiet mode only if it is this specific, narrow case: two sources that are meant to
# be written by the SAME code disagree on direction, i.e. the engine contradicts itself. Everything else
# (including the other HIGH findings) waits for the summary. Kept as an allowlist on purpose.
CRITICAL_PROBES = {"direction_conflict"}
CRITICAL_RELATIONSHIPS = {"same_writer"}


def is_critical(finding):
    try:
        return (finding.get("severity") == "HIGH" and finding.get("probe") in CRITICAL_PROBES
                and finding.get("relationship") in CRITICAL_RELATIONSHIPS)
    except AttributeError:
        return False


def pending_summary(records, now):
    """-> dict(quiet_id, set_payload, held[], critical[], ended_at) when a quiet period has ended and has not been
    summarised yet; None otherwise (still quiet, never quiet, or already summarised)."""
    st = state(records, now)
    if st.active:
        return None
    last = None
    for r in records:
        if r.get("event_type") in (QUIET_SET, QUIET_CLEARED):
            last = r
    if last is None:
        return None
    if last["event_type"] == QUIET_CLEARED:
        qid = (last.get("payload") or {}).get("quiet_id")
        ended = last.get("recorded_at")
    else:
        qid = last.get("event_id")
        u = _until_of(last)
        if u is None:                       # a malformed period never really began: no "quiet ended" message about it
            return None
        ended = u.isoformat()
    if not qid:
        return None
    summarised = [r for r in records if r.get("event_type") == BUS_QUIET_SUMMARY]
    if any((r.get("payload") or {}).get("quiet_id") == qid for r in summarised):
        return None
    taken = {i for r in summarised for i in ((r.get("payload") or {}).get("held_ids") or [])}
    held = [r for r in records if r.get("event_type") == BUS_HELD and r.get("event_id") not in taken]
    crit = [r for r in records if r.get("event_type") == BUS_CRITICAL_SENT
            and (r.get("payload") or {}).get("quiet_id") == qid]
    started = None
    for r in records:
        if r.get("event_id") == qid:
            started = (r.get("payload") or {}).get("set_at") or r.get("recorded_at")
    return {"quiet_id": qid, "held": held, "critical": crit, "started_at": started, "ended_at": ended}


def hhmm(value):
    try:
        return ledger.to_utc(value).strftime("%H:%M UTC")
    except Exception:
        return "an unknown time"
