"""
fast_gate.py — decides, in a couple of seconds, whether THIS trigger should run the full scan job.

WHY IT EXISTS: replies were slow because the bot only looks at Telegram when the scan runs, and the scan ran every 5 minutes. With this gate,
cron-job.org can trigger the workflow every minute, and the heavy job (scan, price fetch, answer messages) only runs when
  * a Telegram message is waiting to be answered, or
  * the last scan is at least SCAN_EVERY_MIN minutes old (the normal rhythm), or
  * the gate cannot tell (fail open: a broken gate must never stop the scanner).
Otherwise the trigger ends here, costing a few seconds and no price-data credits.

WHAT IT NEVER DOES: it never reads or writes the ledger, never answers anything, never confirms (consumes) a Telegram update (it only PEEKS:
getUpdates with limit=1 and no timeout, using the offset the scanner already saved), and never touches the engine. The scanner stays the only
thing that actually receives messages.

Stdlib only. Writes `run=true|false` and `reason=...` to $GITHUB_OUTPUT when set; always prints the decision.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SCAN_EVERY_MIN = 5          # the scanner's normal rhythm
MIN_GAP_MIN = 2             # a waiting message never causes scans closer together than this (protects the price-data allowance in an outage)
STATS_FILE = "stats.json"


def minutes_since_scan(stats, now):
    """Whole minutes since stats['last_scan'] (UTC, minute resolution), or None if there is no readable record."""
    try:
        last = datetime.strptime(str(stats.get("last_scan")), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return int((now.replace(second=0, microsecond=0) - last).total_seconds() // 60)


def decide(stats, now, pending):
    """-> (run: bool, reason: str). `pending` is True (a message is waiting), False (none), or None (could not check)."""
    since = minutes_since_scan(stats or {}, now)
    if since is None:
        return True, "no readable record of the last scan, so running to be safe"
    if since < 0:
        return True, "the last scan looks like it is in the future, so running to be safe"
    if since >= SCAN_EVERY_MIN:
        return True, f"the last scan was {since} minutes ago, so it is time for the regular one"
    if pending is None:
        return True, "could not check Telegram, so running to be safe"
    if pending and since >= MIN_GAP_MIN:
        return True, "a message is waiting"
    if pending:
        return False, f"a message is waiting but the last scan was only {since} minute(s) ago; the next trigger will pick it up"
    return False, f"nothing waiting and the last scan was {since} minute(s) ago"


def peek(token, last_update_id, opener=urllib.request.urlopen):
    """-> True if Telegram has an update the scanner has not taken yet, False if not, None if the check failed. Never confirms an update."""
    if not token:
        return None
    qs = urllib.parse.urlencode({"offset": int(last_update_id or 0) + 1, "limit": 1, "timeout": 0})
    try:
        with opener(f"https://api.telegram.org/bot{token}/getUpdates?{qs}", timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if not data.get("ok"):
        return None
    return bool(data.get("result"))


def main(argv, now=None, opener=urllib.request.urlopen):
    root = argv[1] if len(argv) > 1 else "."
    now = now or datetime.now(timezone.utc)
    try:
        with open(os.path.join(root, STATS_FILE), encoding="utf-8") as f:
            stats = json.load(f)
    except Exception:
        stats = {}
    pending = peek(os.environ.get("TELEGRAM_TOKEN"), (stats or {}).get("last_update_id", 0), opener)
    run, reason = decide(stats, now, pending)
    print(f"[GATE] run={'true' if run else 'false'}: {reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"run={'true' if run else 'false'}\nreason={reason}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")        # the plain-words line on the run's page
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### {'Scan will run' if run else 'Scan skipped on purpose'}\n{reason[0].upper() + reason[1:]}.\n"
                    + ("" if run else "\nThis is normal: the scanner is triggered every minute but only scans every 5 minutes (or sooner when you message it). "
                       "A skipped scan costs no price-data credits, so there is no scan log to show.\n"))
    return run


if __name__ == "__main__":
    main(sys.argv)
