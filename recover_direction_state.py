#!/usr/bin/env python3
"""
Recovery / quarantine tool for the 2026-09-28 stale-bias promotion incident.

WHY THIS EXISTS
  The bot's held direction + campaign were replaced by a faulty 15M
  promotion. Fixing the promotion rule does not repair the state it already
  wrote. This tool restores the last TRUSTWORTHY directional context from
  git history (state.json is committed on every live scan) and keeps the
  contaminated values as forensic record. It never reconstructs a campaign
  from candles.

WHAT IT TOUCHES
  Only the direction/campaign/leg/belief keys of state.json (see
  TRANSPLANT_*), transplanted from an earlier commit into the CURRENT
  state.json. Everything else in state.json is left alone ON PURPOSE:
  it holds dedup cursors (ohlc_history_last_ts_*, zone_log_seen, ...) for
  permanent append-only logs. Rolling a whole state.json back would replay
  those and write duplicate rows.
  markov_transitions.json is restored whole from the same commit (it is
  written only by scanner_live.py, and everything after the cutoff is
  downstream of the bad promotion).
  Permanent logs (digest, scan log, shadow, bank ...) are never edited.
  They are covered by the quarantine record instead.

USAGE  (run from the repo root; DRY RUN unless --apply)
  1. PAUSE the live scanner first (cron-job.org triggers / workflow).
  2. python recover_direction_state.py --list
  3. python recover_direction_state.py --commit <sha>            # review
     python recover_direction_state.py --commit <sha> --apply
  4. Deploy the patched scanner files, commit, resume the scanner.
  No usable commit?  --mark-only --apply  sets campaign_quarantined_since,
  so the patched scanner holds (no signals, no Markov learning) until a
  human decides.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

os.environ.setdefault("TELEGRAM_TOKEN", "unused")      # scanner_common needs
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")         # these at import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scanner_observation import campaign_status        # noqa: E402  (one status law)

DEFAULT_SINCE = "2026-09-28T08:05:00+00:00"   # first scan of the bad promotion
STATE_FILE = "state.json"
MARKOV_FILE = "markov_transitions.json"
QUARANTINE_DIR = "quarantine"
QUARANTINE_LOG = "quarantine_log.jsonl"

TRANSPLANT_EXACT = {
    "macro_bias_confirmed", "macro_bias_stale", "mil_understanding",
    "markov_last_state",
}
TRANSPLANT_PREFIXES = (
    "macro_leg_", "macro_swing_", "macro_candidate_leg_", "campaign_",
    "leg15_", "prior_macro_leg_", "prior_continuation_leg_",
)
# recomputed every scan / handled explicitly - never copied
NEVER_COPY = {"campaign_status", "campaign_status_reason", "campaign_quarantined_since"}


def _is_transplant_key(k):
    if k in NEVER_COPY:
        return False
    return k in TRANSPLANT_EXACT or k.startswith(TRANSPLANT_PREFIXES)


def _git(repo, *args):
    r = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("git " + " ".join(args) + " failed: " + r.stderr.strip())
    return r.stdout


def _commits_for(repo, path, limit=400):
    out = _git(repo, "log", "--format=%H|%cI", "-n", str(limit), "--", path)
    rows = []
    for line in out.splitlines():
        sha, when = line.split("|", 1)
        rows.append((sha, datetime.fromisoformat(when)))
    return rows


def _state_at(repo, sha):
    return json.loads(_git(repo, "show", sha + ":" + STATE_FILE))


def _summ(st):
    status, why = campaign_status(st)
    return ("bias={}{} campaign={} {}->{} status={} mil={}".format(
        st.get("macro_bias_confirmed"), "(stale)" if st.get("macro_bias_stale") else "",
        st.get("campaign_direction"), st.get("campaign_origin"),
        st.get("campaign_current_extreme"), status,
        (st.get("mil_understanding") or {}).get("thesis_status")))


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _append_jsonl(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def cmd_list(repo, since):
    rows = [(s, w) for s, w in _commits_for(repo, STATE_FILE)]
    print("state.json commits around the cutoff " + since.isoformat() + "  (newest first)\n")
    shown = 0
    for sha, when in rows:
        if abs((when - since).total_seconds()) > 36 * 3600:
            continue
        mark = "BEFORE" if when < since else "after "
        try:
            print("{} {} {}  {}".format(mark, sha[:10], when.isoformat(), _summ(_state_at(repo, sha))))
        except Exception as e:                       # noqa
            print("{} {} {}  (unreadable: {})".format(mark, sha[:10], when.isoformat(), e))
        shown += 1
        if shown >= 40:
            break
    print("\nPick the LAST 'BEFORE' commit whose status is VALID/STALE and whose campaign is the")
    print("bearish run you trust, then:  --commit <sha>")


def cmd_restore(repo, sha, since, apply, reason, skip_markov):
    sha = _git(repo, "rev-parse", sha).strip()
    when = dict(_commits_for(repo, STATE_FILE)).get(sha)
    if when is None:
        raise SystemExit("that commit did not touch state.json")
    if not when < since:
        raise SystemExit("REFUSED: commit {} is not before the cutoff {}".format(when.isoformat(), since.isoformat()))

    old = _state_at(repo, sha)
    status, why = campaign_status(old)
    if status not in ("VALID", "STALE"):
        raise SystemExit("REFUSED: campaign in that commit is {} ({}). Not trustworthy.".format(status, why))

    spath = os.path.join(repo, STATE_FILE)
    cur = json.load(open(spath, encoding="utf-8"))

    replaced = {}
    for k in sorted(set(cur) | set(old)):
        if not _is_transplant_key(k):
            continue
        if cur.get(k) != old.get(k):
            replaced[k] = cur.get(k)                 # what we are about to overwrite
    new = dict(cur)
    for k in list(new):
        if _is_transplant_key(k) and k not in old:
            del new[k]
    for k, v in old.items():
        if _is_transplant_key(k):
            new[k] = v
    new.pop("campaign_quarantined_since", None)

    print("restoring from {} ({})".format(sha[:10], when.isoformat()))
    print("  BEFORE (current): " + _summ(cur))
    print("  AFTER  (restored): " + _summ(new))
    print("  keys changed: {}   (cursors and all other keys untouched)".format(len(replaced)))
    if not skip_markov:
        print("  markov_transitions.json: will be restored whole from the same commit")
    if not apply:
        print("\nDRY RUN - nothing written. Re-run with --apply.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    qdir = os.path.join(repo, QUARANTINE_DIR)
    os.makedirs(qdir, exist_ok=True)
    forensic = os.path.join(qdir, "state_contaminated_" + stamp + ".json")
    _atomic_write(forensic, json.dumps({"replaced_values": replaced}, indent=2, default=str))

    markov_note = None
    if not skip_markov:
        mpath = os.path.join(repo, MARKOV_FILE)
        if os.path.exists(mpath):
            _atomic_write(os.path.join(qdir, "markov_transitions_contaminated_" + stamp + ".json"),
                          open(mpath, encoding="utf-8").read())
        try:
            _atomic_write(mpath, _git(repo, "show", sha + ":" + MARKOV_FILE))
            markov_note = "restored from " + sha[:10]
        except SystemExit:
            markov_note = "not present in that commit - left unchanged"

    _atomic_write(spath, json.dumps(new, indent=2, default=str))
    _append_jsonl(os.path.join(repo, QUARANTINE_LOG), {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "action": "restore",
        "quarantine_start": since.isoformat(),
        "reason": reason,
        "restored_from_commit": sha,
        "restored_commit_time": when.isoformat(),
        "keys_replaced": sorted(replaced),
        "forensic_file": os.path.relpath(forensic, repo),
        "markov": markov_note,
        "campaign_before": {k: cur.get(k) for k in ("campaign_direction", "campaign_origin", "campaign_current_extreme")},
        "campaign_after": {k: new.get(k) for k in ("campaign_direction", "campaign_origin", "campaign_current_extreme")},
    })
    print("\nAPPLIED. Forensic copy: " + os.path.relpath(forensic, repo))
    print("Next: deploy patched scanner files, commit state.json + quarantine/ + quarantine_log.jsonl, resume scanner.")


def cmd_mark_only(repo, since, apply, reason):
    spath = os.path.join(repo, STATE_FILE)
    cur = json.load(open(spath, encoding="utf-8"))
    print("current: " + _summ(cur))
    print("would set campaign_quarantined_since = " + since.isoformat())
    if not apply:
        print("DRY RUN - nothing written. Re-run with --apply.")
        return
    cur["campaign_quarantined_since"] = since.isoformat()
    _atomic_write(spath, json.dumps(cur, indent=2, default=str))
    _append_jsonl(os.path.join(repo, QUARANTINE_LOG), {
        "logged_at": datetime.now(timezone.utc).isoformat(), "action": "mark_only",
        "quarantine_start": since.isoformat(), "reason": reason,
    })
    print("APPLIED. Patched scanner will hold (no signals, no Markov learning) until recovery.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--since", default=DEFAULT_SINCE, help="start of the contaminated window (ISO, UTC)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--commit")
    ap.add_argument("--mark-only", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--skip-markov", action="store_true")
    ap.add_argument("--reason", default="stale-bias promotion flipped BEARISH->BULLISH without defeating the campaign")
    a = ap.parse_args()
    since = datetime.fromisoformat(a.since)
    if a.list:
        cmd_list(a.repo, since)
    elif a.commit:
        cmd_restore(a.repo, a.commit, since, a.apply, a.reason, a.skip_markov)
    elif a.mark_only:
        cmd_mark_only(a.repo, since, a.apply, a.reason)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
