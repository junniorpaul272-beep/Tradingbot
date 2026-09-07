"""
worldstate.py

Per chat (physiology work): WorldState is the organism's shared circulation
substrate — it carries explicitly-owned information between organs but does
not acquire authority over the information it carries. Physically living
inside min_scanner.py implied MIN was somehow authoritative over the whole
organism's state, which is wrong; MIN never even calls build_world_state()
itself (only scanner_live.py does, for Telegram commands). This module is a
pure relocation — no logic changed — moving build_world_state()/
format_world_state() out of MIN's file into their own neutral module.

Read-only, writes nothing. Aggregates facts that already have their own
authoritative owners elsewhere (LIVE's state.json, MIN's shadow/leg_obs/bank
logs) into one queryable shape. Does not decide what any of it means.
"""

from datetime import datetime, timezone

from scanner_common import EXPECTED_NEXT_EVENT_MAP
from min_scanner import (
    load_shadow_state, load_shadow_stats, load_leg_obs_state,
    load_market_intent_state, _read_bank_transactions,
)

WORLD_STATE_LIVE_INTERVAL_SEC = 5 * 60
WORLD_STATE_MIN_INTERVAL_SEC  = 5 * 60   # confirmed via min-scan.yml, not assumed
WORLD_STATE_STALE_MULTIPLIER  = 2         # per chat: "2x the owning process's own interval"


def _ws_freshness(iso_ts, interval_sec, now_utc):
    """PURE. Turns an ISO timestamp into a freshness record, or None if
    the timestamp is missing/unparseable — 'preserve absence' rather than
    fabricating an assumed-fresh default (per chat, WorldState review)."""
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts)
    except Exception:
        return None
    age_seconds = (now_utc - ts).total_seconds()
    return {
        "source_timestamp": iso_ts,
        "age_seconds": round(age_seconds, 1),
        "stale": age_seconds > (interval_sec * WORLD_STATE_STALE_MULTIPLIER),
    }


def build_world_state(state):
    """
    Assembles the WorldState tree (world_state_schema.md v0.2). `state` is
    LIVE's IN-MEMORY dict for the pass currently running — same reason
    format_market_thesis(state)/format_market_intent_report(state, ...)
    take `state` as a parameter instead of calling load_state() themselves:
    this pass's own state.json write may not have hit disk yet. Everything
    MIN-owns (shadow/bank/leg_obs) is read from disk, since this runs
    inside Live's process with no in-memory access to MIN's own objects.

    Read-only. Writes nothing. Returns a plain dict, not a new dataclass —
    this is an aggregation of things that already have their own types
    elsewhere, not a new kind of fact.
    """
    now_utc = datetime.now(timezone.utc)

    # ---- sources / freshness / coherence -------------------------------
    mi_state = load_market_intent_state()
    shadow_stats = load_shadow_stats()
    shadow_state_disk = load_shadow_state()
    leg_obs_state = load_leg_obs_state()

    last_bank_ts = None
    try:
        txns = _read_bank_transactions()
        if txns:
            last_bank_ts = txns[-1].get("resolved_at") or txns[-1].get("opened_at")
    except Exception:
        pass

    sources = {
        "state_json":         _ws_freshness(state.get("timestamp"), WORLD_STATE_LIVE_INTERVAL_SEC, now_utc),
        "market_intent_json": _ws_freshness(mi_state.get("_persisted_at"), WORLD_STATE_LIVE_INTERVAL_SEC, now_utc),
        "markov_json":        _ws_freshness(state.get("timestamp"), WORLD_STATE_LIVE_INTERVAL_SEC, now_utc),
        "shadow_json":        _ws_freshness(shadow_stats.get("_persisted_at"), WORLD_STATE_MIN_INTERVAL_SEC, now_utc),
        "leg_obs_json":       _ws_freshness(leg_obs_state.get("_persisted_at"), WORLD_STATE_MIN_INTERVAL_SEC, now_utc),
        "bank_jsonl":         _ws_freshness(last_bank_ts, WORLD_STATE_MIN_INTERVAL_SEC, now_utc),
    }
    stale_sources = [name for name, f in sources.items() if f and f.get("stale")]
    coherence = {"aligned": len(stale_sources) == 0, "stale_sources": stale_sources}

    # ---- phase (MarketPhase atomic facts) -------------------------------
    # transition_cause/aging_reason/break_count/dist_in_atr/swept_boundary/
    # volatility_hint are MarketPhase's own fields, but the only place
    # they're actually PERSISTED today is inside the MarketThesis snapshot
    # (market_thesis_* keys) — there is no separate market_phase_* store
    # for them. Sourced from there on purpose; don't add a second copy.
    phase = {
        "phase":               state.get("market_phase"),
        # BUG FIX (2026-08-26, per chat): this used to read state.get(
        # "macro_bias"), a key nothing in the codebase ever writes —
        # compute_macro_bias() only ever writes "macro_bias_confirmed".
        # world_state["phase"]["macro_bias"] was therefore always None,
        # which silently starved relate_timeframe_conflict() and
        # synthesize_market_understanding() in brain.py (both gate on
        # htf_bias in ("BULLISH","BEARISH")) — every /understand call
        # showed "bias=—", "5M read unavailable", and "HTF bias not
        # directional" regardless of the real bias.
        "macro_bias":          state.get("macro_bias_confirmed"),
        "macro_bias_confirmed": state.get("macro_bias_confirmed"),
        "macro_bias_stale":    state.get("macro_bias_stale"),
        "age_bars":            state.get("market_phase_age_bars"),
        "history":             state.get("market_phase_history"),
        "transition_cause":    state.get("market_thesis_transition_cause"),
        "aging_reason":        state.get("market_thesis_aging_reason"),
        "break_count":         state.get("market_thesis_break_count"),
        "dist_in_atr":         state.get("market_thesis_dist_in_atr"),
        "swept_boundary":      state.get("market_thesis_swept_boundary"),
        "volatility_hint":     state.get("market_thesis_volatility_hint"),
        # ADDED (2026-08-27, per audit — Phase 1 authority separation).
        # This is the SAME (phase, cause) -> text lookup Thesis's own
        # expected_next_event() used to produce market_thesis_expected_
        # next_event — but performed HERE, at WorldState-build time, off
        # the raw phase/transition_cause atoms above, not read back from
        # Thesis's already-committed conclusion. Deliberately NOT the
        # same field as market_thesis_expected_next_event (that field is
        # untouched, still computed, still what EXP7/delta-tracking read
        # — see EXPECTED_NEXT_EVENT_MAP's own docstring in scanner_common.py
        # for why it must not change shape). This is a fresh WorldState
        # atom so brain.py can use the map's default text WITHOUT
        # importing scanner_common directly (brain.py's module docstring:
        # "if Brain needs a fact WorldState doesn't contain, add it to
        # WorldState — not have this file quietly become a second
        # scanner"). Brain decides per-branch whether to use this default,
        # override it, or suppress it — see synthesize_market_
        # understanding()'s next_watch handling in brain.py.
        "default_next_event": EXPECTED_NEXT_EVENT_MAP.get(
            (state.get("market_phase"), state.get("market_thesis_transition_cause"))
        ),
        "macro_leg": {
            "direction":   state.get("macro_leg_direction"),
            "origin":      state.get("macro_leg_origin"),
            "origin_time": state.get("macro_leg_origin_time"),
            "extreme":     state.get("macro_leg_extreme"),
            # ADDED (per chat, "add rejections") — pure surfacing of
            # scanner_observation.py's _track_extreme_rejections()
            # output, same discipline as everything else in this dict:
            # WorldState carries what's already persisted, computes
            # nothing itself.
            "extreme_rejection_count": state.get("macro_leg_extreme_rejection_count"),
            "last_rejection_price":    state.get("macro_leg_last_rejection_price"),
            "last_rejection_at":       state.get("macro_leg_last_rejection_at"),
        } if state.get("macro_leg_direction") else None,
        "prior_macro_leg": {
            "direction": state.get("prior_macro_leg_direction"),
            "origin":    state.get("prior_macro_leg_origin"),
            "extreme":   state.get("prior_macro_leg_extreme"),
        } if state.get("prior_macro_leg_direction") else None,
        # Added 2026-08-25, per chat — see capture_prior_continuation_
        # snapshot()'s docstring in scanner_observation.py. Deliberately a
        # SIBLING field, not a replacement for prior_macro_leg above:
        # that one is the current swing's FOUNDING origin (frozen across
        # same-direction continuations, by design); this one is the leg
        # immediately before the MOST RECENT continuation — the fact that
        # was missing entirely for a campaign N continuations deep.
        "prior_continuation_leg": {
            "direction": state.get("prior_continuation_leg_direction"),
            "origin":    state.get("prior_continuation_leg_origin"),
            "extreme":   state.get("prior_continuation_leg_extreme"),
        } if state.get("prior_continuation_leg_direction") else None,
    }

    # ---- thesis (MarketThesis narrative) --------------------------------
    thesis = {
        "current_state":        state.get("market_thesis_current_state"),
        "transition_narrative": state.get("market_thesis_transition_narrative"),
        "trend_health":         state.get("market_thesis_trend_health"),
        "evidence":             state.get("market_thesis_evidence") or [],
        "weaknesses":           state.get("market_thesis_weaknesses") or [],
        "evidence_prose":       state.get("market_thesis_evidence_prose") or [],
        "weaknesses_prose":     state.get("market_thesis_weaknesses_prose") or [],
        # ADDED (2026-08-27, per chat — /understand repetition fix): see
        # MarketThesis.weakness_categories docstring in scanner_observation.py.
        "weakness_categories":  state.get("market_thesis_weakness_categories") or [],
        "expected_next_event":  state.get("market_thesis_expected_next_event"),
        "failure_risk":         state.get("market_thesis_failure_risk"),
        "failure_risk_reasons": state.get("market_thesis_failure_risk_reasons") or [],
        "failure_risk_reasons_prose": state.get("market_thesis_failure_risk_reasons_prose") or [],
        "invalidation":         state.get("market_thesis_invalidation"),
        "narrative":            state.get("market_thesis_narrative"),
        "confidence":           state.get("market_thesis_confidence"),
        "delta":                state.get("market_thesis_delta") or [],
        "mtf_15m":              state.get("market_thesis_mtf_15m"),
        "mtf_5m":               state.get("market_thesis_mtf_5m"),
        "campaign":             state.get("market_thesis_campaign"),
    }

    # ---- intent (MarketIntent) -------------------------------------------
    intent = {
        "has_content":       state.get("market_intent_has_content"),
        "watching_for":      state.get("market_intent_watch_codes") or [],
        "not_interested_in": state.get("market_intent_caution_codes") or [],
        "reasons":           state.get("market_intent_reasons") or [],
        "narrative":         state.get("market_intent_narrative"),
    }

    # ---- structure.system_observation — WorldState 1B, shipped 2026-08-20
    # (the "sensor layer": build_structure_digest(), scanner_observation.py)
    # v1 SCOPE, deliberately narrow per chat: structure + location +
    # liquidity sensors only, off the SAME MarketFacts instance the live
    # tiers evaluate against — movement/pressure sensors intentionally
    # deferred until this slice is validated against real charts. Written
    # by scanner_live.py every scan, independent of tier outcome (see
    # its own docstring for why that differs from live_tier_digest's
    # post-decision capture). Picked up here with no recomputation — see
    # world_state_schema.md's "don't let the digest become a second state
    # machine" note.
    structure = {"system_observation": state.get("structure_digest")}

    # ---- setups: live tiers + shadow (already persisted) --------------
    setups = {
        # Written every scan by build_live_tier_digest() (scanner_live.py)
        # — same unconditional-write pattern as structure_digest above,
        # independent of tier outcome. The old "None until Phase 1B" note
        # here was stale: Phase 1B shipped 2026-08-20 (see structure
        # comment above) and this has been populated ever since. Can
        # still legitimately be None on a genuine first-ever run before
        # any scan has completed, or if build_live_tier_digest() itself
        # raises (wrapped in its own try/except in scanner_live.py) —
        # but "not built yet" is no longer an accurate reason for None.
        "live_tiers": state.get("live_tier_digest"),
        "shadow_pending": shadow_state_disk.get("pending", []),
        "shadow_stats": {k: v for k, v in shadow_stats.items() if not k.startswith("_")},
    }

    # ---- evidence: Markov only at this global-snapshot level. Per-tier
    # similarity (compute_evidence) is contextual to a SPECIFIC setup, not
    # a standing fact about the market — intentionally left out of a
    # global dump; existing /shadow commands already expose it on demand.
    markov_data = load_markov_data()
    evidence = {
        "markov": {
            "current_state": state.get("markov_last_state"),
            "transitions": markov_data.get("transitions", {}),
        }
    }

    # ---- account: Bank aggregates ----------------------------------------
    # REAL DISCOVERY (not in the v0.2 doc — surfaced while implementing):
    # compute_bank_accounts() returns MULTIPLE named accounts (one per
    # Shadow policy variant, plus Forward Observation), not one flat
    # account. Schema doc needs a v0.3 correction for this — see reply.
    bank_accounts = compute_bank_accounts()
    account = {name: stats for name, stats in bank_accounts} if bank_accounts else None

    return {
        "generated_at": now_utc.isoformat(),
        "sources": sources,
        "coherence": coherence,
        "phase": phase,
        "thesis": thesis,
        "intent": intent,
        "structure": structure,
        "setups": setups,
        "evidence": evidence,
        "account": account,
        "conversation": None,      # reserved — Phase 3, not designed yet
        "trade_reasoning": None,   # reserved — Phase 3, not designed yet
    }


def format_world_state(world_state):
    """Telegram-friendly text rendering of build_world_state(). Deliberately
    a plumbing/debugging view (per chat) — NOT the eventual user experience.
    One fact per line, freshness/coherence at the bottom so a stale read is
    never silently presented as current."""
    ph = world_state["phase"]
    th = world_state["thesis"]
    it = world_state["intent"]
    sysobs = world_state["structure"]["system_observation"]

    lines = ["*World State*", ""]
    lines.append(f"Phase: {ph.get('phase') or '—'} ({ph.get('macro_bias') or '—'})")

    # Transition cause (per chat) — already computed by
    # classify_transition_cause() and already attached to `ph` by
    # build_world_state(); this was the one line missing to actually show
    # it. Human-readable labels, not raw enum values — swept_boundary only
    # shown for SWEEP_RECLAIM (the only cause it's set for).
    cause = ph.get("transition_cause")
    if cause:
        cause_labels = {
            "FRESH_BOS":            "fresh same-direction break, no flip",
            "CHOCH":                "direction flip (CHoCH)",
            "SWEEP_RECLAIM":        "liquidity swept and reclaimed",
            "BIAS_FLIP":            "macro bias flipped",
            "EMA_EXHAUSTION":       "aging out (EMA distance/break count)",
            "VOLATILITY_EXPANSION": "volatility expanding",
            "VOLATILITY_COLLAPSE":  "volatility contracting",
            "FAILED_CONTINUATION":  "failed continuation — leg invalidated, nothing fresh replaced it",
            "UNKNOWN":              "unclear",
        }
        cause_line = f"Why: {cause_labels.get(cause, cause)}"
        if cause == "SWEEP_RECLAIM" and ph.get("swept_boundary"):
            cause_line += f" ({ph['swept_boundary']})"
        lines.append(cause_line)

    lines.append(f"Thesis: {th.get('current_state') or '—'}")
    if ph.get("macro_leg"):
        leg = ph["macro_leg"]
        lines.append(f"Active 1H leg: {leg.get('direction')} from {leg.get('origin')}")
    lines.append(f"Intent: {it.get('narrative') or '—'}")

    if sysobs is None:
        # Phase 1B shipped 2026-08-20 and writes this every scan
        # unconditionally (see structure_digest's own comment) — reaching
        # this branch now means build_structure_digest() raised THIS
        # scan (it's wrapped in its own try/except) or this is state from
        # before the feature shipped, not that the feature is unbuilt.
        lines.append("Structure: _unavailable this scan — check "
                      "[STRUCTURE DIGEST ERROR] in logs if this persists_")
    else:
        ob = sysobs.get("order_block")
        fvg = sysobs.get("fvg")
        lines.append("Order block: " + ("present" if ob and ob.get("present") else "none"))
        lines.append("FVG: " + (f"{fvg.get('distance_pips')} pips away" if fvg and fvg.get("present") else "none"))

    accounts = world_state.get("account")
    if accounts:
        best_name, best = max(accounts.items(), key=lambda kv: kv[1].get("return_pct") or 0)
        lines.append(f"Bank (best: {best_name}): {best.get('return_pct')}% return, {best.get('n_trades')} trades")
    else:
        lines.append("Bank: not enough resolved history yet")

    lines.append("")
    stale = world_state["coherence"]["stale_sources"]
    lines.append("Coherence: aligned" if not stale else f"Coherence: STALE → {', '.join(stale)}")
    for name, f in world_state["sources"].items():
        if f is None:
            lines.append(f"  {name}: no timestamp yet")
        else:
            age_min = round(f["age_seconds"] / 60, 1)
            flag = " ⚠" if f["stale"] else ""
            lines.append(f"  {name}: {age_min}m old{flag}")

    return "\n".join(lines)
