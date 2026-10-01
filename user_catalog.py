"""
user_catalog.py — the User Catalog, v1. HEADLESS: no Telegram, no wording, no
market logic. Stores what the human did, said, believed and left open, on top
of ledger.py. It may remember the human; it must never become a trading brain.

WHAT IT STORES (all as append-only ledger events)
    MESSAGE_RECEIVED   inbound Telegram message, deduped by update_id
    TRADE_LOGGED       a trade the user took (case opens)
    REASONING_LOGGED   the user's own words, verbatim, never rewritten
    BOT_PLAN_ATTACHED  what the engine concluded AS OF the entry (caller
                       supplies the snapshot; the catalog never computes one)
    OUTCOME_RECORDED   how it ended
    INVESTIGATION_ADDED  post-hoc analysis, allowed only after an outcome
    THREAD_OPENED / THREAD_RESOLVED   open questions with a machine-checkable
                       resolution condition
    KNOWLEDGE_DEMONSTRATED   a concept the user showed, WITH evidence

THE FOUR SECTIONS OF A CASE ARE KEPT APART, ALWAYS
    trade | user_reasoning | bot_plan | outcome | investigation
Machine view vs user view is the whole point: they are never merged, and
investigation can only ADD records. Nothing here edits an earlier one.

HONESTY GUARDS (each is enforced in code and covered by a test)
  - A bot plan whose `plan_as_of` is later than the trade's entry time is
    refused (that would be the plan with hindsight).
  - Reasoning typed AFTER the outcome is stored, but flagged
    `written_after_outcome: true` — it may be rationalisation, and a later
    reader must be able to tell.
  - An outcome cannot predate the entry; investigation cannot precede the outcome.
  - A thread resolves only from candles/events strictly AFTER it was opened.
  - "Knows concept X" exists only if it points at real evidence in the ledger.

Imports: ledger only. No engine module may import this one (the layer checker
enforces both directions).
"""

import uuid
from datetime import datetime, timezone

import ledger
from ledger import LedgerError

DEFAULT_PATH = "user_ledger.jsonl"

TRADE_LOGGED = "TRADE_LOGGED"
REASONING_LOGGED = "REASONING_LOGGED"
BOT_PLAN_ATTACHED = "BOT_PLAN_ATTACHED"
OUTCOME_RECORDED = "OUTCOME_RECORDED"
INVESTIGATION_ADDED = "INVESTIGATION_ADDED"
THREAD_OPENED = "THREAD_OPENED"
THREAD_RESOLVED = "THREAD_RESOLVED"
MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
KNOWLEDGE_DEMONSTRATED = "KNOWLEDGE_DEMONSTRATED"

BOT_PLAN_RESULTS = ("TRADE", "NO_VALID_TRADE", "UNKNOWN")   # NO_VALID_TRADE is a legitimate result
OUTCOMES = ("WIN", "LOSS", "BREAKEVEN", "OPEN")
CONDITION_TYPES = ("price_close_above", "price_close_below", "event_code")


def _now(now):
    return ledger.to_utc(now) if now is not None else datetime.now(timezone.utc)


class Catalog:
    def __init__(self, path=DEFAULT_PATH):
        self.path = path

    # ------------------------------------------------------------ plumbing
    def _records(self):
        recs, _bad = ledger.read(self.path)
        return recs

    def _append(self, event_type, payload, ts=None, now=None):
        rec = ledger.append(self.path, event_type, payload, ts=ts, now=now)
        if rec is None:
            raise LedgerError(f"{event_type} could not be written to {self.path}")
        return rec

    def _find(self, event_type, key, value):
        return [r for r in self._records()
                if r["event_type"] == event_type and r["payload"].get(key) == value]

    # ------------------------------------------------------------ inbox
    def record_inbound(self, update_id, text, ts=None, now=None):
        """At-least-once safe: a Telegram update seen twice is stored once.
        Written BEFORE anything is answered, so a command consumed by a scan
        that then fails is not lost. Returns (record, is_new)."""
        existing = self._find(MESSAGE_RECEIVED, "update_id", update_id)
        if existing:
            return existing[0], False
        rec = self._append(MESSAGE_RECEIVED, {"update_id": update_id, "text": text},
                           ts=ts, now=now)
        return rec, True

    # ------------------------------------------------------------ cases
    def log_trade(self, pair, direction, entry, sl, tp, taken_at, now=None):
        direction = (direction or "").upper()
        if direction not in ("BUY", "SELL"):
            raise LedgerError("direction must be BUY or SELL")
        risk = (entry - sl) if direction == "BUY" else (sl - entry)
        reward = (tp - entry) if direction == "BUY" else (entry - tp)
        if risk <= 0 or reward <= 0:
            raise LedgerError("stop/target on the wrong side of entry")
        n = len(of_type_count(self._records(), TRADE_LOGGED)) + 1
        case_id = f"CASE-{n:04d}"
        self._append(TRADE_LOGGED, {
            "case_id": case_id, "pair": pair, "direction": direction,
            "entry": entry, "sl": sl, "tp": tp,
            "planned_r": round(reward / risk, 3),
        }, ts=taken_at, now=now)
        return case_id

    def _trade(self, case_id):
        t = self._find(TRADE_LOGGED, "case_id", case_id)
        if not t:
            raise LedgerError(f"unknown case {case_id}")
        return t[0]

    def _outcome(self, case_id):
        o = self._find(OUTCOME_RECORDED, "case_id", case_id)
        return o[0] if o else None

    def add_reasoning(self, case_id, text, now=None):
        self._trade(case_id)
        stated = _now(now)
        outcome = self._outcome(case_id)
        late = bool(outcome and ledger.to_utc(outcome["ts"]) <= stated)
        return self._append(REASONING_LOGGED, {
            "case_id": case_id, "text": text,
            "written_after_outcome": late,
        }, now=stated)

    def attach_bot_plan(self, case_id, plan_as_of, result, facts, now=None):
        """`facts` is whatever the ENGINE gave the caller as of `plan_as_of`;
        stored verbatim. The catalog does not compute or alter market facts."""
        trade = self._trade(case_id)
        if result not in BOT_PLAN_RESULTS:
            raise LedgerError(f"result must be one of {BOT_PLAN_RESULTS}")
        as_of_dt = ledger.to_utc(plan_as_of)
        if as_of_dt > ledger.to_utc(trade["ts"]):
            raise LedgerError("hindsight refused: bot plan is dated after the trade's entry")
        if self._find(BOT_PLAN_ATTACHED, "case_id", case_id):
            raise LedgerError("a bot plan is already attached to this case; append a correction instead")
        return self._append(BOT_PLAN_ATTACHED, {
            "case_id": case_id, "plan_as_of": ledger._iso(as_of_dt),
            "result": result, "facts": facts,
        }, ts=as_of_dt, now=now)

    def record_outcome(self, case_id, result, closed_at, r=None, now=None):
        trade = self._trade(case_id)
        if result not in OUTCOMES:
            raise LedgerError(f"result must be one of {OUTCOMES}")
        if ledger.to_utc(closed_at) < ledger.to_utc(trade["ts"]):
            raise LedgerError("outcome cannot predate the entry")
        if self._outcome(case_id):
            raise LedgerError("outcome already recorded; append a correction instead")
        return self._append(OUTCOME_RECORDED, {
            "case_id": case_id, "result": result, "r": r,
        }, ts=closed_at, now=now)

    def add_investigation(self, case_id, text, now=None):
        self._trade(case_id)
        if not self._outcome(case_id):
            raise LedgerError("investigation requires a recorded outcome")
        return self._append(INVESTIGATION_ADDED, {"case_id": case_id, "text": text}, now=now)

    def get_case(self, case_id, as_of=None):
        """The five sections, kept separate. With `as_of`, only what the
        system could have known by then (bitemporal), so a case can be
        reconstructed exactly as it stood."""
        view = ledger.view_as_of(self._records(), as_of)   # as_of=None -> everything recorded

        def pick(t):
            return [r for r in view if r["event_type"] == t and r["payload"].get("case_id") == case_id]
        trade = pick(TRADE_LOGGED)
        if not trade:
            return None
        return {
            "case_id": case_id,
            "trade": trade[0],
            "user_reasoning": pick(REASONING_LOGGED),
            "bot_plan": (pick(BOT_PLAN_ATTACHED) or [None])[0],
            "outcome": (pick(OUTCOME_RECORDED) or [None])[0],
            "investigation": pick(INVESTIGATION_ADDED),
        }

    # ------------------------------------------------------------ threads
    def open_thread(self, topic, concern, bot_position, condition, opened_at=None, now=None):
        _validate_condition(condition)
        n = len(of_type_count(self._records(), THREAD_OPENED)) + 1
        thread_id = f"THREAD-{n:04d}"
        self._append(THREAD_OPENED, {
            "thread_id": thread_id, "topic": topic, "concern": concern,
            "bot_position": bot_position, "condition": condition,
        }, ts=opened_at, now=now)
        return thread_id

    def open_threads(self, as_of=None):
        recs = self._records()
        if as_of is not None:
            recs = ledger.as_of(recs, as_of)
        resolved = {r["payload"]["thread_id"] for r in recs if r["event_type"] == THREAD_RESOLVED}
        return [r for r in recs if r["event_type"] == THREAD_OPENED
                and r["payload"]["thread_id"] not in resolved]

    def resolve_open_threads(self, candles=None, events=None, now=None):
        """Deterministic. For each open thread, look for the FIRST candle/event
        strictly after it was opened that satisfies its condition; if found,
        append THREAD_RESOLVED carrying that evidence. Returns the resolutions."""
        out = []
        for t in self.open_threads():
            match = evaluate_condition(t["payload"]["condition"], ledger.to_utc(t["ts"]),
                                       candles or [], events or [])
            if match:
                rec = self._append(THREAD_RESOLVED, {
                    "thread_id": t["payload"]["thread_id"], "evidence": match,
                }, ts=match["at"], now=now)
                out.append(rec)
        return out

    # ------------------------------------------------------------ knowledge (provenance)
    def record_knowledge(self, concept, evidence_event_id, note="", now=None):
        if not any(r["event_id"] == evidence_event_id for r in self._records()):
            raise LedgerError("knowledge requires evidence: unknown evidence_event_id")
        return self._append(KNOWLEDGE_DEMONSTRATED, {
            "concept": concept, "evidence_event_id": evidence_event_id, "note": note,
        }, now=now)

    def knows(self, concept):
        """Evidence records for `concept` (empty list == not known / a guess)."""
        return [r for r in self._records()
                if r["event_type"] == KNOWLEDGE_DEMONSTRATED
                and r["payload"].get("concept") == concept]


# ---------------------------------------------------------------- pure helpers
def of_type_count(records, event_type):
    return [r for r in records if r["event_type"] == event_type]


def _validate_condition(cond):
    if not isinstance(cond, dict) or cond.get("type") not in CONDITION_TYPES:
        raise LedgerError(f"condition.type must be one of {CONDITION_TYPES}")
    if cond["type"] in ("price_close_above", "price_close_below"):
        if not isinstance(cond.get("level"), (int, float)):
            raise LedgerError("price condition needs a numeric level")
    if cond["type"] == "event_code" and not cond.get("code"):
        raise LedgerError("event_code condition needs a code")


def evaluate_condition(cond, opened_at, candles, events):
    """Pure. Returns {"at": iso, ...evidence} for the earliest satisfying
    candle/event strictly after `opened_at`, else None. Candles need
    `datetime` + `close`; events need `code` + `recorded_at`. An event with no
    timestamp can never match — without a time it cannot be shown to be later."""
    kind = cond["type"]
    if kind in ("price_close_above", "price_close_below"):
        for c in sorted(candles, key=lambda c: ledger.to_utc(c["datetime"])):
            t = ledger.to_utc(c["datetime"])
            if t <= opened_at:
                continue
            hit = c["close"] > cond["level"] if kind == "price_close_above" else c["close"] < cond["level"]
            if hit:
                return {"at": ledger._iso(t), "kind": kind, "level": cond["level"], "close": c["close"]}
        return None
    if kind == "event_code":
        for e in sorted((e for e in events if e.get("recorded_at")),
                        key=lambda e: ledger.to_utc(e["recorded_at"])):
            t = ledger.to_utc(e["recorded_at"])
            if t > opened_at and e.get("code") == cond["code"]:
                return {"at": ledger._iso(t), "kind": kind, "code": cond["code"]}
        return None
    return None
