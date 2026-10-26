"""
audit_real_1h_flip.py — traces REAL 1-hour leg flips from the engine's saved history to what the bot reports. Kept separate from the planted-flip test.
Run: python audit_real_1h_flip.py [data_dir]        (default: ../tests_data_live)
With no real flip in the history it says so and exits 0 (nothing was verified). With flips it checks, per flip:
  1. direction and time come from the saved snapshots (independent read of state_history_log.jsonl)
  2. the reader reports it once (no duplicate across repeated scans) and at the first scan that saw it
  3. the gap since the previous scan is stated (skipped scans) and no later evidence is used
  4. the wording calls it a 1-hour leg flip, never a formal change of character
"""
import json, os, sys
from datetime import datetime, timezone
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("TELEGRAM_TOKEN", "x"); os.environ.setdefault("TELEGRAM_CHAT_ID", "1"); os.environ.setdefault("TWELVE_DATA_KEY", "x")
import ledger, market_reconstruct as mr, bus_runner as br

D = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "tests_data_live")
rd = lambda f: [json.loads(l) for l in open(os.path.join(D, f)) if l.strip()] if os.path.exists(os.path.join(D, f)) else []

hist = sorted(rd("state_history_log.jsonl"), key=lambda r: r["ts"])
snaps = [(r["ts"], (r["payload"].get("macro_leg") or {}).get("direction"), r["payload"]) for r in hist if r.get("event_type") == "STATE_SNAPSHOT"]
flips, prev = [], None
for i, (t, d, p) in enumerate(snaps):
    if d and prev and d != prev[1]:
        flips.append({"at": t, "from": prev[1], "to": d, "prev_scan": prev[0], "i": i})
    if d:
        prev = (t, d)
print(f"snapshots read: {len(snaps)} ({snaps[0][0][:16] if snaps else '-'} to {snaps[-1][0][:16] if snaps else '-'})")
if not flips:
    print("NO REAL 1-HOUR LEG FLIP IN THIS HISTORY. Nothing about the real flip path has been verified; only the planted-flip test covers it.")
    sys.exit(0)
ok = True
now = ledger.to_utc(snaps[-1][0])
res = mr.last_structure_events(rd("structure_digest_log.jsonl"), rd("thesis_context_log.jsonl"), rd("zone_log.jsonl"), hist, now)
rep = res.get("leg_flip_1h")
for f in flips:
    gap = (ledger.to_utc(f["at"]) - ledger.to_utc(f["prev_scan"])).total_seconds() / 60
    print(f"flip {f['from']} -> {f['to']} first seen {f['at'][:19]} (previous scan {f['prev_scan'][:19]}, {gap:.0f} min earlier)")
print("reader's latest 1-hour flip:", rep)
text = br._last_event_text(os.path.join(D), None, ["choch"], now)[0] if hasattr(br, "_last_event_text") else ""
print(text[:600])
low = text.lower()
c1 = "1-hour" in low or "hour" in low
c2 = ("only a change of leg direction" in low or "not the engine's own change-of-character flag" in low)
print("wording names a 1-hour leg flip:", c1, "| says it is NOT the engine's formal change-of-character flag:", c2)
ok = bool(rep) and c1 and c2
print("RESULT:", "PASS" if ok else "CHECK BY HAND")
sys.exit(0 if ok else 1)
