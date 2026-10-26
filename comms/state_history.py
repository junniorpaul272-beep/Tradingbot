"""state_history.py -- the per-scan history of the engine's state (Phase C item 7).

WHY: state.json is overwritten every scan, so it can only say what is true NOW. This module appends
one compact, hash-verified record per successful scan to state_history_log.jsonl, so a question about
an earlier moment can be answered from what was recorded THEN, never from today's file.

HOW IT RUNS: as its own workflow step right after the scanner (like inspector.py). It is the ONLY
writer of state_history_log.jsonl. The engine does not know it exists and nothing imports it.
A step that follows a failed scan step is skipped by the workflow, so a failed scan appends nothing.

SAFEGUARDS (each has a test, with a mutation where one is meaningful)
  * No state / unreadable state / no market_data_timestamp  -> nothing is written.
  * The same scan twice -> one record. Identity is `snapshot_key` = hash(market_data_timestamp,
    state.timestamp, scan_id); a scan that returned early leaves state.json untouched, so it repeats
    the key and is skipped (it is not a new observation).
  * Every record has two times: ts = market_data_timestamp (when the state applies) and recorded_at
    (when this step wrote it). as_of(T) needs BOTH <= T. Nothing is ever fabricated for a time with
    no record: snapshot_as_of() returns None, and the adapter then emits nothing.
  * A mistake is fixed by append_correction(): a new record that `corrects` the old one. The original
    stays on disk and is still what as_of(T) shows for any T before the correction was recorded.
  * Missing is not false and not zero: an absent field is stored as null, and the adapter turns null
    into an explicit NOT_PROVIDED/UNKNOWN/STALE gap, never into a value.

The payload is deliberately COMPACT and holds no engine prose (no narratives, no trend-health text).
`state_hash` fingerprints the full state.json at that moment, so a later reader can tell that two
records came from different states even though the full file is not kept.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import ledger

LOG_NAME = "state_history_log.jsonl"
SNAPSHOT = "STATE_SNAPSHOT"
PAYLOAD_SCHEMA = 1
BIAS_VALUES = ("BULLISH", "BEARISH", "CONSOLIDATION")   # same domain as bus_adapters.BIAS_VALUES (a test pins them)
DEDUPE_TAIL_BYTES = 256_000


# Direction leaves recorded verbatim, so a flip can be traced afterwards. Explicit paths, not a scan of the
# state: recorded copies of the past (history lists, prior_*, stored signal events) are deliberately NOT here.
DIRECTION_PATHS = (
    "macro_bias_confirmed", "macro_leg_direction", "structure_digest.regime.macro_bias",
    "structure_digest.structure.bos_direction", "campaign_direction", "market_thesis_campaign.direction",
    "leg15_direction", "market_thesis_mtf_5m.m5_direction",
    "shadow_macro_bias_confirmed", "shadow_macro_leg_direction", "shadow_minleg_bias_confirmed",
    "shadow_minleg_leg_direction", "mil_understanding.primary_thesis.direction",
)


def _dig(state, path):
    cur = state
    for k in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=str)


def state_hash(state):
    return hashlib.sha256(_canon(state).encode("utf-8")).hexdigest()[:16]


def snapshot_key(state):
    body = [state.get("market_data_timestamp"), state.get("timestamp"), state.get("scan_id")]
    return hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()[:16]


def extract(state):
    """state.json dict -> compact payload, or None if the state cannot be placed in time."""
    if not isinstance(state, dict) or not state.get("market_data_timestamp"):
        return None
    try:
        ledger.to_utc(state["market_data_timestamp"])
    except ledger.LedgerError:
        return None
    regime = ((state.get("structure_digest") or {}).get("regime") or {})
    bias = regime.get("macro_bias")
    mil = state.get("mil_understanding") if isinstance(state.get("mil_understanding"), dict) else {}
    pt, ct = mil.get("primary_thesis"), mil.get("counter_thesis")
    lo = state.get("leg_owner")
    age = state.get("market_leg_age_status")
    macro_ok = (state.get("macro_leg_direction") in ("BULLISH", "BEARISH")
                and _num(state.get("macro_leg_origin")) and _num(state.get("macro_leg_extreme")))
    return {
        "payload_schema": PAYLOAD_SCHEMA,
        "snapshot_key": snapshot_key(state),
        "state_hash": state_hash(state),
        "code_version": state.get("code_version"),
        "market_data_timestamp": state.get("market_data_timestamp"),
        "state_persisted_at": state.get("timestamp"),
        "htf_bias": bias if bias in BIAS_VALUES else None,
        "regime_digest_current": (regime.get("as_of_scan_id") == state.get("scan_id")
                                  if regime.get("as_of_scan_id") is not None and state.get("scan_id") is not None else None),
        "htf_bias_stale": regime.get("macro_bias_stale") if isinstance(regime.get("macro_bias_stale"), bool) else None,
        "market_phase": state.get("market_phase"),
        "market_phase_age_bars": state.get("market_phase_age_bars"),
        "thesis_state": state.get("market_thesis_current_state"),
        "failure_risk": state.get("market_thesis_failure_risk"),
        "volatility_hint": state.get("market_thesis_volatility_hint"),
        "break_count": state.get("market_thesis_break_count"),
        "leg_age_status": ({k: age.get(k) for k in ("status", "aging_reason", "break_count", "dist_in_atr")}
                           if isinstance(age, dict) and age.get("status") else None),
        "macro_leg": ({"direction": state["macro_leg_direction"], "origin": state["macro_leg_origin"],
                       "extreme": state["macro_leg_extreme"], "origin_time": state.get("macro_leg_origin_time")}
                      if macro_ok else None),
        "leg_owner": ({k: lo.get(k) for k in ("leg_id", "tier", "status", "upgraded", "claimed_at")}
                      if isinstance(lo, dict) and lo.get("claimed_at") else None),
        "mil_status": mil.get("thesis_status"),
        "mil_primary_direction": pt.get("direction") if isinstance(pt, dict) else None,
        "mil_counter_direction": ct.get("direction") if isinstance(ct, dict) else None,
        "momentum": _momentum(state.get("momentum_state"), state.get("scan_id")),
        "direction_fields": {p: (_dig(state, p) if _dig(state, p) in BIAS_VALUES else None) for p in DIRECTION_PATHS},
    }


def _momentum(m, scan_id):
    """Raw momentum.state readings, verbatim (value, status, reason) plus what produced them. No labels: labels are
    derived later from frozen thresholds, so the history keeps only what was measured. None when absent."""
    if not isinstance(m, dict) or not m.get("version"):
        return None
    out = {k: m.get(k) for k in ("version", "threshold_version", "timeframe", "lookback_bars", "asof_candle_open",
                                 "asof_candle_close", "scan_time", "forming_bar_excluded", "bars_used")}
    for k in ("direction", "strength", "persistence", "expansion"):
        r = m.get(k) if isinstance(m.get(k), dict) else {}
        out[k] = {"status": r.get("status"), "value": r.get("value"), "reason": r.get("reason")}
    out["current"] = (m.get("as_of_scan_id") == scan_id) if m.get("as_of_scan_id") is not None and scan_id is not None else None
    return out


def _recent_keys(path):
    recs, _bad, _trunc = ledger.read_tail(path, DEDUPE_TAIL_BYTES)
    return {(r.get("payload") or {}).get("snapshot_key") for r in recs if r.get("event_type") == SNAPSHOT}


def _is_duplicate(path, key):
    return key in _recent_keys(path)


def record(root=".", now=None):
    """One step run. Returns {'status': ...}; never raises, never repairs."""
    now = ledger.to_utc(now) if now is not None else datetime.now(timezone.utc)
    try:
        with open(os.path.join(root, "state.json")) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {"status": "no_state"}
    payload = extract(state)
    if payload is None:
        return {"status": "unusable_state"}
    path = os.path.join(root, LOG_NAME)
    if _is_duplicate(path, payload["snapshot_key"]):
        return {"status": "duplicate", "snapshot_key": payload["snapshot_key"]}
    try:
        rec = ledger.append(path, SNAPSHOT, payload, ts=payload["market_data_timestamp"],
                            scan_id=state.get("scan_id"), now=now)
    except ledger.LedgerError as e:
        return {"status": "refused", "reason": str(e)}
    if rec is None:
        return {"status": "write_failed"}
    return {"status": "recorded", "event_id": rec["event_id"], "snapshot_key": payload["snapshot_key"]}


def append_correction(path, event_id, fixes, reason, now=None):
    """Append a record that supersedes `event_id`. The original is untouched. `fixes` overrides payload
    fields; the correction keeps the original's ts (the state it describes did not move in time)."""
    if not reason or not isinstance(reason, str):
        raise ledger.LedgerError("a correction needs a reason")
    recs, _ = ledger.read(path)
    orig = next((r for r in recs if r["event_id"] == event_id and r["event_type"] == SNAPSHOT), None)
    if orig is None:
        raise ledger.LedgerError(f"no snapshot {event_id} to correct")
    payload = dict(orig["payload"]); payload.update(fixes)
    payload["correction_reason"] = reason
    return ledger.append(path, SNAPSHOT, payload, ts=orig["ts"], scan_id=orig.get("scan_id"),
                         corrects=event_id, now=now)


def snapshot_as_of(records, T):
    """The latest non-superseded snapshot the system could have known at T (BOTH times <= T, and a
    correction counts only if it too was known by T). None if there is none: never a made-up past."""
    view = [r for r in ledger.view_as_of(records, T)
            if r["event_type"] == SNAPSHOT and r["superseded_by"] is None]
    if not view:
        return None
    best = max(view, key=lambda r: (ledger.to_utc(r["ts"]), ledger.to_utc(r["recorded_at"])))
    T = ledger.to_utc(T)
    return {"record": best, "age_s": int((T - ledger.to_utc(best["ts"])).total_seconds())}


if __name__ == "__main__":
    out = record(sys.argv[1] if len(sys.argv) > 1 else ".")
    print("[STATE HISTORY]", json.dumps(out))
