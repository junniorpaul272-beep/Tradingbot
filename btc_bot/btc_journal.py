"""
btc_journal.py: what Nelly ACTUALLY took and closed on her demo, as opposed to what the bot signalled.

The bot's own files (trades.jsonl, bank_ledger.jsonl, scans.jsonl, latest.json) are written only by the scheduled
runner. This journal is written only by the command handler. One writer per file means the two can never collide
when they commit to git from different runs.

The journal is append-only. Entries:
  taken   she entered a signal           {event_id, side, fill, ref_price, stop, bar_time}
  closed  she closed that trade          {taken_id, fill, ref_price, close_t}
  undo    cancels the latest active entry {undoes}      (there is no redo: record it again instead)
A trade only counts in the "taken" statistics once it has BOTH a taken and a closed entry. A trade that the bot has
already signalled out of, but that she has not confirmed with /closed, is "waiting on you", never guessed.
"""
import json
import os
from datetime import datetime, timezone

_OPEN = ("ENTRY", "REENTRY")
_LEAVE = ("CLOSE", "EXIT")


def read_jsonl(path):
    """All parseable lines of a JSON-lines file ([] if missing). A half-written last line is skipped."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def last_jsonl(path, max_bytes=400_000):
    """The last parseable record of a (possibly large) JSON-lines file, or None."""
    if not os.path.exists(path):
        return None
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - max_bytes))
        chunk = f.read().decode("utf-8", errors="ignore")
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


# --------------------------------------------------------------------------- signals (trades.jsonl records)
def open_signals(signals):
    """Signals the bot is still in, newest first: an ENTRY/REENTRY with no later real CLOSE/EXIT on the same side."""
    cur = {}
    for s in signals:
        if s["kind"] in _OPEN:
            cur[s["side"]] = s
        elif s["kind"] in _LEAVE and not s.get("flat"):
            cur.pop(s["side"], None)
    return sorted(cur.values(), key=lambda s: s["bar_time"], reverse=True)


def exit_after(signals, open_rec):
    """The first real CLOSE/EXIT on the same side after `open_rec`, or None if the bot has not left yet."""
    for s in signals:
        if (s["side"] == open_rec["side"] and s["kind"] in _LEAVE and not s.get("flat")
                and s["bar_time"] >= open_rec["bar_time"] and s["event_id"] != open_rec["event_id"]):
            return s
    return None


# --------------------------------------------------------------------------- the journal
class Journal:
    def __init__(self, path):
        self.path = path
        self.entries = read_jsonl(path)

    def _append(self, entry):
        entry = {"id": f"j{len(self.entries) + 1}", "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 **entry}
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        self.entries.append(entry)
        return entry

    def active(self):
        undone = {e["undoes"] for e in self.entries if e["type"] == "undo"}
        return [e for e in self.entries if e["type"] in ("taken", "closed") and e["id"] not in undone]

    def taken(self):
        return [e for e in self.active() if e["type"] == "taken"]

    def closed_for(self, taken_id):
        for e in self.active():
            if e["type"] == "closed" and e["taken_id"] == taken_id:
                return e
        return None

    def open_taken(self):
        """Her taken trades that she has not closed yet, oldest first."""
        return [t for t in self.taken() if self.closed_for(t["id"]) is None]

    def is_taken(self, event_id):
        return any(t["event_id"] == event_id for t in self.taken())

    def take(self, signal, fill):
        return self._append({"type": "taken", "event_id": signal["event_id"], "side": signal["side"],
                             "fill": fill, "ref_price": signal["price"], "stop": signal["active_stop"],
                             "bar_time": signal["bar_time"]})

    def close(self, taken, fill, ref_price, close_t):
        return self._append({"type": "closed", "taken_id": taken["id"], "fill": fill, "ref_price": ref_price,
                             "close_t": close_t})

    def undo(self):
        """Cancel the latest active entry. Returns it, or None if there is nothing to undo."""
        act = self.active()
        if not act:
            return None
        last = act[-1]
        self._append({"type": "undo", "undoes": last["id"]})
        return last


def taken_trades(journal):
    """Her completed trades, measured from HER fills, in the shape btc_stats expects (risk/pnl per 1 BTC)."""
    out = []
    for t in journal.taken():
        c = journal.closed_for(t["id"])
        if c is None:
            continue
        d = 1 if t["side"] == "LONG" else -1
        risk = abs(t["fill"] - t["stop"])
        out.append({"side": t["side"], "open_t": t["bar_time"], "close_t": c["close_t"], "entry": t["fill"],
                    "exit": c["fill"], "stop": t["stop"], "risk": risk if risk > 0 else None,
                    "pnl": d * (c["fill"] - t["fill"]), "close_kind": "TAKEN", "event_id": t["event_id"]})
    return out
