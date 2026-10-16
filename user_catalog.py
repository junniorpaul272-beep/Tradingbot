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

import math
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
INVESTIGATION_OPENED = "INVESTIGATION_OPENED"  # an investigation started on a case (status PENDING_OUTCOME until an outcome exists)
KNOWLEDGE_STATED = "KNOWLEDGE_STATED"      # something the user SAID they know/do/see — an assertion, never demonstrated knowledge
RESEARCH_QUESTION_CAPTURED = "RESEARCH_QUESTION_CAPTURED"      # stage 1: the user's question, verbatim. Nothing is run or answered.
RESEARCH_DEFINITION_PROPOSED = "RESEARCH_DEFINITION_PROPOSED"  # stage 2: a PROPOSED definition for one part of the test (versioned, never overwritten)
SIGNAL_RESPONDED = "SIGNAL_RESPONDED"      # the user's answer to a fired signal: TAKEN (with its case) or SKIPPED

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
    # #tags are the user's own words: an engine alias INSIDE a tag (#sweep_trap contains "sweep") must not make the engine
    # appear to define the user's term. Aliases are matched in the untagged text; a tag that IS an alias is handled below.
    low = _TAG.sub(" ", text or "").lower()
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
        if engine:
            concept = _ALIAS_TO_CONCEPT.get(key.replace("_", " ")) or _ALIAS_TO_CONCEPT.get(key)
            if concept not in seen:
                seen.add(concept); rec.append({"concept": concept, "matched": key.replace("_", " ")})
        users.append({"raw": raw, "key": key, "status": "recognised_by_engine" if engine else "unrecognised_by_engine"})
    return {"recognised": rec, "user_terms": users}

# Parts of a question that must be defined before it can become a testable spec. "term <word>" gives a word the user used an
# operational definition. These are PROPOSALS: nothing here freezes anything (freezing is research_objects, Phase F).
DEFINITION_FIELDS = ("claim", "outcome", "population", "baseline", "null_model")
KNOWN_NULL_MODELS = ("block_resampling", "component_ablation", "direction_randomization", "entry_timing",
                     "opportunity_selection", "session_assignment")      # a test pins this to research_objects.NULL_MODELS
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
    def log_trade(self, pair, direction, entry, sl, tp, taken_at, now=None, signal_event_id=None):
        direction = (direction or "").upper()
        if direction not in ("BUY", "SELL"):
            raise LedgerError("direction must be BUY or SELL")
        for name, px in (("entry", entry), ("stop", sl), ("target", tp)):
            if not isinstance(px, (int, float)) or isinstance(px, bool) or not math.isfinite(px) or px <= 0:
                raise LedgerError(f"{name} must be a positive price")
        risk = (entry - sl) if direction == "BUY" else (sl - entry)
        reward = (tp - entry) if direction == "BUY" else (entry - tp)
        if risk <= 0 or reward <= 0:
            raise LedgerError("stop/target on the wrong side of entry")
        n = len(of_type_count(self._records(), TRADE_LOGGED)) + 1
        case_id = f"CASE-{n:04d}"
        payload = {"case_id": case_id, "pair": pair, "direction": direction,
                   "entry": entry, "sl": sl, "tp": tp,
                   "planned_r": round(reward / risk, 3)}
        if signal_event_id is not None:         # lets an interrupted /taken be completed instead of repeated
            payload["signal_event_id"] = signal_event_id
        self._append(TRADE_LOGGED, payload, ts=taken_at, now=now)
        return case_id

    def case_for_signal(self, signal_event_id):
        """The case already logged from this signal, or None (used to make /taken safe to retry)."""
        found = self._find(TRADE_LOGGED, "signal_event_id", signal_event_id)
        return found[0]["payload"]["case_id"] if found else None

    def has_bot_plan(self, case_id):
        return bool(self._find(BOT_PLAN_ATTACHED, "case_id", case_id))

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

    # ------------------------------------------------------------ signal responses
    SIGNAL_RESPONSES = ("TAKEN", "SKIPPED")

    def signal_responses(self, as_of=None):
        """{signal_event_id: record} — the user's answer to each fired signal. First answer wins; a second is refused."""
        recs = self._records()
        if as_of is not None:
            recs = ledger.as_of(recs, as_of)
        return {r["payload"]["signal_event_id"]: r for r in recs if r["event_type"] == SIGNAL_RESPONDED}

    def respond_to_signal(self, signal_event_id, response, case_id=None, note="", signal_fired_at=None, now=None):
        """Records that the user took or skipped a fired signal. The signal itself is the engine's record and is
        never touched; this only stores what the USER did about it. TAKEN must point at a real case."""
        if response not in self.SIGNAL_RESPONSES:
            raise LedgerError(f"response must be one of {self.SIGNAL_RESPONSES}")
        if signal_event_id in self.signal_responses():
            raise LedgerError("that signal already has your answer; append a correction instead")
        if response == "TAKEN":
            self._trade(case_id)
        elif case_id is not None:
            raise LedgerError("a skipped signal has no case")
        return self._append(SIGNAL_RESPONDED, {"signal_event_id": signal_event_id, "response": response,
                                               "case_id": case_id, "note": note,
                                               "signal_fired_at": signal_fired_at}, now=now)

    @staticmethod
    def terms_in(text):
        """Which words in `text` the ENGINE has a definition for (and the user's own #tags). Pure; lets BUS use the catalog's
        vocabulary through the object it is handed, without importing this module."""
        return extract_terms(text)

    # ------------------------------------------------------------ research questions (stages 1 and 2 only)
    def capture_question(self, text, source="telegram", now=None):
        """Stage 1. Stores the question verbatim with its time and source and gives it an id. Does NOT interpret, answer,
        run or freeze anything. The same words captured again return the existing question (no duplicate).
        Returns (question_id, is_new)."""
        text = (text or "").strip()
        if not text:
            raise LedgerError("nothing to capture")
        key = re.sub(r"\s+", " ", text).lower()
        for r in self._records():
            if r["event_type"] == RESEARCH_QUESTION_CAPTURED and re.sub(r"\s+", " ", r["payload"]["text"]).lower() == key:
                return r["payload"]["question_id"], False
        n = len(of_type_count(self._records(), RESEARCH_QUESTION_CAPTURED)) + 1
        qid = f"Q-{n:04d}"
        self._append(RESEARCH_QUESTION_CAPTURED, {"question_id": qid, "text": text, "source": source}, now=now)
        return qid, True

    def _question(self, qid):
        f = self._find(RESEARCH_QUESTION_CAPTURED, "question_id", qid)
        if not f:
            raise LedgerError(f"unknown question {qid}")
        return f[0]

    def propose_definition(self, qid, field, text, now=None):
        """Stage 2. A PROPOSED definition for one part of the test, kept beside the original wording. Each new text for a
        field is a new version; earlier ones stay. Returns (record_or_None, version); None = the same text was already proposed."""
        self._question(qid)
        text = (text or "").strip()
        if not text:
            raise LedgerError("a definition needs words")
        base = field.split(" ", 1)[0]
        if base == "term":
            word = field[4:].strip()
            if not word:
                raise LedgerError("say which word: term failed breakdown: <your definition>")
            field = "term " + re.sub(r"\s+", " ", word.lower())
        elif field not in DEFINITION_FIELDS:
            raise LedgerError(f"field must be one of {', '.join(DEFINITION_FIELDS)} or 'term <word>'")
        if field == "null_model" and text not in KNOWN_NULL_MODELS:
            raise LedgerError(f"null model must be one of {', '.join(KNOWN_NULL_MODELS)}")
        prior = [r for r in self._records() if r["event_type"] == RESEARCH_DEFINITION_PROPOSED
                 and r["payload"]["question_id"] == qid and r["payload"]["field"] == field]
        if prior and prior[-1]["payload"]["text"] == text:
            return None, prior[-1]["payload"]["version"]
        version = len(prior) + 1
        rec = self._append(RESEARCH_DEFINITION_PROPOSED, {"question_id": qid, "field": field, "text": text, "version": version}, now=now)
        return rec, version

    def question_view(self, qid, as_of=None):
        """Original wording + the latest PROPOSED definition per field (with how many versions exist) + what is missing.
        `stage` only ever says how far DEFINITION has got; it never claims a test exists, is frozen, or has a result."""
        recs = self._records()
        if as_of is not None:
            recs = ledger.as_of(recs, as_of)
        q = [r for r in recs if r["event_type"] == RESEARCH_QUESTION_CAPTURED and r["payload"]["question_id"] == qid]
        if not q:
            raise LedgerError(f"unknown question {qid}")
        defs = {}
        for r in recs:
            if r["event_type"] == RESEARCH_DEFINITION_PROPOSED and r["payload"]["question_id"] == qid:
                d = defs.setdefault(r["payload"]["field"], {"latest": None, "versions": 0})
                d["latest"], d["versions"] = r["payload"]["text"], r["payload"]["version"]
        missing = [f for f in DEFINITION_FIELDS if f not in defs]
        terms = {k[5:]: v for k, v in defs.items() if k.startswith("term ")}
        stage = "CAPTURED" if not defs else ("READY_TO_FORMALISE" if not missing else "DEFINING")
        return {"question_id": qid, "text": q[0]["payload"]["text"], "captured_at": q[0]["recorded_at"],
                "source": q[0]["payload"].get("source"), "definitions": {f: defs[f] for f in DEFINITION_FIELDS if f in defs},
                "terms": terms, "missing": missing, "stage": stage}

    def questions(self, as_of=None):
        recs = self._records()
        if as_of is not None:
            recs = ledger.as_of(recs, as_of)
        return [r["payload"]["question_id"] for r in recs if r["event_type"] == RESEARCH_QUESTION_CAPTURED]

    # ------------------------------------------------------------ investigation workflow
    # The SAME questions for every case, whatever the result: a win is evidence of an outcome, not of correct
    # reasoning, and a loss is not evidence the bot was wrong. These are prompts, never verdicts.
    INVESTIGATION_QUESTIONS = (
        ("saw", "What did you see when you decided? (your own words: /reason N ...)"),
        ("bot_knew", "What did the bot have at that time? (its plan as stored, never today's market)"),
        ("representable", "Was your concept something the engine can even measure? (recognised terms vs your own)"),
        ("cause", "Did the setup you claimed cause the outcome, or did the market do something else first?"),
        ("frequency", "How often does this kind of divergence happen? (counted over cases, never guessed from one)"),
    )

    def investigation_state(self, case_id):
        """Everything the worksheet may say, read back from the ledger. No judgement is made here."""
        case = self.get_case(case_id)
        if case is None:
            raise LedgerError(f"unknown case {case_id}")
        recs = self._records()
        opened = [r for r in recs if r["event_type"] == INVESTIGATION_OPENED and r["payload"].get("case_id") == case_id]
        terms = [r["payload"] for r in recs if r["event_type"] == TERMS_OBSERVED and r["payload"].get("case_id") == case_id]
        unk = sorted({t["raw"] for p in terms for t in p.get("user_terms", []) if t.get("status") == "unrecognised_by_engine"})
        rec = sorted({x["concept"] for p in terms for x in p.get("recognised", [])})
        pending = [r["payload"]["question"] for r in opened if r["payload"].get("question")]
        return {
            "case": case, "has_outcome": case["outcome"] is not None,
            "outcome": (case["outcome"] or {}).get("payload"),
            "reasoning_n": len(case["user_reasoning"]),
            "reasoning_times": [(r["recorded_at"], bool(r["payload"].get("written_after_outcome"))) for r in case["user_reasoning"]],
            "statements_n": len(self.statements(case_id)),
            "reasoning_after_outcome_n": sum(1 for r in case["user_reasoning"] if r["payload"].get("written_after_outcome")),
            "bot_plan": (case["bot_plan"] or {}).get("payload"),
            "notes": [r["payload"]["text"] for r in case["investigation"]],
            "pending_questions": pending, "opened": bool(opened),
            "statuses": [r["payload"].get("status") for r in opened],
            "recognised_terms": rec, "own_terms": unk,
        }

    def open_investigation(self, case_id, question=None, now=None):
        """Starts (or continues) an investigation. Before an outcome exists nothing can be concluded, so the status is
        PENDING_OUTCOME and any question is stored as pending — never answered. Idempotent: the same status and the same
        question text are not written twice. Returns (record_or_None, status)."""
        st = self.investigation_state(case_id)
        status = "OPEN" if st["has_outcome"] else "PENDING_OUTCOME"
        question = (question or "").strip() or None
        if st["opened"] and st["statuses"][-1] == status and (question is None or question in st["pending_questions"]):
            return None, status
        rec = self._append(INVESTIGATION_OPENED, {"case_id": case_id, "status": status, "question": question}, now=now)
        return rec, status

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
                a["uses"] += 1; a["variants"].add(t["raw"])
                if r["payload"].get("case_id") is not None:       # a general statement counts as a use but belongs to no case
                    a["cases"].add(r["payload"]["case_id"])
                if len(a["examples"]) < 3:
                    a["examples"].append(r["payload"].get("reasoning_event_id") or r["payload"].get("statement_event_id"))
        out = [dict(a, cases=sorted(a["cases"]), variants=sorted(a["variants"])) for a in agg.values()]
        return sorted(out, key=lambda a: (-a["uses"], a["term"]))

    # ------------------------------------------------------------ knowledge (provenance)
    def record_knowledge(self, concept, evidence_event_id, note="", now=None):
        if not any(r["event_id"] == evidence_event_id for r in self._records()):
            raise LedgerError("knowledge requires evidence: unknown evidence_event_id")
        return self._append(KNOWLEDGE_DEMONSTRATED, {
            "concept": concept, "evidence_event_id": evidence_event_id, "note": note,
        }, now=now)

    def state_knowledge(self, text, case_id=None, ts=None, now=None):
        """Records what the user SAYS, verbatim, with when and (optionally) which case it was about. It is an assertion:
        it never becomes KNOWLEDGE_DEMONSTRATED (that needs evidence), and nothing here interprets or edits it — a later
        interpretation would be a separate record. Terms are extracted the same way as for reasoning."""
        text = (text or "").strip()
        if not text:
            raise LedgerError("nothing to record")
        if case_id is not None:
            self._trade(case_id)
        rec = self._append(KNOWLEDGE_STATED, {"text": text, "case_id": case_id}, ts=ts, now=now)
        found = extract_terms(text)
        if found["recognised"] or found["user_terms"]:
            self._append(TERMS_OBSERVED, {"case_id": case_id, "statement_event_id": rec["event_id"], **found}, now=now)
        return rec

    def statements(self, case_id=None, as_of=None):
        recs = self._records()
        if as_of is not None:
            recs = ledger.as_of(recs, as_of)
        return [r for r in recs if r["event_type"] == KNOWLEDGE_STATED
                and (case_id is None or r["payload"].get("case_id") == case_id)]

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
