"""
bus_contract.py — the BUS contract. No wording, no Telegram, no market logic.
Imports ledger only. This is the rulebook the future wording layer (bus.py)
must obey and be tested against; it exists BEFORE any sentence is written.

THE ONE IDEA: the interface may rearrange knowledge but cannot manufacture it.
That is enforced at the OBJECT level, not by convention:

  * A `Claim` cannot be constructed without a registered subject, a registered
    authority that is allowed to assert that kind of thing, a source reference,
    and observed/recorded times. There is no "bare string" path to a claim.
  * `claim_id` is a hash of content, not a field — it can't be forged, and the
    same fact seen twice has the same id (that is what makes "already told you"
    and number-consistency checkable).
  * Kinds cannot be silently blurred: only an authority registered for FACT may
    produce a FACT. The interpretation layer cannot emit a fact; an
    interpretation can't be passed off as one.
  * An `Answer` cannot be constructed inconsistent with its parts (status is
    re-derived and checked) and cannot contain a claim from after its `as_of`.

THE FOUR PIECES
  1. Claim object        (Claim)
  2. Claim rules         (registries + __post_init__ validation + audit_text)
  3. Intent contract     (INTENTS + resolve) — what the user is asking for maps
                         to a fixed list of required subjects; BUS does not
                         compute a new answer because a question was phrased
                         differently.
  4. Silence contract    (decide_speech) — no supported claim -> no statement;
                         unmapped / missing / stale / conflicting evidence is
                         REPORTED as such, never filled with plausible language.

WHAT THIS CANNOT DO (be honest about it)
  * `audit_text` is a lint over finished wording, not a proof of truth: it
    catches numbers and key terms with no supporting claim. A cleverly vague
    sentence passes. It is the floor, not the ceiling.
  * The contract is only as good as the engine fields behind it. Subjects the
    engine does not compute are registered as UNMAPPED, and coverage_report()
    says so. Fixing those is ENGINE work; BUS must say "the engine doesn't
    provide that", not improvise.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional, Tuple

import ledger
from ledger import LedgerError

FACT, INTERPRETATION, EXPECTATION = "FACT", "INTERPRETATION", "EXPECTATION"
KINDS = (FACT, INTERPRETATION, EXPECTATION)

# Same grammar as user_catalog thread conditions (a test asserts they match).
#   price_close_*  a candle CLOSES beyond `level`
#   price_trade_*  price TRADES to/through `level` (a wick counts) — what the engine's
#                  leg-survival rule really tests (check_leg_anchor_survival uses High/Low)
#   price_in_zone  price trades into [low, high]
#   event_code     the engine records an event with this code
#   ema_close_*    a candle CLOSES above/below its EMA(period) on `timeframe`
#   ema_touch      a candle's range contains the EMA(period) as it stood BEFORE that candle (no lookahead)
# Price conditions may carry an optional `timeframe` (5m default); EMA conditions require `period` and `timeframe`.
CONDITION_TYPES = ("price_close_above", "price_close_below", "price_trade_above",
                   "price_trade_below", "price_in_zone", "event_code",
                   "ema_close_above", "ema_close_below", "ema_touch", "structure_flip")
TIMEFRAMES = ("5m", "15m", "1h")


def validate_condition(c):
    """Shared by Claim and (via a test) the catalog's thread grammar."""
    if not isinstance(c, dict) or c.get("type") not in CONDITION_TYPES:
        raise ClaimError(f"condition.type must be one of {CONDITION_TYPES}")
    _plain_json(c)
    t = c["type"]
    if t == "structure_flip" and c.get("what") not in ("choch", "bos"):
        raise ClaimError("structure_flip needs what = choch or bos")
    if "timeframe" in c and c["timeframe"] not in TIMEFRAMES:
        raise ClaimError(f"timeframe must be one of {TIMEFRAMES}")
    if t.startswith("price_close") or t.startswith("price_trade"):
        if isinstance(c.get("level"), bool) or not isinstance(c.get("level"), (int, float)):
            raise ClaimError("price condition needs a numeric level")
    elif t.startswith("ema_"):
        p = c.get("period")
        if isinstance(p, bool) or not isinstance(p, int) or not (2 <= p <= 500):
            raise ClaimError("ema condition needs an integer period between 2 and 500")
        if c.get("timeframe") not in TIMEFRAMES:
            raise ClaimError(f"ema condition needs a timeframe in {TIMEFRAMES}")
    elif t == "price_in_zone":
        lo, hi = c.get("low"), c.get("high")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (lo, hi)) or lo > hi:
            raise ClaimError("price_in_zone needs numeric low <= high")
    elif t == "event_code" and not c.get("code"):
        raise ClaimError("event_code condition needs a code")

BUS_STATEMENT = "BUS_STATEMENT"          # ledger event type for what BUS said
CLOCK_SKEW = timedelta(seconds=60)


class ClaimError(LedgerError):
    pass


# ======================================================================
# Authorities: WHO may assert WHICH kind. This is the anti-blur rule.
# ======================================================================
AUTHORITIES = {
    "engine.state":         (FACT,),
    "engine.leg_owner":     (FACT,),
    "engine.signal_log":    (FACT,),
    "engine.event_log":     (FACT,),
    "engine.leg_obs_log":   (FACT,),
    "engine.thesis":        (INTERPRETATION,),
    "engine.mil":           (INTERPRETATION, EXPECTATION),
    "engine.market_intent": (INTERPRETATION,),
    "catalog.user_ledger":  (FACT,),           # facts about what the HUMAN said/did
    "bus.statement_log":    (FACT,),           # what BUS itself previously said
    "ledger.verify":        (FACT,),
    "inspector.findings":   (FACT,),           # "the independent inspector observed X" — an observation, never a market fact
}


# ======================================================================
# Subjects: the closed vocabulary. A word without a registered subject
# cannot become a claim — no ad hoc vocabulary creeping back in.
# source: where the value really lives, or None == UNMAPPED (engine work).
# ======================================================================
@dataclass(frozen=True)
class Spec:
    kinds: Tuple[str, ...]
    authorities: Tuple[str, ...]
    source: Optional[str]          # None => the engine does not provide this
    note: str = ""


_S = []   # (compiled regex, canonical name, Spec)


def _reg(name, spec, pattern=None):
    _S.append((re.compile(pattern or "^" + re.escape(name) + "$"), name, spec))


# -- engine facts / interpretations that EXIST today (paths confirmed in code)
_reg("market.data_timestamp", Spec((FACT,), ("engine.state",), "state.market_data_timestamp (ISO; set to the scan's now_utc after valid data passed all gates)"))
# CORRECTED 2026-10-01: the first draft said state.macro_bias. There is NO top-level
# state["macro_bias"]; the persisted copy is state["structure_digest"]["regime"].
_reg("htf.bias", Spec((FACT,), ("engine.state",),
     "state.structure_digest.regime.macro_bias in {BULLISH,BEARISH,CONSOLIDATION} + .macro_bias_stale"))
_reg("leg.owner", Spec((FACT,), ("engine.leg_owner",), "state.leg_owner {leg_id,tier,status,upgraded,claimed_at}"))
_reg("thesis.state", Spec((INTERPRETATION,), ("engine.thesis",), "state.market_thesis_current_state"))
_reg("thesis.status", Spec((INTERPRETATION,), ("engine.mil",), "state.mil_understanding.thesis_status"))
_reg("thesis.primary", Spec((INTERPRETATION,), ("engine.mil",),
     "state.mil_understanding.primary_thesis {direction,state,narrative,expected_next_event,confidence}; basis = .supporting_evidence; direction None = UNSET"))
_reg("thesis.counter", Spec((INTERPRETATION,), ("engine.mil",), "state.mil_understanding.counter_thesis"))
_reg("thesis.conflicting_evidence", Spec((INTERPRETATION,), ("engine.mil",), "state.mil_understanding.conflicting_evidence"))
_reg("thesis.failure_condition", Spec((EXPECTATION,), ("engine.mil",),
     "state.mil_understanding.failure_condition — ORIGIN mechanism: origin_direction 'above'/'below' = price TRADING through origin_level (check_leg_anchor_survival tests High/Low, not closes) -> price_trade_above/below",
     "0.0 is the UNSET placeholder. Until 2026-10-01 nothing ever replaced it after bootstrap (every real file carried 0.0); mil.derive_failure_condition now fills it from the leg. retrace_level is carried as a number; the second mechanism's trigger is not turned into a second condition"))
_reg("thesis.invalidation", Spec((INTERPRETATION,), ("engine.thesis",),
     "state.market_thesis_invalidation (engine text) + state.macro_leg_origin/macro_leg_direction"))
_reg("thesis.phase", Spec((INTERPRETATION,), ("engine.thesis",), "state.market_phase + state.market_phase_age_bars"))
_reg("thesis.trend_health", Spec((INTERPRETATION,), ("engine.thesis",), "state.market_thesis_trend_health (+ market_thesis_aging_reason)"))
_reg("thesis.failure_risk", Spec((INTERPRETATION,), ("engine.thesis",), "state.market_thesis_failure_risk (+ _reasons_prose)"))
_reg("thesis.expected_next_event", Spec((INTERPRETATION,), ("engine.thesis",), "state.market_thesis_expected_next_event"))
_reg("volatility.state", Spec((INTERPRETATION,), ("engine.thesis",), "state.market_thesis_volatility_hint"))
_reg("leg.age_status", Spec((INTERPRETATION,), ("engine.thesis",),
     "state.market_leg_age_status.status (explicit maturity: DEVELOPING/MATURE/EXTENDED/MATURE_AND_EXTENDED/AGING_UNSPECIFIED; absent when the phase is not expansion/exhaustion) + market_thesis_trend_health + market_thesis_aging_reason + market_thesis_break_count"))
_reg("leg.macro", Spec((FACT,), ("engine.state",), "state.macro_leg_{direction,origin,extreme,origin_time}"))
_reg("campaign.state", Spec((FACT,), ("engine.state",), "state.campaign_{direction,origin,origin_time,initial_impulse_pips,current_extreme,continuation_count}"))
_reg("watch.zones", Spec((INTERPRETATION,), ("engine.market_intent",), "state.market_intent_watch_codes [{code,role,zone_low,zone_high,sentence}]"))
_reg("cautions", Spec((INTERPRETATION,), ("engine.market_intent",), "state.market_intent_caution_codes [{code,role,sentence}]"))
_reg("signal.undelivered", Spec((FACT,), ("engine.signal_log",), "signal_event_log.jsonl"))
_reg("event.recorded", Spec((FACT,), ("engine.event_log",), "market_event_log.jsonl (permanent; pending queue is transient)"))
_reg("leg.outcome", Spec((FACT,), ("engine.leg_obs_log",), "leg_obs_log.jsonl (fate per leg_obs_id)"))

# -- UNMAPPED: wanted by the explanation contract, NOT computed by the engine today
for _n, _note in (
    ("thesis.expectations", "MIL's expectations exist only as prose strings with no machine-checkable condition, so they cannot be EXPECTATION claims; the engine would have to attach a condition to each"),
    ("momentum.state", "no momentum state is exposed (market_thesis_volatility_hint is VOLATILITY, a different thing, and is mapped separately as volatility.state)"),
    ("story.controller", "who controlled the move is not computed"),
    ("story.counter_attempt", "where the other side fought back, and whether it worked, is not computed"),
):
    _reg(_n, Spec((INTERPRETATION, EXPECTATION), ("engine.mil", "engine.thesis"), None, _note))

# -- catalog / system (exist; built this rebuild)
_ID = r"[A-Za-z0-9-]+"
for _sec in ("trade", "user_reasoning", "bot_plan", "outcome", "investigation"):
    _reg(f"case.<id>.{_sec}", Spec((FACT,), ("catalog.user_ledger",),
         f"user_ledger.jsonl via Catalog.get_case()['{_sec}']"), pattern=rf"^case\.{_ID}\.{_sec}$")
_reg("thread.<id>.status", Spec((FACT,), ("catalog.user_ledger",), "user_ledger.jsonl THREAD_OPENED/RESOLVED"),
     pattern=rf"^thread\.{_ID}\.status$")
_reg("alerts.open", Spec((FACT,), ("catalog.user_ledger",), "user_ledger.jsonl THREAD_OPENED (topic 'price alert') not resolved/cancelled",
     "the user's open price alerts; checked only on scans (see BLUEPRINT/HANDOFF: the live scanner runs 07:00-15:55 UTC Mon-Fri)"))
_reg("user.terms", Spec((FACT,), ("catalog.user_ledger",), "user_ledger.jsonl TERMS_OBSERVED / USER_TERM_ALIAS",
     "how often the user used each of their OWN terms; it counts words, it never says what a term means"))
_reg("research.cases", Spec((FACT,), ("catalog.user_ledger",), "user_ledger.jsonl HYPOTHESIS_DRAFTED / EXPERIMENT_* / RESEARCH_RESULT_RECORDED",
     "the research index: what was hypothesised, what was frozen, and what each experiment's status is; statuses describe an experiment, never the strategy"))
_reg("user.knows.<concept>", Spec((FACT,), ("catalog.user_ledger",), "user_ledger.jsonl KNOWLEDGE_DEMONSTRATED (needs evidence)"),
     pattern=r"^user\.knows\.[A-Za-z0-9_-]+$")
_reg("system.integrity", Spec((FACT,), ("inspector.findings",),
     "integrity_ledger.jsonl (open INTEGRITY_FINDING events not yet INTEGRITY_RESOLVED)",
     "what the independent inspector currently has open; it observes the bot, it never says what the market is doing"))
_reg("system.ledger_integrity", Spec((FACT,), ("ledger.verify",), "ledger.verify(read(user_ledger.jsonl))"))
_reg("bus.statement.<subject>", Spec((FACT,), ("bus.statement_log",), "BUS_STATEMENT ledger events"),
     pattern=r"^bus\.statement\..+$")


def spec_for(subject):
    """Registry lookup; None if the subject is not registered."""
    for rx, name, spec in _S:
        if rx.match(subject):
            return name, spec
    return None


# ======================================================================
# 1. THE CLAIM OBJECT
# ======================================================================
def _plain_json(x):
    try:
        json.dumps(x, sort_keys=True)
    except (TypeError, ValueError) as e:
        raise ClaimError(f"claim value/condition must be plain JSON: {e}")


@dataclass(frozen=True)
class Claim:
    kind: str
    subject: str
    value: Any
    authority: str
    ref: str                       # pointer to the persisted record this came from — REQUIRED
    observed_at: Any               # when it became true (event time)
    recorded_at: Any               # when the system learned it (knowledge time)
    scan_id: Optional[int] = None
    basis: Tuple[str, ...] = ()    # supporting evidence the AUTHORITY provided (for "why")
    condition: Optional[dict] = None   # EXPECTATION only: machine-checkable
    unit: Optional[str] = None

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ClaimError(f"kind must be one of {KINDS}")
        found = spec_for(self.subject)
        if not found:
            raise ClaimError(f"unregistered subject {self.subject!r} — add it to the registry first")
        _name, spec = found
        if self.authority not in AUTHORITIES:
            raise ClaimError(f"unregistered authority {self.authority!r}")
        if self.kind not in AUTHORITIES[self.authority]:
            raise ClaimError(f"authority {self.authority!r} may not assert a {self.kind} "
                             f"(allowed: {AUTHORITIES[self.authority]}) — kinds are never blurred")
        if self.authority not in spec.authorities or self.kind not in spec.kinds:
            raise ClaimError(f"subject {self.subject!r} accepts {spec.kinds} from {spec.authorities}, "
                             f"got {self.kind} from {self.authority}")
        if not isinstance(self.ref, str) or not self.ref.strip():
            raise ClaimError("a claim without a source ref is not a claim")
        _plain_json(self.value)
        object.__setattr__(self, "basis", tuple(str(b) for b in self.basis))
        o, r = ledger.to_utc(self.observed_at), ledger.to_utc(self.recorded_at)
        if o > r + CLOCK_SKEW:
            raise ClaimError("observed_at is after recorded_at: a claim cannot be known before it happened")
        object.__setattr__(self, "observed_at", o)
        object.__setattr__(self, "recorded_at", r)
        if self.kind == EXPECTATION:
            if not isinstance(self.condition, dict):
                raise ClaimError(f"an EXPECTATION needs a machine-checkable condition, type in {CONDITION_TYPES}")
            validate_condition(self.condition)
        elif self.condition is not None:
            raise ClaimError("only an EXPECTATION may carry a condition")

    @property
    def claim_id(self):
        body = {"kind": self.kind, "subject": self.subject, "value": self.value,
                "authority": self.authority, "ref": self.ref,
                "observed_at": ledger._iso(self.observed_at)}
        return "c_" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]

    def to_dict(self):
        return {"claim_id": self.claim_id, "kind": self.kind, "subject": self.subject,
                "value": self.value, "authority": self.authority, "ref": self.ref,
                "observed_at": ledger._iso(self.observed_at),
                "recorded_at": ledger._iso(self.recorded_at), "scan_id": self.scan_id,
                "basis": list(self.basis), "condition": self.condition, "unit": self.unit}


# ======================================================================
# GAPS — the explicit states that are NOT a value. A number or a boolean never
# carries "missing" by accident: a placeholder 0.0 is an UNSET gap, not a
# claim that a level is zero; an absent key is NOT_PROVIDED, not False.
#   UNSET         the authority supplied a PLACEHOLDER meaning "no value yet"
#   UNKNOWN       the authority itself reports that it does not know
#   NOT_PROVIDED  the field/record is simply absent from the source
#   STALE         the authority flags its own value as stale
# (KNOWN is just a Claim.)
# ======================================================================
GAP_STATES = ("UNSET", "UNKNOWN", "NOT_PROVIDED", "STALE")
_GAP_REASON = {"UNSET": "unset", "UNKNOWN": "unknown",
               "NOT_PROVIDED": "not_provided", "STALE": "flagged_stale"}


@dataclass(frozen=True)
class Gap:
    subject: str
    state: str
    authority: str
    ref: str
    observed_at: Any
    recorded_at: Any
    detail: str = ""

    def __post_init__(self):
        found = spec_for(self.subject)
        if not found:
            raise ClaimError(f"unregistered subject {self.subject!r}")
        if self.state not in GAP_STATES:
            raise ClaimError(f"gap state must be one of {GAP_STATES}")
        if self.authority not in AUTHORITIES or self.authority not in found[1].authorities:
            raise ClaimError(f"authority {self.authority!r} is not registered for {self.subject!r}")
        if not isinstance(self.ref, str) or not self.ref.strip():
            raise ClaimError("a gap without a source ref is not evidence of anything")
        o, r = ledger.to_utc(self.observed_at), ledger.to_utc(self.recorded_at)
        if o > r + CLOCK_SKEW:
            raise ClaimError("gap observed_at is after recorded_at")
        object.__setattr__(self, "observed_at", o)
        object.__setattr__(self, "recorded_at", r)


# ======================================================================
# 3. INTENT CONTRACT
# ======================================================================
@dataclass(frozen=True)
class Slot:
    subject: str                       # may contain {param}
    kinds: Tuple[str, ...] = KINDS
    required: bool = True
    max_age_s: Optional[int] = None    # older than this => stale => treated as missing
    mode: str = "current"              # "current" | "change"
    needs_basis: bool = False          # "why": a claim with no basis cannot explain itself


@dataclass(frozen=True)
class Intent:
    name: str
    slots: Tuple[Slot, ...]
    params: Tuple[str, ...] = ()
    doc: str = ""


INTENTS = {i.name: i for i in (
    Intent("current_thesis", (
        Slot("system.integrity", (FACT,), required=False),
        Slot("thesis.primary", (INTERPRETATION,)), Slot("thesis.status", (INTERPRETATION,)),
        Slot("thesis.failure_condition", (EXPECTATION,), required=False),
        Slot("thesis.counter", (INTERPRETATION,), required=False),
        Slot("htf.bias", (FACT,), required=False))),     # present so a thesis/bias disagreement can be noticed
    Intent("why", (Slot("{about}", needs_basis=True), Slot("htf.bias", (FACT,), required=False),
                   Slot("thesis.status", (INTERPRETATION,), required=False)), params=("about",),
           doc="explain a claim from the basis its authority supplied; no basis => cannot explain"),
    Intent("what_changed", (
        Slot("htf.bias", (FACT,), required=False, mode="change"),
        Slot("thesis.status", (INTERPRETATION,), required=False, mode="change"),
        Slot("thesis.primary", (INTERPRETATION,), required=False, mode="change"),
        Slot("leg.owner", (FACT,), required=False, mode="change"))),
    Intent("what_would_change_it", (
        Slot("thesis.failure_condition", (EXPECTATION,), required=False),
        Slot("thesis.invalidation", (INTERPRETATION,)),
        Slot("thesis.conflicting_evidence", (INTERPRETATION,), required=False))),
    Intent("trade_reconstruction", (
        Slot("case.{case_id}.trade", (FACT,)), Slot("case.{case_id}.bot_plan", (FACT,), required=False),
        Slot("case.{case_id}.user_reasoning", (FACT,), required=False),
        Slot("case.{case_id}.outcome", (FACT,), required=False),
        Slot("case.{case_id}.investigation", (FACT,), required=False)), params=("case_id",)),
    Intent("my_alerts", (Slot("alerts.open", (FACT,)),), doc="the user's open price alerts"),
    Intent("research_cases", (Slot("research.cases", (FACT,)),),
           doc="the research index; it reports experiments and their statuses and never concludes that a strategy works"),
    Intent("my_terms", (Slot("user.terms", (FACT,)),),
           doc="the user's own vocabulary and how often they used it; no definitions are ever supplied"),
    Intent("thread_status", (Slot("thread.{thread_id}.status", (FACT,)),), params=("thread_id",)),
    Intent("bot_vs_user", (
        Slot("case.{case_id}.bot_plan", (FACT,)), Slot("case.{case_id}.user_reasoning", (FACT,)),
        Slot("case.{case_id}.outcome", (FACT,), required=False)), params=("case_id",),
        doc="shows both views side by side; the COMPARISON is investigation, never BUS"),
    Intent("integrity", (
        Slot("system.ledger_integrity", (FACT,)),
        Slot("system.integrity", (FACT,), required=False),
        Slot("market.data_timestamp", (FACT,), max_age_s=1800))),
    Intent("integrity_alert", (Slot("system.integrity", (FACT,)),),
           doc="proactive: announced only when the set of open findings changes"),
    Intent("signal_alert", (Slot("signal.undelivered", (FACT,)),),
           doc="proactive, behind the BUS_SIGNALS flag: one fired signal, stated from its own log record and nothing else"),
    Intent("market_state", (
        Slot("system.integrity", (FACT,), required=False),
        Slot("htf.bias", (FACT,)), Slot("thesis.phase", (INTERPRETATION,)),
        Slot("thesis.state", (INTERPRETATION,), required=False),
        Slot("leg.age_status", (INTERPRETATION,), required=False),
        Slot("thesis.failure_risk", (INTERPRETATION,), required=False),
        Slot("volatility.state", (INTERPRETATION,), required=False),
        Slot("leg.macro", (FACT,), required=False),
        Slot("thesis.status", (INTERPRETATION,), required=False),
        Slot("thesis.primary", (INTERPRETATION,), required=False),
        Slot("cautions", (INTERPRETATION,), required=False),
        Slot("leg.owner", (FACT,), required=False),
        Slot("market.data_timestamp", (FACT,), required=False, max_age_s=1200)),
        doc="the everyday 'what's happening' read; data older than 20 minutes is reported stale, not shown as current"),
    Intent("watching", (
        Slot("watch.zones", (INTERPRETATION,)), Slot("thesis.expected_next_event", (INTERPRETATION,), required=False),
        Slot("thesis.invalidation", (INTERPRETATION,), required=False),
        Slot("thesis.failure_condition", (EXPECTATION,), required=False))),
    Intent("current_market_story", (
        Slot("system.integrity", (FACT,), required=False),
        Slot("htf.bias", (FACT,)), Slot("thesis.phase", (INTERPRETATION,)),
        Slot("thesis.status", (INTERPRETATION,)), Slot("thesis.primary", (INTERPRETATION,)),
        Slot("leg.age_status", (INTERPRETATION,), required=False),
        Slot("thesis.invalidation", (INTERPRETATION,), required=False),
        Slot("story.controller", required=False), Slot("story.counter_attempt", required=False),
        Slot("momentum.state", required=False)),
        doc="the optional slots the engine does not compute are REPORTED missing, never filled"),
)}


# CURRENT-STATE FRESHNESS (found by the runner test: a state.json three hours old was being shown
# as "what's happening now"). These subjects come from the overwritten-every-scan state.json, so
# once the snapshot is older than a few scans it is reported stale instead of presented as current.
# Scans run every 5 minutes; 20 minutes tolerates a missed run or two. History-mode slots
# (what_changed) and ledger/log-backed subjects are exempt: old is legitimate for them.
STATE_FRESHNESS_S = 1200
STATE_SUBJECTS = {"htf.bias", "leg.owner", "thesis.state", "thesis.primary", "thesis.counter",
                  "thesis.conflicting_evidence", "thesis.failure_condition", "thesis.invalidation",
                  "thesis.phase", "thesis.trend_health", "thesis.failure_risk", "thesis.expected_next_event",
                  "volatility.state", "leg.age_status", "leg.macro", "campaign.state", "watch.zones", "cautions"}


def _apply_state_freshness():
    from dataclasses import replace
    for name, intent in list(INTENTS.items()):
        slots = tuple(replace(sl, max_age_s=STATE_FRESHNESS_S)
                      if (sl.mode == "current" and sl.max_age_s is None and sl.subject in STATE_SUBJECTS) else sl
                      for sl in intent.slots)
        INTENTS[name] = replace(intent, slots=slots)


_apply_state_freshness()


@dataclass(frozen=True)
class Missing:
    subject: str
    reason: str          # no_evidence | stale | wrong_kind | no_basis | engine_does_not_provide | unknown_referent
                         # | unset | unknown | not_provided | flagged_stale   (the last four come from a Gap)
    required: bool
    detail: str = ""


@dataclass(frozen=True)
class Conflict:
    subject: str
    claim_ids: Tuple[str, ...]


@dataclass(frozen=True)
class Item:
    label: str           # CURRENT | PREVIOUS | UNCHANGED
    claim: Claim
    since: Any = None    # CURRENT/UNCHANGED in "change" mode: when this value was FIRST seen in the
                         # unbroken run that ends now (so "changed at 10:05", not "as of the last refresh")


def derive_status(items, missing, conflicts):
    if not items:
        return "NO_EVIDENCE"
    if conflicts or any(m.required for m in missing):
        return "PARTIAL"
    return "ANSWER"


@dataclass(frozen=True)
class Answer:
    intent: str
    params: dict
    as_of: Any
    items: Tuple[Item, ...]
    missing: Tuple[Missing, ...]
    conflicts: Tuple[Conflict, ...]
    status: str
    notes: Tuple[str, ...] = ()

    def __post_init__(self):
        T = ledger.to_utc(self.as_of)
        object.__setattr__(self, "as_of", T)
        for it in self.items:
            if it.claim.observed_at > T or it.claim.recorded_at > T:
                raise ClaimError(f"hindsight: claim {it.claim.claim_id} is later than the answer's as_of")
        if self.status != derive_status(self.items, self.missing, self.conflicts):
            raise ClaimError("answer status is inconsistent with its contents")

    @property
    def claims(self):
        return tuple(i.claim for i in self.items)


# ======================================================================
# CONSISTENCY NOTES — facts that only exist BETWEEN claims. Derived in the
# contract (not left to wording) so BUS cannot present a tidy story over a
# disagreement. audit_text() requires the wording to acknowledge each one.
# ======================================================================
NOTE_KEYWORDS = {
    "INTEGRITY_ALERT": ("integrity", "inspector", "flagged by", "alarm"),
    "DIRECTION_DISAGREEMENT": ("disagree", "differ", "mismatch", "conflict", "mid-transition",
                               "transitioning", "doesn't match", "not aligned", "out of step"),
}


def _dir_of(value):
    if isinstance(value, dict):
        value = value.get("direction")
    return value if value in ("BULLISH", "BEARISH") else None


def consistency_notes(items):
    cur = {i.claim.subject: i.claim for i in items if i.label in ("CURRENT", "UNCHANGED")}
    notes = []
    bias, prim = cur.get("htf.bias"), cur.get("thesis.primary")
    if bias and prim and _dir_of(bias.value) and _dir_of(prim.value) and _dir_of(bias.value) != _dir_of(prim.value):
        notes.append("DIRECTION_DISAGREEMENT")
    integ = cur.get("system.integrity")
    if integ and any(f.get("severity") == "HIGH" for f in (integ.value or {}).get("open", [])):
        notes.append("INTEGRITY_ALERT")
    return tuple(notes)


def _bind(subject_tpl, params):
    try:
        return subject_tpl.format(**params), None
    except KeyError as e:
        return None, f"missing parameter {e.args[0]!r}"


def resolve(intent_name, pool, as_of, params=None, gaps=()):
    """Build an Answer from a pool of Claims, as the system could have known at
    `as_of`. Never computes, infers or fills: every slot is either a claim that
    exists, or an explicit Missing/Conflict."""
    if intent_name not in INTENTS:
        raise ClaimError(f"unknown intent {intent_name!r}")
    intent, params, T = INTENTS[intent_name], dict(params or {}), ledger.to_utc(as_of)
    visible = [c for c in pool if c.observed_at <= T and c.recorded_at <= T]   # no hindsight
    vgaps = [g for g in gaps if g.observed_at <= T and g.recorded_at <= T]
    items, missing, conflicts = [], [], []

    for slot in intent.slots:
        subject, err = _bind(slot.subject, params)
        if err or not spec_for(subject):
            missing.append(Missing(slot.subject, "unknown_referent", slot.required,
                                   err or f"{subject!r} is not a registered subject"))
            continue
        _n, spec = spec_for(subject)
        if spec.source is None:
            missing.append(Missing(subject, "engine_does_not_provide", slot.required, spec.note))
            continue
        cands = sorted((c for c in visible if c.subject == subject),
                       key=lambda c: (c.observed_at, c.recorded_at))
        sgaps = sorted((g for g in vgaps if g.subject == subject),
                       key=lambda g: (g.observed_at, g.recorded_at))
        # A gap at least as new as the newest claim SUPERSEDES it: if the
        # authority now says "unset"/"stale"/"unknown", the older value is no
        # longer what the system holds. A newer claim supersedes an older gap.
        if sgaps and (not cands or sgaps[-1].observed_at >= cands[-1].observed_at):
            g = sgaps[-1]
            missing.append(Missing(subject, _GAP_REASON[g.state], slot.required, g.detail))
            continue
        if not cands:
            missing.append(Missing(subject, "no_evidence", slot.required))
            continue
        latest_t = cands[-1].observed_at
        top = [c for c in cands if c.observed_at == latest_t]
        if len({json.dumps(c.value, sort_keys=True) for c in top}) > 1:
            conflicts.append(Conflict(subject, tuple(sorted(c.claim_id for c in top))))
            continue                                   # neither is asserted
        cur = top[-1]
        if cur.kind not in slot.kinds:
            missing.append(Missing(subject, "wrong_kind", slot.required,
                                   f"have {cur.kind}, slot needs {slot.kinds}"))
            continue
        if slot.max_age_s is not None and (T - cur.observed_at).total_seconds() > slot.max_age_s:
            missing.append(Missing(subject, "stale", slot.required,
                                   f"observed {int((T - cur.observed_at).total_seconds())}s before as_of"))
            continue
        if slot.needs_basis and not cur.basis:
            missing.append(Missing(subject, "no_basis", slot.required,
                                   "the authority supplied no supporting evidence for this claim"))
            continue
        if slot.mode == "change":
            earlier = [c for c in cands if json.dumps(c.value, sort_keys=True)
                       != json.dumps(cur.value, sort_keys=True)]
            cur_key = json.dumps(cur.value, sort_keys=True)
            run = []                                   # unbroken run of the current value, newest backwards
            for c in reversed(cands):
                if json.dumps(c.value, sort_keys=True) != cur_key:
                    break
                run.append(c)
            since = run[-1].observed_at if run else cur.observed_at
            if earlier:
                items += [Item("PREVIOUS", earlier[-1]), Item("CURRENT", cur, since=since)]
            else:
                items.append(Item("UNCHANGED", cur, since=since))
        else:
            items.append(Item("CURRENT", cur))

    status = derive_status(items, missing, conflicts)
    return Answer(intent_name, params, T, tuple(items), tuple(missing), tuple(conflicts), status,
                  notes=consistency_notes(tuple(items)))


# ======================================================================
# 4. SILENCE CONTRACT
# ======================================================================
@dataclass(frozen=True)
class Speech:
    decision: str        # SPEAK | SPEAK_MISSING | NO_MESSAGE
    reason: str


def decide_speech(answer, trigger, already_communicated=()):
    """trigger: 'user_asked' | 'system_event'. When the user asks, BUS always
    responds — if there is no evidence it says exactly that. When the system
    initiates, silence is the default and a message needs something NEW."""
    if trigger not in ("user_asked", "system_event"):
        raise ClaimError("trigger must be 'user_asked' or 'system_event'")
    if trigger == "user_asked":
        return Speech("SPEAK_MISSING" if answer.status == "NO_EVIDENCE" else "SPEAK", answer.status)
    if not answer.items:
        return Speech("NO_MESSAGE", "NOTHING_NEW")
    told = set(already_communicated)
    fresh = [i for i in answer.items if i.label != "UNCHANGED" and i.claim.claim_id not in told]
    if not fresh:
        return Speech("NO_MESSAGE", "ALREADY_KNOWN")
    return Speech("SPEAK", "NEW_CLAIMS")


# ======================================================================
# Statement memory — so a correction ("didn't you say bullish?") can be
# answered from what BUS actually said, not reconstructed from nothing.
# ======================================================================
def statement_payload(answer):
    return {"intent": answer.intent, "params": answer.params,
            "as_of": ledger._iso(answer.as_of), "status": answer.status,
            "items": [{"label": i.label, **i.claim.to_dict()} for i in answer.items],
            "missing": [vars(m) for m in answer.missing],
            "conflicts": [{"subject": c.subject, "claim_ids": list(c.claim_ids)} for c in answer.conflicts]}


def prior_claims(records, subject, before):
    """Claims about `subject` that BUS had stated and that were known by
    `before`, oldest first."""
    out = []
    for r in ledger.as_of([r for r in records if r.get("event_type") == BUS_STATEMENT], before):
        for it in r["payload"]["items"]:
            if it["subject"] == subject:
                out.append({"said_at": r["payload"]["as_of"], **it})
    return sorted(out, key=lambda d: d["said_at"])


def reconcile(subject, current_claim, records, before):
    """What BUS previously said about `subject`, and whether it differs from
    the claim it would make now. Never pretends the earlier statement didn't happen."""
    prev = prior_claims(records, subject, before)
    if not prev:
        return {"previously_said": None, "changed": None}
    last = prev[-1]
    return {"previously_said": last["value"], "said_at": last["said_at"],
            "changed": json.dumps(last["value"], sort_keys=True)
                       != json.dumps(current_claim.value, sort_keys=True)}


# ======================================================================
# audit_text — the mechanical test the wording layer must pass.
# ======================================================================
_TF = re.compile(r"\b\d+\s*-?\s*(?:m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week)\b", re.I)
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}\b")
_NUM = re.compile(r"(?<![\w.\-])-?\d+(?:\.\d+)?")
# Opaque identifiers (record uuids, claim ids). They are not data: their digits must never be read as
# numbers. Found by a 1-in-30 flaky test: the timeframe stripper ate "879d" out of a uuid, left "-42a4",
# and the leftover "-42" matched no claim (89 random uuids in 3000 failed). An identifier may appear in
# wording only if it is verbatim in a claim; otherwise it is an unsupported identifier.
_IDENT = re.compile(r"\b(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|c_[0-9a-f]{16})\b")

TERM_RULES = (   # (regex, kind-of-requirement, what)
    (re.compile(r"\b(aging|ageing|mature|maturing|tired|exhausted|fresh)\b", re.I), "subject", "leg.age_status"),
    (re.compile(r"\bmomentum\b", re.I), "subject", "momentum.state"),
    (re.compile(r"\b(in control|took control|seized control|taking control|controlled)\b", re.I), "subject", "story.controller"),
    (re.compile(r"\b(fought back|fights? back|counter-?attack\w*)\b", re.I), "subject", "story.counter_attempt"),
    (re.compile(r"\binvalidat\w*", re.I), "subject", ("thesis.failure_condition", "thesis.invalidation")),
    (re.compile(r"\bbullish\b", re.I), "value", "BULLISH"),
    (re.compile(r"\bbearish\b", re.I), "value", "BEARISH"),
)


def _leaves(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _leaves(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _leaves(v)
    else:
        yield x


def _allowed_numbers(claims):
    nums = []
    for c in claims:
        leaves = list(_leaves(c.value)) + list(c.basis) + list(_leaves(c.condition or {}))
        for lf in leaves:
            if isinstance(lf, bool):
                continue
            if isinstance(lf, (int, float)):
                nums.append(float(lf))
            elif isinstance(lf, str):
                nums += [float(t) for t in _NUM.findall(lf)]
    return nums


ABSENT_OPEN, ABSENT_CLOSE = "\u27e6", "\u27e7"      # ⟦ ⟧ — wording marks a concept it names ONLY to say it is missing
ENGINE_OPEN, ENGINE_CLOSE = "\u2039", "\u203a"      # ‹ › — wording marks text QUOTED from the engine
_ENGINE_SEG = re.compile(ENGINE_OPEN + r"(.*?)" + ENGINE_CLOSE, re.S)


_ABSENT_SEG = re.compile(ABSENT_OPEN + r"(.*?)" + ABSENT_CLOSE, re.S)


def audit_text(text, answer, absent_labels=()):
    """Violations found in finished wording, given the Answer it claims to be
    rendering. Empty list == passed the lint (NOT proof of truth).

    Text between ENGINE_OPEN/ENGINE_CLOSE is the engine's own words, quoted. BUS may
    quote them but not author them, so each quoted segment must appear VERBATIM in a
    claim's text; the term lint then applies to BUS's own words only (an engine
    evidence line that says 'fresh leg' is the engine speaking, not BUS asserting it)."""
    claims = answer.claims
    v = []
    leaf_strings = [lf for c in claims for lf in list(_leaves(c.value)) + list(c.basis) if isinstance(lf, str)]
    for seg in _ENGINE_SEG.findall(text):
        if not any(seg.strip() in ls for ls in leaf_strings):
            v.append(f"quoted engine text not found in any claim: {seg[:60]!r}")
    text = _ENGINE_SEG.sub(" ", text)
    # "momentum: the engine doesn't compute that" names momentum without asserting it. Such mentions are
    # marked, and a marked name is only allowed if it is exactly a label the Answer reports as MISSING —
    # so the mark cannot be used to smuggle an assertion past the term lint.
    for seg in _ABSENT_SEG.findall(text):
        if seg not in set(absent_labels):
            v.append(f"'{seg}' is marked as missing but the answer does not report it missing")
    text = _ABSENT_SEG.sub(" ", text)
    for ident in set(_IDENT.findall(text)):
        if not (any(ident in ls for ls in leaf_strings) or any(c.claim_id == ident for c in claims)):
            v.append(f"identifier {ident} appears in the text but in no claim")
    text = _IDENT.sub(" ", text)
    stripped = _CLOCK.sub(" ", _TF.sub(" ", text))
    allowed = _allowed_numbers(claims)
    for tok in _NUM.findall(stripped):
        d = len(tok.split(".")[1]) if "." in tok else 0
        t = float(tok)
        if not any(round(n, d) == t for n in allowed):
            v.append(f"number {tok} appears in the text but in no claim")
    subjects = {c.subject for c in claims}
    values = {str(x).upper() for c in claims for x in _leaves(c.value)}
    for rx, kind, what in TERM_RULES:
        m = rx.search(text)
        if not m:
            continue
        if kind == "subject" and not (set(what if isinstance(what, tuple) else (what,)) & subjects):
            v.append(f"'{m.group(0)}' asserted but there is no {what if isinstance(what, str) else ' / '.join(what)} claim")
        if kind == "value" and what not in values:
            v.append(f"'{m.group(0)}' asserted but no claim has value {what}")
    for note in answer.notes:
        kws = NOTE_KEYWORDS.get(note, ())
        if kws and not any(k in text.lower() for k in kws):
            v.append(f"the answer carries note {note} but the text does not acknowledge it")
    if claims and not any(i.label in ("CURRENT", "UNCHANGED") for i in answer.items) \
            and re.search(r"\b(currently|right now|at the moment)\b", text, re.I):
        v.append("historical claims presented as current")
    return v


# ======================================================================
def coverage_report():
    """Which registered subjects the engine really provides vs. not, and which
    intents are therefore blocked on ENGINE work (not BUS work)."""
    unmapped = {n for _rx, n, s in _S if s.source is None}
    return {
        "mapped": sorted((n, s.source) for _rx, n, s in _S if s.source is not None),
        "unmapped": sorted((n, s.note) for _rx, n, s in _S if s.source is None),
        "intents_blocked_by_engine_work": {
            i.name: sorted(sl.subject for sl in i.slots if sl.subject in unmapped)
            for i in INTENTS.values() if any(sl.subject in unmapped for sl in i.slots)},
    }


if __name__ == "__main__":
    rep = coverage_report()
    print("MAPPED subjects:", len(rep["mapped"]))
    print("UNMAPPED (engine work):")
    for n, note in rep["unmapped"]:
        print(f"  - {n}: {note}")
    print("Intents blocked until the engine provides them:", rep["intents_blocked_by_engine_work"])
