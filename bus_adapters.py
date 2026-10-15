"""
bus_adapters.py — persisted records -> Claims/Gaps. A HARD AUTHORITY BOUNDARY,
not a convenience layer. Imports bus_contract and ledger ONLY.

The adapter's whole job:   "What authoritative information already exists, and
how do I expose it as a valid Claim?"   It is NOT allowed to ask "given these
values, what do I think the market is doing?" — that belongs upstream.

THE RULES (each is enforced by code, a test, or the layer checker)
  1. No presentation/wording imports                    (layer checker)
  2. No live market data, no network, no clock          (pure: every function
       takes already-persisted data; `now`-like values are PARAMETERS)
  3. No new market concepts                             (Claim refuses an
       unregistered subject; subjects are a closed registry in bus_contract)
  4. Every claim names its REAL authority and source ref, never "adapter"
       (no such authority exists in the registry)
  5. Missing != false != zero                           (see below)

MISSING != FALSE != ZERO
  Values are never allowed to carry "missing" by accident. Each source field is
  classified into exactly one of:
     KNOWN         -> a Claim (0, False, "", [] are legitimate KNOWN values where
                      the field's own rule says so)
     UNSET         -> a Gap: the authority wrote a PLACEHOLDER (mil.py's 0.0
                      failure levels; a None thesis direction)
     UNKNOWN       -> a Gap: the authority itself says it doesn't know
                      (ThesisStatus "UNKNOWN")
     NOT_PROVIDED  -> a Gap: the key/record is absent from the source
     STALE         -> a Gap: the authority flags its own value stale
                      (macro_bias_stale)
  Only the placeholder fields listed in PLACEHOLDER_FIELDS get 0.0 -> UNSET.
  Everywhere else 0.0 is a number.

TIME HONESTY
  state.json is OVERWRITTEN every scan, so it can only say what is true AS OF
  its snapshot. State-derived claims are therefore stamped with the snapshot
  time and are invisible to any earlier as_of — correct: the system cannot
  claim to know yesterday's state from today's file. Real history comes only
  from append-only sources (signal/market-event/leg-outcome logs, the ledger,
  and MIL's own `history`), and the claim matrix says which is which.

Engine-authored TEXT (MIL narrative, caution sentences) is carried as values
under explicit `engine_*` keys — quoted evidence of what the engine said, never
adapter wording.
"""

import json
import sys
from dataclasses import dataclass, field
from typing import List

import bus_contract as bc
import ledger
from bus_contract import Claim, Gap, FACT, INTERPRETATION, EXPECTATION

PLACEHOLDER_FIELDS = {
    # field path -> placeholder value that means "not set". ONLY these.
    "failure_condition.origin_level": 0.0,
    "failure_condition.retrace_level": 0.0,
}
BIAS_VALUES = ("BULLISH", "BEARISH", "CONSOLIDATION")   # scanner_observation.compute_macro_bias docstring


class AdapterError(Exception):
    pass


@dataclass
class Adapted:
    claims: List[Claim] = field(default_factory=list)
    gaps: List[Gap] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)      # source problems, never swallowed silently

    def extend(self, other):
        self.claims += other.claims
        self.gaps += other.gaps
        self.errors += other.errors
        return self


def merge(*parts):
    out = Adapted()
    for p in parts:
        out.extend(p)
    return out


# ---------------------------------------------------------------- helpers
def _t(value):
    return ledger.to_utc(value)


def _rec(a, snap, recorded=None):
    """recorded_at can never precede observed_at."""
    return max(_t(a), _t(recorded if recorded is not None else a))


class _Ctx:
    """`snap` is when the state's DATA was current (market_data_timestamp: set at the
    start of the scan). `rec` is when it was actually PERSISTED (state['timestamp'],
    ~3s later in the real file: leg_owner.claimed_at and macro_swing_confirmed_at are
    both stamped after market_data_timestamp). Claims are OBSERVED at `snap` but only
    RECORDED at `rec`, so an as_of taken between the two correctly cannot see them —
    found by running the adapters on a real state.json (a claim stamped 3s later was
    invisible at as_of=market_data_timestamp)."""

    def __init__(self, snapshot_at, persisted_at=None):
        self.snap = _t(snapshot_at)
        try:
            self.rec = max(self.snap, _t(persisted_at)) if persisted_at else self.snap
        except ledger.LedgerError:
            self.rec = self.snap

    def claim(self, out, kind, subject, value, authority, ref, observed_at, basis=(), condition=None,
              scan_id=None, recorded_at=None):
        """recorded_at defaults to the snapshot time (we only learned a CURRENT-state
        value when this snapshot was taken). Dated history passes its own."""
        o = _t(observed_at)
        out.claims.append(Claim(kind, subject, value, authority, ref, o,
                                max(o, _t(recorded_at) if recorded_at is not None else self.rec),
                                scan_id=scan_id, basis=tuple(basis), condition=condition))

    def gap(self, out, subject, state, authority, ref, detail="", observed_at=None):
        o = _t(observed_at) if observed_at is not None else self.snap
        out.gaps.append(Gap(subject, state, authority, ref, o, max(o, self.rec), detail))


# ======================================================================
# state.json  (CURRENT-ONLY)
# ======================================================================
def adapt_state(state, snapshot_at=None):
    """`snapshot_at` defaults to state['market_data_timestamp']. If neither is
    available there is no honest timestamp for anything, so NOTHING is emitted
    and an error is returned — never a guessed time."""
    out = Adapted()
    snap = snapshot_at or (state or {}).get("market_data_timestamp")
    if not snap:
        out.errors.append("state has no market_data_timestamp and no snapshot_at was given; "
                          "state-derived claims cannot be timestamped, so none were produced")
        return out
    try:
        cx = _Ctx(snap, (state or {}).get("timestamp"))
    except ledger.LedgerError as e:
        out.errors.append(f"unusable snapshot time: {e}")
        return out

    # -- market.data_timestamp
    cx.claim(out, FACT, "market.data_timestamp", cx.snap.isoformat(), "engine.state",
             "state.json#market_data_timestamp", cx.snap)

    # -- htf.bias (+ stale flag)
    regime = ((state.get("structure_digest") or {}).get("regime") or {})
    ref = "state.json#structure_digest.regime.macro_bias"
    if "macro_bias" not in regime:
        cx.gap(out, "htf.bias", "NOT_PROVIDED", "engine.state", ref, "no structure_digest.regime in state")
    elif (regime.get("as_of_scan_id") is not None and state.get("scan_id") is not None
          and regime["as_of_scan_id"] != state["scan_id"]):
        cx.gap(out, "htf.bias", "STALE", "engine.state", ref + "_as_of",
               f"this copy was written by scan {regime['as_of_scan_id']}, the state is scan {state['scan_id']}; not re-stated")
    elif regime.get("macro_bias_stale") is True:
        cx.gap(out, "htf.bias", "STALE", "engine.state", ref + "_stale", "engine flags macro_bias_stale=True")
    elif regime["macro_bias"] not in BIAS_VALUES:
        cx.gap(out, "htf.bias", "NOT_PROVIDED", "engine.state", ref,
               f"unrecognised macro_bias value {regime['macro_bias']!r}; not guessed")
    else:
        cx.claim(out, FACT, "htf.bias", regime["macro_bias"], "engine.state", ref, cx.snap)

    # -- leg.owner  (absence is NOT_PROVIDED: the engine pops the key on release, but an
    #    absent key could also mean "never set", and the adapter does not decide which)
    lo = state.get("leg_owner")
    ref = "state.json#leg_owner"
    if lo is None:
        cx.gap(out, "leg.owner", "NOT_PROVIDED", "engine.leg_owner", ref, "no leg_owner key")
    elif not isinstance(lo, dict) or not lo.get("claimed_at"):
        cx.gap(out, "leg.owner", "NOT_PROVIDED", "engine.leg_owner", ref, "leg_owner present but has no claimed_at")
    else:
        cx.claim(out, FACT, "leg.owner", {k: lo.get(k) for k in ("leg_id", "tier", "status", "upgraded")},
                 "engine.leg_owner", ref, lo["claimed_at"])

    # -- market_thesis_current_state (opaque JSON value, passed through untouched)
    if "market_thesis_current_state" in state and state["market_thesis_current_state"] is not None:
        cx.claim(out, INTERPRETATION, "thesis.state", state["market_thesis_current_state"], "engine.thesis",
                 "state.json#market_thesis_current_state", cx.snap)
    else:
        cx.gap(out, "thesis.state", "NOT_PROVIDED", "engine.thesis", "state.json#market_thesis_current_state")

    # -- cautions: [] is a KNOWN "the engine checked and listed none"; absent is NOT_PROVIDED
    ref = "state.json#market_intent_caution_codes"
    if "market_intent_caution_codes" not in state:
        cx.gap(out, "cautions", "NOT_PROVIDED", "engine.market_intent", ref)
    else:
        cc = state["market_intent_caution_codes"]
        if not isinstance(cc, list):
            cx.gap(out, "cautions", "NOT_PROVIDED", "engine.market_intent", ref, "not a list")
        else:
            cx.claim(out, INTERPRETATION, "cautions",
                     [{"code": c.get("code"), "role": c.get("role"), "engine_sentence": c.get("sentence")}
                      for c in cc if isinstance(c, dict)],
                     "engine.market_intent", ref, state.get("market_intent_updated_at") or cx.snap)

    out.extend(_adapt_thesis_fields(state, cx))
    out.extend(_adapt_mil(state.get("mil_understanding"), cx))
    return out


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _adapt_thesis_fields(state, cx):
    """The engine's flattened thesis/phase/leg/campaign/watch fields (all verified
    present in a real state.json, 2026-10-01). Each is passed through as the engine
    wrote it; engine-authored prose rides under `engine_*` keys."""
    out = Adapted()
    t_obs = state.get("market_thesis_updated_at") or cx.snap.isoformat()
    i_obs = state.get("market_intent_updated_at") or cx.snap.isoformat()

    def present(key):
        return key in state and state[key] is not None

    # phase (+ age in bars)
    if present("market_phase"):
        cx.claim(out, INTERPRETATION, "thesis.phase",
                 {"phase": state["market_phase"], "age_bars": state.get("market_phase_age_bars")},
                 "engine.thesis", "state.json#market_phase", t_obs)
    else:
        cx.gap(out, "thesis.phase", "NOT_PROVIDED", "engine.thesis", "state.json#market_phase")

    # the engine's own age/exhaustion read. aging_reason None == "no aging reason"; it is
    # passed through as null, NOT treated as missing and NOT as the text "fresh".
    # `status` is the engine's explicit maturity label (leg_age.py, Phase C item 1); it is
    # included only when the engine wrote one -- never derived here from the prose.
    age = state.get("market_leg_age_status")
    age_status = age.get("status") if isinstance(age, dict) else None
    if present("market_thesis_trend_health") or age_status:
        val = {}
        if present("market_thesis_trend_health"):
            val.update({"engine_trend_health": state["market_thesis_trend_health"],
                        "aging_reason": state.get("market_thesis_aging_reason"),
                        "break_count": state.get("market_thesis_break_count")})
        if age_status:
            val["status"] = age_status
        cx.claim(out, INTERPRETATION, "leg.age_status", val, "engine.thesis",
                 "state.json#market_thesis_trend_health" if present("market_thesis_trend_health")
                 else "state.json#market_leg_age_status", t_obs)
    else:
        cx.gap(out, "leg.age_status", "NOT_PROVIDED", "engine.thesis", "state.json#market_thesis_trend_health")
    if present("market_thesis_trend_health"):
        cx.claim(out, INTERPRETATION, "thesis.trend_health", state["market_thesis_trend_health"],
                 "engine.thesis", "state.json#market_thesis_trend_health", t_obs)
    else:
        cx.gap(out, "thesis.trend_health", "NOT_PROVIDED", "engine.thesis", "state.json#market_thesis_trend_health")

    if present("market_thesis_failure_risk"):
        cx.claim(out, INTERPRETATION, "thesis.failure_risk",
                 {"level": state["market_thesis_failure_risk"],
                  "engine_reasons": list(state.get("market_thesis_failure_risk_reasons_prose") or [])},
                 "engine.thesis", "state.json#market_thesis_failure_risk", t_obs)
    else:
        cx.gap(out, "thesis.failure_risk", "NOT_PROVIDED", "engine.thesis", "state.json#market_thesis_failure_risk")

    if present("market_thesis_expected_next_event"):
        cx.claim(out, INTERPRETATION, "thesis.expected_next_event", state["market_thesis_expected_next_event"],
                 "engine.thesis", "state.json#market_thesis_expected_next_event", t_obs)
    else:
        cx.gap(out, "thesis.expected_next_event", "NOT_PROVIDED", "engine.thesis",
               "state.json#market_thesis_expected_next_event")

    if present("market_thesis_volatility_hint"):
        cx.claim(out, INTERPRETATION, "volatility.state", state["market_thesis_volatility_hint"],
                 "engine.thesis", "state.json#market_thesis_volatility_hint", t_obs)
    else:
        cx.gap(out, "volatility.state", "NOT_PROVIDED", "engine.thesis", "state.json#market_thesis_volatility_hint")

    # invalidation: the engine's text plus the leg anchor numbers it refers to (no condition is
    # derived from the text — the structured condition comes only from MIL's failure_condition)
    inv = state.get("market_thesis_invalidation")
    if isinstance(inv, str) and inv.strip() and not inv.startswith("N/A"):
        cx.claim(out, INTERPRETATION, "thesis.invalidation",
                 {"engine_text": inv, "leg_origin": state.get("macro_leg_origin"),
                  "leg_direction": state.get("macro_leg_direction")},
                 "engine.thesis", "state.json#market_thesis_invalidation", t_obs)
    elif isinstance(inv, str) and inv.startswith("N/A"):
        cx.gap(out, "thesis.invalidation", "UNSET", "engine.thesis", "state.json#market_thesis_invalidation",
               "engine reports no active directional leg to invalidate", observed_at=t_obs)
    else:
        cx.gap(out, "thesis.invalidation", "NOT_PROVIDED", "engine.thesis", "state.json#market_thesis_invalidation")

    # the 1H leg anchors and the campaign (facts; numbers pass through untouched)
    if state.get("macro_leg_direction") in ("BULLISH", "BEARISH") and _num(state.get("macro_leg_origin")) \
            and _num(state.get("macro_leg_extreme")):
        cx.claim(out, FACT, "leg.macro",
                 {k: state.get(f"macro_leg_{k}") for k in ("direction", "origin", "extreme", "origin_time")},
                 "engine.state", "state.json#macro_leg_*", cx.snap)
    else:
        cx.gap(out, "leg.macro", "NOT_PROVIDED", "engine.state", "state.json#macro_leg_*", "no standing 1H leg")
    if state.get("campaign_direction") in ("BULLISH", "BEARISH") and _num(state.get("campaign_origin")):
        cx.claim(out, FACT, "campaign.state",
                 {k: state.get(f"campaign_{k}") for k in ("direction", "origin", "origin_time",
                                                         "initial_impulse_pips", "current_extreme",
                                                         "continuation_count")},
                 "engine.state", "state.json#campaign_*", cx.snap)
    else:
        cx.gap(out, "campaign.state", "NOT_PROVIDED", "engine.state", "state.json#campaign_*", "no standing campaign")

    # watch zones: [] is a KNOWN "nothing to watch right now"; absent is NOT_PROVIDED
    if "market_intent_watch_codes" in state and isinstance(state["market_intent_watch_codes"], list):
        cx.claim(out, INTERPRETATION, "watch.zones",
                 [{"code": w.get("code"), "role": w.get("role"), "zone_low": w.get("zone_low"),
                   "zone_high": w.get("zone_high"), "engine_sentence": w.get("sentence")}
                  for w in state["market_intent_watch_codes"] if isinstance(w, dict)],
                 "engine.market_intent", "state.json#market_intent_watch_codes", i_obs)
    else:
        cx.gap(out, "watch.zones", "NOT_PROVIDED", "engine.market_intent", "state.json#market_intent_watch_codes")
    return out


def _adapt_mil(mil, cx):
    out = Adapted()
    base = "state.json#mil_understanding"
    if not isinstance(mil, dict):
        for subj in ("thesis.status", "thesis.primary", "thesis.counter",
                     "thesis.conflicting_evidence", "thesis.failure_condition"):
            cx.gap(out, subj, "NOT_PROVIDED", "engine.mil", base, "no mil_understanding in state")
        return out
    obs = mil.get("last_reconciled_at") or cx.snap.isoformat()

    # -- thesis.status (UNKNOWN is the authority saying it doesn't know — a gap, not a value)
    st = mil.get("thesis_status")
    if st is None:
        cx.gap(out, "thesis.status", "NOT_PROVIDED", "engine.mil", base + ".thesis_status", observed_at=obs)
    elif st == "UNKNOWN":
        cx.gap(out, "thesis.status", "UNKNOWN", "engine.mil", base + ".thesis_status",
               "MIL reports UNKNOWN (bootstrap, or recovering)", observed_at=obs)
    else:
        cx.claim(out, INTERPRETATION, "thesis.status", st, "engine.mil", base + ".thesis_status", obs)

    # -- real history: each transition is a dated claim (the only past MIL state persisted)
    for i, h in enumerate(mil.get("history") or []):
        try:
            to_state, at = h["to_state"], h["at"]
            if to_state == "UNKNOWN":
                continue
            # A history entry is stamped with the scan time it was written in, so the
            # system KNEW it then: recorded_at = at (documented assumption; MIL writes
            # transitions during the scan whose timestamp it carries). Using the
            # snapshot time here would hide real past transitions from earlier views.
            cx.claim(out, INTERPRETATION, "thesis.status", to_state, "engine.mil",
                     f"{base}.history[{i}]", at, recorded_at=at,
                     basis=tuple(x for x in (h.get("transition_cause"),) if x))
        except (KeyError, ledger.LedgerError, bc.ClaimError) as e:
            out.errors.append(f"mil history[{i}] skipped: {e}")

    # -- thesis.primary: a claim only if a direction exists; None is MIL's placeholder
    pt = mil.get("primary_thesis")
    ref = base + ".primary_thesis"
    if not isinstance(pt, dict):
        cx.gap(out, "thesis.primary", "NOT_PROVIDED", "engine.mil", ref, observed_at=obs)
    elif pt.get("direction") is None:
        cx.gap(out, "thesis.primary", "UNSET", "engine.mil", ref,
               "primary_thesis.direction is None (not yet computed)", observed_at=obs)
    else:
        cx.claim(out, INTERPRETATION, "thesis.primary",
                 {"direction": pt.get("direction"), "state": pt.get("state"),
                  "expected_next_event": pt.get("expected_next_event"),
                  "confidence": pt.get("confidence"), "engine_narrative": pt.get("narrative")},
                 "engine.mil", ref, obs, basis=tuple(mil.get("supporting_evidence") or ()))

    # -- thesis.counter
    ct = mil.get("counter_thesis")
    if ct is None:
        cx.gap(out, "thesis.counter", "NOT_PROVIDED", "engine.mil", base + ".counter_thesis",
               "no counter_thesis recorded", observed_at=obs)
    else:
        cx.claim(out, INTERPRETATION, "thesis.counter", dict(ct), "engine.mil", base + ".counter_thesis", obs)

    # -- conflicting evidence: [] is KNOWN-none, absent is NOT_PROVIDED
    if "conflicting_evidence" not in mil:
        cx.gap(out, "thesis.conflicting_evidence", "NOT_PROVIDED", "engine.mil", base + ".conflicting_evidence",
               observed_at=obs)
    else:
        cx.claim(out, INTERPRETATION, "thesis.conflicting_evidence", list(mil["conflicting_evidence"]),
                 "engine.mil", base + ".conflicting_evidence", obs)

    # -- failure condition: ORIGIN mechanism is a field-for-field translation
    fc, ref = mil.get("failure_condition"), base + ".failure_condition"
    if fc is None:
        cx.gap(out, "thesis.failure_condition", "UNSET", "engine.mil", ref,
               "failure_condition is None", observed_at=obs)
    else:
        origin = fc.get("origin_level")
        direction = fc.get("origin_direction")
        placeholder = PLACEHOLDER_FIELDS["failure_condition.origin_level"]
        if origin is None or isinstance(origin, bool) or origin == placeholder:
            cx.gap(out, "thesis.failure_condition", "UNSET", "engine.mil", ref + ".origin_level",
                   f"origin_level is the placeholder {origin!r}", observed_at=obs)
        elif direction not in ("above", "below"):
            cx.gap(out, "thesis.failure_condition", "NOT_PROVIDED", "engine.mil", ref + ".origin_direction",
                   f"origin_direction {direction!r} is not 'above'/'below'; not guessed", observed_at=obs)
        else:
            retrace = fc.get("retrace_level")
            retrace_known = retrace is not None and not isinstance(retrace, bool) \
                and retrace != PLACEHOLDER_FIELDS["failure_condition.retrace_level"]
            cx.claim(out, EXPECTATION, "thesis.failure_condition",
                     {"origin_level": origin, "origin_direction": direction,
                      # a placeholder retrace is carried as ABSENT, never as 0.0
                      **({"retrace_level": retrace} if retrace_known else {})},
                     "engine.mil", ref, obs,
                     # price_TRADE_*, not price_close_*: the engine's rule (check_leg_anchor_survival)
                     # tests the window's High/Low, so a wick through the origin fires it.
                     condition={"type": "price_trade_" + direction, "level": origin})
    return out


# ======================================================================
# state_history_log.jsonl  (HISTORICAL — written by state_history.py, one record per scan)
# ======================================================================
STATE_SNAPSHOT = "STATE_SNAPSHOT"      # event-type name only; state_history is NOT imported (a test pins the name)


def adapt_state_history(records):
    """Past state, from what was recorded then. `records` are raw ledger records of state_history_log.jsonl
    (pass ledger.as_of(...) for a historical view; supersession by a correction is computed from the records
    given). Each snapshot yields claims OBSERVED at its market-data time and RECORDED when it was written, so
    no snapshot can describe a moment before it was known. An absent field becomes an explicit GAP (never a
    value) — and because a newer gap supersedes an older claim, a released leg owner stops looking owned.

    Only the subjects whose value shape is identical to adapt_state's are emitted, so history and the current
    state never disagree merely in shape. Prose (trend-health text, narratives) is never stored or emitted;
    thesis.primary stays current-only on purpose."""
    out = Adapted()
    for r in ledger.current([x for x in records if x.get("event_type") == STATE_SNAPSHOT]):
        try:
            p, obs, rec = r["payload"], r["ts"], r["recorded_at"]
            base = f"state_history_log.jsonl#{r['event_id']}"

            def claim(kind, subject, value, authority, fld):
                out.claims.append(Claim(kind, subject, value, authority, f"{base}.{fld}", obs, rec, scan_id=r.get("scan_id")))

            def gap(subject, state, authority, fld, detail=""):
                out.gaps.append(Gap(subject, state, authority, f"{base}.{fld}", _t(obs), max(_t(obs), _t(rec)), detail))

            if p.get("regime_digest_current") is False:
                gap("htf.bias", "STALE", "engine.state", "regime_digest_current", "the bias copy was left over from an earlier scan")
            elif p.get("htf_bias_stale") is True:
                gap("htf.bias", "STALE", "engine.state", "htf_bias_stale", "engine flagged macro_bias_stale=True")
            elif p.get("htf_bias") in BIAS_VALUES:
                claim(FACT, "htf.bias", p["htf_bias"], "engine.state", "htf_bias")
            else:
                gap("htf.bias", "NOT_PROVIDED", "engine.state", "htf_bias")

            st = p.get("mil_status")
            if st is None:
                gap("thesis.status", "NOT_PROVIDED", "engine.mil", "mil_status")
            elif st == "UNKNOWN":
                gap("thesis.status", "UNKNOWN", "engine.mil", "mil_status", "MIL reported UNKNOWN")
            else:
                claim(INTERPRETATION, "thesis.status", st, "engine.mil", "mil_status")

            if p.get("market_phase") is not None:
                claim(INTERPRETATION, "thesis.phase", {"phase": p["market_phase"], "age_bars": p.get("market_phase_age_bars")},
                      "engine.thesis", "market_phase")
            else:
                gap("thesis.phase", "NOT_PROVIDED", "engine.thesis", "market_phase")

            if isinstance(p.get("macro_leg"), dict):
                claim(FACT, "leg.macro", {k: p["macro_leg"].get(k) for k in ("direction", "origin", "extreme", "origin_time")},
                      "engine.state", "macro_leg")
            else:
                gap("leg.macro", "NOT_PROVIDED", "engine.state", "macro_leg", "no standing 1H leg")

            if isinstance(p.get("leg_owner"), dict):
                claim(FACT, "leg.owner", {k: p["leg_owner"].get(k) for k in ("leg_id", "tier", "status", "upgraded")},
                      "engine.leg_owner", "leg_owner")
            else:
                gap("leg.owner", "NOT_PROVIDED", "engine.leg_owner", "leg_owner", "no leg owner recorded")

            if isinstance(p.get("leg_age_status"), dict) and p["leg_age_status"].get("status"):
                claim(INTERPRETATION, "leg.age_status", {k: p["leg_age_status"].get(k) for k in ("status", "aging_reason", "break_count")},
                      "engine.thesis", "leg_age_status")
            else:
                gap("leg.age_status", "NOT_PROVIDED", "engine.thesis", "leg_age_status")

            if p.get("volatility_hint") is not None:
                claim(INTERPRETATION, "volatility.state", p["volatility_hint"], "engine.thesis", "volatility_hint")
            else:
                gap("volatility.state", "NOT_PROVIDED", "engine.thesis", "volatility_hint")
        except (KeyError, ledger.LedgerError, bc.ClaimError) as e:
            out.errors.append(f"state snapshot {r.get('event_id')} skipped: {e}")
    return out


# ======================================================================
# append-only logs (HISTORICAL)
# ======================================================================
def _line_ref(name, rec, key):
    return f"{name}#{rec.get(key)}"


def adapt_signal_events(records):
    """signal_event_log.jsonl lines written by scanner_live._record_undelivered_signal."""
    out = Adapted()
    for r in records:
        try:
            if r.get("status") != "FIRED_RISK_GATE_PASSED_UNDELIVERED":
                continue
            rr = r.get("risk_result") or {}
            value = {"event_id": r["event_id"], "tier_label": r.get("tier_label"),
                     "direction": r.get("direction"), "score": r.get("score"),
                     "tier_rating": r.get("tier_rating"), "leg_id": r.get("leg_id"),
                     "entry": rr.get("entry"), "sl": rr.get("sl"), "tp": rr.get("tp"),
                     "target_r": rr.get("target_r"), "entry_candle_ts": r.get("entry_candle_ts"),
                     # what the legacy alert showed, so the BUS alert can state the same facts. Absent -> None, never 0.
                     "fired_at": r.get("fired_at"), "pair": r.get("pair"), "macro_bias": r.get("macro_bias"),
                     "macro_bias_stale": (r.get("snapshot") or {}).get("macro_bias_stale"),
                     "risk_pips": rr.get("risk_pips"), "reward_pips": rr.get("reward_pips"), "size_mult": rr.get("size_mult"),
                     "risk_band": (r.get("risk_at_hand") or {}).get("band"),
                     "risk_reasons": list((r.get("risk_at_hand") or {}).get("reasons") or []),
                     "risk_recommendation": (r.get("risk_at_hand") or {}).get("recommendation"),
                     "reason": r.get("reason"),
                     "low_atr_warning": r.get("low_atr_warning"), "very_low_atr_warning": r.get("very_low_atr_warning"),
                     "cautions": ([c.get("sentence") for c in (r.get("snapshot") or {}).get("market_intent_caution_codes") if isinstance(c, dict) and c.get("sentence")]
                                  if isinstance((r.get("snapshot") or {}).get("market_intent_caution_codes"), list) else None)}
            t = _t(r["fired_at"])
            out.claims.append(Claim(FACT, "signal.undelivered", value, "engine.signal_log",
                                    _line_ref("signal_event_log.jsonl", r, "event_id"), t, t,
                                    scan_id=r.get("scan_id")))
        except (KeyError, ledger.LedgerError, bc.ClaimError) as e:
            out.errors.append(f"signal record skipped: {e}")
    return out


def adapt_market_events(records):
    """market_event_log.jsonl lines written by scanner_live._evaluate_and_record_market_event."""
    out = Adapted()
    for i, r in enumerate(records):
        try:
            t = _t(r["recorded_at"])          # records without it cannot be placed in time: skipped, reported
            value = {"code": r["code"], "category": r.get("category"),
                     "story_impact": r.get("story_impact"), "leg_obs_id": r.get("leg_obs_id"),
                     "identity": list(r.get("identity") or [])}
            out.claims.append(Claim(FACT, "event.recorded", value, "engine.event_log",
                                    f"market_event_log.jsonl#scan{r.get('scan_id')}/{r['code']}/{i}", t, t,
                                    scan_id=r.get("scan_id")))
        except (KeyError, ledger.LedgerError, bc.ClaimError) as e:
            out.errors.append(f"market event record {i} skipped: {e}")
    return out


def adapt_leg_outcomes(records):
    """leg_obs_log.jsonl: one line per resolved leg (fields verified: id, leg_id, fate, resolved_at, bars_open)."""
    out = Adapted()
    for r in records:
        try:
            if not r.get("fate"):
                out.errors.append(f"leg record {r.get('id')} has no fate; not an outcome")
                continue
            t = _t(r["resolved_at"])
            out.claims.append(Claim(FACT, "leg.outcome",
                                    {"leg_obs_id": r.get("id"), "leg_id": r.get("leg_id"),
                                     "fate": r["fate"], "bars_open": r.get("bars_open")},
                                    "engine.leg_obs_log", _line_ref("leg_obs_log.jsonl", r, "id"), t, t))
        except (KeyError, ledger.LedgerError, bc.ClaimError) as e:
            out.errors.append(f"leg outcome skipped: {e}")
    return out


# ======================================================================
# catalog / ledger records (HISTORICAL via ledger as_of)
# ======================================================================
def adapt_case(case_view):
    """`case_view` is Catalog.get_case(case_id, as_of=T) — already a bitemporal view,
    so nothing later than T can be in it. One claim per section; sections are
    never merged."""
    out = Adapted()
    if not case_view:
        return out
    cid = case_view["case_id"]

    def one(section, rec):
        p = dict(rec["payload"])
        p.pop("case_id", None)
        out.claims.append(Claim(FACT, f"case.{cid}.{section}", p, "catalog.user_ledger",
                                f"user_ledger.jsonl#{rec['event_id']}", rec["ts"], rec["recorded_at"]))

    def many(section, recs):
        if recs:
            last = max(recs, key=lambda r: (ledger.to_utc(r["ts"]), ledger.to_utc(r["recorded_at"])))
            out.claims.append(Claim(
                FACT, f"case.{cid}.{section}", {"entries": [dict(r["payload"], at=r["ts"]) for r in recs]},
                "catalog.user_ledger", "user_ledger.jsonl#" + ",".join(r["event_id"] for r in recs),
                last["ts"], max(ledger.to_utc(r["recorded_at"]) for r in recs)))
    try:
        one("trade", case_view["trade"])
        if case_view.get("bot_plan"):
            one("bot_plan", case_view["bot_plan"])
        if case_view.get("outcome"):
            one("outcome", case_view["outcome"])
        many("user_reasoning", case_view.get("user_reasoning") or [])
        many("investigation", case_view.get("investigation") or [])
    except (KeyError, ledger.LedgerError, bc.ClaimError) as e:
        out.errors.append(f"case {cid} skipped: {e}")
    return out


def adapt_threads(records):
    """Raw user-ledger records (pass ledger.as_of(...) for a historical view)."""
    out = Adapted()
    resolved = {r["payload"]["thread_id"]: r for r in records if r.get("event_type") == "THREAD_RESOLVED"}
    cancelled = {r["payload"]["thread_id"]: r for r in records if r.get("event_type") == "THREAD_CANCELLED"}
    for r in records:
        if r.get("event_type") != "THREAD_OPENED":
            continue
        try:
            p = r["payload"]
            tid = p["thread_id"]
            res = resolved.get(tid) or cancelled.get(tid)
            status = "RESOLVED" if tid in resolved else ("CANCELLED" if tid in cancelled else "OPEN")
            value = {"status": status, "topic": p.get("topic"),
                     "concern": p.get("concern"), "bot_position": p.get("bot_position"),
                     "condition": p.get("condition")}
            if tid in resolved:
                value["evidence"] = res["payload"].get("evidence")
            src = res or r
            out.claims.append(Claim(FACT, f"thread.{tid}.status", value, "catalog.user_ledger",
                                    f"user_ledger.jsonl#{src['event_id']}", src["ts"], src["recorded_at"]))
        except (KeyError, ledger.LedgerError, bc.ClaimError) as e:
            out.errors.append(f"thread skipped: {e}")
    return out


def adapt_knowledge(records):
    out = Adapted()
    for r in records:
        if r.get("event_type") != "KNOWLEDGE_DEMONSTRATED":
            continue
        try:
            p = r["payload"]
            out.claims.append(Claim(FACT, f"user.knows.{p['concept']}",
                                    {"evidence_event_id": p["evidence_event_id"], "note": p.get("note", "")},
                                    "catalog.user_ledger", f"user_ledger.jsonl#{r['event_id']}",
                                    r["ts"], r["recorded_at"]))
        except (KeyError, ledger.LedgerError, bc.ClaimError) as e:
            out.errors.append(f"knowledge record skipped: {e}")
    return out


def adapt_alerts(records):
    """Open price alerts = THREAD_OPENED with topic 'price alert' and no THREAD_RESOLVED / THREAD_CANCELLED.
    ONE aggregated claim whose counts are claim fields. An empty list is a KNOWN 'no open alerts' (a claim),
    but only once the user has ever set one; before that there is simply no claim."""
    out = Adapted()
    opened = [r for r in records if r.get("event_type") == "THREAD_OPENED" and (r.get("payload") or {}).get("topic") == "price alert"]
    if not opened:
        return out
    try:
        done = {r["payload"]["thread_id"] for r in records if r.get("event_type") in ("THREAD_RESOLVED", "THREAD_CANCELLED")}
        alerts = [{"thread_id": r["payload"]["thread_id"], "condition": r["payload"]["condition"],
                   "set_at": r["ts"]} for r in opened if r["payload"]["thread_id"] not in done]
        last = max(records, key=lambda r: ledger.to_utc(r["recorded_at"]))
        t = ledger.to_utc(last["recorded_at"])
        out.claims.append(Claim(FACT, "alerts.open", {"alerts": alerts, "n_open": len(alerts)}, "catalog.user_ledger",
                                "user_ledger.jsonl#THREAD_OPENED", t, t))
    except (KeyError, ledger.LedgerError, bc.ClaimError) as e:
        out.errors.append(f"alerts skipped: {e}")
    return out


def adapt_terms(records):
    """TERMS_OBSERVED / USER_TERM_ALIAS -> one `user.terms` claim (usage counts after aliasing). Computed from
    the records passed in, so a historical view (ledger.as_of) gives the historical usage."""
    out = Adapted()
    obs = [r for r in records if r.get("event_type") == "TERMS_OBSERVED"]
    if not obs:
        return out
    try:
        alias = {}
        for r in records:
            if r.get("event_type") == "USER_TERM_ALIAS":
                alias[r["payload"]["variant"]] = r["payload"]["canonical"]

        def canon(k):
            seen = set()
            while k in alias:
                if k in seen:
                    return min(seen)          # alias cycle -> one term, chosen deterministically
                seen.add(k); k = alias[k]
            return k
        agg = {}
        for r in obs:
            for t in r["payload"]["user_terms"]:
                k = canon(t["key"])
                g = agg.setdefault(k, {"term": k, "status": t["status"], "uses": 0, "cases": set(), "variants": set()})
                g["uses"] += 1; g["cases"].add(r["payload"]["case_id"]); g["variants"].add(t["raw"])
        terms = sorted(({"term": g["term"], "status": g["status"], "uses": g["uses"], "n_cases": len(g["cases"]),
                         "variants": sorted(g["variants"])} for g in agg.values()), key=lambda d: (-d["uses"], d["term"]))
        last = max(records, key=lambda r: ledger.to_utc(r["recorded_at"]))
        t = ledger.to_utc(last["recorded_at"])
        out.claims.append(Claim(FACT, "user.terms", {"terms": terms}, "catalog.user_ledger",
                                "user_ledger.jsonl#TERMS_OBSERVED", t, t))
    except (KeyError, ledger.LedgerError, bc.ClaimError) as e:
        out.errors.append(f"terms skipped: {e}")
    return out


def adapt_research(records):
    """Research events (written by research_objects.py) -> one `research.cases` claim. Reads the records
    by event-type name only; research_objects is NOT imported (a test pins this output to its own
    cases()/family_summary() so the two cannot drift)."""
    out = Adapted()
    R = [r for r in records if r.get("event_type") in ("HYPOTHESIS_DRAFTED", "EXPERIMENT_SPEC_DRAFTED", "EXPERIMENT_FROZEN",
                                                       "RESEARCH_RESULT_RECORDED")]
    if not R:
        return out
    try:
        hyp = {r["payload"]["hypothesis_id"]: {"hypothesis_id": r["payload"]["hypothesis_id"], "question": r["payload"]["question"],
                                               "research_family_id": r["payload"]["research_family_id"], "experiments": []}
               for r in R if r["event_type"] == "HYPOTHESIS_DRAFTED"}
        frozen = {r["payload"]["spec_id"] for r in R if r["event_type"] == "EXPERIMENT_FROZEN"}
        status = {r["payload"]["spec_id"]: r["payload"]["status"] for r in R if r["event_type"] == "RESEARCH_RESULT_RECORDED"}
        peeked = {r["payload"]["spec_id"] for r in R if r["event_type"] == "RESEARCH_RESULT_RECORDED" and r["payload"].get("final_test_peeked")}
        fam = {}
        for r in R:
            if r["event_type"] == "EXPERIMENT_SPEC_DRAFTED":
                p = r["payload"]
                fam.setdefault(p["research_family_id"], set()).add(p["spec_id"])
                if p["hypothesis_id"] in hyp and not any(e["spec_id"] == p["spec_id"] for e in hyp[p["hypothesis_id"]]["experiments"]):
                    hyp[p["hypothesis_id"]]["experiments"].append({"spec_id": p["spec_id"], "valid": p["valid"], "frozen": p["spec_id"] in frozen,
                                                                   "status": status.get(p["spec_id"], "NO_RESULT"),
                                                                   "final_test_peeked": p["spec_id"] in peeked})
        cases = []
        for h in hyp.values():
            ex = h["experiments"]
            cases.append(dict(h, family_searches=len(fam.get(h["research_family_id"], ())),
                              n_specs=len(ex), n_frozen=sum(1 for e in ex if e["frozen"])))
        last = max(R, key=lambda r: ledger.to_utc(r["recorded_at"]))
        t = ledger.to_utc(last["recorded_at"])
        out.claims.append(Claim(FACT, "research.cases", {"cases": cases}, "catalog.user_ledger",
                                "user_ledger.jsonl#research", t, t))
    except (KeyError, ledger.LedgerError, bc.ClaimError) as e:
        out.errors.append(f"research records skipped: {e}")
    return out


def adapt_statements(records):
    """BUS_STATEMENT ledger events -> what BUS itself previously said, per subject."""
    out = Adapted()
    for r in records:
        if r.get("event_type") != bc.BUS_STATEMENT:
            continue
        try:
            for it in r["payload"]["items"]:
                out.claims.append(Claim(FACT, f"bus.statement.{it['subject']}",
                                        {"said": it["value"], "label": it["label"],
                                         "about_claim_id": it["claim_id"], "kind": it["kind"]},
                                        "bus.statement_log", f"user_ledger.jsonl#{r['event_id']}",
                                        r["payload"]["as_of"], r["recorded_at"]))
        except (KeyError, ledger.LedgerError, bc.ClaimError) as e:
            out.errors.append(f"statement record skipped: {e}")
    return out


def adapt_findings(records, checked_at):
    """integrity_ledger.jsonl (ledger envelope): open INTEGRITY_FINDING events. ONE aggregated claim,
    so a changed set of open findings is a new claim_id (=> announced once) and an unchanged set is
    ALREADY_KNOWN. `checked_at` is when the ledger was read (the claim is only as fresh as that)."""
    out = Adapted()
    open_ = {}
    latest = None
    for r in records:
        et, p = r.get("event_type"), r.get("payload") or {}
        try:
            if et == "INTEGRITY_FINDING":
                open_[p["fingerprint"]] = {k: p.get(k) for k in ("fingerprint", "probe", "severity", "summary", "paths",
                                                                  "first_divergence", "first_seen")}
                # the relationship that failed (same_writer, ...) — quiet mode's critical test needs it; never worded
                open_[p["fingerprint"]]["relationship"] = (p.get("evidence") or {}).get("relationship")
            elif et == "INTEGRITY_RESOLVED":
                open_.pop(p["fingerprint"], None)
            else:
                continue
            t = ledger.to_utc(r["recorded_at"])
            latest = t if latest is None or t > latest else latest
        except (KeyError, ledger.LedgerError) as e:
            out.errors.append(f"integrity record skipped: {e}")
    if latest is None:
        return out
    ordered = sorted(open_.values(), key=lambda f: ({"HIGH": 0, "MED": 1, "LOW": 2}.get(f["severity"], 3), f["fingerprint"]))
    t = _t(checked_at)
    # counts are CLAIM FIELDS: wording may only say a number the claim carries (a derived len() in the renderer
    # passed the audit only by coincidence until it did not — found when a count of 5 matched nothing)
    out.claims.append(Claim(FACT, "system.integrity", {"open": ordered, "n_open": len(ordered)}, "inspector.findings",
                            "integrity_ledger.jsonl", latest, max(latest, t)))
    return out


def adapt_integrity(records, bad_lines, checked_at, path="user_ledger.jsonl"):
    problems = ledger.verify(records)
    out = Adapted()
    t = _t(checked_at)
    out.claims.append(Claim(FACT, "system.ledger_integrity",
                            {"ok": not problems and bad_lines == 0, "records": len(records),
                             "problems": [list(p) for p in problems], "bad_lines": bad_lines},
                            "ledger.verify", f"ledger.verify({path})", t, t))
    return out


# ======================================================================
# CLAIM MATRIX — generated from the contract, so it cannot drift from it
# ======================================================================
# subject (or pattern name) -> (adapter function, as-of capable?, how)
ADAPTER_PRODUCES = {
    "market.data_timestamp":       ("adapt_state", False, "state.json is overwritten each scan: snapshot only"),
    "htf.bias":                    ("adapt_state", True, "current + state_history_log.jsonl snapshots (adapt_state_history), one per scan since it was deployed; nothing earlier"),
    "leg.owner":                   ("adapt_state", True, "current + state_history_log.jsonl snapshots (adapt_state_history), one per scan since it was deployed; nothing earlier"),
    "thesis.state":                ("adapt_state", False, "current-only"),
    "cautions":                    ("adapt_state", False, "current-only"),
    "thesis.status":               ("adapt_state", True,  "current + MIL's own dated transition history + state_history_log.jsonl snapshots"),
    "thesis.primary":              ("adapt_state", False, "current-only"),
    "thesis.counter":              ("adapt_state", False, "current-only"),
    "thesis.conflicting_evidence": ("adapt_state", False, "current-only"),
    "thesis.failure_condition":    ("adapt_state", False, "current-only; populated by MIL since 2026-10-01 (UNSET before)"),
    "thesis.invalidation":         ("adapt_state", False, "current-only (engine text + leg anchor)"),
    "thesis.phase":                ("adapt_state", True, "current + state_history_log.jsonl snapshots (adapt_state_history), one per scan since it was deployed; nothing earlier"),
    "thesis.trend_health":         ("adapt_state", False, "current-only"),
    "thesis.failure_risk":         ("adapt_state", False, "current-only"),
    "thesis.expected_next_event":  ("adapt_state", False, "current-only"),
    "volatility.state":            ("adapt_state", True, "current + state_history_log.jsonl snapshots (adapt_state_history), one per scan since it was deployed; nothing earlier"),
    "leg.age_status":              ("adapt_state", True, "current + state_history_log.jsonl snapshots (adapt_state_history), one per scan since it was deployed; nothing earlier"),
    "leg.macro":                   ("adapt_state", True, "current + state_history_log.jsonl snapshots (adapt_state_history), one per scan since it was deployed; nothing earlier"),
    "campaign.state":              ("adapt_state", False, "current-only"),
    "watch.zones":                 ("adapt_state", False, "current-only"),
    "signal.undelivered":          ("adapt_signal_events", True, "append-only log"),
    "event.recorded":              ("adapt_market_events", True, "append-only log (permanent; the pending queue is not)"),
    "leg.outcome":                 ("adapt_leg_outcomes", True, "append-only log"),
    "case.<id>.trade":             ("adapt_case", True, "ledger as_of"),
    "case.<id>.user_reasoning":    ("adapt_case", True, "ledger as_of"),
    "case.<id>.bot_plan":          ("adapt_case", True, "ledger as_of"),
    "case.<id>.outcome":           ("adapt_case", True, "ledger as_of"),
    "case.<id>.investigation":     ("adapt_case", True, "ledger as_of"),
    "thread.<id>.status":          ("adapt_threads", True, "ledger as_of"),
    "user.knows.<concept>":        ("adapt_knowledge", True, "ledger as_of"),
    "bus.statement.<subject>":     ("adapt_statements", True, "ledger as_of"),
    "user.terms":                  ("adapt_terms", True, "ledger as_of"),
    "alerts.open":                 ("adapt_alerts", True, "ledger as_of"),
    "research.cases":              ("adapt_research", True, "ledger as_of"),
    "system.ledger_integrity":     ("adapt_integrity", False, "a check run now, not a history"),
    "system.integrity":            ("adapt_findings", True, "append-only integrity ledger"),
}


def _generic(subject):
    """Template name for a concrete or templated slot subject."""
    s = subject
    for pre in ("case.", "thread."):
        if s.startswith(pre):
            rest = s[len(pre):]
            tail = rest.split(".", 1)[1] if "." in rest else ""
            return f"{pre}<id>.{tail}"
    if s.startswith("user.knows."):
        return "user.knows.<concept>"
    if s.startswith("bus.statement."):
        return "bus.statement.<subject>"
    return s


def claim_matrix():
    rows = []
    for intent in bc.INTENTS.values():
        for sl in intent.slots:
            g = _generic(sl.subject.replace("{about}", "<about>").replace("{case_id}", "x")
                         .replace("{thread_id}", "x").replace("{case_id}", "x"))
            found = bc.spec_for(sl.subject.replace("{case_id}", "X").replace("{thread_id}", "X")) \
                if "{about}" not in sl.subject else None
            spec = found[1] if found else None
            prod = ADAPTER_PRODUCES.get(g)
            rows.append({
                "intent": intent.name, "subject": sl.subject, "required": sl.required,
                "kinds": "/".join(sl.kinds) if len(sl.kinds) < 3 else "any",
                "authority": ", ".join(spec.authorities) if spec else "(depends on {about})",
                "source": (spec.source if spec and spec.source else
                           ("(depends on {about})" if not spec else "— ENGINE DOES NOT PROVIDE —")),
                "adapter": (prod[0] if prod else ("—" if spec else "(any)")),
                "as_of": ("yes" if prod and prod[1] else ("no" if prod else "—")),
                "if_missing": ("Missing -> PARTIAL" if sl.required else "Missing (optional)")
                              + (f"; stale after {sl.max_age_s}s" if sl.max_age_s else "")
                              + ("; needs basis" if sl.needs_basis else ""),
                "note": (prod[2] if prod else (spec.note if spec else ""))})
    return rows


def matrix_markdown():
    rows = claim_matrix()
    head = ("| Intent | Slot subject | Req | Kind | Authority | Source record | Adapter | As-of? | If missing |\n"
            "|---|---|---|---|---|---|---|---|---|\n")
    body = "".join(
        f"| {r['intent']} | `{r['subject']}` | {'yes' if r['required'] else 'no'} | {r['kinds']} | {r['authority']} | "
        f"{r['source']} | {r['adapter']} | {r['as_of']} | {r['if_missing']} |\n" for r in rows)
    notes = "".join(f"- `{r['subject']}` ({r['intent']}): {r['note']}\n" for r in rows if r["note"])
    return ("# BUS claim matrix (generated by `python bus_adapters.py --matrix` — do not edit)\n\n"
            "Every sentence BUS may say traces: intent -> slot -> claim -> authority -> source record. "
            "Rows marked ENGINE DOES NOT PROVIDE are engine work, not BUS work.\n\n" + head + body +
            "\n## Notes\n" + notes)


# ======================================================================
# CLI: verify the mapping against REAL files
# ======================================================================
def _read_json(path):
    with open(path) as f:
        return json.load(f)


def _read_jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return rows


def _check(root):
    import os
    st = _read_json(os.path.join(root, "state.json"))
    res = merge(adapt_state(st),
                adapt_signal_events(_read_jsonl(os.path.join(root, "signal_event_log.jsonl"))),
                adapt_market_events(_read_jsonl(os.path.join(root, "market_event_log.jsonl"))),
                adapt_leg_outcomes(_read_jsonl(os.path.join(root, "leg_obs_log.jsonl"))))
    print(f"claims: {len(res.claims)}   gaps: {len(res.gaps)}   errors: {len(res.errors)}")
    for c in res.claims:
        print(f"  CLAIM {c.subject:32s} {c.kind:14s} {json.dumps(c.value, default=str)[:70]}")
    for g in res.gaps:
        print(f"  GAP   {g.subject:32s} {g.state:13s} {g.detail}")
    for e in res.errors:
        print(f"  ERROR {e}")


if __name__ == "__main__":
    if "--matrix" in sys.argv:
        sys.stdout.write(matrix_markdown())
    elif "--check" in sys.argv:
        _check(sys.argv[sys.argv.index("--check") + 1] if len(sys.argv) > sys.argv.index("--check") + 1 else ".")
    else:
        print("usage: bus_adapters.py --matrix | --check <repo-dir with state.json>")
