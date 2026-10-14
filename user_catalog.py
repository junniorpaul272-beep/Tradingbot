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

import re
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
THREAD_CANCELLED = "THREAD_CANCELLED"
MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
KNOWLEDGE_DEMONSTRATED = "KNOWLEDGE_DEMONSTRATED"
TERMS_OBSERVED = "TERMS_OBSERVED"
USER_TERM_ALIAS = "USER_TERM_ALIAS"

# ----------------------------------------------------------------------
# THE USER'S OWN LANGUAGE (design: BLUEPRINT section 12).
# Your words are preserved BEFORE they are understood. Three levels:
#   raw text            always stored verbatim (REASONING_LOGGED never changes)
#   recognised concept  only if the ENGINE has a measurable definition for it
#   unrecognised term   a user assertion, status `unrecognised_by_engine`
# The catalog never invents a definition: "support" stays your word until a measurable one exists.
# Explicit user terms are written #like_this — no guessing which words are "concepts".
# ----------------------------------------------------------------------
# canonical concept -> aliases. ONLY concepts the engine really computes (the function in parentheses was
# found in scanner_observation.py on 2026-10-02); this list is data, extend it when the engine learns one.
ENGINE_CONCEPTS = {
    "break_of_structure": ("bos", "break of structure"),                  # detect_bos_impulse
    "change_of_character": ("choch", "change of character"),              # event code CHOCH_* in market_read
    "order_block": ("order block", "ob"),                                 # detect_order_block
    "fair_value_gap": ("fvg", "fair value gap", "imbalance"),             # detect_fvg / detect_significant_fvg
    "liquidity_sweep": ("liquidity sweep", "sweep", "stop hunt"),         # detect_liquidity_sweep
    "fib_pocket": ("fib pocket", "fib zone", "fib"),                      # compute_fib_fraction / is_fib_zone_stale
    "macro_bias": ("htf bias", "macro bias", "higher timeframe bias"),    # compute_macro_bias
    "campaign": ("campaign",),                                            # compute_campaign_extension
    "market_phase": ("phase", "market phase"),                            # compute_market_phase
    "session": ("london session", "ny session", "asian session"),         # is_active_session
    "swing": ("swing high", "swing low", "fractal"),                      # find_all_fractals
}
_ALIAS_TO_CONCEPT = {a: c for c, al in ENGINE_CONCEPTS.items() for a in al}
_TAG = re.compile(r"(?<![\w#])#([A-Za-z][A-Za-z0-9_\-]{1,40})")


def term_key(raw):
    """Normalised comparison key for a user term; the RAW spelling is always kept alongside."""
    return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")


def extract_terms(text):
    """Pure. -> {"recognised": [{"concept","matched"}], "user_terms": [{"raw","key","status"}]}.
    Recognised = a known engine alias appears as a whole word/phrase. User terms = explicit #tags only;
    a #tag that matches an engine alias is `recognised_by_engine`, anything else `unrecognised_by_engine`."""
    low = (text or "").lower()
    rec, seen = [], set()
    for alias, concept in sorted(_ALIAS_TO_CONCEPT.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", low) and concept not in seen:
            seen.add(concept); rec.append({"concept": concept, "matched": alias})
    users, kseen = [], set()
    for m in _TAG.finditer(text or ""):
        raw = m.group(1)
        key = term_key(raw)
        if key in kseen:
            continue
        kseen.add(key)
        engine = key.replace("_", " ") in _ALIAS_TO_CONCEPT or key in _ALIAS_TO_CONCEPT
        users.append({"raw": raw, "key": key, "status": "recognised_by_engine" if engine else "unrecognised_by_engine"})
    return {"recognised": rec, "user_terms": users}

BOT_PLAN_RESULTS = ("TRADE", "NO_VALID_TRADE", "UNKNOWN")   # NO_VALID_TRADE is a legitimate result
OUTCOMES = ("WIN", "LOSS", "BREAKEVEN", "OPEN")
CONDITION_TYPES = ("price_close_above", "price_close_below", "price_trade_above",
                   "price_trade_below", "price_in_zone", "event_code",
                   "ema_close_above", "ema_close_below", "ema_touch")   # must equal bus_contract.CONDITION_TYPES (a test checks)
TIMEFRAMES = ("5m", "15m", "1h")
EMA_WARMUP_FACTOR = 3        # an EMA(p) is only trusted once 3*p candles precede the candle being tested


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
        rec = self._append(REASONING_LOGGED, {
            "case_id": case_id, "text": text,
            "written_after_outcome": late,
        }, now=stated)
        found = extract_terms(text)
        if found["recognised"] or found["user_terms"]:
            self._append(TERMS_OBSERVED, {"case_id": case_id, "reasoning_event_id": rec["event_id"], **found}, now=stated)
        return rec

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

    def cancel_thread(self, thread_id, now=None):
        """Append-only: the alert/thread stays in the ledger, it just stops being checked."""
        if not any(r["event_type"] == THREAD_OPENED and r["payload"]["thread_id"] == thread_id for r in self._records()):
            raise LedgerError(f"unknown thread {thread_id}")
        if any(r["event_type"] in (THREAD_RESOLVED, THREAD_CANCELLED) and r["payload"]["thread_id"] == thread_id for r in self._records()):
            raise LedgerError(f"{thread_id} is already finished")
        return self._append(THREAD_CANCELLED, {"thread_id": thread_id}, now=now)

    def open_threads(self, as_of=None):
        recs = self._records()
        if as_of is not None:
            recs = ledger.as_of(recs, as_of)
        resolved = {r["payload"]["thread_id"] for r in recs if r["event_type"] in (THREAD_RESOLVED, THREAD_CANCELLED)}
        return [r for r in recs if r["event_type"] == THREAD_OPENED
                and r["payload"]["thread_id"] not in resolved]

    def resolve_open_threads(self, candles=None, events=None, now=None, candles_by_tf=None):
        """Deterministic. For each open thread, look for the FIRST candle/event
        strictly after it was opened that satisfies its condition; if found,
        append THREAD_RESOLVED carrying that evidence. Returns the resolutions."""
        out = []
        for t in self.open_threads():
            cond = t["payload"]["condition"]
            tf = cond.get("timeframe", "5m")
            pool = (candles_by_tf or {}).get(tf)
            if pool is None:
                pool = candles if tf == "5m" else None      # a 15m/1h condition is never evaluated on 5m candles
            match = evaluate_condition(cond, ledger.to_utc(t["ts"]), pool or [], events or [])
            if match:
                rec = self._append(THREAD_RESOLVED, {
                    "thread_id": t["payload"]["thread_id"], "evidence": match,
                }, ts=match["at"], now=now)
                out.append(rec)
        return out

    # ------------------------------------------------------------ the user's own terms
    def alias_term(self, variant, canonical, now=None):
        """"suport" means "support". Append-only: the raw spelling stays in every record it appeared in;
        this only changes how usage is COUNTED. Last alias wins at read time."""
        v, c = term_key(variant), term_key(canonical)
        if not v or not c or v == c:
            raise LedgerError("alias needs two different terms")
        return self._append(USER_TERM_ALIAS, {"variant": v, "canonical": c, "raw_variant": variant, "raw_canonical": canonical}, now=now)

    def term_usage(self, as_of=None):
        """-> list of {"term","status","uses","cases","variants","examples"} sorted by use. Counts what the
        user typed (after aliasing); it never states what a term MEANS."""
        recs = self._records()
        if as_of is not None:
            recs = ledger.as_of(recs, as_of)
        alias = {}
        for r in recs:
            if r["event_type"] == USER_TERM_ALIAS:
                alias[r["payload"]["variant"]] = r["payload"]["canonical"]

        def canon(k):
            seen = set()
            while k in alias:
                if k in seen:
                    return min(seen)          # an alias CYCLE (a=b then b=a): everything in it is one term; pick deterministically
                seen.add(k); k = alias[k]
            return k
        agg = {}
        for r in recs:
            if r["event_type"] != TERMS_OBSERVED:
                continue
            for t in r["payload"]["user_terms"]:
                k = canon(t["key"])
                a = agg.setdefault(k, {"term": k, "status": t["status"], "uses": 0, "cases": set(), "variants": set(), "examples": []})
                a["uses"] += 1; a["cases"].add(r["payload"]["case_id"]); a["variants"].add(t["raw"])
                if len(a["examples"]) < 3:
                    a["examples"].append(r["payload"]["reasoning_event_id"])
        out = [dict(a, cases=sorted(a["cases"]), variants=sorted(a["variants"])) for a in agg.values()]
        return sorted(out, key=lambda a: (-a["uses"], a["term"]))

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
    t = cond["type"]
    if "timeframe" in cond and cond["timeframe"] not in TIMEFRAMES:
        raise LedgerError(f"timeframe must be one of {TIMEFRAMES}")
    if t.startswith("ema_"):
        p = cond.get("period")
        if isinstance(p, bool) or not isinstance(p, int) or not (2 <= p <= 500):
            raise LedgerError("ema condition needs an integer period between 2 and 500")
        if cond.get("timeframe") not in TIMEFRAMES:
            raise LedgerError(f"ema condition needs a timeframe in {TIMEFRAMES}")
    if t.startswith("price_close") or t.startswith("price_trade"):
        if isinstance(cond.get("level"), bool) or not isinstance(cond.get("level"), (int, float)):
            raise LedgerError("price condition needs a numeric level")
    if t == "price_in_zone":
        lo, hi = cond.get("low"), cond.get("high")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (lo, hi)) or lo > hi:
            raise LedgerError("price_in_zone needs numeric low <= high")
    if t == "event_code" and not cond.get("code"):
        raise LedgerError("event_code condition needs a code")


def ema_series(closes, period):
    """EMA(period): seeded with the simple average of the first `period` closes, then
    ema = ema_prev + 2/(period+1) * (close - ema_prev). Entries before the seed are None."""
    out = [None] * len(closes)
    if len(closes) < period:
        return out
    ema = sum(closes[:period]) / period
    out[period - 1] = ema
    k = 2.0 / (period + 1)
    for i in range(period, len(closes)):
        ema += k * (closes[i] - ema)
        out[i] = ema
    return out


def candles_needed_for_ema(period):
    return EMA_WARMUP_FACTOR * period + 1


def evaluate_condition(cond, opened_at, candles, events):
    """Pure. Returns {"at": iso, ...evidence} for the earliest satisfying
    candle/event strictly after `opened_at`, else None. Candles need
    `datetime` + `close`; events need `code` + `recorded_at`. An event with no
    timestamp can never match — without a time it cannot be shown to be later."""
    kind = cond["type"]
    if kind.startswith("ema_"):
        cs = sorted(candles, key=lambda c: ledger.to_utc(c["datetime"]))
        times = [ledger.to_utc(c["datetime"]) for c in cs]
        ema = ema_series([c["close"] for c in cs], cond["period"])
        for i, c in enumerate(cs):
            if times[i] <= opened_at or i < EMA_WARMUP_FACTOR * cond["period"] or ema[i] is None:
                continue                                    # not after the alert, or the EMA is not yet settled
            if kind == "ema_close_above":   hit, val = c["close"] > ema[i], ema[i]
            elif kind == "ema_close_below": hit, val = c["close"] < ema[i], ema[i]
            else:                           # touch: the EMA as it stood BEFORE this candle (known at its open — no lookahead)
                val = ema[i - 1]
                hit = val is not None and c["low"] <= val <= c["high"]
            if hit:
                return {"at": ledger._iso(times[i]), "kind": kind, "period": cond["period"], "timeframe": cond["timeframe"],
                        "ema": round(val, 6), "close": c["close"], "high": c["high"], "low": c["low"]}
        return None
    if kind.startswith("price_"):
        for c in sorted(candles, key=lambda c: ledger.to_utc(c["datetime"])):
            t = ledger.to_utc(c["datetime"])
            if t <= opened_at:
                continue
            if kind == "price_close_above":   hit, seen = c["close"] > cond["level"], {"close": c["close"]}
            elif kind == "price_close_below": hit, seen = c["close"] < cond["level"], {"close": c["close"]}
            elif kind == "price_trade_above": hit, seen = c["high"] >= cond["level"], {"high": c["high"]}
            elif kind == "price_trade_below": hit, seen = c["low"] <= cond["level"], {"low": c["low"]}
            else:                             hit, seen = (c["low"] <= cond["high"] and c["high"] >= cond["low"]), {"low": c["low"], "high": c["high"]}
            if hit:
                return {"at": ledger._iso(t), "kind": kind, **{k: v for k, v in cond.items() if k != "type"}, **seen}
        return None
    if kind == "event_code":
        for e in sorted((e for e in events if e.get("recorded_at")),
                        key=lambda e: ledger.to_utc(e["recorded_at"])):
            t = ledger.to_utc(e["recorded_at"])
            if t > opened_at and e.get("code") == cond["code"]:
                return {"at": ledger._iso(t), "kind": kind, "code": cond["code"]}
        return None
    return None
