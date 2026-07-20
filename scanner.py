"""
GBPUSD SMC Scanner V3 — "Rule of Law" Edition
================================================
STAGE 3 — Tiers are fully implemented (no longer stubs), FVG has been
removed from every LIVE decision path, and a full Market Intelligence
Network (MIN) has been added alongside the live bot.

ARCHITECTURE
------------
    Market
      |
    MACRO BIAS          <- ONE authority: 1H timeframe. PURE — returns
      |                    (bias, state_updates), never mutates state.
    MARKET CONTEXT       <- Is the environment tradeable right now? PURE
      |                    for the same reason.
    MARKET FACTS          <- Pure OBSERVATION. Knows BOS, CHoCH, Order
      |                    Blocks (bias-locked demand/supply zones),
      |                    liquidity sweeps, swings, fib — and nothing
      |                    about trading. Tiers ask it questions
      |                    ("facts.has_order_block()"); it never says
      |                    "this is a buy." NOTE: FVG is intentionally
      |                    NOT exposed here — see FVG note below.
    RULE OF LAW           <- Arbitration only. Decides WHICH tier owns
      |                    the current leg. Higher-priority tiers may
      |                    UPGRADE (steal) a WATCHING lower-priority
      |                    owner; nothing can steal from a FIRED owner;
      |                    each leg can be upgraded at most once.
      +-- TIER 1: Premium POI Reaction    (Order Block only, no FVG)
      +-- TIER 2: HTF Fib Pullback
      +-- TIER 3: Structure Confirmation
      |
    TRADE MANAGEMENT      <- Risk gate, dedup backstop, alerting,
                             active-trade freeze.
      |
    MARKET INTELLIGENCE   <- Research only, never touches a live
    NETWORK (MIN)            decision. Runs every scan, independently of
                             what the live bot did. Renamed from "Shadow
                             Pipeline" (per chat) into named departments,
                             each with one job — nothing here was
                             discarded, only reorganized and, in one
                             case, merged:
                               - Intelligence Database:  the permanent
                                 record (SHADOW_TRADE_LOG_FILE — file
                                 name unchanged on purpose, see below).
                               - Evidence & Research Department: proves
                                 things about resolved trades. Merged
                                 from what were separately drafted as
                                 "Evidence", "Research", and "Historical
                                 Analysis" departments — all three were
                                 mechanically the same operation (find
                                 similar past setups, report facts), so
                                 keeping them apart would have meant two
                                 near-identical systems for one job.
                               - Experimental Lab: EXP1-7, the filter/
                                 variant A-B tests.
                             NOT YET BUILT (parked pending 2 weeks of
                             clean data — see chat): Forward Observation
                             (predicts the next structural event, not
                             win/loss) and the Failure Investigation
                             Bureau (per-loss fingerprint deviation
                             report) — both are next in line, in that
                             priority order, once there's enough data
                             to make either one meaningful.
                             See the "MARKET INTELLIGENCE NETWORK"
                             section for the full department/experiment
                             list.

FVG — LIVE vs SHADOW
---------------------
Personal preference baked into this build: the live bot never trades an
FVG. Its only POI is the Order Block, which is already bias-locked (a
BULLISH leg can only ever produce a demand zone, a BEARISH leg only ever
a supply zone — see detect_order_block()). FVG detection still exists
(detect_fvg / detect_significant_fvg), but it is used ONLY by the Shadow
Pipeline's Experiment 3 (POI), and even there it's filtered for quality
(FVG_MIN_SIZE_ATR_MULT / FVG_MAX_AGE_CANDLES) — not every 3-candle gap
gets logged, only ones big and fresh enough to plausibly matter.

WHAT CHANGED SINCE STAGE 1
-----------------------------
1. MARKET FACTS LAYER (new). Tiers will receive a `MarketFacts` object,
   never raw df_5m/df_15m/df_1h. If a tier needs new information about
   the market, the fix is always "add a fact/method to MarketFacts,"
   never "reach into the dataframes from inside a tier."

2. PURITY. `compute_macro_bias()` and `evaluate_market_context()` no
   longer mutate the `state` dict — they return (result, updates), and
   the caller applies updates via `apply_state_updates()`, the ONLY
   function that writes computed results into `state`. This also fixed
   a second hidden mutation Stage 1 had: mutating the caller's df_5m
   dataframe in place to attach an ATR column. Every ATR series is now
   computed locally by whichever function needs it.

   Note: `manage_active_trade()`, `apply_leg_ownership()`, and the
   `claim/release` helpers are still *intentionally* imperative — they
   ARE the "apply this decision" step, not a "compute a value" step.
   Purity is for functions that compute a result; functions whose whole
   job is "make this side effect happen" (send a Telegram message, write
   leg ownership, close a trade) stay direct and clearly named as such.

3. OWNERSHIP UPGRADE. Stage 1's Rule of Law was strictly monopolistic —
   once a tier claimed a leg, nothing else was ever evaluated again
   until that tier released it. Now: while the owner's status is
   WATCHING (never FIRED — an open/locked trade is untouchable) and the
   leg hasn't already been upgraded once, every tier with HIGHER
   priority than the current owner gets evaluated too; the first one
   that activates steals ownership. Lower-priority tiers never get a
   chance to steal, and no leg can be upgraded more than once (guards
   against ownership ping-ponging on a choppy leg).

AUDIT FIXES (post-Stage-3 review)
-----------------------------------
Each confirmed with a targeted test before/after; see chat history for
the actual test runs.
  A. Tier 1 and Tier 2 could never actually reach a WATCHING state —
     `activated` and `fired` were the same condition, so a leg went
     straight from unclaimed to FIRED in one step. That silently made
     the entire ownership-upgrade mechanism above unreachable (nothing
     was ever left in WATCHING to upgrade FROM). Fixed by splitting
     "structural conditions met, worth claiming" (activated) from
     "rejection_candle() also confirmed" (fired) in both tiers. Tier 3
     intentionally stays atomic — see the comment in its evaluate()
     for why that's correct, not an oversight.
  B. detect_order_block() matched candles by price overlap ONLY, with
     no recency constraint — an unrelated candle from hours earlier
     sharing the same price band could out-rank the real, current
     displacement candle and get selected instead. Fixed by adding
     origin_idx to detect_bos_impulse()'s return value and using it to
     bound the order-block search to candles at or after the leg's own
     origin. MarketFacts.order_block() now passes the same lookback
     window bos_15m() used, so the index lines up correctly.
  C. Experiment 4 and Experiment E's shadow leg_id included a value
     that changes almost every scan (a candle timestamp / rounded
     entry price), which defeated log_shadow_setup's dedup entirely —
     the same real-world event was being logged as several separate,
     correlated "trades," quietly inflating the research stats. Fixed
     by anchoring both to stable leg/level identity instead, matching
     how Experiments 1/2/3/6 already dedup.
  D. SHADOW_ABLATION_VARIANTS's comment for "no_ema_agreement" described
     a different proxy than what experiment_5_filter_ablation actually
     implements — comment corrected to match the code.
"""

import os
import json
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# =========================================================================
# CREDENTIALS
# =========================================================================
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_DATA_KEY   = os.environ["TWELVE_DATA_KEY"]


# =========================================================================
# CONFIG — grouped by the architectural layer that owns each constant.
# =========================================================================

# ---- GLOBAL ---------------------------------------------------------------
PAIR      = "GBP/USD"
PIP_SIZE  = 0.0001
RR_RATIO  = 3.0

DIAGNOSTIC_MODE = os.environ.get("DIAGNOSTIC_MODE", "1") != "0"

STATE_FILE           = "state.json"
STATE_MAX_AGE_HOURS   = 6
STATS_FILE            = "stats.json"

# ---- DATA QUALITY -----------------------------------------------------
DATA_SPIKE_ATR_MULT           = 8
FRESHNESS_MAX_CANDLE_AGE_MULT = 3
# Tied to atr() period (14) — the actual number of bars a corrupted bar
# stays inside the rolling ATR window and keeps skewing fib zones,
# break buffers, and adaptive thresholds downstream. Not an arbitrary
# lookback like the old tail(5).
DATA_SPIKE_BLOCK_BARS         = 14

# ---- MACRO BIAS (1H — the ONE authority for direction) --------------------
HTF_BIAS_MIN_BARS          = 100
HTF_EMA_SLOPE_BARS         = 10
HTF_CONSOLIDATION_ATR_MULT = 0.5
HTF_EMA_FLAT_THRESHOLD     = 0.15
HTF_STRUCTURE_WING         = 3
PROMOTION_MIN_BREAK_COUNT  = 2

# ---- MARKET CONTEXT (is the environment tradeable right now?) -------------
ATR_MIN_PIPS = 6

REGIME_SHIFT_ENABLED      = True
REGIME_SHIFT_SHORT_PERIOD = 5
REGIME_SHIFT_LONG_PERIOD  = 50
REGIME_SHIFT_THRESHOLD    = 2.0
REGIME_SHIFT_OPEN_WARMUP  = 6
POST_SPIKE_COOLDOWN_BASE  = 2
POST_SPIKE_COOLDOWN_SCALE = 1.5
POST_SPIKE_COOLDOWN_MAX   = 10

SESSION_WINDOWS_UTC = [
    (7, 16),   # London
    (12, 21),  # New York
]

# ---- STRUCTURE PRIMITIVES (shared building blocks for MarketFacts) --------
FRACTAL_WING         = 2
INVALIDATION_RETRACE = 0.786
SWING_LOOKBACK_15    = 48

BOS_15M_BREAK_BUFFER_ATR_MULT = 0.15
STATE_MEMORY_MAX_DRIFT_PIPS   = 80

ZONE_TOLERANCE_PIPS   = 3
ENGULF_TOLERANCE_PIPS = 1
ATR_ENGULF_MIN        = 0.4
ENGULF_CLOSE_LOCATION_MIN = 0.5

FIB_ZONE_NEAR = 0.382
FIB_ZONE_FAR  = 0.618
FIB_SCORE_MIN_FRACTION = 5 / 19

SWEEP_LOOKBACK_CANDLES  = 3
SWEEP_MAX_DISTANCE_PIPS = 6

# ---- ORDER BLOCK (Tier 1's structural primitive) ---------------------------
OB_MIN_DISPLACEMENT_ATR_MULT = 1.5
OB_OPPOSING_LOOKBACK_CANDLES = 5

# ---- FAIR VALUE GAP (SHADOW-ONLY — never used by a live tier) -------------
# Personal preference: the live bot only ever trades a high-probability
# Order Block (a demand zone in a BULLISH leg, a supply zone in a BEARISH
# leg — see detect_order_block, which is already bias-locked because it's
# derived from the BOS leg that IS the bias). FVG is kept only as a
# research signal in the Shadow Pipeline (Experiment 3 — POI), and even
# there it's filtered for "quality" — not every 3-candle gap gets logged.
FVG_LOOKBACK_CANDLES       = 20    # how far back a gap is still considered "live"
FVG_MIN_SIZE_ATR_MULT      = 0.5   # gap must be at least this many ATR(15m) wide
FVG_MAX_AGE_CANDLES        = 12    # gap must have formed within this many candles (freshness)

# ---- RULE OF LAW (arbitration — routing only, no quality judgment) --------
TIER_PRIORITY = ["TIER_1_POI", "TIER_2_FIB", "TIER_3_STRUCTURE"]
LEG_MATCH_TOLERANCE_PIPS = 5

# ---- CONVICTION (Phase 3 — replaces the old score>=X? implicit gate) ------
# UNVALIDATED PLACEHOLDERS. Every number below is a first guess, not a
# derived threshold — do not treat these as final. Calibrate against
# resolved journal/shadow R-outcomes (Experiment 5 ablation + Experiment E
# rejected-live hypothetical R) before trusting them with real sizing.
# Activation (mandatory gates, per tier's own evaluate()) is UNCHANGED and
# stays a hard pass/fail. Conviction only decides what an ACTIVATED setup
# is allowed to do: FIRE at what size, WATCH, or REJECT outright.
CONVICTION_MIN_BY_TIER = {
    "TIER_1_POI":       65,
    "TIER_2_FIB":        60,
    "TIER_3_STRUCTURE":  75,
}

# Management bands keyed by score, independent of which tier. Applied only
# AFTER a tier clears its own CONVICTION_MIN_BY_TIER — a setup below its
# tier's minimum never reaches this table, it's REJECTED before sizing is
# even considered.
#   size_mult    — suggested position-size multiplier (informational; this
#                  bot sends signals, it doesn't place sized orders, so
#                  this rides in the Telegram message for you to apply)
#   target_r     — replaces the flat RR_RATIO for TP placement
#   partial_r    — take partials at this R (None = no partial)
#   breakeven_r  — move SL to breakeven at this R (None = don't)
CONVICTION_MANAGEMENT_BANDS = [
    # (score_floor, label,          size_mult, target_r, partial_r, breakeven_r)
    (90,  "FULL_EXTENDED",  1.0, 3.0, 2.0, 1.0),
    (80,  "FULL",           1.0, 3.0, 2.0, 1.0),
    (70,  "NORMAL",         1.0, 2.5, 1.5, 1.0),
    (0,   "CONSERVATIVE",   0.5, 2.0, None, 0.5),
]


def classify_conviction(tier_label, score):
    """
    Phase 3 — Execution. Pure function: (tier, score) -> decision. Does
    NOT gate activation (that's each tier's own mandatory-condition
    check) — only decides whether an ACTIVATED, structurally-confirmed
    setup is allowed to fire, and if so, how it should be managed.

    Returns a dict, always populated with `reason` so a REJECT/WATCH is
    just as legible in the diagnostic report as a FIRE.
    """
    minimum = CONVICTION_MIN_BY_TIER.get(tier_label, 100)
    below = score < minimum

    band = None
    for floor, label, size_mult, target_r, partial_r, breakeven_r in CONVICTION_MANAGEMENT_BANDS:
        if score >= floor:
            band = {
                "band_label": label, "size_mult": size_mult, "target_r": target_r,
                "partial_r": partial_r, "breakeven_r": breakeven_r,
            }
            break

    if below:
        return {
            "decision": "REJECT", "score": score, "minimum": minimum,
            "reason": f"{tier_label} conviction {score} < minimum {minimum} — activated but not convincing enough",
            **(band or {"band_label": None, "size_mult": None, "target_r": None,
                        "partial_r": None, "breakeven_r": None}),
        }

    return {
        "decision": "FIRE", "score": score, "minimum": minimum,
        "reason": f"{tier_label} conviction {score} >= minimum {minimum} — {band['band_label']} band",
        **band,
    }

# ---- MARKET INTELLIGENCE NETWORK: Intelligence Database -------------------
# "What could we have learned from this setup?" Every experiment below
# logs setups the live bot would never touch (or touches for a different
# reason), and tracks them forward to 1R/2R/3R so the STATS answer the
# question, not a gut feeling. Nothing here can ever set stats["active_trade"]
# or state["leg_owner"] — it is strictly read-only against live state.
#
# Renamed from "Shadow Pipeline" to "Market Intelligence Network" (per
# chat) — pure reorganization/documentation, zero logic changes. File
# names below are DELIBERATELY left unchanged even though they still say
# "shadow" — renaming them would orphan every trade already logged on
# disk under the old names. The constant names stay for the same reason;
# only the surrounding language and section banners changed.
SHADOW_STATE_FILE = "shadow_state.json"
SHADOW_STATS_FILE = "shadow_stats.json"
# Permanent, append-only, NEVER overwritten wholesale (unlike the two
# files above, which are full-state snapshots rewritten every scan).
# Every resolved shadow trade — win, loss, or timeout — gets one line
# appended here forever. This IS the Intelligence Database: the actual
# raw dataset the ATR suitability analysis (compute_atr_suitability) and
# the Evidence & Research Department both read from, and it survives
# even if shadow_state.json/shadow_stats.json were ever lost, reset, or
# corrupted.
SHADOW_TRADE_LOG_FILE = "shadow_trade_log.jsonl"
BIAS_AB_LOG_FILE  = "bias_ab_log.json"    # live (gated) vs shadow (old-rule) 1H bias, for /biasab
BIAS_AB_LOG_MAX_ENTRIES = 500

# Maps a live tier label to the plain tier number used in ATR-band
# analysis and Telegram output (Tier: 1 / Tier: 2 / Tier: 3).
TIER_NUMBER = {"TIER_1_POI": 1, "TIER_2_FIB": 2, "TIER_3_STRUCTURE": 3}
ATR_SUITABILITY_BAND_WIDTH_PIPS = 1.0   # bucket width for the ATR band table

JOURNAL_MAX_ENTRIES = 100

SHADOW_MAX_PENDING_BARS   = 200   # ~16.6h of 5M bars before a stale shadow setup is force-resolved
SHADOW_MAX_PENDING_PER_EXPERIMENT = 20   # safety valve against runaway logging

# ---- MARKET INTELLIGENCE NETWORK: Experimental Lab -------------------------
# "What if...?" — every question below becomes an experiment, tracked
# forward the same way as everything else in the Database.
#
# Experiment 5 (Filter Ablation) — each variant strips exactly ONE filter
# from an otherwise-Tier-3-shaped structure setup, so any R-multiple
# difference vs Experiment 1 is attributable to that ONE filter.
SHADOW_ABLATION_VARIANTS = [
    "no_liquidity_sweep",   # Tier 3 shape but sweep NOT required
    "no_ema_agreement",     # ignore _promotion_confirmed-style EMA filter
                              # (proxy actually implemented: log even while
                              # macro_bias_stale is True — see
                              # experiment_5_filter_ablation. Comment fixed
                              # during audit; previously described a
                              # different, unimplemented proxy.)
    "choch_only",           # require CHoCH specifically, not any BOS
    "bias_15m",             # use 15M structure direction instead of 1H macro_bias
]

# ---- TRADE MANAGEMENT (shared regardless of which tier fired) -------------
SL_ATR_MULT            = 1.5
SL_MIN_PIPS             = 5
SL_VOL_SPIKE_RATIO      = 1.5
SL_ATR_MULT_COMPRESSED  = 1.2

MAX_RISK_ATR_MULT = 3.0
MAX_RISK_PIPS     = 45

WATCHING_EXIT_PIPS             = 8
TRADE_STATUS_UPDATE_MINUTES    = 30
NEUTRAL_WATCH_COOLDOWN_MINUTES = 60
NEUTRAL_WATCH_MIN_RETRACE      = FIB_ZONE_FAR

RESULT_TRACKING_ENABLED = True
STATS_SUMMARY_EVERY     = 50

# ---- MARKET INTELLIGENCE NETWORK: Evidence & Research Department ----------
# Merged department (per chat) — absorbs what were separately proposed as
# "Evidence", "Research", and "Historical Analysis": all three asked the
# same underlying question ("have I seen something like this before, and
# what happened?"), just phrased differently. One department, one method,
# rather than two/three near-identical systems doing the same lookup.
#
# Read-only annotation layer. Looks up EXP7_TIER_ATR's permanent, per-tier
# resolved trade log (already tracks real R outcomes for every ACTIVATED
# tier setup regardless of whether it fired live) for setups whose
# STRUCTURAL FACTS — not scores — match the current one, and reports what
# actually happened. This NEVER touches `fired`, `decision`, sizing, or
# classify_conviction. It cannot override Rule of Law even by accident —
# see compute_evidence()'s docstring and how it's wired into scan().
#
# Dormant by design: below EVIDENCE_MIN_N similar resolved trades, nothing
# is shown at all. An n=7 "71% win rate" is noise wearing a lab coat, not
# evidence — see chat history for why this gate exists.
EVIDENCE_MIN_N = 50

EVIDENCE_STRENGTH_BANDS = [
    (200, "VERY_STRONG"),
    (100, "STRONG"),
    (50,  "MODERATE"),
]

# Only these boolean-valued keys from each tier's own `breakdown` dict are
# compared for similarity — NOT the *_bonus/score-derived keys alongside
# them (comparing those too would double-count the same underlying fact
# and inflate apparent similarity). Fewer, honest dimensions beat many
# correlated ones — this is the direct fix for the "25 features fake
# precision" problem raised in chat.
TIER_EVIDENCE_KEYS = {
    "TIER_1_POI":       ["order_block", "rejection_candle", "fresh_bos_aligned", "choch"],
    "TIER_2_FIB":        ["in_fib_zone", "rejection_candle", "bos_aligned", "liquidity_sweep"],
    "TIER_3_STRUCTURE":  ["choch", "bos_aligned", "liquidity_sweep"],
}
# A historical record must agree with the live setup on this fraction of
# its tier's own keys to count as "similar". Set to an EXACT match
# (1.0) deliberately: with only 3-4 comparable keys per tier, any looser
# threshold lets a single mismatched fact slip through as "similar" (e.g.
# 3/4 keys agreeing = 0.75, which would silently equate a CHoCH setup
# with a non-CHoCH one). Found via testing — see chat. Fewer, exactly-
# matched facts beat many loosely-matched ones.
EVIDENCE_MATCH_THRESHOLD = 1.0


# =========================================================================
# STATE / STATS PERSISTENCE
# =========================================================================
_REMOVE = object()   # sentinel: apply_state_updates() pops the key instead
                        # of setting it when a computed update uses this


def apply_state_updates(state, updates):
    """
    The ONLY function that writes a compute_*() function's returned
    result into the live `state` dict. Every compute_macro_bias() /
    evaluate_market_context() / tier evaluate() call returns a plain
    (result, updates) pair; this is what actually applies `updates`.
    A value of _REMOVE deletes that key instead of setting it — this is
    how a pure function expresses "this should no longer be in state"
    without being able to call state.pop() itself.
    """
    for k, v in updates.items():
        if v is _REMOVE:
            state.pop(k, None)
        else:
            state[k] = v


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    except Exception:
        return {"status": "NONE"}

    ts = state.get("timestamp")
    if ts:
        try:
            saved_at = datetime.fromisoformat(ts)
            age_hours = (datetime.now(timezone.utc) - saved_at).total_seconds() / 3600
            if age_hours > STATE_MAX_AGE_HOURS:
                return {"status": "NONE"}
        except Exception:
            return {"status": "NONE"}

    return state


def save_state(state):
    state = dict(state)
    state["timestamp"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print("[STATE SAVE ERROR] " + str(e))


_STATS_DEFAULTS = {
    "total_scans":          0,
    "consolidation_skip":   0,
    "atr_too_low":          0,
    "regime_shift_skip":    0,
    "no_leg_owner":         0,
    "tier1_signals":        0,
    "tier2_signals":        0,
    "tier3_signals":        0,
    "ownership_upgrades":   0,
    "risk_gate_suppressed": 0,
    "signals_sent":         0,
    "wins":                 0,
    "losses":               0,
    "first_scan":           None,
    "last_scan":            None,
    "last_update_id":       0,
    # ── Diagnostic / journal additions (ported from V6) ──────────────────
    "result_logged_for_signal": None,
    "last_closed_trade":        None,
    "last_journal_signal":      None,
    "last_journal_entry":       None,
    "last_journal_tier_label":  None,
    "last_journal_score":       None,
    "last_journal_tier_rating": None,
    "last_journal_time":        None,
    "last_journal_timeline":    None,
    "_pending_trade_query":     False,
    # NOTE: "journal" and "pending_confirm" are deliberately NOT given
    # mutable defaults here (dict/list defaults shared across
    # dict(_STATS_DEFAULTS) calls would alias between loads that never
    # got saved in between). Both are lazily created with
    # stats.setdefault(...) at the point of first use instead.
}


def load_stats():
    try:
        with open(STATS_FILE, "r") as f:
            saved = json.load(f)
        stats = dict(_STATS_DEFAULTS)
        stats.update(saved)
        return stats
    except Exception:
        return dict(_STATS_DEFAULTS)


def save_stats(stats):
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        print("[STATS SAVE ERROR] " + str(e))


def load_bias_ab_log():
    """Ported from V6. Rolling log of live (gated) vs shadow (old-rule)
    1H bias agreement, one entry per scan, for /biasab."""
    try:
        with open(BIAS_AB_LOG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_bias_ab_log(log):
    try:
        with open(BIAS_AB_LOG_FILE, "w") as f:
            json.dump(log[-BIAS_AB_LOG_MAX_ENTRIES:], f, indent=2)
    except Exception as e:
        print("[BIAS AB LOG SAVE ERROR] " + str(e))


# =========================================================================
# TELEGRAM
# =========================================================================
def send_telegram(message):
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print("Telegram alert sent.")
    except Exception as e:
        print("[TELEGRAM ERROR] " + str(e))


# =========================================================================
# DATA LAYER
# =========================================================================
# Per-interval cache of the last SUCCESSFULLY fetched dataframe, used only
# as a fallback after every retry has failed. Deliberately capped by age
# (see OHLC_FALLBACK_MAX_AGE_MIN) — silently trading off a badly stale
# dataframe is arguably worse than skipping the scan entirely, since a
# gap/news candle could be missing from it. This is a last resort for a
# brief API hiccup, not a substitute for fresh data.
_LAST_GOOD_OHLC = {}
OHLC_FETCH_RETRIES = 3
OHLC_FALLBACK_MAX_AGE_MIN = 30


def _fallback_ohlc(interval):
    cached = _LAST_GOOD_OHLC.get(interval)
    if cached is None:
        return None
    df, fetched_at = cached
    age_min = (time.time() - fetched_at) / 60.0
    if age_min > OHLC_FALLBACK_MAX_AGE_MIN:
        print(f"[FETCH FALLBACK] {interval}: last-good dataframe is {age_min:.1f} min old "
              f"(> {OHLC_FALLBACK_MAX_AGE_MIN} min cap) — refusing to use it, returning None")
        return None
    print(f"[FETCH FALLBACK] {interval}: all retries failed — using last-good dataframe "
          f"from {age_min:.1f} min ago")
    return df


def fetch_ohlc(interval, outputsize=200):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": PAIR,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_KEY,
        "format": "JSON",
    }

    resp = None
    for attempt in range(OHLC_FETCH_RETRIES):
        is_last_attempt = attempt == OHLC_FETCH_RETRIES - 1
        try:
            resp = requests.get(url, params=params, timeout=15).json()
        except Exception as e:
            if is_last_attempt:
                print(f"[FETCH ERROR] {interval} (all {OHLC_FETCH_RETRIES} attempts failed): {e}")
                return _fallback_ohlc(interval)
            backoff = 2 ** attempt  # 1s, 2s, 4s
            print(f"[FETCH RETRY] {interval} attempt {attempt + 1}/{OHLC_FETCH_RETRIES} "
                  f"failed ({e}); retrying in {backoff}s")
            time.sleep(backoff)
            continue

        if "values" not in resp:
            msg = resp.get("message") or resp.get("code") or "Unknown error"
            if is_last_attempt:
                print(f"[API ERROR] {interval} (all {OHLC_FETCH_RETRIES} attempts failed): {msg}")
                return _fallback_ohlc(interval)
            backoff = 2 ** attempt
            print(f"[API RETRY] {interval} attempt {attempt + 1}/{OHLC_FETCH_RETRIES}: "
                  f"{msg}; retrying in {backoff}s")
            time.sleep(backoff)
            continue

        break  # got a response with "values" — proceed to parse below
    else:
        # Loop exhausted without an explicit return (shouldn't happen given
        # the is_last_attempt branches above, but keep this as a hard
        # backstop rather than let `resp` be used unset).
        return _fallback_ohlc(interval)

    df = pd.DataFrame(resp["values"])
    df.index = pd.to_datetime(df["datetime"], utc=True)
    df = df[["open", "high", "low", "close"]].rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close"
    }).astype(float).sort_index()

    df = df.iloc[:-1]
    _LAST_GOOD_OHLC[interval] = (df, time.time())
    return df


def is_forex_weekend(now_utc):
    wd = now_utc.weekday()
    if wd == 5:
        return True
    # FIX (per chat): forex reopens Sunday evening (~22:00 UTC / 5pm EST
    # winter, 5pm EDT summer — using a fixed UTC hour here is an
    # approximation; DST means the true reopen drifts by an hour twice a
    # year, but 22:00 UTC is close enough not to matter for a 5-minute
    # scan cadence). The old code treated ALL of Sunday as weekend with
    # no reopen check at all, so it would keep skipping scans for hours
    # after the market was genuinely back open.
    if wd == 6 and now_utc.hour < 22:
        return True
    if wd == 4 and now_utc.hour >= 21:
        return True
    return False


def check_data_freshness(df, interval_minutes, label, now_utc):
    if df is None or df.empty:
        return True

    if is_forex_weekend(now_utc):
        return True

    last_candle_time = df.index[-1]
    age_minutes = (now_utc - last_candle_time).total_seconds() / 60
    max_age = interval_minutes * FRESHNESS_MAX_CANDLE_AGE_MULT

    if age_minutes > max_age:
        print(
            "[STALE DATA] {}: last closed candle is {:.0f} min old "
            "(max allowed {:.0f} min) — feed may be delayed or stuck.".format(
                label, age_minutes, max_age)
        )
        return False

    return True


def data_looks_sane(df, label, max_spike_mult=DATA_SPIKE_ATR_MULT):
    if df is None or df.empty:
        return False

    ohlc = df[["Open", "High", "Low", "Close"]]
    if ohlc.isna().any().any():
        print("[DATA WARNING] " + label + ": contains NaN values.")
        return False
    if (ohlc <= 0).any().any():
        print("[DATA WARNING] " + label + ": contains zero/negative prices.")
        return False

    bar_range = df["High"] - df["Low"]
    avg_range = bar_range.rolling(20, min_periods=5).mean()
    spike = bar_range > (avg_range * max_spike_mult)

    recent_spike = spike.tail(DATA_SPIKE_BLOCK_BARS)
    if recent_spike.any():
        bad_idx = recent_spike[recent_spike].index
        bars_since_last_spike = len(df) - 1 - df.index.get_loc(bad_idx[-1])
        bars_remaining = DATA_SPIKE_BLOCK_BARS - bars_since_last_spike
        print(
            "[DATA WARNING] " + label + ": possible bad tick in recent bars. "
            "spike_at={} range={} avg_range={} clears_in={} more bar(s)".format(
                list(bad_idx),
                bar_range.loc[bad_idx].round(5).tolist(),
                avg_range.loc[bad_idx].round(5).tolist(),
                bars_remaining,
            )
        )
        return False

    return True


# =========================================================================
# INDICATORS
# =========================================================================
def atr(df, period=14):
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def close_location(candle):
    """0 (closed at the low) to 1 (closed at the high)."""
    rng = candle["High"] - candle["Low"]
    if rng <= 0:
        return 0.5
    return (candle["Close"] - candle["Low"]) / rng


# =========================================================================
# MACRO BIAS — the ONE authority for direction (1H only). PURE: every
# function here returns a result; state is only ever changed by the
# caller applying the returned updates via apply_state_updates().
# =========================================================================
def find_all_fractals(df, wing=2):
    highs = df["High"].values
    lows = df["Low"].values
    n = len(highs)
    fractals = []

    for i in range(wing, n - wing):
        window_h = list(highs[i - wing:i]) + list(highs[i + 1:i + wing + 1])
        if all(highs[i] > h for h in window_h):
            fractals.append({"idx": i, "type": "high", "price": highs[i], "time": df.index[i]})

        window_l = list(lows[i - wing:i]) + list(lows[i + 1:i + wing + 1])
        if all(lows[i] < l for l in window_l):
            fractals.append({"idx": i, "type": "low", "price": lows[i], "time": df.index[i]})

    fractals.sort(key=lambda f: f["idx"])
    return fractals


def detect_bos_impulse(df, wing=2, invalidation_retrace=INVALIDATION_RETRACE,
                        break_buffer_atr_mult=0.0):
    """
    Fractal-based dominant-leg tracker. Returns the current confirmed
    BOS/CHoCH leg (direction, origin, extreme, break_count) or None.
    Pure — takes a dataframe, returns a value, touches no external state.
    """
    fractals = find_all_fractals(df, wing=wing)
    if len(fractals) < 2:
        return None

    closes = df["Close"].values
    highs = df["High"].values
    lows = df["Low"].values
    n = len(df)

    atr_vals = atr(df, period=14).values if break_buffer_atr_mult > 0 else None

    external_high = None
    external_low = None
    candidate_low_origin = None
    candidate_high_origin = None
    dominant = None
    leg_break_count = 0
    # Records whether the CURRENT dominant leg's formation was a genuine
    # CHoCH (flipped from an opposite-direction dominant that existed
    # right before it) vs formed with no prior dominant at all (per chat
    # — "was this OB born from a CHoCH or a plain continuation?"). Only
    # ever (re)set in the two branches below that actually REPLACE
    # `dominant`; a same-direction continuation (leg_break_count += 1,
    # no reassignment) leaves it untouched on purpose, since the leg's
    # origin — and therefore its formation story — hasn't changed.
    was_choch = False

    fractal_iter = iter(fractals)
    next_fractal = next(fractal_iter, None)

    for i in range(n):
        while next_fractal is not None and next_fractal["idx"] == i:
            if next_fractal["type"] == "high":
                if candidate_high_origin is None or next_fractal["price"] > candidate_high_origin["price"]:
                    candidate_high_origin = next_fractal
                if external_high is None:
                    external_high = next_fractal["price"]
            else:
                if candidate_low_origin is None or next_fractal["price"] < candidate_low_origin["price"]:
                    candidate_low_origin = next_fractal
                if external_low is None:
                    external_low = next_fractal["price"]
            next_fractal = next(fractal_iter, None)

        close = closes[i]
        high = highs[i]
        low = lows[i]

        if dominant is not None:
            if dominant["direction"] == "BULLISH":
                origin_violated = low <= dominant["origin"]
                leg_range = dominant["extreme"] - dominant["origin"]
                retrace_violated = (
                    leg_range > 0 and
                    (dominant["extreme"] - low) / leg_range >= invalidation_retrace
                )
                if origin_violated or retrace_violated:
                    dominant = None
            else:
                origin_violated = high >= dominant["origin"]
                leg_range = dominant["origin"] - dominant["extreme"]
                retrace_violated = (
                    leg_range > 0 and
                    (high - dominant["extreme"]) / leg_range >= invalidation_retrace
                )
                if origin_violated or retrace_violated:
                    dominant = None

        new_candidate = None
        buf = 0.0
        if atr_vals is not None and not pd.isna(atr_vals[i]):
            buf = atr_vals[i] * break_buffer_atr_mult

        if external_high is not None and close > external_high + buf and candidate_low_origin is not None:
            new_candidate = {
                "direction": "BULLISH",
                "origin": candidate_low_origin["price"],
                "origin_idx": candidate_low_origin["idx"],
                "extreme": close,
            }
            external_high = close
            external_low = None
            candidate_low_origin = None

        if external_low is not None and close < external_low - buf and candidate_high_origin is not None:
            new_candidate = {
                "direction": "BEARISH",
                "origin": candidate_high_origin["price"],
                "origin_idx": candidate_high_origin["idx"],
                "extreme": close,
            }
            external_low = close
            external_high = None
            candidate_high_origin = None

        if new_candidate is not None:
            if dominant is None:
                dominant = new_candidate
                leg_break_count = 1
                was_choch = False  # no prior dominant to flip from -- fresh formation, not a CHoCH
            elif new_candidate["direction"] == dominant["direction"]:
                leg_break_count += 1
            else:
                dominant = new_candidate
                leg_break_count = 1
                was_choch = True  # genuine flip from an opposite-direction dominant

        if dominant is not None:
            if dominant["direction"] == "BULLISH":
                dominant["extreme"] = max(dominant["extreme"], high)
            else:
                dominant["extreme"] = min(dominant["extreme"], low)

    if dominant is None:
        return None

    return {
        "direction": dominant["direction"],
        "impulse_start": dominant["origin"],
        "impulse_end": dominant["extreme"],
        "break_count": leg_break_count,
        # Index (within the dataframe passed in) of the fractal that
        # founded this leg. Purely additive — existing callers that only
        # read direction/impulse_start/impulse_end/break_count are
        # unaffected. Added so detect_order_block() can bound its search
        # by RECENCY, not just price overlap (see its docstring).
        "origin_idx": dominant.get("origin_idx"),
        # Per chat — "was this leg's formation a genuine CHoCH, or a
        # fresh leg with no prior direction to flip from?" Purely
        # additive, same as origin_idx above: existing callers that
        # don't read this key are completely unaffected.
        "was_choch": was_choch,
    }


def fractal_swings(df, wing=2):
    highs = df["High"].values
    lows = df["Low"].values
    n = len(highs)
    last_sh = last_sl = None

    for i in range(n - wing - 1, wing - 1, -1):
        if last_sh is None:
            window_h = list(highs[i - wing:i]) + list(highs[i + 1:i + wing + 1])
            if all(highs[i] > h for h in window_h):
                last_sh = highs[i]
        if last_sl is None:
            window_l = list(lows[i - wing:i]) + list(lows[i + 1:i + wing + 1])
            if all(lows[i] < l for l in window_l):
                last_sl = lows[i]
        if last_sh is not None and last_sl is not None:
            break

    return (
        last_sh if last_sh is not None else df["High"].max(),
        last_sl if last_sl is not None else df["Low"].min(),
    )


def check_leg_anchor_survival(state, prefix, window_df, invalidation_retrace=INVALIDATION_RETRACE):
    """
    PURE version of what used to be `_refresh_leg_anchor` — reads a
    standing leg anchor (state[prefix+"_direction"/"_origin"/"_extreme"])
    and checks it directly against window_df's High/Low, WITHOUT
    mutating state. Returns (survived: bool, updates: dict) — apply via
    apply_state_updates(). This is what distinguishes "the origin
    fractal scrolled out of the fetch window" from "the leg actually
    broke," without the caller losing visibility into what changed.
    """
    anchor_dir     = state.get(prefix + "_direction")
    anchor_origin  = state.get(prefix + "_origin")
    anchor_extreme = state.get(prefix + "_extreme")

    if anchor_dir not in ("BULLISH", "BEARISH") or anchor_origin is None or anchor_extreme is None:
        return False, {}

    window_low  = window_df["Low"].min()
    window_high = window_df["High"].max()

    if anchor_dir == "BULLISH":
        new_extreme      = max(anchor_extreme, window_high)
        leg_range        = new_extreme - anchor_origin
        origin_violated  = window_low <= anchor_origin
        retrace_violated = leg_range > 0 and (new_extreme - window_low) / leg_range >= invalidation_retrace
    else:
        new_extreme      = min(anchor_extreme, window_low)
        leg_range        = anchor_origin - new_extreme
        origin_violated  = window_high >= anchor_origin
        retrace_violated = leg_range > 0 and (window_high - new_extreme) / leg_range >= invalidation_retrace

    if origin_violated or retrace_violated:
        return False, {
            prefix + "_direction": _REMOVE,
            prefix + "_origin": _REMOVE,
            prefix + "_extreme": _REMOVE,
        }

    return True, {prefix + "_extreme": new_extreme}


def _promotion_confirmed(break_count, df_1h, direction, min_break_count=PROMOTION_MIN_BREAK_COUNT):
    """
    Gates whether a 15M break is strong enough to promote/override a
    STALE 1H hold. Only meaningful while bias is already stale. Pure.
      1. break_count >= min_break_count (confirmed follow-through, not
         just the single reversal wick that founded it)
      2. EMA agreement — 1H close on the correct side of EMA_100
    """
    if break_count < min_break_count:
        return False, f"break_count {break_count} < {min_break_count} required"

    df_1h = df_1h.copy()
    df_1h["EMA_100"] = df_1h["Close"].ewm(span=100, adjust=False).mean()
    close_now = df_1h["Close"].iloc[-1]
    ema_now = df_1h["EMA_100"].iloc[-1]

    ema_agrees = (direction == "BULLISH" and close_now > ema_now) or \
                 (direction == "BEARISH" and close_now < ema_now)
    if not ema_agrees:
        return False, "1H close disagrees with EMA_100"

    return True, f"break_count {break_count} >= {min_break_count} and EMA agrees"


def _macro_swing_updates(direction, origin, extreme):
    """PURE helper — builds the state-update dict for the auditable 1H
    swing points backing the current confirmed bias, so state.json
    always shows exactly which swing points justify the current bias."""
    if direction == "BULLISH":
        swing_low, swing_high = origin, extreme
    else:
        swing_high, swing_low = origin, extreme
    return {
        "macro_swing_low": swing_low,
        "macro_swing_high": swing_high,
        "macro_swing_confirmed_at": datetime.now(timezone.utc).isoformat(),
    }


def compute_macro_bias(df_1h, df_15m, state):
    """
    PURE. The single authority for direction. Returns (bias, updates) —
    `state` is read-only here; the caller applies `updates` via
    apply_state_updates(). Returns "BULLISH", "BEARISH", or
    "CONSOLIDATION".

    Flip gate: ONLY a confirmed 1H BOS/CHoCH can change the held
    direction. A bare EMA_100 cross with no matching structural break
    does not flip bias.

    Also owns stale-bias promotion end-to-end: if the confirmed 1H leg
    has gone stale (invalidated with nothing fresh to replace it), a
    strong enough 15M BOS/CHoCH can promote a new direction — folded in
    here (rather than left for a tier or scan() to reach back into bias
    bookkeeping) so Macro Bias is the ONE place direction gets decided,
    full stop.
    """
    updates = {}
    df_1h_x = df_1h.copy()
    df_1h_x["EMA_100"] = df_1h_x["Close"].ewm(span=100, adjust=False).mean()
    df_1h_x["ATR_1H"] = atr(df_1h_x, period=14)

    close_now = df_1h_x["Close"].iloc[-1]
    ema_now = df_1h_x["EMA_100"].iloc[-1]
    atr_now = df_1h_x["ATR_1H"].iloc[-1]
    confirmed = state.get("macro_bias_confirmed")

    is_flat = False
    if not (pd.isna(atr_now) or atr_now == 0 or len(df_1h_x) <= HTF_EMA_SLOPE_BARS):
        ema_then = df_1h_x["EMA_100"].iloc[-1 - HTF_EMA_SLOPE_BARS]
        dist_in_atr = abs(close_now - ema_now) / atr_now
        slope_in_atr = abs(ema_now - ema_then) / atr_now
        is_flat = dist_in_atr < HTF_CONSOLIDATION_ATR_MULT and slope_in_atr < HTF_EMA_FLAT_THRESHOLD

    if is_flat:
        updates["macro_bias_confirmed"] = "CONSOLIDATION"
        updates["macro_bias_stale"] = False
        return "CONSOLIDATION", updates

    bos_1h = detect_bos_impulse(df_1h_x, wing=HTF_STRUCTURE_WING)
    if bos_1h is not None:
        bias = bos_1h["direction"]
        updates.update({
            "macro_bias_confirmed": bias,
            "macro_bias_stale": False,
            "macro_leg_direction": bias,
            "macro_leg_origin": bos_1h["impulse_start"],
            "macro_leg_extreme": bos_1h["impulse_end"],
        })
        updates.update(_macro_swing_updates(bias, bos_1h["impulse_start"], bos_1h["impulse_end"]))
        return bias, updates

    survived, anchor_updates = check_leg_anchor_survival(state, "macro_leg", df_1h_x)
    if survived:
        updates.update(anchor_updates)
        bias = state.get("macro_leg_direction")
        updates["macro_bias_confirmed"] = bias
        updates["macro_bias_stale"] = False
        return bias, updates

    if confirmed in ("BULLISH", "BEARISH"):
        bias = confirmed
        stale = True

        # ── Stale-bias promotion via 15M structure ──────────────────
        early_lookback = df_15m.tail(SWING_LOOKBACK_15)
        early_bos = detect_bos_impulse(
            early_lookback, wing=FRACTAL_WING,
            break_buffer_atr_mult=BOS_15M_BREAK_BUFFER_ATR_MULT,
        )
        if early_bos is None:
            survived_15, anchor_updates_15 = check_leg_anchor_survival(state, "leg15", early_lookback)
            if survived_15:
                updates.update(anchor_updates_15)
                early_bos = {
                    "direction": state.get("leg15_direction"),
                    "impulse_start": state.get("leg15_origin"),
                    "impulse_end": anchor_updates_15.get("leg15_extreme", state.get("leg15_extreme")),
                    "break_count": state.get("leg15_break_count", 1),
                }
        else:
            updates["leg15_break_count"] = early_bos["break_count"]

        if early_bos is not None and early_bos.get("direction") not in (None, bias):
            promo_ok, promo_reason = _promotion_confirmed(
                early_bos.get("break_count", 1), df_1h_x, early_bos["direction"])
            if promo_ok:
                bias = early_bos["direction"]
                stale = False
                updates.update({
                    "macro_bias_confirmed": bias,
                    "macro_bias_stale": False,
                    "macro_leg_direction": bias,
                    "macro_leg_origin": early_bos["impulse_start"],
                    "macro_leg_extreme": early_bos["impulse_end"],
                })
                updates.update(_macro_swing_updates(bias, early_bos["impulse_start"], early_bos["impulse_end"]))
                print(f"  [BIAS] 15M BOS ({bias}) reconfirms over stale 1H hold — {promo_reason}")

        if stale:
            updates["macro_bias_stale"] = True
        return bias, updates

    raw_bias = "BULLISH" if close_now > ema_now else "BEARISH"
    updates["macro_bias_confirmed"] = raw_bias
    updates["macro_bias_stale"] = True
    return raw_bias, updates


def compute_macro_bias_shadow_old_rule(df_1h, state):
    """
    SHADOW ONLY — never used for trading decisions. PURE, same
    (result, updates) shape as compute_macro_bias. Mirrors the pre-gate
    behavior: any 1H break, either direction, flips bias immediately (no
    CHoCH+BOS follow-through requirement, no EMA agreement). Reads/
    writes only shadow_-prefixed keys.
    """
    updates = {}
    df_1h_x = df_1h.copy()
    df_1h_x["EMA_100"] = df_1h_x["Close"].ewm(span=100, adjust=False).mean()
    df_1h_x["ATR_1H"] = atr(df_1h_x, period=14)

    close_now = df_1h_x["Close"].iloc[-1]
    ema_now = df_1h_x["EMA_100"].iloc[-1]
    atr_now = df_1h_x["ATR_1H"].iloc[-1]
    confirmed = state.get("shadow_macro_bias_confirmed")

    is_flat = False
    if not (pd.isna(atr_now) or atr_now == 0 or len(df_1h_x) <= HTF_EMA_SLOPE_BARS):
        ema_then = df_1h_x["EMA_100"].iloc[-1 - HTF_EMA_SLOPE_BARS]
        dist_in_atr = abs(close_now - ema_now) / atr_now
        slope_in_atr = abs(ema_now - ema_then) / atr_now
        is_flat = dist_in_atr < HTF_CONSOLIDATION_ATR_MULT and slope_in_atr < HTF_EMA_FLAT_THRESHOLD

    if is_flat:
        updates["shadow_macro_bias_confirmed"] = "CONSOLIDATION"
        return "CONSOLIDATION", updates

    bos_1h = detect_bos_impulse(df_1h_x, wing=HTF_STRUCTURE_WING)
    if bos_1h is not None:
        updates["shadow_macro_bias_confirmed"] = bos_1h["direction"]
        updates["shadow_macro_leg_direction"]  = bos_1h["direction"]
        updates["shadow_macro_leg_origin"]     = bos_1h["impulse_start"]
        updates["shadow_macro_leg_extreme"]    = bos_1h["impulse_end"]
        return bos_1h["direction"], updates

    survived, anchor_updates = check_leg_anchor_survival(state, "shadow_macro_leg", df_1h_x)
    if survived:
        updates.update(anchor_updates)
        return state.get("shadow_macro_leg_direction"), updates

    if confirmed in ("BULLISH", "BEARISH"):
        return confirmed, updates

    raw_bias = "BULLISH" if close_now > ema_now else "BEARISH"
    updates["shadow_macro_bias_confirmed"] = raw_bias
    return raw_bias, updates


# =========================================================================
# MARKET CONTEXT — is the environment tradeable right now? PURE: returns
# (ctx, reason, updates); caller applies updates via apply_state_updates.
# =========================================================================
def is_active_session(now_utc, windows=SESSION_WINDOWS_UTC):
    hour = now_utc.hour
    for start, end in windows:
        if start <= hour < end:
            return True
    return False


def detect_regime_shift(df_5m, current_atr, now_utc):
    """Compares short-term ATR (~25min) vs long-term ATR (~4h session
    baseline). Ratio above threshold = a news spike/liquidity event
    shifted the volatility environment. Pure."""
    if not REGIME_SHIFT_ENABLED:
        return False, 0.0, 0.0

    if len(df_5m) < REGIME_SHIFT_LONG_PERIOD + 2:
        return False, 0.0, 0.0

    session_open = now_utc.replace(hour=8, minute=0, second=0, microsecond=0)
    bars_since_open = int((now_utc - session_open).total_seconds() / 300)
    if 0 <= bars_since_open < REGIME_SHIFT_OPEN_WARMUP:
        return False, 0.0, 0.0

    atr_series = atr(df_5m, period=14)
    short_atr = atr_series.rolling(
        REGIME_SHIFT_SHORT_PERIOD, min_periods=REGIME_SHIFT_SHORT_PERIOD
    ).mean().iloc[-1]
    long_atr = atr_series.rolling(
        REGIME_SHIFT_LONG_PERIOD, min_periods=REGIME_SHIFT_LONG_PERIOD // 2
    ).mean().iloc[-1]

    if pd.isna(short_atr) or pd.isna(long_atr) or long_atr == 0:
        return False, 0.0, 0.0

    ratio = short_atr / long_atr
    return ratio >= REGIME_SHIFT_THRESHOLD, ratio, short_atr / PIP_SIZE


def scaled_cooldown_bars(regime_ratio):
    bars = POST_SPIKE_COOLDOWN_BASE + (regime_ratio - REGIME_SHIFT_THRESHOLD) * POST_SPIKE_COOLDOWN_SCALE
    return int(min(max(bars, POST_SPIKE_COOLDOWN_BASE), POST_SPIKE_COOLDOWN_MAX))


class MarketContext:
    """Single bundled read of 'is now tradeable at all,' handed to every
    tier so none of them repeat this work or disagree about it."""
    def __init__(self, atr_ok, current_atr, current_atr_pips,
                 regime_shifted, regime_ratio, post_spike_active, session_active):
        self.atr_ok = atr_ok
        self.current_atr = current_atr
        self.current_atr_pips = current_atr_pips
        self.regime_shifted = regime_shifted
        self.regime_ratio = regime_ratio
        self.post_spike_active = post_spike_active
        self.session_active = session_active

    @property
    def tradeable(self):
        """Hard gate only — ATR floor. Regime shift/post-spike are NOT
        gates here; they're passed through so tiers weigh them on their
        own terms (a Tier 1 POI reaction may reasonably care less about
        a regime shift than a Tier 2 gradual pullback does)."""
        return self.atr_ok


def evaluate_market_context(df_5m, state, now_utc):
    """
    PURE. Builds one MarketContext for this scan. Returns
    (ctx, reason, updates) — updates carries the new
    post_spike_cooldown_remaining value only; caller applies via
    apply_state_updates(). Computes its own ATR series locally rather
    than depending on the caller having attached one to df_5m (Stage 1
    mutated the caller's dataframe to do this — fixed here).
    """
    atr_series = atr(df_5m, period=14)
    current_atr = atr_series.iloc[-1]
    if pd.isna(current_atr) or current_atr == 0:
        return (MarketContext(False, current_atr, 0.0, False, 0.0, False, False),
                "ATR invalid (NaN/0)", {})

    current_atr_pips = current_atr / PIP_SIZE
    atr_ok = current_atr_pips >= ATR_MIN_PIPS

    regime_shifted, regime_ratio, _short_pips = detect_regime_shift(df_5m, current_atr, now_utc)

    cooldown_remaining = state.get("post_spike_cooldown_remaining", 0)
    if regime_shifted:
        cooldown_remaining = scaled_cooldown_bars(regime_ratio)
    elif cooldown_remaining > 0:
        cooldown_remaining -= 1
    updates = {"post_spike_cooldown_remaining": max(0, cooldown_remaining)}
    post_spike_active = updates["post_spike_cooldown_remaining"] > 0

    session_active = is_active_session(now_utc)

    ctx = MarketContext(
        atr_ok=atr_ok, current_atr=current_atr, current_atr_pips=current_atr_pips,
        regime_shifted=regime_shifted, regime_ratio=regime_ratio,
        post_spike_active=post_spike_active, session_active=session_active,
    )
    reason = None if atr_ok else f"ATR {current_atr_pips:.1f}p < {ATR_MIN_PIPS}p floor"
    return ctx, reason, updates


# =========================================================================
# STRUCTURE PRIMITIVES — pure building blocks the MarketFacts layer
# wraps. None of these decide whether a tier fires.
# =========================================================================
def adaptive_fib_ratio(df_5m, current_atr, near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR, lookback=50):
    """Fib zone gets SHALLOWER as volatility expands relative to its own
    recent average."""
    atr_series = atr(df_5m, period=14)
    avg_atr = atr_series.rolling(lookback, min_periods=10).mean().iloc[-1]
    if pd.isna(avg_atr) or avg_atr == 0 or pd.isna(current_atr):
        return (near + far) / 2

    expansion = current_atr / avg_atr
    expansion = max(0.5, min(2.0, expansion))
    t = (expansion - 0.5) / (2.0 - 0.5)
    return far - (t * (far - near))


def compute_fib_fraction(swing_high, swing_low, extremity, macro_bias,
                          near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR):
    """How deep an entry reached into its own NEAR-FAR band, 0-1."""
    structural_range = swing_high - swing_low
    if structural_range <= 0:
        return 0.0
    if macro_bias == "BULLISH":
        retrace_fraction = (swing_high - extremity) / structural_range
    else:
        retrace_fraction = (extremity - swing_low) / structural_range
    if far <= near:
        return 1.0
    fraction = (retrace_fraction - near) / (far - near)
    return max(0.0, min(1.0, fraction))


def detect_liquidity_sweep(df_5m, df_15m, level, macro_bias,
                            lookback_candles=SWEEP_LOOKBACK_CANDLES):
    """Generic sweep detector against ANY level. Returns (swept, label,
    distance_pips). distance_pips is how far price traded beyond `level`
    before reclaiming it (None when not swept) — added per chat as one
    of the fingerprint facts; the boolean/label behavior is UNCHANGED,
    this only adds a third return value."""
    recent = df_5m.iloc[-(lookback_candles + 2):-2]

    if macro_bias == "BULLISH":
        swept = recent[(recent["Low"] < level) & (recent["Close"] > level)]
        if swept.empty:
            return False, "no sweep", None
        if df_15m.iloc[-1]["Close"] <= level:
            return False, "15M closed below level", None
        distance_pips = round((level - swept["Low"].min()) / PIP_SIZE, 1)
        return True, "SWEEP CONFIRMED", distance_pips
    else:
        swept = recent[(recent["High"] > level) & (recent["Close"] < level)]
        if swept.empty:
            return False, "no sweep", None
        if df_15m.iloc[-1]["Close"] >= level:
            return False, "15M closed above level", None
        distance_pips = round((swept["High"].max() - level) / PIP_SIZE, 1)
        return True, "SWEEP CONFIRMED", distance_pips


def detect_order_block(df_15m, bos, atr_series,
                        min_displacement_atr_mult=OB_MIN_DISPLACEMENT_ATR_MULT,
                        opposing_lookback=OB_OPPOSING_LOOKBACK_CANDLES):
    """
    Locates the supply/demand order block behind a confirmed BOS
    impulse: "the candle which led OR is immediately followed by an
    aggressive move which led to a strong BOS displacement." See Stage 1
    for the full method writeup. Returns a dict or None.

    FIX (found during audit): candles are bounded by BOTH price overlap
    AND recency (bos["origin_idx"] onward). Price overlap alone is not
    enough — GBPUSD frequently revisits the same handful of pips over a
    trading day, so an unrelated candle from hours earlier that happens
    to share the current leg's price band was able to win the
    displacement search (its local ATR context could easily make its
    ratio look larger than the real, current displacement candle's).
    `df_15m` passed in here MUST be the same window bos was computed
    from, so bos["origin_idx"] indexes correctly into it — see
    MarketFacts.order_block(), which passes the same lookback slice used
    for bos_15m() for exactly this reason.
    """
    if bos is None:
        return None

    direction = bos["direction"]
    start_price, end_price = bos["impulse_start"], bos["impulse_end"]
    origin_idx = bos.get("origin_idx")

    lo_bound, hi_bound = sorted([start_price, end_price])
    price_mask = (df_15m["High"] >= lo_bound) & (df_15m["Low"] <= hi_bound)

    if origin_idx is not None and 0 <= origin_idx < len(df_15m):
        recency_mask = pd.Series(False, index=df_15m.index)
        recency_mask.iloc[origin_idx:] = True
        in_leg_mask = price_mask & recency_mask
    else:
        # Defensive fallback only — every live call path now supplies
        # origin_idx. Keeps old price-only behavior rather than
        # silently returning None if it's ever missing.
        in_leg_mask = price_mask

    leg_idx = df_15m.index[in_leg_mask]
    if len(leg_idx) < 2:
        return None
    leg_df = df_15m.loc[leg_idx]
    leg_atr = atr_series.loc[leg_idx]

    candle_range = leg_df["High"] - leg_df["Low"]
    displacement_ratio = candle_range / leg_atr.replace(0, np.nan)
    if displacement_ratio.isna().all():
        return None

    displacement_pos = displacement_ratio.values.argmax()
    displacement_ratio_val = displacement_ratio.iloc[displacement_pos]
    if pd.isna(displacement_ratio_val) or displacement_ratio_val < min_displacement_atr_mult:
        return None

    displacement_idx_label = leg_df.index[displacement_pos]
    displacement_pos_global = df_15m.index.get_loc(displacement_idx_label)

    ob_candle = None
    ob_pos_global = None
    window_start = max(0, displacement_pos_global - opposing_lookback)
    for pos in range(displacement_pos_global - 1, window_start - 1, -1):
        candle = df_15m.iloc[pos]
        is_bear_candle = candle["Close"] < candle["Open"]
        is_bull_candle = candle["Close"] > candle["Open"]
        if direction == "BULLISH" and is_bear_candle:
            ob_candle, ob_pos_global = candle, pos
            break
        if direction == "BEARISH" and is_bull_candle:
            ob_candle, ob_pos_global = candle, pos
            break

    if ob_candle is None and displacement_pos_global > 0:
        ob_pos_global = displacement_pos_global - 1
        ob_candle = df_15m.iloc[ob_pos_global]

    if ob_candle is None:
        return None

    ob_high, ob_low = float(ob_candle["High"]), float(ob_candle["Low"])

    # Real mitigation tracking (per chat — was a TODO stub returning
    # False unconditionally, meaning the same OB could trigger repeat
    # signals as price oscillated around it in a ranging market).
    #
    # An OB counts as MITIGATED if price already traded back into its
    # zone on some PRIOR closed candle, AFTER the displacement leg that
    # defined the OB completed — i.e. this is not price's first return
    # since the OB formed. Scan starts at displacement_pos_global + 1,
    # NOT ob_pos_global + 1: the displacement candle itself routinely
    # opens from right inside/against the OB zone (that's the move AWAY
    # from it) and would otherwise be misread as an immediate return —
    # caught via testing (Case A below), not just by reading the logic.
    #
    # The most recent CLOSED candle is deliberately EXCLUDED from this
    # scan: it may BE the current reaction that has_order_block()/
    # price_in_order_block() are being asked to evaluate right now — a
    # first touch must not be able to mark itself as already-mitigated.
    #
    # df_15m here is already closed-candles-only (fetch_ohlc drops the
    # still-forming bar before this function ever sees the data).
    mitigated = False
    mitigated_at_idx = None
    for pos in range(displacement_pos_global + 1, len(df_15m) - 1):
        candle = df_15m.iloc[pos]
        if candle["High"] >= ob_low and candle["Low"] <= ob_high:
            mitigated = True
            mitigated_at_idx = pos
            break

    return {
        "high": ob_high,
        "low": ob_low,
        "origin_idx": ob_pos_global,
        "displacement_idx": displacement_pos_global,
        "direction": direction,
        "mitigated": mitigated,
        "mitigated_at_idx": mitigated_at_idx,
    }


def detect_fvg(df, direction, lookback=FVG_LOOKBACK_CANDLES):
    """
    Fair Value Gap — classic 3-candle imbalance: candle[i-2] and
    candle[i] don't overlap at all, meaning candle[i-1] traded through a
    range neither neighbor retraced into.

      BULLISH gap: candle[i-2].High < candle[i].Low
      BEARISH gap: candle[i-2].Low  > candle[i].High

    Only returns gaps still OPEN as of the end of `df` — a gap counts as
    closed once a later candle has fully traded back through it.
    Returns a list of {high, low, idx} dicts, oldest first.
    """
    window = df.tail(lookback)
    highs, lows = window["High"].values, window["Low"].values
    n = len(window)
    gaps = []

    for i in range(2, n):
        if direction == "BULLISH":
            if highs[i - 2] < lows[i]:
                gap_low, gap_high = highs[i - 2], lows[i]
                closed = (lows[i + 1:] <= gap_low).any() if i + 1 < n else False
                if not closed:
                    gaps.append({"high": float(gap_high), "low": float(gap_low), "idx": i})
        else:
            if lows[i - 2] > highs[i]:
                gap_low, gap_high = highs[i], lows[i - 2]
                closed = (highs[i + 1:] >= gap_high).any() if i + 1 < n else False
                if not closed:
                    gaps.append({"high": float(gap_high), "low": float(gap_low), "idx": i})

    return gaps


def detect_significant_fvg(df_15m, direction, atr_series,
                            lookback=FVG_LOOKBACK_CANDLES,
                            min_size_atr_mult=FVG_MIN_SIZE_ATR_MULT,
                            max_age_candles=FVG_MAX_AGE_CANDLES):
    """
    SHADOW-ONLY. "Not every FVG is important" — this is the quality gate
    that keeps Experiment 3 (POI) from just logging every 3-candle
    imbalance. A gap only counts if BOTH:
      1. size >= min_size_atr_mult * ATR(15m as of the gap's middle
         candle) — a gap smaller than that is noise, not an imbalance
         institutions actually need to come back and fill.
      2. it formed within the last `max_age_candles` candles — an old,
         technically-still-open gap that price has drifted away from is
         a stale reference, not a live one.
    Returns the same list shape as detect_fvg(), each dict additionally
    carrying "size_atr_ratio" and "age_candles" for later logging/analysis.
    """
    window = df_15m.tail(lookback)
    atr_window = atr_series.reindex(window.index)
    n = len(window)
    raw_gaps = detect_fvg(df_15m, direction, lookback=lookback)

    quality_gaps = []
    for gap in raw_gaps:
        age = (n - 1) - gap["idx"]
        if age > max_age_candles:
            continue
        gap_atr = atr_window.iloc[gap["idx"]]
        if pd.isna(gap_atr) or gap_atr == 0:
            continue
        size = gap["high"] - gap["low"]
        size_atr_ratio = size / gap_atr
        if size_atr_ratio < min_size_atr_mult:
            continue
        enriched = dict(gap)
        enriched["size_atr_ratio"] = float(size_atr_ratio)
        enriched["age_candles"] = int(age)
        quality_gaps.append(enriched)

    return quality_gaps


def is_fib_zone_stale(c_spike, swing_high, swing_low, fib_zone, current_price):
    """Post-spike staleness check for a Tier 2 fib zone."""
    structural_range = swing_high - swing_low
    spike_range = c_spike["High"] - c_spike["Low"]

    if spike_range > structural_range:
        return True, (
            f"spike range {spike_range/PIP_SIZE:.1f}p > structural range "
            f"{structural_range/PIP_SIZE:.1f}p"
        )
    if c_spike["Close"] > swing_high:
        return True, f"spike close {c_spike['Close']:.5f} broke above SwH {swing_high:.5f}"
    if c_spike["Close"] < swing_low:
        return True, f"spike close {c_spike['Close']:.5f} broke below SwL {swing_low:.5f}"

    zone_drift = abs(current_price - fib_zone) / structural_range
    if zone_drift > 0.5:
        return True, (
            f"price {abs(current_price - fib_zone)/PIP_SIZE:.1f}p from zone "
            f"(>{structural_range * 0.5 / PIP_SIZE:.1f}p threshold)"
        )
    return False, "zone still valid"


# =========================================================================
# MARKET FACTS LAYER — pure observation. Knows BOS, CHoCH, Order Blocks,
# FVG, liquidity sweeps, swings, fib. Knows NOTHING about trading. Tiers
# ask it questions; it never says "this is a buy." Computed once per
# scan and handed to every tier — if a tier needs new information about
# the market that isn't here, add a fact HERE, never reach into raw OHLC
# from inside a tier.
# =========================================================================
class MarketFacts:
    def __init__(self, df_5m, df_15m, df_1h, macro_bias, swing_high, swing_low, now_utc):
        self.df_5m = df_5m
        self.df_15m = df_15m
        self.df_1h = df_1h
        self.macro_bias = macro_bias
        self.swing_high = swing_high
        self.swing_low = swing_low
        self.now_utc = now_utc

        self._atr_5m_series = atr(df_5m, period=14)
        self._atr_15m_series = atr(df_15m, period=14)

        # lazy caches — computed at most once per instance regardless of
        # how many tiers ask the same question this scan
        self._bos_15m_cache = "UNSET"
        self._ob_cache = "UNSET"

    # ---- raw candle access (still "facts," just at candle grain) ------
    def last_candle_5m(self):
        return self.df_5m.iloc[-1]

    def prev_candle_5m(self):
        return self.df_5m.iloc[-2]

    def current_atr_5m(self):
        return self._atr_5m_series.iloc[-1]

    def current_atr_5m_pips(self):
        return self.current_atr_5m() / PIP_SIZE

    def atr_percentile_15m(self):
        """Where the CURRENT 15m ATR sits within its own recent history
        (0-100). Self-contained — uses only self._atr_15m_series, which
        already exists on every MarketFacts instance, so this needed no
        new plumbing into any call site. Fingerprint fact (per chat),
        not a live gate."""
        series = self._atr_15m_series.dropna()
        if len(series) < 2:
            return None
        current = series.iloc[-1]
        return round((series < current).mean() * 100, 1)

    # ---- structure facts ------------------------------------------------
    def bos_15m(self):
        """Fresh 15M BOS/CHoCH leg, or None."""
        if self._bos_15m_cache == "UNSET":
            lookback = self.df_15m.tail(SWING_LOOKBACK_15)
            self._bos_15m_cache = detect_bos_impulse(
                lookback, wing=FRACTAL_WING,
                break_buffer_atr_mult=BOS_15M_BREAK_BUFFER_ATR_MULT,
            )
        return self._bos_15m_cache

    def has_fresh_bos_aligned_with_bias(self):
        b = self.bos_15m()
        return b is not None and b["direction"] == self.macro_bias

    def has_choch_15m(self):
        """Whether the current 15M leg is a direction FLIP versus the
        immediately preceding leg (a CHoCH), not a same-direction
        follow-through BOS. break_count==1 is set exactly when a leg
        flips (see detect_bos_impulse)."""
        b = self.bos_15m()
        return b is not None and b.get("break_count") == 1

    def has_order_block(self):
        # A MITIGATED OB is treated as if it doesn't exist for live
        # gating purposes (per chat — this is what stops the same OB
        # from re-triggering as price oscillates around it in a ranging
        # market). order_block() itself still returns the raw dict with
        # mitigated=True so research/tags can see it happened — only the
        # live gate treats it as absent.
        ob = self.order_block()
        return ob is not None and not ob.get("mitigated", False)

    def order_block(self):
        if self._ob_cache == "UNSET":
            bos = self.bos_15m()
            if bos is None:
                self._ob_cache = None
            else:
                # Must be the SAME window bos_15m() computed against, so
                # bos["origin_idx"] indexes correctly (see the recency-
                # bounding fix in detect_order_block's docstring).
                lookback = self.df_15m.tail(SWING_LOOKBACK_15)
                atr_lookback = self._atr_15m_series.loc[lookback.index]
                self._ob_cache = detect_order_block(lookback, bos, atr_lookback)
        return self._ob_cache

    def price_in_order_block(self, tolerance_pips=ZONE_TOLERANCE_PIPS):
        ob = self.order_block()
        if ob is None or ob.get("mitigated", False):
            return False
        tol = tolerance_pips * PIP_SIZE
        c = self.last_candle_5m()
        return (c["Low"] <= ob["high"] + tol) and (c["High"] >= ob["low"] - tol)

    # NOTE: FVG is intentionally NOT exposed here. The live bot only ever
    # trades a high-probability Order Block — a demand zone while bias is
    # BULLISH, a supply zone while bias is BEARISH (order_block() above is
    # already bias-locked, since it's derived from the BOS leg that IS the
    # bias). FVG lives exclusively in the Shadow Pipeline's POI experiment,
    # gated by a size/freshness quality filter — see detect_significant_fvg().

    # ---- fib / zone facts (parametrized — a level is a fact input, not
    # a hardcoded field, since different tiers ask about different levels) -
    def fib_zone(self, near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR):
        ratio = adaptive_fib_ratio(self.df_5m, self.current_atr_5m(), near=near, far=far)
        if self.macro_bias == "BULLISH":
            return self.swing_high - ratio * (self.swing_high - self.swing_low)
        else:
            return self.swing_low + ratio * (self.swing_high - self.swing_low)

    def fib_fraction(self, extremity, near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR):
        return compute_fib_fraction(self.swing_high, self.swing_low, extremity,
                                     self.macro_bias, near, far)

    def price_in_zone(self, level, tolerance_pips=ZONE_TOLERANCE_PIPS):
        tol = tolerance_pips * PIP_SIZE
        c, c_prev = self.last_candle_5m(), self.prev_candle_5m()
        if self.macro_bias == "BULLISH":
            return min(c["Low"], c_prev["Low"]) <= level + tol
        else:
            return max(c["High"], c_prev["High"]) >= level - tol

    def has_liquidity_sweep(self, level):
        swept, _, _ = detect_liquidity_sweep(self.df_5m, self.df_15m, level, self.macro_bias)
        return swept

    def liquidity_sweep_label(self, level):
        _, label, _ = detect_liquidity_sweep(self.df_5m, self.df_15m, level, self.macro_bias)
        return label

    def sweep_distance_pips(self, level):
        """How far price traded beyond `level` before reclaiming it, in
        pips. None if there was no sweep. Fingerprint fact (per chat)."""
        _, _, distance = detect_liquidity_sweep(self.df_5m, self.df_15m, level, self.macro_bias)
        return distance

    # ---- candle-quality facts -------------------------------------------
    def rejection_candle(self, direction=None):
        """Engulf/rejection on the last closed 5M bar: prior candle
        opposite color, last candle correct color, engulfs prior body
        (within tolerance), real body vs ATR, closes in the correct half
        of its own range."""
        direction = direction or self.macro_bias
        c_last, c_prev = self.last_candle_5m(), self.prev_candle_5m()
        atr_now = self.current_atr_5m()
        body_last = abs(c_last["Close"] - c_last["Open"])
        atr_threshold = ATR_ENGULF_MIN * atr_now
        engulf_tol = ENGULF_TOLERANCE_PIPS * PIP_SIZE

        if direction == "BULLISH":
            bear_prev = c_prev["Close"] < c_prev["Open"]
            bull_last = c_last["Close"] > c_last["Open"]
            engulfs = (c_last["Close"] >= c_prev["Open"] - engulf_tol and
                       c_last["Open"]  <= c_prev["Close"] + engulf_tol)
            loc_ok = close_location(c_last) >= ENGULF_CLOSE_LOCATION_MIN
            return bool(bear_prev and bull_last and engulfs and body_last > atr_threshold and loc_ok)
        else:
            bull_prev = c_prev["Close"] > c_prev["Open"]
            bear_last = c_last["Close"] < c_last["Open"]
            engulfs = (c_last["Close"] <= c_prev["Open"] + engulf_tol and
                       c_last["Open"]  >= c_prev["Close"] - engulf_tol)
            loc_ok = close_location(c_last) <= (1 - ENGULF_CLOSE_LOCATION_MIN)
            return bool(bull_prev and bear_last and engulfs and body_last > atr_threshold and loc_ok)


# =========================================================================
# RULE OF LAW — arbitration only. Never judges setup quality; decides
# WHICH tier gets to evaluate the current leg, applies ownership
# UPGRADES from higher-priority tiers, and makes sure exactly one tier
# can hold a leg at a time.
# =========================================================================
def compute_leg_id(macro_bias, swing_high, swing_low):
    return "{}|{:.5f}|{:.5f}".format(macro_bias, swing_high, swing_low)


def _same_leg(leg_id_a, leg_id_b, tolerance_pips=LEG_MATCH_TOLERANCE_PIPS):
    if leg_id_a == leg_id_b:
        return True
    try:
        dir_a, hi_a, lo_a = leg_id_a.split("|")
        dir_b, hi_b, lo_b = leg_id_b.split("|")
    except (AttributeError, ValueError):
        return False
    if dir_a != dir_b:
        return False
    tol = tolerance_pips * PIP_SIZE
    return abs(float(hi_a) - float(hi_b)) <= tol and abs(float(lo_a) - float(lo_b)) <= tol


def get_leg_owner(state):
    return state.get("leg_owner")


def apply_leg_ownership(state, decision):
    """
    The ONLY function that mutates state["leg_owner"]. Every place that
    used to call claim_leg()/release_leg() directly now builds a
    decision dict and passes it here — one auditable choke point for
    every ownership change, and every change is logged.

    decision: {"action": "claim", "tier": ..., "leg_id": ..., "status": ...,
               "upgraded": bool}
           or {"action": "release", "reason": ...}
    """
    if decision["action"] == "release":
        owner = state.get("leg_owner")
        if owner:
            print(f"  [RULE OF LAW] Released {owner.get('tier')} ownership of leg "
                  f"{owner.get('leg_id')} — {decision.get('reason','')}")
        state.pop("leg_owner", None)
    elif decision["action"] == "claim":
        state["leg_owner"] = {
            "leg_id": decision["leg_id"],
            "tier": decision["tier"],
            "status": decision["status"],
            "upgraded": decision.get("upgraded", False),
            "claimed_at": datetime.now(timezone.utc).isoformat(),
        }


def release_leg(state, reason=""):
    """Thin convenience wrapper around apply_leg_ownership for call
    sites outside the Rule of Law module itself (e.g. manage_active_trade
    releasing ownership when a trade closes)."""
    apply_leg_ownership(state, {"action": "release", "reason": reason})


def bias_to_side(macro_bias):
    """BULLISH/BEARISH (structure vocabulary) -> BUY/SELL (trade vocabulary)."""
    return "BUY" if macro_bias == "BULLISH" else "SELL"


def tier_rating_from_score(score):
    if score >= 85:
        return "A"
    if score >= 65:
        return "B"
    return "C"


class TierResult:
    """
    Uniform return shape every tier's evaluate() produces.

    activated:   did this tier's activation trigger fire at all this
                 scan? Independent of `fired` — a tier can be activated
                 and sit in its own WATCHING state for several scans.
    fired:       ready to hand off to Trade Management this scan
    direction / entry / sl_raw: only meaningful if fired. sl_raw is an
                 UN-buffered structural reference (Trade Management
                 applies the shared ATR/SL_MIN buffer + risk gate).
    tier_label:  e.g. "TIER_1_POI"
    score / tier_rating / breakdown: THIS TIER'S OWN scoring — each tier
                 has its own weights/formula, not a shared 0-100 table.
    reason:      human-readable, always populated.
    state_updates: this tier's OWN namespaced state bookkeeping (e.g. a
                 break-retest timing window) as a plain dict, applied by
                 the caller via apply_state_updates() — tiers do not
                 mutate `state` directly, same discipline as Macro Bias
                 and Market Context.
    """
    def __init__(self, activated=False, fired=False, direction=None,
                 entry=None, sl_raw=None, tier_label=None,
                 score=None, tier_rating=None, breakdown=None,
                 reason="", state_updates=None, conviction=None):
        self.activated = activated
        self.fired = fired
        self.direction = direction
        self.entry = entry
        self.sl_raw = sl_raw
        self.tier_label = tier_label
        self.score = score
        self.tier_rating = tier_rating
        self.breakdown = breakdown or {}
        self.reason = reason
        self.state_updates = state_updates or {}
        # conviction: output of classify_conviction() — decision/minimum/
        # band_label/size_mult/target_r/partial_r/breakeven_r/reason.
        # Only populated once a tier's mandatory activation conditions
        # AND its structural fire-trigger (rejection candle / CHoCH) have
        # already passed — conviction never gates activation, only what
        # an activated setup is allowed to do next.
        self.conviction = conviction


# =========================================================================
# DIAGNOSTIC REPORT — ported from V6. V6's version had keys matching its
# own single-pass scoring model (macro_bias/structure/liquidity/
# confirmation/htf_gate/confidence_score/risk_gate/volatility_filter/
# duplicate_check). V3 has no equivalent single scoring pass — it has a
# tier-arbitration model instead — so the keys below are remapped to what
# V3 ACTUALLY evaluates, in the order scan() actually evaluates them:
#
#   macro_bias      — CONSOLIDATION vs directional, and stale/confirmed
#   market_context  — ctx.tradeable (ATR floor, regime shift, session)
#   tier_evaluation — did any tier ACTIVATE on the current leg
#   leg_ownership   — did the activated tier actually FIRE (own leg,
#                     not just WATCHING) this scan
#   risk_gate       — dual ATR/flat-pip risk ceiling
#
# Only gates that were actually reached are populated (None = never
# ran, an earlier gate already stopped the scan) — same "honest record"
# principle as V6's version.
# =========================================================================
def new_diagnostic():
    keys = ["macro_bias", "market_context", "tier_evaluation",
            "leg_ownership", "risk_gate"]
    return {k: None for k in keys}


DIAGNOSTIC_LABELS = {
    "macro_bias":      "Macro bias (1H)",
    "market_context":  "Market context (ATR/session/regime)",
    "tier_evaluation": "Tier evaluation",
    "leg_ownership":   "Leg ownership / fire",
    "risk_gate":       "Risk gate",
}


def diag_set(diag, key, passed, reason=None):
    """Record one gate's outcome. No-op if the key isn't tracked or diag
    is None (DIAGNOSTIC_MODE off)."""
    if diag is not None and key in diag:
        diag[key] = (bool(passed), reason)
    return diag


def build_diagnostic_report(diag, header="No signal this scan"):
    """
    Renders the diag dict into a plain-language record, e.g.:

        No signal this scan

        Because:
        ✓ Macro bias (1H)
        ✓ Market context (ATR/session/regime)
        ✗ Tier evaluation (no tier activated on this leg)

    Console-only (matches V6 — this is a per-scan developer record, not
    something worth pushing to Telegram every time nothing fires).
    """
    lines = [header, "", "Because:"]
    for key in diag:
        entry = diag[key]
        if entry is None:
            continue
        passed, reason = entry
        mark  = "✓" if passed else "✗"
        label = DIAGNOSTIC_LABELS.get(key, key)
        suffix = f" ({reason})" if (not passed and reason) else ""
        lines.append(f"{mark} {label}{suffix}")
    if len(lines) == 3:
        lines.append("_(no gates recorded)_")
    return "\n".join(lines)


# =========================================================================
# SIGNAL TIMELINE — ported from V6's format_timeline_diagnostics. Tracks,
# per LEG (reset whenever leg_id changes — see evaluate_rule_of_law),
# when each confirmation stage was first observed. Populated
# opportunistically from MarketFacts each scan a leg is alive; read back
# via /last once a signal fires.
# =========================================================================
def compute_signal_timeline_reset(state, leg_id):
    """PURE. Same trigger as leg-ownership release: a new 1H leg means a
    new hypothetical setup, so prior stage timestamps stop meaning
    anything. Returns an updates dict (possibly empty) for the caller to
    apply via apply_state_updates — never mutates `state` itself."""
    if state.get("signal_timeline_leg_id") != leg_id:
        return {"signal_timeline_leg_id": leg_id, "signal_timeline": {}}
    return {}


def compute_signal_timeline_updates(facts, timeline, now_utc):
    """
    PURE. `timeline` is read-only (the CURRENT state["signal_timeline"]
    dict, already reset for this leg if needed). Returns only the NEW
    stage keys to merge in — a stage already stamped is never
    overwritten, so re-observing the same True fact next scan is a
    no-op. Caller merges and applies via apply_state_updates(), same
    discipline as every other compute_*() in this file.
    """
    updates = {}

    def _stage(stage_key, label=None):
        at_key = f"{stage_key}_at"
        if timeline.get(at_key) is None and at_key not in updates:
            updates[at_key] = now_utc.isoformat()
            if label is not None:
                updates[f"{stage_key}_label"] = label

    if facts.has_choch_15m() or facts.has_fresh_bos_aligned_with_bias():
        label = "CHoCH" if facts.has_choch_15m() else "BOS"
        _stage("choch_bos", label)

    zone = facts.fib_zone()
    if facts.price_in_zone(zone):
        _stage("fib_entered")
        if facts.has_liquidity_sweep(zone):
            _stage("sweep")

    if facts.rejection_candle():
        _stage("engulf")

    return updates


def format_timeline_diagnostics(timeline, now_utc):
    """Ported near-verbatim from V6 — compact per-signal stage timing."""
    def _parse(iso):
        try:
            return datetime.fromisoformat(iso) if iso else None
        except Exception:
            return None

    stages = [
        ("CHoCH/BOS",   _parse(timeline.get("choch_bos_at")),   timeline.get("choch_bos_label") or "N/A"),
        ("Fib entered", _parse(timeline.get("fib_entered_at")), None),
        ("Sweep",       _parse(timeline.get("sweep_at")),       None),
        ("Engulf",      _parse(timeline.get("engulf_at")),      None),
        ("Signal sent", now_utc,                                None),
    ]
    anchor = next((t for _, t, _ in stages if t is not None), None)

    lines = []
    for label, ts, tag in stages:
        if ts is None:
            lines.append(f"     {label}: —")
            continue
        lag = f" (+{int((ts - anchor).total_seconds() // 60)}m)" if anchor and ts != anchor else ""
        tag_str = f" [{tag}]" if tag and tag != "N/A" else ""
        lines.append(f"     {label}: {ts.strftime('%H:%M UTC')}{lag}{tag_str}")

    return "🕒 *Timeline:*\n" + "\n".join(lines)


# ---- TIERS (Stage 3 — live) -------------------------------------------------
# Signature is (facts, ctx, state, now_utc) — a tier receives the
# MarketFacts object and MarketContext, and read-only `state` for its own
# historical bookkeeping. It must return a TierResult; any state it wants
# persisted goes in TierResult.state_updates, never a direct mutation.
def _tier1_poi_evaluate(facts, ctx, state, now_utc):
    """
    TIER 1 — Premium POI Reaction.

    The live bot's ONLY point of interest is the Order Block, because
    detect_order_block() is already bias-locked: it's carved out of the
    BOS impulse that defines the current bias, so in a BULLISH leg it can
    only ever be a demand zone, and in a BEARISH leg only ever a supply
    zone. There is no FVG check here on purpose — see the note on
    MarketFacts and FVG_MIN_SIZE_ATR_MULT in CONFIG. If you personally
    want FVG reactions, that lives in the Shadow Pipeline's Experiment 3
    (POI) only, gated by a quality filter.

    Mandatory:
      - facts.has_fresh_bos_aligned_with_bias()  (the leg backing the OB
        is still the live, bias-aligned leg — not a stale/superseded one)
      - facts.has_order_block()            (the zone itself)
      - facts.price_in_order_block()        (price is actually reacting AT it)
      - facts.rejection_candle()            (real rejection, not just a touch)

    Optional (score only, never gates):
      - facts.has_choch_15m()  (a flip-driven OB > a mere continuation OB)
    """
    label = "TIER_1_POI"

    if not facts.has_fresh_bos_aligned_with_bias():
        return TierResult(tier_label=label, reason="no fresh 15M BOS aligned with 1H bias")

    if not facts.has_order_block():
        return TierResult(tier_label=label, reason="no order block behind the current BOS impulse")

    if not facts.price_in_order_block():
        return TierResult(tier_label=label, reason="price not currently inside the order block zone")

    # FIX (found during audit): activated and fired used to be the same
    # condition, meaning this tier went straight from unclaimed to FIRED
    # in one step — no tier could ever be claimed as WATCHING, which
    # silently made the Rule of Law ownership-upgrade mechanism
    # unreachable (nothing to upgrade FROM). Splitting them here: the
    # structural conditions above (aligned BOS + order block + price
    # there) are enough to CLAIM/WATCH the leg; rejection_candle() is
    # the separate trigger that promotes WATCHING to FIRED. This also
    # gives a higher-priority tier a real multi-scan window to steal the
    # leg before this one actually fires.
    if not facts.rejection_candle():
        return TierResult(activated=True, fired=False, tier_label=label,
                           reason="price inside order block, watching for rejection candle")

    ob = facts.order_block()
    direction = facts.macro_bias
    side = bias_to_side(direction)
    c_last = facts.last_candle_5m()

    choch = facts.has_choch_15m()
    loc = close_location(c_last)
    rejection_strength = loc if side == "BUY" else (1 - loc)

    score = 55
    breakdown = {
        "order_block": True, "rejection_candle": True,
        "fresh_bos_aligned": True, "choch": choch,
    }
    if choch:
        score += 25
        breakdown["choch_bonus"] = 25
    if rejection_strength >= 0.7:
        score += 15
        breakdown["strong_rejection_bonus"] = 15
    elif rejection_strength >= 0.55:
        score += 8
        breakdown["rejection_bonus"] = 8

    entry = float(c_last["Close"])
    # sl_raw: UN-buffered structural reference — the far edge of the zone
    # itself. Trade Management applies the shared ATR/SL_MIN buffer.
    sl_raw = ob["low"] if side == "BUY" else ob["high"]

    # Phase 3 — Execution. Structural conditions + rejection candle are
    # enough to be an ACTIVATED, confirmed Tier 1 setup — they are not
    # enough to FIRE on their own anymore. Conviction decides that.
    conviction = classify_conviction(label, score)
    fire = conviction["decision"] == "FIRE"
    base_reason = "Order block reaction ({} zone){}, rejection strength {:.2f}".format(
        "demand" if side == "BUY" else "supply",
        " + CHoCH" if choch else "", rejection_strength)

    return TierResult(
        activated=True, fired=fire, direction=side,
        entry=entry, sl_raw=sl_raw, tier_label=label,
        score=score, tier_rating=tier_rating_from_score(score),
        breakdown=breakdown, conviction=conviction,
        reason=base_reason + " — " + conviction["reason"],
    )


def _tier2_fib_evaluate(facts, ctx, state, now_utc):
    """
    TIER 2 — HTF Fib Pullback.

    Mandatory:
      - facts.price_in_zone(facts.fib_zone())   (price has retraced into
        the adaptive fib pocket)
      - facts.rejection_candle()                 (a real reaction there,
        not just a touch-and-continue)

    Optional (score only, never gates):
      - facts.has_fresh_bos_aligned_with_bias()  (BOS "optional toggle" —
        confirms momentum has resumed, but a pure pullback entry doesn't
        need it)
      - facts.has_liquidity_sweep(zone)          (a swept pocket is a
        cleaner reaction than an untouched one)

    Supply/demand (order block) is NOT required — that's Tier 1's job.
    """
    label = "TIER_2_FIB"
    direction = facts.macro_bias
    side = bias_to_side(direction)
    zone = facts.fib_zone()

    if not facts.price_in_zone(zone):
        return TierResult(tier_label=label, reason="price not in the HTF fib pocket")

    # Same fix as Tier 1 — see its comment. Price being in the pocket is
    # enough to WATCH; rejection_candle() is the separate FIRE trigger.
    # This is also a closer match to your own framework's "1H locates,
    # 15M confirms, 5M executes" split: arriving at the pocket is the
    # 1H/15M "location" step, the rejection candle is the "confirms" step.
    if not facts.rejection_candle():
        return TierResult(activated=True, fired=False, tier_label=label,
                           reason="price in HTF fib pocket, watching for rejection candle")

    bos_aligned = facts.has_fresh_bos_aligned_with_bias()
    swept = facts.has_liquidity_sweep(zone)

    score = 50
    breakdown = {"in_fib_zone": True, "rejection_candle": True,
                 "bos_aligned": bos_aligned, "liquidity_sweep": swept}
    if bos_aligned:
        score += 15
        breakdown["bos_bonus"] = 15
    if swept:
        score += 20
        breakdown["sweep_bonus"] = 20
    c_last = facts.last_candle_5m()
    reaction_extremity = c_last["Low"] if side == "BUY" else c_last["High"]
    fraction = facts.fib_fraction(reaction_extremity)
    if fraction >= 0.6:
        score += 10
        breakdown["deep_pullback_bonus"] = 10

    entry = float(c_last["Close"])
    # sl_raw: structural reference is the FAR edge of the fib band (a
    # fixed FIB_ZONE_FAR retrace, not the adaptive zone itself) — if price
    # retraces beyond that, the pullback thesis is simply wrong.
    if side == "BUY":
        sl_raw = facts.swing_high - FIB_ZONE_FAR * (facts.swing_high - facts.swing_low)
    else:
        sl_raw = facts.swing_low + FIB_ZONE_FAR * (facts.swing_high - facts.swing_low)

    conviction = classify_conviction(label, score)
    fire = conviction["decision"] == "FIRE"
    base_reason = "HTF fib pullback reaction{}{}".format(
        " + aligned BOS" if bos_aligned else "",
        " + swept" if swept else "")

    return TierResult(
        activated=True, fired=fire, direction=side,
        entry=entry, sl_raw=sl_raw, tier_label=label,
        score=score, tier_rating=tier_rating_from_score(score),
        breakdown=breakdown, conviction=conviction,
        reason=base_reason + " — " + conviction["reason"],
    )


def _tier3_structure_evaluate(facts, ctx, state, now_utc):
    """
    TIER 3 — Structure Confirmation. V2's old "Tier 1 break-retest" logic,
    reframed under its own gate set (lowest priority — only gets a look
    when nothing at Tier 1/2 has already claimed the leg).

    Mandatory:
      - facts.has_choch_15m()   (a genuine flip, not a same-direction
        continuation BOS — this tier trades the FIRST confirmation of a
        new leg, not the fifth)
      - facts.bos_15m() aligned with HTF bias (implied by has_choch_15m()
        returning True only for the live leg, but checked explicitly for
        clarity/robustness)

    Strongly preferred (heavy score weight, not a hard gate — matches the
    "strongly preferred" language in the spec):
      - facts.has_liquidity_sweep(level) against the leg's own origin

    Optional (light score bonus):
      - price already inside the fib pocket (fib is optional here, Tier 2
        owns fib primarily)
    """
    label = "TIER_3_STRUCTURE"

    # NOTE: unlike Tier 1/2, this tier's STRUCTURAL trigger (a CHoCH) is
    # atomic, not a zone to sit and wait in — by the time
    # facts.has_choch_15m() is true, the confirming break has already
    # happened, so there's no structural WATCHING state to pass through.
    # activated is therefore always True here once the mandatory gates
    # pass. `fired`, however, is now gated by conviction (Phase 3) same
    # as Tier 1/2 — a low-conviction Tier 3 setup is REJECTED, not
    # fired, even though the CHoCH event itself already happened. This
    # means Tier 3 CAN now sit as an activated-but-not-fired (REJECTED)
    # owner — it just never sits as WATCHING, since there's nothing
    # structural left to wait for; only conviction stands between it
    # and firing.

    if not facts.has_choch_15m():
        return TierResult(tier_label=label, reason="current 15M leg is a continuation BOS, not a CHoCH")

    bos = facts.bos_15m()
    if bos is None or bos["direction"] != facts.macro_bias:
        return TierResult(tier_label=label, reason="no 15M structure aligned with 1H bias")

    direction = facts.macro_bias
    side = bias_to_side(direction)
    swept = facts.has_liquidity_sweep(bos["impulse_start"])

    score = 45
    breakdown = {"choch": True, "bos_aligned": True, "liquidity_sweep": swept}
    if swept:
        score += 30
        breakdown["sweep_bonus"] = 30
    else:
        score -= 10
        breakdown["no_sweep_penalty"] = -10

    if facts.price_in_zone(facts.fib_zone()):
        score += 10
        breakdown["fib_confluence_bonus"] = 10

    entry = float(facts.last_candle_5m()["Close"])
    # sl_raw: the leg's own origin fractal — if price trades back through
    # the point the CHoCH originated from, the new structure is invalid.
    sl_raw = bos["impulse_start"]

    conviction = classify_conviction(label, score)
    fire = conviction["decision"] == "FIRE"
    base_reason = "CHoCH/BOS structure confirmation{}".format(" + swept origin" if swept else " (unswept)")

    return TierResult(
        activated=True, fired=fire, direction=side,
        entry=entry, sl_raw=sl_raw, tier_label=label,
        score=score, tier_rating=tier_rating_from_score(score),
        breakdown=breakdown, conviction=conviction,
        reason=base_reason + " — " + conviction["reason"],
    )


TIER_REGISTRY = {
    "TIER_1_POI":       _tier1_poi_evaluate,
    "TIER_2_FIB":        _tier2_fib_evaluate,
    "TIER_3_STRUCTURE":  _tier3_structure_evaluate,
}


def _gate_stale_bias(result, state):
    """
    Hard live-fire gate (per chat — real live risk, not just a research
    finding): when the confirmed 1H bias is STALE (the leg that last
    confirmed it has since invalidated and nothing fresh has replaced
    it), a tier may still ACTIVATE or sit WATCHING — leg-ownership
    bookkeeping stays honest either way — but it can never actually
    FIRE a live signal off a held-over, no-longer-confirmed direction.

    MUST be applied to `result` BEFORE the caller decides
    apply_leg_ownership(status="FIRED" if result.fired else "WATCHING").
    Applying it after would still record the leg as FIRED in state for
    a signal that was silently suppressed — permanently freezing that
    leg as "untouchable FIRED" for a trade that never happened. Every
    call site in evaluate_rule_of_law() gates immediately after _run(),
    before any ownership decision — see the three call sites.
    """
    if not (result.fired and state.get("macro_bias_stale")):
        return result
    conviction = dict(result.conviction) if result.conviction else None
    if conviction is not None:
        conviction["decision"] = "REJECT"
        conviction["reason"] = "macro_bias_stale=True — held-over direction, not live-confirmed"
    return TierResult(
        activated=result.activated, fired=False, direction=result.direction,
        entry=result.entry, sl_raw=result.sl_raw, tier_label=result.tier_label,
        score=result.score, tier_rating=result.tier_rating,
        breakdown=result.breakdown, conviction=conviction,
        reason=result.reason + " — BLOCKED: macro_bias_stale=True "
               "(1H bias is a held-over direction, not live-confirmed)",
        state_updates=result.state_updates,
    )


def evaluate_rule_of_law(facts, ctx, state, stats, now_utc):
    """
    The arbitration layer. Returns the winning tier's TierResult (or an
    empty TierResult if nothing activated). Every state mutation this
    function needs goes through apply_leg_ownership() / apply_state_updates()
    — nothing here pokes state["leg_owner"] directly.

    Ownership rules:
      1. Compute leg_id from the current confirmed 1H swing. If the
         existing owner's leg_id no longer matches (a genuinely new 1H
         leg formed), release immediately regardless of which tier held it.
      2. If a tier still owns this leg:
           - status == "FIRED": untouchable. Only the owner is evaluated.
             (In practice manage_active_trade() already freezes the whole
             scan before this runs once a trade is open — this branch
             mainly covers the brief window between an entry firing and
             the trade being recorded.)
           - status == "WATCHING" and not yet upgraded: every
             STRICTLY-HIGHER-priority tier is evaluated first. The first
             one that activates STEALS ownership (an "upgrade") and is
             returned immediately — lower/equal-priority tiers, including
             the current owner, are not re-checked this scan.
           - No higher tier activated (or the leg was already upgraded
             once): fall through to re-evaluating the current owner only.
      3. Leg is unclaimed: walk TIER_PRIORITY top to bottom, first
         activation claims it. Lower-priority tiers are never evaluated
         once a higher one has already activated.
      4. Nothing activates: leg stays unclaimed, counted in stats.
    """
    leg_id = compute_leg_id(facts.macro_bias, facts.swing_high, facts.swing_low)
    owner = get_leg_owner(state)

    if owner is not None and not _same_leg(owner["leg_id"], leg_id):
        apply_leg_ownership(state, {"action": "release", "reason": "1H leg changed"})
        owner = None

    # Timeline resets whenever the underlying leg changes, same trigger as
    # leg ownership above — a new leg means a new hypothetical setup, so
    # stage timestamps from the previous leg are no longer meaningful.
    # PURE compute + apply_state_updates(), same discipline as every
    # other section of this function.
    apply_state_updates(state, compute_signal_timeline_reset(state, leg_id))
    timeline_now = state.get("signal_timeline", {})
    new_stages = compute_signal_timeline_updates(facts, timeline_now, now_utc)
    if new_stages:
        merged_timeline = dict(timeline_now)
        merged_timeline.update(new_stages)
        apply_state_updates(state, {"signal_timeline": merged_timeline})

    def _run(tier_label):
        result = TIER_REGISTRY[tier_label](facts, ctx, state, now_utc)
        apply_state_updates(state, result.state_updates)
        return result

    if owner is not None and owner["status"] == "FIRED":
        # Hard short-circuit — a FIRED owner is never re-evaluated, by
        # anyone, for any reason. In practice manage_active_trade()
        # already freezes the whole scan before this function runs again
        # once a trade is genuinely open, so this branch only guards
        # against leg_owner and stats["active_trade"] ever desyncing —
        # but if that happens, silently downgrading a FIRED owner back to
        # WATCHING (which re-running its stub would do, since a stub has
        # no memory of "I already fired") would be worse than a no-op.
        return TierResult(activated=True, fired=False, tier_label=owner["tier"],
                           reason=f"{owner['tier']} owns this leg as FIRED — untouchable, not re-evaluated")

    if owner is not None:
        owner_priority = TIER_PRIORITY.index(owner["tier"])
        can_upgrade = owner["status"] == "WATCHING" and not owner.get("upgraded", False)

        if can_upgrade:
            for tier_label in TIER_PRIORITY[:owner_priority]:
                result = _run(tier_label)
                result = _gate_stale_bias(result, state)
                if result.activated:
                    apply_leg_ownership(state, {
                        "action": "claim", "tier": tier_label, "leg_id": leg_id,
                        "status": "FIRED" if result.fired else "WATCHING",
                        "upgraded": True,
                    })
                    stats["ownership_upgrades"] = stats.get("ownership_upgrades", 0) + 1
                    print(f"  [RULE OF LAW] {tier_label} UPGRADES ownership from "
                          f"{owner['tier']} — {result.reason}")
                    return result

        result = _run(owner["tier"])
        result = _gate_stale_bias(result, state)
        if not result.activated:
            apply_leg_ownership(state, {
                "action": "release",
                "reason": f"{owner['tier']} deactivated — {result.reason}",
            })
        else:
            apply_leg_ownership(state, {
                "action": "claim", "tier": owner["tier"], "leg_id": leg_id,
                "status": "FIRED" if result.fired else "WATCHING",
                "upgraded": owner.get("upgraded", False),
            })
        return result

    # Unclaimed leg — priority walk, first activation wins.
    for tier_label in TIER_PRIORITY:
        result = _run(tier_label)
        result = _gate_stale_bias(result, state)
        if result.activated:
            apply_leg_ownership(state, {
                "action": "claim", "tier": tier_label, "leg_id": leg_id,
                "status": "FIRED" if result.fired else "WATCHING",
                "upgraded": False,
            })
            print(f"  [RULE OF LAW] {tier_label} claims leg {leg_id} — {result.reason}")
            return result

    stats["no_leg_owner"] = stats.get("no_leg_owner", 0) + 1
    return TierResult(reason="no tier activated on this leg")


# =========================================================================
# MARKET INTELLIGENCE NETWORK — Experimental Lab. "What could we have
# learned from this setup?" Nothing in this section can ever touch
# stats["active_trade"] or state["leg_owner"] — every experiment below
# is READ-ONLY against live state and free-running against its own
# shadow_state.json/shadow_stats.json (file names unchanged — see the
# Intelligence Database banner above for why).
#
# These are called "experiments," not "tiers": in the live system tiers
# represent PRIORITY (who gets the leg). In research nothing has priority
# — every experiment runs every scan, independently, logging whatever it
# logs regardless of what any other experiment or the live bot decided.
#
#   Experiment 1 — Structure        every clean CHoCH/BOS continuation
#   Experiment 2 — Fib              every quality HTF pullback (rejection
#                                    candle ignored on purpose), 3 variants
#                                    (adaptive / fixed 38.2 / fixed 50) so
#                                    "is 38.2 better than 50?" has an answer
#   Experiment 3 — POI              every Order Block reaction AND every
#                                    quality-filtered FVG reaction, logged
#                                    and tagged separately so "which type
#                                    performs best?" is answerable
#   Experiment 4 — Liquidity        every liquidity sweep against the
#                                    macro swing level, confirmed or not
#   Experiment 5 — Filter Ablation  a Tier-3-shaped setup with exactly ONE
#                                    filter stripped per variant
#   Experiment 6 — Alternative Bias the OLD bias rule's hypothetical trade,
#                                    whenever it diverges from live bias
#   Experiment E — Rejected Live    every scan the live bot said NO, log
#                                    WHY (mandatory-condition breakdown per
#                                    tier) and track the hypothetical R
#                                    outcome anyway
# =========================================================================
import uuid


# ---- persistence -----------------------------------------------------------
def load_shadow_state():
    try:
        with open(SHADOW_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"pending": [], "last_leg": {}}


def save_shadow_state(shadow_state):
    try:
        with open(SHADOW_STATE_FILE, "w") as f:
            json.dump(shadow_state, f)
    except Exception as e:
        print("[SHADOW STATE SAVE ERROR] " + str(e))


_SHADOW_STATS_EXPERIMENT_KEYS = [
    "EXP1_STRUCTURE", "EXP2_FIB", "EXP3_POI", "EXP4_LIQUIDITY",
    "EXP5_ABLATION", "EXP6_ALT_BIAS", "EXP7_TIER_ATR", "EXPE_REJECTED_LIVE",
]


def _empty_experiment_stat():
    return {"logged": 0, "resolved": 0, "wins": 0, "losses": 0,
            "timed_out": 0, "sum_r": 0.0, "hit_1r": 0, "hit_2r": 0, "hit_3r": 0}


def load_shadow_stats():
    try:
        with open(SHADOW_STATS_FILE, "r") as f:
            stats = json.load(f)
    except Exception:
        stats = {}
    for key in _SHADOW_STATS_EXPERIMENT_KEYS:
        stats.setdefault(key, _empty_experiment_stat())
    return stats


def save_shadow_stats(shadow_stats):
    try:
        with open(SHADOW_STATS_FILE, "w") as f:
            json.dump(shadow_stats, f, indent=2)
    except Exception as e:
        print("[SHADOW STATS SAVE ERROR] " + str(e))


# ---- building + tracking setups --------------------------------------------
def build_shadow_setup(experiment, direction, entry, sl_raw, now_utc,
                        variant=None, tags=None, note="", atr_pips=None, tier_number=None):
    """A hypothetical trade for research purposes only. Returns None if the
    risk is degenerate (entry == sl_raw).

    atr_pips: the 5M ATR (in pips) at the moment this setup was logged —
    the SAME metric ATR_MIN_PIPS gates live trading on. Carried through
    to resolution and permanently appended to SHADOW_TRADE_LOG_FILE so
    ATR-vs-outcome can be analyzed later (see compute_atr_suitability()).
    tier_number: 1/2/3 when this setup mirrors one of the live tiers
    (Experiment 7 — Tier ATR Mirror); None for every other experiment.
    """
    risk = (entry - sl_raw) if direction == "BUY" else (sl_raw - entry)
    if risk is None or risk <= 0:
        return None
    sign = 1 if direction == "BUY" else -1
    return {
        "id": uuid.uuid4().hex[:12],
        "experiment": experiment,
        "variant": variant,
        "direction": direction,
        "entry": float(entry),
        "sl": float(sl_raw),
        "r1": float(entry + sign * 1 * risk),
        "r2": float(entry + sign * 2 * risk),
        "r3": float(entry + sign * 3 * risk),
        "opened_at": now_utc.isoformat(),
        "bars_open": 0,
        "max_r_reached": 0,
        "tags": tags or {},
        "note": note,
        "atr_pips": round(atr_pips, 2) if atr_pips is not None else None,
        "tier_number": tier_number,
    }


def _dedup_key(experiment, variant):
    return experiment if variant is None else f"{experiment}::{variant}"


def log_shadow_setup(shadow_state, shadow_stats, setup, leg_id):
    """Applies the per-experiment dedup (one log per leg_id) and the
    per-experiment pending cap, then appends + bumps 'logged' count."""
    if setup is None:
        return
    key = _dedup_key(setup["experiment"], setup["variant"])
    last_leg = shadow_state["last_leg"]
    if last_leg.get(key) == leg_id:
        return  # already logged this exact leg for this experiment/variant

    pending_count = sum(1 for p in shadow_state["pending"] if p["experiment"] == setup["experiment"])
    if pending_count >= SHADOW_MAX_PENDING_PER_EXPERIMENT:
        return

    last_leg[key] = leg_id
    shadow_state["pending"].append(setup)
    shadow_stats[setup["experiment"]]["logged"] += 1


def _append_shadow_trade_log(setup, outcome, r_achieved, now_utc):
    """Permanently appends ONE resolved shadow trade to
    SHADOW_TRADE_LOG_FILE (append mode — this file is never truncated or
    rewritten, unlike shadow_state.json/shadow_stats.json). This is the
    raw per-trade record (tier, ATR, result) the ATR-suitability analysis
    is built on."""
    record = {
        # FIX (per chat): build_shadow_setup() has generated a unique
        # "id" per trade all along, but this function was building the
        # permanent record from scratch and never carrying it over — so
        # the id existed only while the trade sat in the transient
        # pending queue, then vanished the moment it resolved. That's the
        # exact gap that made "pinpoint which specific historical trade
        # this is" impossible. Trades resolved BEFORE this fix simply
        # won't have a trade_id; every reader must treat it as optional.
        "trade_id":     setup.get("id"),
        "resolved_at":  now_utc.isoformat(),
        "experiment":   setup["experiment"],
        "variant":      setup["variant"],
        "tier_number":  setup["tier_number"],
        "atr_pips":     setup["atr_pips"],
        "direction":    setup["direction"],
        "opened_at":    setup["opened_at"],
        "bars_open":    setup["bars_open"],
        "outcome":      outcome,          # "WIN" / "LOSS" / "TIMEOUT_WIN" / "TIMEOUT_LOSS"
        "r_achieved":   round(r_achieved, 2),
        # AUDIT NOTE (drill-down rework): tags used to be dropped at
        # resolution time, which meant the /shadow drill-down commands
        # (session split, rejection-reason breakdown, tier-activation
        # breakdown for EXPE_REJECTED_LIVE) had nothing to read — the
        # only place `tags` ever lived was the transient pending setup.
        # Persisting them here is what makes those commands possible.
        # Trades resolved BEFORE this change will simply have tags=None;
        # every drill-down below handles that gracefully.
        "tags":         setup.get("tags") or {},
    }
    try:
        with open(SHADOW_TRADE_LOG_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print("[SHADOW TRADE LOG ERROR] " + str(e))


def update_pending_shadow_setups(shadow_state, shadow_stats, df_5m, now_utc):
    """Walks every pending shadow setup forward one bar using the latest
    closed 5M candle: updates max_r_reached, resolves (WIN/LOSS/TIMEOUT)
    and rolls the outcome into shadow_stats. Runs every scan regardless of
    what the live bot or any experiment logs THIS scan."""
    c_last = df_5m.iloc[-1]
    high, low = float(c_last["High"]), float(c_last["Low"])
    still_pending = []

    for setup in shadow_state["pending"]:
        setup["bars_open"] += 1
        direction = setup["direction"]
        exp_stats = shadow_stats.setdefault(setup["experiment"], _empty_experiment_stat())

        if direction == "BUY":
            hit_sl = low <= setup["sl"]
            for r_level, r_val in ((setup["r3"], 3), (setup["r2"], 2), (setup["r1"], 1)):
                if high >= r_level:
                    setup["max_r_reached"] = max(setup["max_r_reached"], r_val)
                    break
        else:
            hit_sl = high >= setup["sl"]
            for r_level, r_val in ((setup["r3"], 3), (setup["r2"], 2), (setup["r1"], 1)):
                if low <= r_level:
                    setup["max_r_reached"] = max(setup["max_r_reached"], r_val)
                    break

        timed_out = setup["bars_open"] >= SHADOW_MAX_PENDING_BARS
        reached_3r = setup["max_r_reached"] >= 3

        if hit_sl and setup["max_r_reached"] == 0:
            exp_stats["resolved"] += 1
            exp_stats["losses"] += 1
            exp_stats["sum_r"] += -1.0
            _append_shadow_trade_log(setup, "LOSS", -1.0, now_utc)
            continue  # resolved — drop from pending

        if reached_3r:
            exp_stats["resolved"] += 1
            exp_stats["wins"] += 1
            exp_stats["hit_1r"] += 1
            exp_stats["hit_2r"] += 1
            exp_stats["hit_3r"] += 1
            exp_stats["sum_r"] += 3.0
            _append_shadow_trade_log(setup, "WIN", 3.0, now_utc)
            continue

        if hit_sl or timed_out:
            # SL hit AFTER already banking some R, or ran out the clock —
            # either way, resolve at whatever R was actually reached.
            exp_stats["resolved"] += 1
            if timed_out and not hit_sl:
                exp_stats["timed_out"] += 1
            r = setup["max_r_reached"]
            if r > 0:
                exp_stats["wins"] += 1
            else:
                exp_stats["losses"] += 1
            exp_stats["sum_r"] += float(r) if r > 0 else -1.0
            if r >= 1:
                exp_stats["hit_1r"] += 1
            if r >= 2:
                exp_stats["hit_2r"] += 1
            outcome_label = ("TIMEOUT_WIN" if (timed_out and not hit_sl and r > 0) else
                              "TIMEOUT_LOSS" if (timed_out and not hit_sl) else
                              "WIN" if r > 0 else "LOSS")
            _append_shadow_trade_log(setup, outcome_label, float(r) if r > 0 else -1.0, now_utc)
            continue

        still_pending.append(setup)

    shadow_state["pending"] = still_pending


# ---- Experiment 1: Structure Only ------------------------------------------
def experiment_1_structure(facts, current_atr_pips, shadow_state, shadow_stats, now_utc):
    """Logs every clean CHoCH/BOS continuation aligned with 1H bias —
    answers: does structure alone have an edge, and how often does it
    reach 1R/2R/3R?"""
    if not facts.has_fresh_bos_aligned_with_bias():
        return
    bos = facts.bos_15m()
    side = bias_to_side(facts.macro_bias)
    leg_id = "{}|{:.5f}|{:.5f}".format(side, bos["impulse_start"], bos["impulse_end"])
    setup = build_shadow_setup(
        "EXP1_STRUCTURE", side, float(facts.last_candle_5m()["Close"]), bos["impulse_start"], now_utc,
        tags={"choch": facts.has_choch_15m()},
        note="clean CHoCH/BOS continuation aligned with 1H bias",
        atr_pips=current_atr_pips,
    )
    log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)


# ---- Experiment 2: Fib Only -------------------------------------------------
def experiment_2_fib(facts, current_atr_pips, shadow_state, shadow_stats, now_utc):
    """Logs every quality HTF pullback — rejection candle is IGNORED on
    purpose (per spec: 'ignore rejection candles if necessary'). Logs 3
    variants of the SAME leg (adaptive / fixed 38.2 / fixed 50) so their
    R-outcomes can be compared head to head."""
    side = bias_to_side(facts.macro_bias)
    leg_id = "{}|{:.5f}|{:.5f}".format(side, facts.swing_high, facts.swing_low)

    variants = [
        ("adaptive", facts.fib_zone()),
        ("fixed_382", facts.fib_zone(near=0.382, far=0.382)),
        ("fixed_50", facts.fib_zone(near=0.5, far=0.5)),
    ]
    for variant_name, zone in variants:
        if not facts.price_in_zone(zone):
            continue
        setup = build_shadow_setup(
            "EXP2_FIB", side, float(facts.last_candle_5m()["Close"]), zone, now_utc,
            variant=variant_name,
            tags={"rejection_candle_present": facts.rejection_candle()},
            note=f"HTF fib pullback ({variant_name}), rejection candle not required",
            atr_pips=current_atr_pips,
        )
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id + "|" + variant_name)


# ---- Experiment 3: POI Only (Order Block AND quality-filtered FVG) --------
def experiment_3_poi(facts, atr_15m_series, current_atr_pips, shadow_state, shadow_stats, now_utc):
    """Logs every Order Block reaction AND every quality FVG reaction,
    tagged separately (variant="order_block" / variant="fvg") — answers:
    are order blocks carrying the strategy, and which POI type performs
    best? Rejection candle is NOT required here (unlike live Tier 1) —
    tagged instead, so it can be sliced afterward."""
    side = bias_to_side(facts.macro_bias)

    ob = facts.order_block()
    if ob is not None and facts.price_in_order_block():
        leg_id = "ob|{}|{:.5f}|{:.5f}".format(side, ob["high"], ob["low"])
        setup = build_shadow_setup(
            "EXP3_POI", side, float(facts.last_candle_5m()["Close"]),
            ob["low"] if side == "BUY" else ob["high"], now_utc,
            variant="order_block",
            tags={"rejection_candle_present": facts.rejection_candle(),
                  "choch": facts.has_choch_15m()},
            note="Order block reaction",
            atr_pips=current_atr_pips,
        )
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)

    quality_gaps = detect_significant_fvg(facts.df_15m, facts.macro_bias, atr_15m_series)
    if quality_gaps:
        gap = quality_gaps[-1]  # nearest/most-recent quality gap
        leg_id = "fvg|{}|{}".format(side, gap["idx"])
        gap_level = gap["low"] if side == "BUY" else gap["high"]
        setup = build_shadow_setup(
            "EXP3_POI", side, float(facts.last_candle_5m()["Close"]), gap_level, now_utc,
            variant="fvg",
            tags={"size_atr_ratio": round(gap["size_atr_ratio"], 2),
                  "age_candles": gap["age_candles"],
                  "rejection_candle_present": facts.rejection_candle()},
            note="Quality-filtered FVG reaction (size>={}x ATR, age<={} candles)".format(
                FVG_MIN_SIZE_ATR_MULT, FVG_MAX_AGE_CANDLES),
            atr_pips=current_atr_pips,
        )
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)


# ---- Experiment 4: Liquidity Sweep -----------------------------------------
def experiment_4_liquidity(facts, current_atr_pips, shadow_state, shadow_stats, now_utc):
    """Logs every liquidity sweep of the macro swing level that would
    matter for the CURRENT bias, confirmed or not — answers: does every
    sweep need confirmation, and which sweep distance works best?"""
    side = bias_to_side(facts.macro_bias)
    level = facts.swing_low if facts.macro_bias == "BULLISH" else facts.swing_high
    swept = facts.has_liquidity_sweep(level)
    if not swept:
        return

    label = facts.liquidity_sweep_label(level)
    confirmed = "CONFIRMED" in label
    c_last = facts.last_candle_5m()
    distance_pips = abs((c_last["Low"] if side == "BUY" else c_last["High"]) - level) / PIP_SIZE
    # FIX (found during audit): this used to include the current candle's
    # timestamp, which changes every single scan — that defeats
    # log_shadow_setup's dedup entirely (it compares exact leg_id
    # equality), and detect_liquidity_sweep's window keeps a given sweep
    # candle "visible" for up to 3 consecutive scans, so the SAME sweep
    # was getting logged as up to 3 separate correlated "trades." Keyed
    # on side+level only now, matching how Experiments 1/2/3/6 already
    # dedup (a genuinely new leg has a different level anyway, since
    # `level` comes from the current macro swing).
    leg_id = "{}|{:.5f}".format(side, level)

    setup = build_shadow_setup(
        "EXP4_LIQUIDITY", side, float(c_last["Close"]), level, now_utc,
        tags={"confirmed": confirmed, "distance_pips": round(distance_pips, 1)},
        note=f"Liquidity sweep of macro swing level ({label})",
        atr_pips=current_atr_pips,
    )
    log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)


# ---- Experiment 5: Filter Ablation ------------------------------------------
def experiment_5_filter_ablation(facts, ctx, state, df_15m, shadow_state, shadow_stats, now_utc):
    """A Tier-3-shaped setup (CHoCH + aligned BOS) with exactly ONE filter
    stripped per variant, so any R-multiple difference vs Experiment 1 (or
    vs each other) is attributable to that ONE filter."""
    if not facts.has_fresh_bos_aligned_with_bias():
        return
    bos = facts.bos_15m()
    side = bias_to_side(facts.macro_bias)
    choch = facts.has_choch_15m()
    swept = facts.has_liquidity_sweep(bos["impulse_start"])
    bias_stale = state.get("macro_bias_stale", False)
    entry = float(facts.last_candle_5m()["Close"])

    # variant: no_liquidity_sweep — only log the un-swept subset (the
    # swept subset is already fully covered by Experiment 1 / Tier 3).
    if choch and not swept:
        leg_id = "nls|{}|{:.5f}".format(side, bos["impulse_start"])
        setup = build_shadow_setup("EXP5_ABLATION", side, entry, bos["impulse_start"], now_utc,
                                    variant="no_liquidity_sweep",
                                    note="CHoCH+BOS setup that would be REJECTED by a sweep-required gate",
                                    atr_pips=ctx.current_atr_pips)
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)

    # variant: no_ema_agreement — proxy: log even while the 1H bias is
    # STALE (i.e. ignore the distrust a stale bias normally earns).
    if choch and bias_stale:
        leg_id = "nea|{}|{:.5f}".format(side, bos["impulse_start"])
        setup = build_shadow_setup("EXP5_ABLATION", side, entry, bos["impulse_start"], now_utc,
                                    variant="no_ema_agreement",
                                    note="CHoCH+BOS setup taken despite a STALE 1H bias",
                                    atr_pips=ctx.current_atr_pips)
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)

    # variant: choch_only — isolated CHoCH-only bucket (excludes plain
    # continuation BOS, which Experiment 1 mixes in).
    if choch:
        leg_id = "co|{}|{:.5f}".format(side, bos["impulse_start"])
        setup = build_shadow_setup("EXP5_ABLATION", side, entry, bos["impulse_start"], now_utc,
                                    variant="choch_only",
                                    note="Pure CHoCH-only bucket, continuation BOS excluded",
                                    atr_pips=ctx.current_atr_pips)
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)

    # variant: bias_15m — use the 15M structure's OWN direction as the
    # authority instead of the 1H macro bias (may DISAGREE with facts.macro_bias).
    bos_15m_only = detect_bos_impulse(df_15m.tail(SWING_LOOKBACK_15), wing=FRACTAL_WING,
                                       break_buffer_atr_mult=BOS_15M_BREAK_BUFFER_ATR_MULT)
    if bos_15m_only is not None:
        alt_side = bias_to_side(bos_15m_only["direction"])
        leg_id = "b15|{}|{:.5f}|{:.5f}".format(alt_side, bos_15m_only["impulse_start"], bos_15m_only["impulse_end"])
        setup = build_shadow_setup("EXP5_ABLATION", alt_side, entry, bos_15m_only["impulse_start"], now_utc,
                                    variant="bias_15m",
                                    tags={"agrees_with_1h_bias": alt_side == side},
                                    note="15M structure used AS the bias authority (may diverge from 1H)",
                                    atr_pips=ctx.current_atr_pips)
        log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)


# ---- Experiment 6: Alternative Bias Logic ----------------------------------
def experiment_6_alt_bias(facts, state, current_atr_pips, shadow_state, shadow_stats, now_utc):
    """Whenever the OLD bias rule (compute_macro_bias_shadow_old_rule)
    disagrees with the live 1H bias, logs the hypothetical CHoCH/BOS-style
    trade IN THE OLD RULE'S DIRECTION, so the divergence gets a real R
    outcome instead of just a print statement."""
    shadow_bias = state.get("shadow_macro_bias_confirmed")
    if shadow_bias not in ("BULLISH", "BEARISH") or shadow_bias == facts.macro_bias:
        return

    side = bias_to_side(shadow_bias)
    entry = float(facts.last_candle_5m()["Close"])
    origin = state.get("shadow_macro_leg_origin")
    if origin is None:
        return
    leg_id = "{}|{:.5f}".format(side, origin)
    setup = build_shadow_setup("EXP6_ALT_BIAS", side, entry, origin, now_utc,
                                note=f"Old-rule bias ({shadow_bias}) diverges from live bias ({facts.macro_bias})",
                                atr_pips=current_atr_pips)
    log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)


# ---- Experiment E: Rejected Live Trades ------------------------------------
def experiment_e_rejected_live(facts, ctx, state, live_result, now_utc,
                                shadow_state, shadow_stats):
    """Every time the live bot says NO, record WHY (per-tier mandatory-
    condition breakdown) and still track a hypothetical R outcome using
    the current bias direction + a generic ATR-based stop, so you can see
    which filters are genuinely protecting you vs just cutting frequency."""
    if live_result.fired:
        return  # bot said YES this scan — nothing to log here

    checks = {}
    for tier_label, tier_fn in TIER_REGISTRY.items():
        peek = tier_fn(facts, ctx, state, now_utc)  # read-only peek — state_updates discarded
        checks[tier_label] = {"activated": peek.activated, "reason": peek.reason}

    # A tier can peek as structurally activated even on a scan where the
    # GLOBAL ATR floor (ctx.tradeable) blocked the whole scan before
    # Rule of Law ever ran — evaluate_rule_of_law() is never called in
    # that case, so no tier-level conviction check happened at all. Without
    # this flag, a tier blocked by the ATR floor and a tier blocked by its
    # OWN conviction score are indistinguishable in the persisted record —
    # this is the fix that makes /shadow blocked <tier> possible.
    blocked_by_atr = not ctx.tradeable

    side = bias_to_side(facts.macro_bias)
    entry = float(facts.last_candle_5m()["Close"])
    generic_sl_distance = max(SL_ATR_MULT * facts.current_atr_5m(), SL_MIN_PIPS * PIP_SIZE)
    sl_raw = entry - generic_sl_distance if side == "BUY" else entry + generic_sl_distance

    # FIX (found during audit): this used to include entry rounded to
    # the pip, which drifts almost every scan — same dedup-defeating bug
    # as Experiment 4, but worse in practice, since a rejection reason
    # like "CONSOLIDATION" or "no fresh BOS" can persist for HOURS,
    # spamming a new correlated pseudo-trade every 5 minutes until the
    # pending cap absorbed it. Anchored to the actual leg identity
    # (swing high/low) + reason instead — stable while the same
    # underlying setup/rejection persists, fresh once the leg genuinely
    # changes, matching the dedup semantics every other experiment uses.
    leg_id = "{}|{:.5f}|{:.5f}|{}".format(side, facts.swing_high, facts.swing_low, live_result.reason)
    setup = build_shadow_setup(
        "EXPE_REJECTED_LIVE", side, entry, sl_raw, now_utc,
        tags={"_blocked_by_atr": blocked_by_atr, **checks},
        note="Live bot took no action this scan — " + (live_result.reason or "no tier activated"),
        atr_pips=ctx.current_atr_pips,
    )
    log_shadow_setup(shadow_state, shadow_stats, setup, leg_id)


# ---- Experiment 7: Tier ATR Mirror -----------------------------------------
MARKET_STATE_VOLATILITY_LOOKBACK  = 10  # candles back for expanding/contracting comparison
MARKET_STATE_COMPRESSION_LOOKBACK = 10  # candles used for the recent-range/ATR ratio


def compute_market_state(facts, bos):
    """
    Market state snapshot (per chat — "it should think in terms of
    market states, not just trade outcomes"). Pure, single-scan,
    CURRENT-moment ("at entry") snapshot.

    Deliberately OMITS a "liquidity" field: Twelve Data's forex feed has
    no volume/order-flow data at all, so any liquidity figure here would
    be a plausible-looking guess wearing a real name, not a measurement
    — same reasoning as declining a fake "volume expansion" field
    earlier tonight.

    Deliberately does NOT (yet) compute this retroactively at the OB's
    FORMATION point — only "now". That needs a truncated historical
    slice of df_15m with correctly re-aligned ATR, real complexity
    worth its own careful pass rather than rushing tonight. was_choch
    (on detect_bos_impulse's return dict, see there) already captures
    the single most important piece of "how did this form" — full
    formation-time state is the next safe increment, not this one.
    """
    df = facts.df_15m
    atr_series = facts._atr_15m_series
    current_atr = atr_series.iloc[-1]
    current_atr_pips = (current_atr / PIP_SIZE) if current_atr else None

    trend_strength = None
    pullback_depth_pct = None
    if bos and current_atr_pips:
        leg_range = abs(bos["impulse_end"] - bos["impulse_start"])
        trend_strength = round((leg_range / PIP_SIZE) / current_atr_pips, 2)
        if leg_range > 0:
            last_close = df["Close"].iloc[-1]
            pullback_depth_pct = round(abs(bos["impulse_end"] - last_close) / leg_range * 100, 1)

    volatility_state = None
    if len(atr_series.dropna()) > MARKET_STATE_VOLATILITY_LOOKBACK:
        atr_then = atr_series.iloc[-1 - MARKET_STATE_VOLATILITY_LOOKBACK]
        if atr_then and current_atr:
            change = (current_atr - atr_then) / atr_then
            volatility_state = "expanding" if change > 0.15 else "contracting" if change < -0.15 else "flat"

    compression_ratio = None
    if len(df) > MARKET_STATE_COMPRESSION_LOOKBACK and current_atr_pips:
        recent = df.tail(MARKET_STATE_COMPRESSION_LOOKBACK)
        recent_range_pips = (recent["High"].max() - recent["Low"].min()) / PIP_SIZE
        compression_ratio = round(recent_range_pips / current_atr_pips, 2)

    return {
        "trend_strength_atr_mult": trend_strength,
        "volatility_state": volatility_state,
        "compression_ratio": compression_ratio,
        "pullback_depth_pct": pullback_depth_pct,
    }


def classify_regime(facts, ctx, state):
    """
    Discretizes current market conditions into regime tags (per chat —
    salvaged from the Thompson Sampling pitch). The bandit/tier-selection
    part of that proposal was rejected as a category error (tiers aren't
    interchangeable bandit arms, they're a structural priority
    waterfall) — but this piece, classifying the current regime for
    TAGGING purposes only, is safe and useful on its own.

    Returns a dict of SEPARATE fields (atr_bucket, session, bias_state,
    spike_state), not one concatenated regime string. That's deliberate:
    a combined string ("low_vol_london_early_fresh_normal") forces exact
    match across all 4 dimensions before anything counts as "similar" —
    the exact cell-fragmentation problem that made the original 48-regime
    Thompson Sampling design impractical. Separate fields let Evidence &
    Research / Research Centre filter by ANY SUBSET later (e.g. "just
    show me high_vol trades", ignoring session) instead of requiring all
    4 to match at once.

    Pure function. Used only to TAG resolved trades for later filtering
    — never to select a tier or make a live decision.
    """
    atr_pct = facts.atr_percentile_15m()
    if atr_pct is None:
        atr_bucket = "unknown"
    elif atr_pct < 25:
        atr_bucket = "low_vol"
    elif atr_pct < 75:
        atr_bucket = "normal_vol"
    else:
        atr_bucket = "high_vol"

    hour = facts.now_utc.hour
    if 7 <= hour < 12:
        session = "london_early"
    elif 12 <= hour < 16:
        session = "london_ny_overlap"
    elif 16 <= hour < 21:
        session = "ny_late"
    else:
        session = "off_session"

    bias_state = "stale" if state.get("macro_bias_stale") else "fresh"
    spike_state = "post_spike" if ctx.post_spike_active else "normal"

    return {
        "atr_bucket": atr_bucket,
        "session": session,
        "bias_state": bias_state,
        "spike_state": spike_state,
    }


def experiment_7_tier_atr_mirror(facts, ctx, state, now_utc, shadow_state, shadow_stats):
    """
    Answers the friend's question directly: for EACH live tier, log the
    setup + current ATR(5m, pips) + eventual R result — WITHOUT the
    ATR_MIN_PIPS floor gating anything. Runs every scan regardless of
    ctx.tradeable, including scans the live bot skips entirely for being
    below the ATR floor, so the resulting dataset spans the FULL ATR
    range, not just the range that already clears the current threshold
    (which would make the analysis circular).

    Each tier is peeked read-only (state_updates discarded, exactly like
    Experiment E) — this can never claim a leg or touch live state. A
    tier is logged if its OWN mandatory conditions are met (`activated`),
    regardless of conviction score or the live ATR gate — conviction and
    ATR floor are both things we're trying to CALIBRATE, so neither
    should filter the data used to calibrate them.

    After 200-500 of these accumulate, run compute_atr_suitability() (or
    /atrbands) to see each tier's real ATR sweet spot, then eventually
    let CONVICTION_MIN_BY_TIER / an ATR-suitability score replace
    ATR_MIN_PIPS as a hard gate entirely.
    """
    leg_key = compute_leg_id(facts.macro_bias, facts.swing_high, facts.swing_low)
    entry = float(facts.last_candle_5m()["Close"])

    # Fingerprint facts (per chat) — cheap, self-contained, computed once
    # per scan since they describe the current leg/market, not any one
    # tier. Deliberately NOT included here: EMA slope (would need df_1h
    # threaded into this function, which doesn't receive it today —
    # bigger plumbing change, held back rather than rushed), sweep
    # distance (needs detect_liquidity_sweep's return signature changed,
    # done separately below), volume (Twelve Data's forex feed has no
    # volume field at all — not a "later", genuinely unavailable),
    # reaction-time-in-candles (needs cross-scan state tracking of first
    # zone touch — real bug surface, held back for its own session).
    bos = facts.bos_15m()
    ob = facts.order_block()
    fingerprint = {
        "leg_length_pips": round(abs(bos["impulse_end"] - bos["impulse_start"]) / PIP_SIZE, 1) if bos else None,
        "break_count": bos["break_count"] if bos else None,
        "atr_percentile_15m": facts.atr_percentile_15m(),
        "ob_freshness_candles": (len(facts.df_15m) - 1 - ob["origin_idx"]) if ob else None,
        # Same level Tier 3 already checks liquidity_sweep against
        # (bos["impulse_start"]) — kept consistent so this is directly
        # comparable to that tier's own boolean flag, not a different
        # measurement of a different thing wearing a similar name.
        "sweep_distance_pips": facts.sweep_distance_pips(bos["impulse_start"]) if bos else None,
        # Regime tags (per chat, salvaged from Thompson Sampling pitch) —
        # tagging only, no decision logic. See classify_regime()'s
        # docstring for why these are 4 separate fields, not one
        # concatenated string.
        **classify_regime(facts, ctx, state),
        # Formation story (per chat) — was THIS leg a genuine CHoCH flip,
        # or a fresh formation with no prior dominant to flip from? See
        # detect_bos_impulse()'s was_choch tracking.
        "was_choch": bos["was_choch"] if bos else None,
        **compute_market_state(facts, bos),
    }

    for tier_label, tier_fn in TIER_REGISTRY.items():
        peek = tier_fn(facts, ctx, state, now_utc)  # read-only — state_updates discarded
        if not peek.activated:
            continue

        tier_number = TIER_NUMBER.get(tier_label)
        # Use the tier's own proposed entry/sl when it fired one; fall
        # back to current price + that tier's structural sl_raw is
        # unavailable pre-fire, so a non-fired-but-activated peek (e.g.
        # blocked only by conviction) still needs SOME sl. Re-derive it
        # the same way the tier would if it fires, by using peek.sl_raw
        # when present (tiers always set it alongside activated=True in
        # this codebase), otherwise skip — no structural sl means no
        # honest R multiple.
        if peek.sl_raw is None:
            continue

        setup = build_shadow_setup(
            "EXP7_TIER_ATR", peek.direction or bias_to_side(facts.macro_bias),
            entry, peek.sl_raw, now_utc,
            variant=tier_label,
            # "Store everything" (per chat) — conviction_score/
            # would_have_fired_live/atr_floor_pips were already here;
            # peek.breakdown is merged in so the EVIDENCE ENGINE has the
            # tier's actual structural facts (order_block, choch, sweep,
            # etc.) to compare against, not just its final score. Raw
            # facts, not another derived number — same principle as the
            # friend's "rows of facts, not scores" design.
            tags={"conviction_score": peek.score, "would_have_fired_live": peek.fired,
                  "atr_floor_pips": ATR_MIN_PIPS, **(peek.breakdown or {}), **fingerprint},
            note=f"Tier {tier_number} ({tier_label}) mirror — ATR {ctx.current_atr_pips:.1f}p, "
                 f"ATR floor {'MET' if ctx.tradeable else 'NOT MET (this scan would be skipped live)'}",
            atr_pips=ctx.current_atr_pips,
            tier_number=tier_number,
        )
        log_shadow_setup(shadow_state, shadow_stats, setup, f"{leg_key}|{tier_label}")


def compute_atr_suitability(band_width=ATR_SUITABILITY_BAND_WIDTH_PIPS):
    """
    Reads the PERMANENT shadow_trade_log.jsonl and buckets EXP7_TIER_ATR
    records by (tier_number, ATR band) -> {n, win_rate, avg_r}. This is
    the exact table the friend described: rows = ATR band, columns =
    Tier 1/2/3 win rate. Pure function — safe to call from a Telegram
    command or an offline analysis script.
    """
    table = {}  # {tier_number: {band_label: {"n":, "wins":, "sum_r":}}}
    try:
        with open(SHADOW_TRADE_LOG_FILE, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return table

    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("experiment") != "EXP7_TIER_ATR":
            continue
        tier_number = rec.get("tier_number")
        atr_pips = rec.get("atr_pips")
        if tier_number is None or atr_pips is None:
            continue

        band_floor = int(atr_pips // band_width) * band_width
        band_label = f"{band_floor:.0f}-{band_floor + band_width:.0f}"

        tier_table = table.setdefault(tier_number, {})
        bucket = tier_table.setdefault(band_label, {"n": 0, "wins": 0, "sum_r": 0.0})
        bucket["n"] += 1
        r = rec.get("r_achieved", 0.0)
        if r > 0:
            bucket["wins"] += 1
        bucket["sum_r"] += r

    return table


def format_atr_suitability_table(band_width=ATR_SUITABILITY_BAND_WIDTH_PIPS, min_n=1):
    """Human-readable ATR-band x Tier win-rate table — see /atrbands."""
    table = compute_atr_suitability(band_width)
    if not table:
        return None

    all_bands = sorted(
        {band for tier_table in table.values() for band in tier_table},
        key=lambda b: float(b.split("-")[0]),
    )
    lines = ["📊 *ATR Suitability — Tier x ATR band*\n"]
    header = "Band(p)  | " + " | ".join(f"Tier {t}" for t in sorted(table.keys()))
    lines.append(f"`{header}`")
    for band in all_bands:
        row = [f"{band:>7}"]
        for t in sorted(table.keys()):
            bucket = table[t].get(band)
            if bucket is None or bucket["n"] < min_n:
                row.append("   —  ")
            else:
                wr = bucket["wins"] / bucket["n"] * 100
                row.append(f"{wr:5.0f}%")
        lines.append("`" + " | ".join(row) + "`")

    lines.append("")
    for t in sorted(table.keys()):
        total_n = sum(b["n"] for b in table[t].values())
        total_r = sum(b["sum_r"] for b in table[t].values())
        lines.append(f"Tier {t}: {total_n} logged, avg R {total_r/total_n:+.2f}" if total_n else f"Tier {t}: 0 logged")
    return "\n".join(lines)


# ---- orchestrator -----------------------------------------------------------
def _evidence_similarity(current_tags, historical_tags, keys):
    """Fraction of `keys` on which current_tags and historical_tags agree
    (both present AND equal). A key missing from either side counts as a
    mismatch, not a skip — an older log line without a key can't be
    assumed to match it. PURE."""
    if not keys:
        return 0.0
    matches = sum(
        1 for k in keys
        if k in current_tags and k in historical_tags and current_tags[k] == historical_tags[k]
    )
    return matches / len(keys)


def compute_evidence(tier_label, breakdown, min_n=EVIDENCE_MIN_N):
    """
    Phase 4 — Evidence Engine. PURE, read-only, informational ONLY.

    "Jury, not judge" (per chat): looks up this tier's OWN permanent
    EXP7_TIER_ATR log — tier-isolated on purpose, Tier 1 only ever learns
    from Tier 1 — keeps records whose structural facts agree with
    `breakdown` on at least EVIDENCE_MATCH_THRESHOLD of that tier's
    TIER_EVIDENCE_KEYS, and reports what actually happened to them.

    Returns None (stays SILENT — the "sleeping pathway") if fewer than
    `min_n` similar RESOLVED trades exist yet. There is no code path from
    this function back into `fired`, `decision`, `classify_conviction`,
    or sizing — callers only ever use its output to annotate a message,
    never to decide anything. If that ever changes, it stops being a
    jury and starts being a second, un-validated rule engine — see chat
    history for why that's the one thing this must never become.
    """
    keys = TIER_EVIDENCE_KEYS.get(tier_label)
    if not keys or not breakdown:
        return None

    tier_number = TIER_NUMBER.get(tier_label)
    records = _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
    similar = [
        r for r in records
        if r.get("tier_number") == tier_number
        and _evidence_similarity(breakdown, r.get("tags") or {}, keys) >= EVIDENCE_MATCH_THRESHOLD
    ]

    n = len(similar)
    if n < min_n:
        return None  # dormant — not enough resolved history yet, say nothing at all

    r_values = [float(r.get("r_achieved", 0.0)) for r in similar]
    wins = sum(1 for r in r_values if r > 0)
    avg_r = sum(r_values) / n

    # "Let it say what it hit — 1R, 2R, 3R, or SL" (per chat): reuse the
    # same exact-bucket distribution Research Centre's block analysis
    # uses, rather than collapsing to one "most common" number. Two
    # different summaries of the same underlying data is exactly the
    # kind of clutter this whole reporting rework was meant to remove.
    buckets = outcome_distribution(similar)

    strength = "MODERATE"
    for floor, label in EVIDENCE_STRENGTH_BANDS:
        if n >= floor:
            strength = label
            break

    return {
        "n": n,
        "win_rate": round(100 * wins / n, 1),
        "avg_r": round(avg_r, 2),
        "outcome_buckets": buckets,
        "strength": strength,
    }


def format_evidence_note(tier_label, evidence):
    """Renders compute_evidence()'s output as a short, human-readable
    annotation — never a directive, never a decision. `evidence` may be
    None (dormant), in which case this returns None too."""
    if evidence is None:
        return None
    tier_number = TIER_NUMBER.get(tier_label, "?")
    dist_line = format_outcome_distribution(evidence["outcome_buckets"])
    return (
        f"📚 *Research Evidence (Tier {tier_number}, informational only):*\n"
        f"`{evidence['n']}` similar resolved setups this tier has seen — "
        f"`{evidence['win_rate']}%` win rate, avg `{evidence['avg_r']}R`\n"
        + (dist_line + "\n" if dist_line else "")
        + f"Strength: `{evidence['strength']}` — this never overrides Rule of Law."
    )


def run_shadow_pipeline(facts, ctx, state, df_15m, live_result, now_utc):
    """
    Single entry point called from scan(). Every experiment is wrapped so
    ONE broken experiment can never take down the live bot or another
    experiment — errors are logged and swallowed, never raised.

    IMPORTANT: this is called from scan() in BOTH the normal path AND the
    ATR-too-low path — see scan()'s "SHADOW PIPELINE" comments. That's
    intentional: it's what "remove the ATR rejection" from the shadow
    pipeline means in practice. Only the LIVE trade path stays gated by
    ATR_MIN_PIPS; nothing in this function is.
    """
    shadow_state = load_shadow_state()
    shadow_stats = load_shadow_stats()

    try:
        update_pending_shadow_setups(shadow_state, shadow_stats, facts.df_5m, now_utc)
    except Exception as e:
        print("[SHADOW ERROR] update_pending: " + str(e))

    atr_15m_series = atr(df_15m, period=14)
    current_atr_pips = ctx.current_atr_pips

    experiments = [
        ("Experiment 1 (Structure)", lambda: experiment_1_structure(facts, current_atr_pips, shadow_state, shadow_stats, now_utc)),
        ("Experiment 2 (Fib)", lambda: experiment_2_fib(facts, current_atr_pips, shadow_state, shadow_stats, now_utc)),
        ("Experiment 3 (POI)", lambda: experiment_3_poi(facts, atr_15m_series, current_atr_pips, shadow_state, shadow_stats, now_utc)),
        ("Experiment 4 (Liquidity)", lambda: experiment_4_liquidity(facts, current_atr_pips, shadow_state, shadow_stats, now_utc)),
        ("Experiment 5 (Filter Ablation)", lambda: experiment_5_filter_ablation(
            facts, ctx, state, df_15m, shadow_state, shadow_stats, now_utc)),
        ("Experiment 6 (Alt Bias)", lambda: experiment_6_alt_bias(facts, state, current_atr_pips, shadow_state, shadow_stats, now_utc)),
        ("Experiment 7 (Tier ATR Mirror)", lambda: experiment_7_tier_atr_mirror(
            facts, ctx, state, now_utc, shadow_state, shadow_stats)),
        ("Experiment E (Rejected Live)", lambda: experiment_e_rejected_live(
            facts, ctx, state, live_result, now_utc, shadow_state, shadow_stats)),
    ]
    for name, fn in experiments:
        try:
            fn()
        except Exception as e:
            print(f"[SHADOW ERROR] {name}: {e}")

    save_shadow_state(shadow_state)
    save_shadow_stats(shadow_stats)


# ---- Market Intelligence Network reporting: dashboard + drill-downs ---
# REWORK NOTE: /shadow used to print one full paragraph per experiment,
# which stopped being readable once EXPE_REJECTED_LIVE alone hit 90+
# logged setups. Now /shadow is a compact aligned table (a health check,
# not a report), and everything else lives behind drill-down commands:
#   /shadow            -> this table (renamed "Market Intelligence
#                          Network" on screen — command itself left as
#                          /shadow on purpose, so nothing you already
#                          type by habit breaks)
#   /shadow <name>     -> per-experiment detail (Experimental Lab, aliases below)
#   /shadow rejected   -> EXPE_REJECTED_LIVE-specific breakdown (Evidence & Research)
#   /shadow blocked    -> per-tier block-reason breakdown (Evidence & Research)
#   /shadow recent     -> last 10 resolved shadow trades, any experiment
#   /leaderboard       -> every experiment ranked by avg R

_SHADOW_DISPLAY_NAME = {
    "EXP1_STRUCTURE":     "EXP1 Structure",
    "EXP2_FIB":           "EXP2 Fib",
    "EXP3_POI":           "EXP3 POI",
    "EXP4_LIQUIDITY":     "EXP4 Liquidity",
    "EXP5_ABLATION":      "EXP5 Ablation",
    "EXP6_ALT_BIAS":      "EXP6 Alt Bias",
    "EXP7_TIER_ATR":      "EXP7 Tier ATR",
    "EXPE_REJECTED_LIVE": "Rejected Live",
}

# What a person is likely to type for /shadow <name> -> canonical stats key.
_SHADOW_ALIASES = {
    "exp1": "EXP1_STRUCTURE", "structure": "EXP1_STRUCTURE", "exp1_structure": "EXP1_STRUCTURE",
    "exp2": "EXP2_FIB", "fib": "EXP2_FIB", "exp2_fib": "EXP2_FIB",
    "exp3": "EXP3_POI", "poi": "EXP3_POI", "exp3_poi": "EXP3_POI",
    "exp4": "EXP4_LIQUIDITY", "liquidity": "EXP4_LIQUIDITY", "exp4_liquidity": "EXP4_LIQUIDITY",
    "exp5": "EXP5_ABLATION", "ablation": "EXP5_ABLATION", "exp5_ablation": "EXP5_ABLATION",
    "exp6": "EXP6_ALT_BIAS", "altbias": "EXP6_ALT_BIAS", "exp6_alt_bias": "EXP6_ALT_BIAS",
    "exp7": "EXP7_TIER_ATR", "atr": "EXP7_TIER_ATR", "exp7_tier_atr": "EXP7_TIER_ATR",
    "rejected": "EXPE_REJECTED_LIVE", "reject": "EXPE_REJECTED_LIVE", "live": "EXPE_REJECTED_LIVE",
}

# Rejection-reason bucketing. These substrings are copied verbatim from
# the actual TierResult.reason strings each tier returns (see the
# `reason=` grep in the module docstring area / tier evaluate() functions)
# — NOT a guessed category list. If a tier's wording changes, update the
# matching substring here too, or the bucket will silently fall through
# to "Other / Unclassified."
_REJECTION_REASON_BUCKETS = [
    ("ATR too low",                         "Market Context (ATR/session/regime)"),
    ("no order block",                      "POI — no order block"),
    ("price not currently inside the order block", "POI — price not in zone"),
    ("price not in the HTF fib pocket",     "Fib — price not in pocket"),
    ("not a CHoCH",                         "Structure — continuation, not CHoCH"),
    ("no 15M structure aligned",            "Structure — no aligned structure"),
    ("no fresh 15M BOS",                    "Structure — no fresh BOS"),
    ("conviction",                          "Conviction score below minimum"),
    ("no tier activated",                   "No tier activated at all"),
]


def _classify_rejection_reason(reason):
    if not reason:
        return "Other / Unclassified"
    for needle, bucket in _REJECTION_REASON_BUCKETS:
        if needle.lower() in reason.lower():
            return bucket
    return "Other / Unclassified"


def _classify_session(iso_ts):
    """Buckets an ISO timestamp into London / New York / Overlap /
    Off-session using the SAME SESSION_WINDOWS_UTC the live bot's
    is_active_session() already gates on — not a separately invented
    schedule."""
    try:
        dt = datetime.fromisoformat(iso_ts)
    except Exception:
        return "Unknown"
    hour = dt.hour
    london = SESSION_WINDOWS_UTC[0][0] <= hour < SESSION_WINDOWS_UTC[0][1]
    ny     = SESSION_WINDOWS_UTC[1][0] <= hour < SESSION_WINDOWS_UTC[1][1]
    if london and ny:
        return "London/NY overlap"
    if london:
        return "London"
    if ny:
        return "New York"
    return "Off-session"


def _read_shadow_trade_log(experiment=None, limit=None):
    """Reads the PERMANENT shadow_trade_log.jsonl, optionally filtered to
    one experiment. Returns a list of dicts, oldest-first. Pure, safe to
    call from a Telegram command."""
    records = []
    try:
        with open(SHADOW_TRADE_LOG_FILE, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return records
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if experiment is not None and rec.get("experiment") != experiment:
            continue
        records.append(rec)
    if limit:
        records = records[-limit:]
    return records


def format_shadow_summary(shadow_stats):
    """Compact aligned dashboard — a health check, not a report. Call
    periodically (e.g. every STATS_SUMMARY_EVERY scans) and on /shadow.
    Drill down with /shadow <name>, /shadow rejected, /shadow recent, or
    /leaderboard for anything this table doesn't show."""
    rows = []
    for key in _SHADOW_STATS_EXPERIMENT_KEYS:
        s = shadow_stats.get(key, _empty_experiment_stat())
        if s["logged"] == 0:
            continue
        win_rate = (s["wins"] / s["resolved"] * 100) if s["resolved"] else None
        avg_r = (s["sum_r"] / s["resolved"]) if s["resolved"] else None
        rows.append((key, s, win_rate, avg_r))

    if not rows:
        return None

    name_w = max(len(_SHADOW_DISPLAY_NAME.get(k, k)) for k, _, _, _ in rows)
    lines = ["🔬 *Market Intelligence Network*", "`" + "─" * (name_w + 28) + "`"]
    for key, s, win_rate, avg_r in rows:
        name = _SHADOW_DISPLAY_NAME.get(key, key).ljust(name_w)
        wr_str = f"WR {win_rate:3.0f}%" if win_rate is not None else "WR  — "
        avgr_str = f"AvgR {avg_r:+.2f}" if avg_r is not None else "AvgR  —  "
        lines.append(f"`{name}  {s['logged']:>3} | {s['resolved']:>3} | {wr_str} | {avgr_str}`")
    lines.append("")
    lines.append("_logged | resolved | win rate | avg R — drill down with /shadow <name>, "
                  "/shadow rejected, /shadow recent, or /leaderboard_")
    return "\n".join(lines)


def format_shadow_detail(shadow_stats, key):
    """Per-experiment drill-down. Aggregate counts come from shadow_stats
    (fast, always available); duration/session/direction/variant splits
    are derived from the permanent trade log, so they only reflect
    RESOLVED trades and only what's accumulated so far."""
    if key not in _SHADOW_STATS_EXPERIMENT_KEYS:
        return None
    s = shadow_stats.get(key, _empty_experiment_stat())
    if s["logged"] == 0:
        return f"🔬 *{_SHADOW_DISPLAY_NAME.get(key, key)}*\n_Nothing logged yet._"

    win_rate = (s["wins"] / s["resolved"] * 100) if s["resolved"] else None
    avg_r = (s["sum_r"] / s["resolved"]) if s["resolved"] else None

    lines = [f"🔬 *{_SHADOW_DISPLAY_NAME.get(key, key)}*", "─────────────────────"]
    lines.append(f"Logged: `{s['logged']}`   Resolved: `{s['resolved']}`")
    lines.append(f"Wins: `{s['wins']}`   Losses: `{s['losses']}`   Timed out: `{s['timed_out']}`")
    lines.append(f"Win Rate: `{win_rate:.0f}%`" if win_rate is not None else "Win Rate: `—`")
    lines.append(f"Average R: `{avg_r:+.2f}`" if avg_r is not None else "Average R: `—`")
    lines.append(f"Reached — 1R: `{s['hit_1r']}`  2R: `{s['hit_2r']}`  3R: `{s['hit_3r']}`")

    records = _read_shadow_trade_log(experiment=key)
    if records:
        durations = [r["bars_open"] for r in records if r.get("bars_open") is not None]
        if durations:
            lines.append(f"\nAverage duration: `{sum(durations)/len(durations):.1f} candles` (5m)")

        buys  = sum(1 for r in records if r.get("direction") == "BUY")
        sells = sum(1 for r in records if r.get("direction") == "SELL")
        lines.append(f"Buy/Sell: `{buys}` / `{sells}`")

        sessions = {}
        for r in records:
            sess = _classify_session(r.get("opened_at", ""))
            sessions[sess] = sessions.get(sess, 0) + 1
        if sessions:
            lines.append("\nBy Session:")
            for sess, n in sorted(sessions.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {sess}: `{n}`")

        variants = {}
        for r in records:
            v = r.get("variant")
            if v:
                variants.setdefault(v, {"n": 0, "wins": 0, "sum_r": 0.0})
                variants[v]["n"] += 1
                variants[v]["sum_r"] += r.get("r_achieved", 0.0)
                if r.get("r_achieved", 0.0) > 0:
                    variants[v]["wins"] += 1
        if variants:
            lines.append("\nBy Variant:")
            for v, d in sorted(variants.items(), key=lambda kv: -kv[1]["n"]):
                wr = d["wins"] / d["n"] * 100
                lines.append(f"  {v}: `{d['n']}` logged, WR `{wr:.0f}%`, avg R `{d['sum_r']/d['n']:+.2f}`")
    else:
        lines.append("\n_No resolved trades in the permanent log yet — "
                      "duration/session/variant splits need at least one resolution._")

    return "\n".join(lines)


def format_rejected_live_detail(shadow_stats):
    """Drill-down for EXPE_REJECTED_LIVE specifically — the one the
    friend said he'd use most. Two breakdowns:
      1. Rejection reason buckets, from EVERY logged rejection (tags are
         captured at build time, so this covers all resolved+pending).
      2. Of the ones that resolved as WINNERS, which tier(s) were
         actually "activated" (watching/close) at the moment of
         rejection — i.e. which filter cost you a real winning trade.
    Both need `tags` to have been persisted at resolution time (see the
    audit note in _append_shadow_trade_log) — records resolved before
    that fix won't have tags and are silently excluded from these two
    breakdowns (they still count in the top-line win/loss numbers).
    """
    key = "EXPE_REJECTED_LIVE"
    s = shadow_stats.get(key, _empty_experiment_stat())
    if s["logged"] == 0:
        return "🔬 *Rejected Live Analysis*\n_Nothing logged yet._"

    win_rate = (s["wins"] / s["resolved"] * 100) if s["resolved"] else None
    avg_r = (s["sum_r"] / s["resolved"]) if s["resolved"] else None

    lines = ["🔬 *Rejected Live Analysis*", "─────────────────────"]
    lines.append(f"Rejected: `{s['logged']}`   Resolved: `{s['resolved']}`")
    lines.append(f"Won: `{s['wins']}`   Lost: `{s['losses']}`")
    lines.append(f"Win Rate: `{win_rate:.0f}%`" if win_rate is not None else "Win Rate: `—`")
    lines.append(f"Average R: `{avg_r:+.2f}`" if avg_r is not None else "Average R: `—`")

    records = _read_shadow_trade_log(experiment=key)
    # What actually hit, not just win/loss (per chat) — every resolved
    # record has an exact r_achieved regardless of whether tags exist,
    # so this line works even for records logged before the tag-
    # persistence fix (unlike the reason/tier breakdowns below).
    resolved_records = [r for r in records if r.get("outcome")]
    dist_line = format_outcome_distribution(outcome_distribution(resolved_records))
    if dist_line:
        lines.append(dist_line.replace("    ↳ ", "Hit: "))

    tagged = [r for r in records if r.get("tags")]
    if not tagged:
        lines.append("\n_No tag data yet for reason/tier breakdowns — this needs at least one "
                      "resolution AFTER the tag-persistence fix. Check back after the next few "
                      "resolved rejections._")
        return "\n".join(lines)

    reason_buckets = {}
    for r in tagged:
        # tags here is {"_blocked_by_atr": bool, tier_label: {"activated","reason"}, ...}
        # — _blocked_by_atr is a SIBLING key, not a per-tier entry, so it
        # must be filtered out before treating every value as a tier dict.
        # (Found via testing just now: this crashed on the first record
        # logged after _blocked_by_atr was added — the two isinstance()
        # guards two blocks below were already written correctly; these
        # two were the gap.)
        checks = {k: v for k, v in r["tags"].items() if isinstance(v, dict)}
        any_activated = any(v.get("activated") for v in checks.values())
        if any_activated:
            bucket = "Activated but didn't fire (conviction/risk gate)"
        else:
            # Pick the most specific (non-generic) reason among the tiers.
            reasons = [v.get("reason", "") for v in checks.values()]
            bucket = _classify_rejection_reason(next((x for x in reasons if x), ""))
        reason_buckets[bucket] = reason_buckets.get(bucket, 0) + 1

    lines.append("\nReasons rejected:")
    for bucket, n in sorted(reason_buckets.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {bucket}: `{n}`")

    # Tier breakdown of the "activated but didn't fire" bucket specifically
    # — i.e. of the setups blocked by conviction/risk gate rather than a
    # tier gate, which tier(s) they belonged to, and how those actually
    # resolved. This is the denominator needed to judge whether a given
    # tier's conviction threshold is cutting real winners, not just the
    # numerator ("rejected winners") shown below.
    activated_not_fired = [r for r in tagged
                           if any(isinstance(v, dict) and v.get("activated")
                                  for v in r["tags"].values())]
    if activated_not_fired:
        tier_breakdown = {}
        for r in activated_not_fired:
            won = r.get("r_achieved", 0.0) > 0
            for tier_label, v in r["tags"].items():
                if isinstance(v, dict) and v.get("activated"):
                    d = tier_breakdown.setdefault(tier_label, {"n": 0, "wins": 0})
                    d["n"] += 1
                    if won:
                        d["wins"] += 1
        lines.append("\nOf 'activated but didn't fire', by tier:")
        for tier_label, d in sorted(tier_breakdown.items(), key=lambda kv: -kv[1]["n"]):
            wr = (d["wins"] / d["n"] * 100) if d["n"] else 0.0
            lines.append(f"  {tier_label}: `{d['n']}` (won `{d['wins']}`, `{wr:.0f}%`)")

    winners_tagged = [r for r in tagged if r.get("r_achieved", 0.0) > 0]
    if winners_tagged:
        tier_wins = {}
        for r in winners_tagged:
            for tier_label, v in r["tags"].items():
                if isinstance(v, dict) and v.get("activated"):
                    tier_wins[tier_label] = tier_wins.get(tier_label, 0) + 1
        lines.append(f"\nRejected winners: `{len(winners_tagged)}`")
        if tier_wins:
            lines.append("Of those, tier was activated but blocked elsewhere:")
            for tier_label, n in sorted(tier_wins.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {tier_label}: `{n}`")
        else:
            lines.append("_None of the tagged winners had any tier activated — "
                          "these were rejected before any tier saw a setup at all._")

    return "\n".join(lines)


def outcome_distribution(records):
    """Exact R-outcome buckets for a set of resolved shadow records — SL
    (-1R), 1R, 2R, 3R — not just win/loss. r_achieved in this codebase
    only ever lands on one of these four values (see
    update_pending_shadow_setups: it tracks max_r_reached as a discrete
    1/2/3 checkpoint crossing, or -1.0 on a stop-out), so round()-based
    bucketing here is exact, not an approximation. 'Other' exists only
    to catch anything that doesn't fit that pattern (e.g. old records
    from before this resolution logic), so it's never silently hidden."""
    buckets = {"SL (-1R)": 0, "1R": 0, "2R": 0, "3R": 0, "Other": 0}
    for r in records:
        r_val = round(float(r.get("r_achieved", 0.0)))
        if r_val == -1:
            buckets["SL (-1R)"] += 1
        elif r_val == 1:
            buckets["1R"] += 1
        elif r_val == 2:
            buckets["2R"] += 1
        elif r_val == 3:
            buckets["3R"] += 1
        else:
            buckets["Other"] += 1
    return buckets


def format_outcome_distribution(buckets):
    """Renders outcome_distribution()'s buckets as one compact line.
    Zero-count buckets are omitted rather than padded with zeros — with
    small samples, a row of mostly-zero buckets reads as more precision
    than the data actually supports."""
    parts = [f"{label} `{count}`" for label, count in buckets.items() if count > 0]
    return "    ↳ " + "  ".join(parts) if parts else ""


def format_tier_block_analysis(tier_label):
    """
    "Tier Block Analysis" (per chat, one level deeper than the existing
    'activated but didn't fire, by tier' line): of this tier's own
    resolved 'activated but didn't fire' cases, what specifically
    blocked it, and how did those setups actually resolve — down to
    which R level was hit, not just win/loss (per chat: "let it say
    what it hit be it 1R or 2R or 3R or SL").

    HONEST SCOPE NOTE: only two real, distinguishable block reasons
    exist in this codebase today —
      - ATR_FLOOR:        the GLOBAL ATR_MIN_PIPS gate blocked the whole
                           scan before Rule of Law ever ran (this tier
                           never got a conviction check at all that scan)
      - CONVICTION_GATE:  this tier's own score, via classify_conviction(),
                           came in under CONVICTION_MIN_BY_TIER
    There is no separate risk:reward gate or spread gate anywhere in the
    live code — the friend's 5-category list doesn't map onto what's
    actually implemented. If those become real gates later, add their
    own bucket here then; this function only reports what's computed.
    """
    records = _read_shadow_trade_log(experiment="EXPE_REJECTED_LIVE")
    tier_number = TIER_NUMBER.get(tier_label, "?")

    buckets = {"ATR_FLOOR": {"records": []},
               "CONVICTION_GATE": {"records": []},
               "OTHER / Unclassified": {"records": []}}

    for r in records:
        tags = r.get("tags") or {}
        v = tags.get(tier_label)
        if not isinstance(v, dict) or not v.get("activated"):
            continue  # this tier wasn't even structurally activated that scan

        if tags.get("_blocked_by_atr"):
            bucket = "ATR_FLOOR"
        elif "conviction" in (v.get("reason") or "").lower():
            bucket = "CONVICTION_GATE"
        else:
            bucket = "OTHER / Unclassified"

        buckets[bucket]["records"].append(r)

    total = sum(len(b["records"]) for b in buckets.values())
    if total == 0:
        return (f"🔬 *Tier {tier_number} Block Analysis* (`{tier_label}`)\n"
                "_No resolved 'activated but didn't fire' records for this tier yet._")

    lines = [f"🔬 *Tier {tier_number} Block Analysis* (`{tier_label}`)",
             "─────────────────────",
             f"Activated-but-blocked (resolved): `{total}`", ""]
    for bucket, b in sorted(buckets.items(), key=lambda kv: -len(kv[1]["records"])):
        recs = b["records"]
        n = len(recs)
        if n == 0:
            continue
        wins = sum(1 for r in recs if r.get("r_achieved", 0.0) > 0)
        wr = (wins / n * 100) if n else 0.0
        lines.append(f"*{bucket}*: `{n}` (won `{wins}`, `{wr:.0f}%`)")
        dist_line = format_outcome_distribution(outcome_distribution(recs))
        if dist_line:
            lines.append(dist_line)
    lines.append("")
    lines.append("_Only ATR floor and conviction gate are real, distinguishable blocks "
                 "in the current code — no separate risk/spread gate exists yet to "
                 "report on. Small buckets (<~30) are directional, not conclusive._")
    return "\n".join(lines)


# ---- MARKET INTELLIGENCE NETWORK: Information Coefficient ------------------
# Salvaged from the IC/Grinold material (per chat). The original theory
# is CROSS-SECTIONAL — correlating a signal across many different stocks
# on the same day, then multiplying by breadth (sqrt of independent
# bets). That doesn't transfer here: this bot trades ONE pair,
# sequentially, so there's no cross-section and no real "breadth" the
# way Grinold's law assumes (consecutive trades share the same regime
# and aren't independent bets). What DOES transfer: correlating a
# continuous feature against r_achieved OVER TIME, on this tier's own
# resolved trades. Different math (time-series correlation, not
# cross-sectional), same underlying tool (Pearson correlation).
IC_MIN_N = 30


def compute_ic(tier_label, feature_key, min_n=IC_MIN_N):
    """
    Pearson correlation between one continuous fingerprint feature and
    r_achieved, over this tier's own resolved EXP7 trades only. Pure
    stdlib (no numpy dependency). Returns None below min_n — same
    discipline as Evidence Engine: a correlation coefficient computed on
    a handful of trades is noise dressed as a finding, not a result.
    """
    records = [r for r in _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
               if r.get("variant") == tier_label]
    pairs = [(r.get("tags", {}).get(feature_key), r.get("r_achieved"))
             for r in records
             if r.get("tags", {}).get(feature_key) is not None and r.get("r_achieved") is not None]
    n = len(pairs)
    if n < min_n:
        return None

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return {"n": n, "ic": 0.0}
    ic = cov / ((var_x ** 0.5) * (var_y ** 0.5))
    return {"n": n, "ic": round(ic, 3)}


_IC_FEATURES = ["leg_length_pips", "break_count", "atr_percentile_15m",
                "ob_freshness_candles", "sweep_distance_pips",
                # Added per chat alongside was_choch/market state. Numeric
                # only, by design: was_choch (bool) and volatility_state
                # (categorical) aren't valid inputs for Pearson
                # correlation without encoding, which is out of scope
                # for this pass -- they're still stored as tags, just not
                # correlated here.
                "trend_strength_atr_mult", "compression_ratio", "pullback_depth_pct"]


def format_ic_report(tier_label):
    """On-demand report: IC for every known continuous fingerprint
    field, this tier's own resolved trades only. Deliberately NOT a
    ranked "most predictive variable" list — several of these features
    move together (ATR percentile and leg length, for instance), so
    ranking them as if independent would overstate what's actually
    known. Raw numbers, side by side, reader draws their own conclusion
    — same "concatenation, not verdict" discipline as Advisory Council."""
    tier_number = TIER_NUMBER.get(tier_label, "?")
    lines = [f"📈 *Information Coefficient — Tier {tier_number}* (`{tier_label}`)",
             "─────────────────────",
             "_Correlation only — NOT a ranking or \"most predictive variable\" claim. "
             "Several of these features move together (e.g. ATR percentile and leg "
             "length), so treating this as a ranked list would overstate independence "
             "that isn't there. Each number below is |IC| vs r_achieved, computed the "
             "same simple way, nothing more._", ""]
    any_shown = False
    for field in _IC_FEATURES:
        result = compute_ic(tier_label, field)
        if result is None:
            lines.append(f"  `{field}`: _insufficient data (<{IC_MIN_N} resolved)_")
        else:
            lines.append(f"  `{field}`: IC=`{result['ic']:+.3f}` (n=`{result['n']}`)")
            any_shown = True
    if not any_shown:
        lines.append("\n_No field has enough data yet — check back later._")
    return "\n".join(lines)


# ---- MARKET INTELLIGENCE NETWORK: Failure Investigation Bureau ------------
# "Instead of recording LOSS, investigate it" (per chat). Purely
# read-only, on-demand, one trade at a time — never triggered
# automatically from scan(), never touches a live decision. Compares one
# resolved trade's own fingerprint against the AVERAGE fingerprint of
# that SAME TIER's winners, and reports which fields deviated most.
# Requires trade_id, which is why /shadow recent now displays it.
FAILURE_INVESTIGATION_MIN_WINNERS = 10


def format_failure_investigation(trade_id):
    """
    Given one resolved trade's id, reports how its fingerprint compares
    to its own tier's typical winner — not a verdict on why it lost
    (that would be an inference this function isn't entitled to make),
    just the facts side by side: this trade's leg length/ATR percentile/
    OB freshness/sweep distance vs. the tier's own winners' averages.
    Gated at FAILURE_INVESTIGATION_MIN_WINNERS the same way everything
    else in this file is gated — a comparison against 3 winners isn't a
    baseline, it's noise.
    """
    records = _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
    target = next((r for r in records if r.get("trade_id") == trade_id), None)
    if target is None:
        return f"🕵️ _No resolved EXP7 trade found with id `{trade_id}`. " \
               f"Check `/shadow recent` for valid ids._"

    tier_label = target.get("variant")
    tier_number = TIER_NUMBER.get(tier_label, "?")
    r_achieved = target.get("r_achieved", 0.0)
    outcome_word = "WIN" if r_achieved > 0 else target.get("outcome", "LOSS")

    winners = [r for r in records
               if r.get("variant") == tier_label
               and r.get("r_achieved", 0.0) > 0
               and r.get("trade_id") != trade_id]

    lines = [f"🕵️ *Failure Investigation — `{trade_id}`* (Tier {tier_number})",
             "─────────────────────",
             f"Outcome: `{outcome_word}` (`{r_achieved:+.2f}R`)", ""]

    if len(winners) < FAILURE_INVESTIGATION_MIN_WINNERS:
        lines.append(f"_Only {len(winners)} resolved winners for this tier so far "
                      f"(need {FAILURE_INVESTIGATION_MIN_WINNERS}+) — not enough to "
                      f"compare against yet._")
        return "\n".join(lines)

    lines.append(f"Compared against `{len(winners)}` resolved winners, same tier:")
    lines.append("")
    target_tags = target.get("tags", {}) or {}
    any_shown = False
    for field in _IC_FEATURES:
        this_val = target_tags.get(field)
        winner_vals = [r.get("tags", {}).get(field) for r in winners
                       if r.get("tags", {}).get(field) is not None]
        if this_val is None or not winner_vals:
            continue
        avg_winner = sum(winner_vals) / len(winner_vals)
        lines.append(f"  `{field}`: this trade=`{this_val}`  winners avg=`{avg_winner:.1f}`")
        any_shown = True
    if not any_shown:
        lines.append("_No comparable fingerprint fields on this trade — "
                      "likely logged before fingerprint tracking was added._")

    return "\n".join(lines)


def format_advisory_council(tier_label):
    """
    Advisory Council (per chat) — SAFE, concatenation-only version.
    Lays out what the Experimental Lab and Evidence & Research
    Department have ALREADY separately computed about one tier, side by
    side, clearly labeled by source. Deliberately does NOT synthesize a
    combined verdict, confidence score, or recommendation — that would
    reintroduce the exact "unearned number" problem already declined for
    /shadow lab's auto-discovery and Mirror Learning's auto-apply path
    (see chat). This is a reading room, not a judge: every figure below
    is produced by calling that section's own existing function/formula
    — nothing here is a newly invented statistic.
    """
    tier_number = TIER_NUMBER.get(tier_label, "?")
    lines = [f"🏛 *Advisory Council — Tier {tier_number}* (`{tier_label}`)",
             "─────────────────────",
             "_Concatenation only — no combined score, no recommendation. "
             "Each section is exactly what that department already reports._",
             ""]

    # ---- Experimental Lab: this tier's own fired-side EXP7 stats -------
    # Same n/win-rate/avg-R computation format_shadow_detail's "By
    # variant" section already uses for EXP7 — reused verbatim, not
    # recomputed a second, possibly-inconsistent way.
    records = _read_shadow_trade_log(experiment="EXP7_TIER_ATR")
    tier_records = [r for r in records if r.get("variant") == tier_label]
    lines.append("*Experimental Lab — EXP7 (fired-side):*")
    if tier_records:
        n = len(tier_records)
        wins = sum(1 for r in tier_records if r.get("r_achieved", 0.0) > 0)
        sum_r = sum(r.get("r_achieved", 0.0) for r in tier_records)
        lines.append(f"  `{n}` resolved — win rate `{wins / n * 100:.0f}%`, avg `{sum_r / n:+.2f}R`")
        dist_line = format_outcome_distribution(outcome_distribution(tier_records))
        if dist_line:
            lines.append("  " + dist_line.strip())
    else:
        lines.append("  _No resolved EXP7 data for this tier yet._")
    lines.append("")

    # ---- Evidence & Research Department: block analysis (blocked-side) -
    lines.append("*Evidence & Research Department — blocked-side:*")
    block_report = format_tier_block_analysis(tier_label)
    block_body = "\n".join(
        ln for ln in block_report.split("\n")
        if not ln.startswith(f"🔬 *Tier {tier_number} Block Analysis*")
    ).strip()
    lines.append(("  " + block_body.replace("\n", "\n  ")) if block_body else "  _Nothing to report yet._")
    lines.append("")

    # ---- Experimental Lab: ATR suitability for this tier ---------------
    lines.append("*Experimental Lab — ATR Suitability (this tier):*")
    tier_bands = compute_atr_suitability().get(tier_number, {})
    if tier_bands:
        for band_label, bucket in sorted(tier_bands.items(), key=lambda kv: float(kv[0].split("-")[0])):
            n = bucket["n"]
            if n == 0:
                continue
            wr = bucket["wins"] / n * 100
            avg_r = bucket["sum_r"] / n
            lines.append(f"  `{band_label}p`: n=`{n}` WR `{wr:.0f}%` avgR `{avg_r:+.2f}`")
    else:
        lines.append("  _No ATR-banded data for this tier yet._")

    return "\n".join(lines)


def format_shadow_leaderboard(shadow_stats, min_resolved=5):
    """Ranks every experiment by average R. Experiments below
    min_resolved resolved trades are marked Insufficient Data rather
    than ranked — an avg R from 2 trades is noise, not a result."""
    ranked, insufficient = [], []
    for key in _SHADOW_STATS_EXPERIMENT_KEYS:
        s = shadow_stats.get(key, _empty_experiment_stat())
        if s["logged"] == 0:
            continue
        if s["resolved"] < min_resolved:
            insufficient.append(key)
            continue
        avg_r = s["sum_r"] / s["resolved"]
        ranked.append((key, avg_r, s["resolved"]))

    if not ranked and not insufficient:
        return None

    ranked.sort(key=lambda t: -t[1])
    lines = ["🏆 *Experiment Leaderboard*", "─────────────────────"]
    pos = 1
    for key, avg_r, n in ranked:
        lines.append(f"{pos}. *{_SHADOW_DISPLAY_NAME.get(key, key)}*\n   AvgR `{avg_r:+.2f}` ({n} resolved)")
        pos += 1
    for key in insufficient:
        s = shadow_stats.get(key, _empty_experiment_stat())
        lines.append(f"{pos}. *{_SHADOW_DISPLAY_NAME.get(key, key)}*\n   Insufficient Data ({s['resolved']}/{min_resolved} resolved)")
        pos += 1
    return "\n".join(lines)


def format_shadow_recent(limit=10):
    """Last N resolved shadow trades across every experiment, most
    recent first — a fast way to spot patterns without scrolling
    through Telegram history."""
    records = _read_shadow_trade_log(limit=200)  # read a bit extra, then trim
    if not records:
        return None
    records = records[-limit:][::-1]
    lines = [f"🕒 *Shadow — Last {len(records)} Resolved*", "─────────────────────"]
    for r in records:
        try:
            t = datetime.fromisoformat(r["resolved_at"]).strftime("%H:%M")
        except Exception:
            t = "??:??"
        name = _SHADOW_DISPLAY_NAME.get(r.get("experiment"), r.get("experiment", "?"))
        outcome = r.get("outcome", "?")
        icon = "✅" if r.get("r_achieved", 0.0) > 0 else "❌"
        tid = r.get("trade_id")
        tid_str = f"  `id:{tid}`" if tid else ""
        lines.append(f"`{t}`  {name}  {r.get('direction','?')}  {icon} {outcome}  "
                      f"`{r.get('r_achieved', 0):+.1f}R`{tid_str}")
    return "\n".join(lines)


# =========================================================================
# TRADE MANAGEMENT — shared regardless of which tier fired.
# =========================================================================
def apply_risk_gate_and_finalize(prospective_entry, prospective_sl, direction,
                                   current_atr, stats, score, tier_label,
                                   conviction=None):
    """Shared final stage for every tier: dual risk ceiling (ATR-relative
    + flat pip cap), then trade field assembly. Every tier goes through
    identical risk discipline.

    conviction: output of classify_conviction(), if provided. target_r
    from the conviction band REPLACES the flat RR_RATIO for TP placement
    — a CONSERVATIVE-band setup gets a nearer target than a FULL-band
    one. Falls back to RR_RATIO if conviction wasn't supplied (keeps this
    function callable the old way, e.g. from experiment/shadow code that
    doesn't run through Phase 3)."""
    target_r = conviction["target_r"] if conviction and conviction.get("target_r") else RR_RATIO
    if direction == "BUY":
        prospective_risk = prospective_entry - prospective_sl
    else:
        prospective_risk = prospective_sl - prospective_entry

    risk_ceiling_atr  = MAX_RISK_ATR_MULT * current_atr
    risk_ceiling_pips = MAX_RISK_PIPS * PIP_SIZE
    risk_too_wide = (prospective_risk > risk_ceiling_atr or prospective_risk > risk_ceiling_pips)

    if risk_too_wide:
        stats["risk_gate_suppressed"] += 1
        breach = []
        if prospective_risk > risk_ceiling_atr:
            breach.append("{:.1f}p > {:.1f}p ATR cap ({}x)".format(
                prospective_risk / PIP_SIZE, risk_ceiling_atr / PIP_SIZE, MAX_RISK_ATR_MULT))
        if prospective_risk > risk_ceiling_pips:
            breach.append("{:.1f}p > {}p flat cap".format(prospective_risk / PIP_SIZE, MAX_RISK_PIPS))
        return {"fired": False, "risk_gate_pass": False, "risk_gate_reason": "; ".join(breach)}

    risk_pips   = prospective_risk / PIP_SIZE
    reward_pips = (target_r * prospective_risk) / PIP_SIZE
    tp = (prospective_entry + target_r * prospective_risk if direction == "BUY"
          else prospective_entry - target_r * prospective_risk)
    return {
        "fired": True,
        "entry": prospective_entry, "sl": prospective_sl, "tp": tp,
        "risk_pips": risk_pips, "reward_pips": reward_pips,
        "risk_gate_pass": True, "risk_gate_reason": None,
        "tier_label": tier_label, "score": score,
        "target_r": target_r,
        "size_mult": conviction.get("size_mult") if conviction else None,
        "partial_r": conviction.get("partial_r") if conviction else None,
        "breakeven_r": conviction.get("breakeven_r") if conviction else None,
        "band_label": conviction.get("band_label") if conviction else None,
    }


def sl_multiplier_for_context(ctx):
    return SL_ATR_MULT_COMPRESSED if ctx.regime_ratio >= SL_VOL_SPIKE_RATIO else SL_ATR_MULT


# =========================================================================
# TELEGRAM COMMANDS — ported from V6. Long-poll on getUpdates with an
# offset (stats["last_update_id"]) so each message is processed exactly
# once; safe to call every scan. Field names below are V3's own
# (tier_label / tier_rating / score), not V6's (structure / score /
# score_breakdown) — this is a re-mapping, not a verbatim copy.
# =========================================================================
def _last_signal_context(stats):
    """Pulls the most recently sent signal's context for journal
    enrichment. Returns "?" placeholders if nothing has fired yet."""
    return {
        "signal":      stats.get("last_journal_signal",      "?"),
        "entry":       stats.get("last_journal_entry",       "?"),
        "tier_label":  stats.get("last_journal_tier_label",  "?"),
        "score":       stats.get("last_journal_score",       "?"),
        "tier_rating": stats.get("last_journal_tier_rating", "?"),
        "signal_time": stats.get("last_journal_time",        "?"),
    }


def format_stats_summary(stats):
    """
    Funnel breakdown using V3's ACTUAL stats keys (tier1/2/3_signals,
    ownership_upgrades, no_leg_owner, risk_gate_suppressed) — V6's keys
    (watching_alerts/fib_reached/pattern_passed) don't exist in V3's
    tier-arbitration model, so this is a re-derivation, not a copy.
    """
    n = stats["total_scans"]
    if n == 0:
        return "No scans recorded yet."

    def pct(val):
        return f"{val/n*100:.1f}%" if n > 0 else "—"

    first = stats.get("first_scan", "?")
    last  = stats.get("last_scan",  "?")

    wins, losses = stats.get("wins", 0), stats.get("losses", 0)
    total_results = wins + losses
    win_rate = f"{wins}/{total_results} ({wins/total_results*100:.0f}%)" if total_results > 0 else "no results yet"

    if total_results > 0:
        wr = wins / total_results
        # KNOWN GAP: since target_r now varies by conviction band, this
        # flat-RR_RATIO expectancy is an approximation again — it doesn't
        # know each closed trade's ACTUAL target_r. journal entries don't
        # carry target_r yet. Fix before trusting this number: add
        # target_r to each journal append (both auto-close and /win|/loss)
        # and average the real per-trade R here instead.
        expectancy = (wr * RR_RATIO) - (1 - wr)
        exp_str = f"{expectancy:+.2f}R per trade"
    else:
        exp_str = "—"

    lines = [
        "",
        "📊 *SMC Scanner V3 — Funnel Stats*",
        f"_Period: {first} → {last}_",
        "─────────────────────",
        f"🔍 Total scans:          `{n}`",
        f"➖ Consolidation skip:   `{stats.get('consolidation_skip', 0)}` ({pct(stats.get('consolidation_skip', 0))})",
        f"📉 ATR too low:          `{stats.get('atr_too_low', 0)}` ({pct(stats.get('atr_too_low', 0))})",
        f"⚡ Regime shift skip:    `{stats.get('regime_shift_skip', 0)}` ({pct(stats.get('regime_shift_skip', 0))})",
        f"🕳 No leg owner:         `{stats.get('no_leg_owner', 0)}` ({pct(stats.get('no_leg_owner', 0))})",
        f"⬆️ Ownership upgrades:   `{stats.get('ownership_upgrades', 0)}`",
        f"🏛 Tier 1 (POI):         `{stats.get('tier1_signals', 0)}`",
        f"🏛 Tier 2 (Fib):         `{stats.get('tier2_signals', 0)}`",
        f"🏛 Tier 3 (Structure):   `{stats.get('tier3_signals', 0)}`",
        f"🛑 Risk-gate suppressed: `{stats.get('risk_gate_suppressed', 0)}`",
        f"🚨 Signals sent:         `{stats.get('signals_sent', 0)}` ({pct(stats.get('signals_sent', 0))})",
        "─────────────────────",
        f"🏆 Win rate:             `{win_rate}`",
        f"📐 Expectancy:           `{exp_str}`",
        "─────────────────────",
    ]

    if n > 20:
        if stats.get("atr_too_low", 0) / n > 0.3:
            lines.append("⚠️ _>30% of scans skipped on ATR — ATR_MIN_PIPS may be too high for current regime._")
        elif stats.get("no_leg_owner", 0) / n > 0.5:
            lines.append("⚠️ _Legs rarely find an owner — tier gates may be too strict for current conditions._")
        elif total_results >= 10 and wins / total_results < 0.35:
            lines.append("⚠️ _Win rate below 35% over 10+ trades — review tier entry logic._")
        else:
            lines.append("✅ _Funnel behaving as expected._")

    if RESULT_TRACKING_ENABLED:
        lines.append("_Send /win or /loss to log the last trade result._")

    return "\n".join(lines)


def format_bias_ab_summary(bias_ab_log):
    """
    /biasab — live (gated CHoCH+BOS+EMA) vs shadow (old ungated rule) 1H
    bias agreement, scan by scan. Ported from V6's format_shadow_summary,
    renamed to avoid colliding with V3's own format_shadow_summary()
    (which summarizes the 6-experiment research pipeline — a different
    thing entirely, kept under /shadow).
    """
    if not bias_ab_log:
        return "🕶️ _No bias A/B log entries yet — give it a few scans._"

    n = len(bias_ab_log)
    agree = sum(1 for e in bias_ab_log if e.get("agree"))
    diverge = n - agree
    agree_pct = f"{agree/n*100:.0f}%"

    first_t = bias_ab_log[0].get("time", "?")
    last_t  = bias_ab_log[-1].get("time", "?")

    lines = [
        "",
        "🕶️ *Bias A/B — Live (gated) vs Old Rule (ungated)*",
        f"_Period: {first_t} → {last_t}_",
        "─────────────────────",
        f"🔍 Scans logged: `{n}`",
        f"🤝 Agreement:    `{agree}/{n}` ({agree_pct})",
        f"↔️ Divergence:   `{diverge}`",
        "─────────────────────",
    ]

    if diverge == 0:
        lines.append("_No divergences yet._")
    else:
        windows = []
        current = None
        for e in bias_ab_log:
            if not e.get("agree"):
                if current is None:
                    current = {"start": e["time"], "end": e["time"],
                               "live": e["live_bias"], "shadow": e["shadow_bias"]}
                else:
                    current["end"] = e["time"]
            else:
                if current is not None:
                    windows.append(current)
                    current = None
        if current is not None:
            windows.append(current)

        lines.append(f"*Recent divergence windows* (last {min(len(windows), 8)} of {len(windows)}):")
        for w in windows[-8:]:
            lines.append(f"  `{w['start']}` → `{w['end']}` — live=`{w['live']}` vs old-rule=`{w['shadow']}`")

    return "\n".join(lines)


def check_result_commands(stats):
    """
    Polls Telegram for result/journal commands (ported from V6, field
    names re-mapped to V3):
      /win [note], /loss [note]  — log a result, with 3 safeguards:
        1. per-signal cooldown (no double-logging the same signal)
        2. /undo — unconditional reversal of the last journal entry
        3. /confirm — override a flip within 60s of the last log
      /undo, /confirm, /stats, /trade, /shadow (+ exp1..exp7/rejected/recent),
      /leaderboard, /atrbands, /biasab, /bias, /journal, /last
    """
    if not RESULT_TRACKING_ENABLED:
        return stats

    url    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = stats.get("last_update_id", 0) + 1

    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 2}, timeout=5).json()
    except Exception:
        return stats

    if not resp.get("ok") or not resp.get("result"):
        return stats

    stats.setdefault("journal", [])

    def _log_result(stats, result, note, now_str, sig_time):
        entry_ctx = _last_signal_context(stats)
        journal_entry = {
            "time":      sig_time,
            "logged_at": now_str,
            "result":    result,
            "note":      note or "—",
            **entry_ctx,
        }
        stats["journal"].append(journal_entry)
        stats["journal"] = stats["journal"][-JOURNAL_MAX_ENTRIES:]
        if result == "WIN":
            stats["wins"] = stats.get("wins", 0) + 1
        else:
            stats["losses"] = stats.get("losses", 0) + 1
        stats["result_logged_for_signal"] = sig_time
        active = stats.get("active_trade")
        if active:
            stats["last_closed_trade"] = {
                "direction":  active.get("direction", "?"),
                "entry":      f"{active['entry']:.5f}" if "entry" in active else "?",
                "exit":       "manual",
                "result":     result,
                "hit":        "manual /win" if result == "WIN" else "manual /loss",
                "pips":       None,
                "closed_at":  now_str,
                "opened_at_display": active.get("opened_at_display", "?"),
            }
        stats.pop("active_trade", None)
        return stats

    def _undo_last(stats):
        j = stats.get("journal", [])
        if not j:
            return None
        last = j.pop()
        if last.get("result") == "WIN":
            stats["wins"] = max(0, stats.get("wins", 0) - 1)
        elif last.get("result") == "LOSS":
            stats["losses"] = max(0, stats.get("losses", 0) - 1)
        stats["result_logged_for_signal"] = None
        stats.pop("pending_confirm", None)
        return last

    for update in resp["result"]:
        update_id = update.get("update_id", 0)
        stats["last_update_id"] = max(stats.get("last_update_id", 0), update_id)

        msg     = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        raw     = msg.get("text", "").strip()

        if chat_id != TELEGRAM_CHAT_ID:
            continue

        cmd  = raw.split()[0].lower() if raw else ""
        note = raw[len(cmd):].strip() if len(raw) > len(cmd) else ""

        now_utc_dt = datetime.now(timezone.utc)
        now_str    = now_utc_dt.strftime("%Y-%m-%d %H:%M UTC")
        sig_time   = stats.get("last_journal_time", "?")

        if cmd in ("/win", "win", "/w", "/loss", "loss", "/l"):
            result = "WIN" if cmd in ("/win", "win", "/w") else "LOSS"

            locked_sig = stats.get("result_logged_for_signal")
            if locked_sig is not None and sig_time != "?" and locked_sig == sig_time:
                last_entry = stats["journal"][-1] if stats.get("journal") else None
                is_flip    = last_entry is not None and last_entry.get("result") != result
                within_60s = False
                if last_entry is not None:
                    try:
                        last_logged = datetime.strptime(
                            last_entry.get("logged_at", ""), "%Y-%m-%d %H:%M UTC"
                        ).replace(tzinfo=timezone.utc)
                        within_60s = (now_utc_dt - last_logged).total_seconds() <= 60
                    except ValueError:
                        within_60s = False

                if is_flip and within_60s:
                    stats["pending_confirm"] = {"cmd": cmd, "note": note, "sig_time": sig_time}
                    send_telegram(
                        f"⚠️ *A result was just logged for this signal* "
                        f"(`{last_entry.get('result')}`). Send /confirm to override with `{result}`."
                    )
                else:
                    prior   = last_entry.get("result") if last_entry else "?"
                    prior_t = last_entry.get("logged_at") if last_entry else "?"
                    send_telegram(
                        f"🚫 *Result already logged for this signal* (`{prior}` at `{prior_t}`).\n"
                        f"Use /undo to reverse it, then log again — or wait for the next signal."
                    )
                print(f"  [RESULT] Blocked duplicate {result} for signal @ {sig_time}.")
                continue

            stats = _log_result(stats, result, note, now_str, sig_time)
            wins, losses = stats.get("wins", 0), stats.get("losses", 0)
            total = wins + losses
            wr    = f"{wins/total*100:.0f}%" if total > 0 else "—"
            icon  = "✅" if result == "WIN" else "❌"
            note_line = f"\n📝 _{note}_" if note else ""
            send_telegram(
                f"{icon} *{result} logged*{note_line}\n"
                f"Running record: `{wins}W / {losses}L` ({wr} win rate)\n"
                f"_Send /journal to see recent entries._"
            )
            print(f"  [RESULT] {result} logged. Note: '{note}'. Running: {wins}W / {losses}L")

        elif cmd in ("/confirm", "confirm"):
            pending = stats.get("pending_confirm")
            if not pending:
                send_telegram("_Nothing pending to confirm._")
                continue
            _undo_last(stats)
            result = "WIN" if pending["cmd"] in ("/win", "win", "/w") else "LOSS"
            stats = _log_result(stats, result, pending["note"], now_str, pending["sig_time"])
            stats.pop("pending_confirm", None)
            wins, losses = stats.get("wins", 0), stats.get("losses", 0)
            total = wins + losses
            wr    = f"{wins/total*100:.0f}%" if total > 0 else "—"
            send_telegram(
                f"🔁 *Result overridden* — now logged as `{result}`.\n"
                f"Running record: `{wins}W / {losses}L` ({wr} win rate)"
            )
            print(f"  [RESULT] Override confirmed — now {result}. Running: {wins}W / {losses}L")

        elif cmd in ("/undo", "undo"):
            last = _undo_last(stats)
            if last is None:
                send_telegram("_Nothing to undo — journal is empty._")
            else:
                wins, losses = stats.get("wins", 0), stats.get("losses", 0)
                total = wins + losses
                wr    = f"{wins/total*100:.0f}%" if total > 0 else "—"
                send_telegram(
                    f"↩️ *Undone* — removed `{last.get('result','?')}` logged at "
                    f"`{last.get('logged_at', last.get('time','?'))}`.\n"
                    f"Running record: `{wins}W / {losses}L` ({wr} win rate)"
                )
            print("  [RESULT] /undo — reverted last entry.")

        elif cmd in ("/stats", "stats"):
            send_telegram(format_stats_summary(stats))

        elif cmd in ("/trade", "trade"):
            # df_5m isn't fetched yet at this point (commands are
            # processed before the data fetch so they're never dropped
            # by a fetch failure) — defer to scan(), right after df_5m
            # is available, same pattern as V6.
            stats["_pending_trade_query"] = True

        elif cmd in ("/shadow", "shadow"):
            shadow_stats = load_shadow_stats()
            arg = note.strip().lower()  # text after the command, e.g. "/shadow exp3" -> "exp3"

            if not arg:
                summary = format_shadow_summary(shadow_stats)
                send_telegram(summary or "🔬 _Shadow pipeline has no logged experiments yet._")

            elif arg in ("rejected", "reject", "live"):
                send_telegram(format_rejected_live_detail(shadow_stats))

            elif arg in ("recent", "last10"):
                send_telegram(format_shadow_recent() or "🕒 _No resolved shadow trades yet._")

            elif arg.startswith("blocked"):
                tier_arg = arg[len("blocked"):].strip()
                tier_map = {"tier1": "TIER_1_POI", "1": "TIER_1_POI",
                            "tier2": "TIER_2_FIB", "2": "TIER_2_FIB",
                            "tier3": "TIER_3_STRUCTURE", "3": "TIER_3_STRUCTURE"}
                tier_label = tier_map.get(tier_arg)
                if not tier_label:
                    send_telegram("🔬 _Usage: `/shadow blocked tier1`, `tier2`, or `tier3`._")
                else:
                    send_telegram(format_tier_block_analysis(tier_label))

            elif arg.startswith("advisory"):
                tier_arg = arg[len("advisory"):].strip()
                tier_map = {"tier1": "TIER_1_POI", "1": "TIER_1_POI",
                            "tier2": "TIER_2_FIB", "2": "TIER_2_FIB",
                            "tier3": "TIER_3_STRUCTURE", "3": "TIER_3_STRUCTURE"}
                tier_label = tier_map.get(tier_arg)
                if not tier_label:
                    send_telegram("🏛 _Usage: `/shadow advisory tier1`, `tier2`, or `tier3`._")
                else:
                    send_telegram(format_advisory_council(tier_label))

            elif arg.startswith("ic"):
                tier_arg = arg[len("ic"):].strip()
                tier_map = {"tier1": "TIER_1_POI", "1": "TIER_1_POI",
                            "tier2": "TIER_2_FIB", "2": "TIER_2_FIB",
                            "tier3": "TIER_3_STRUCTURE", "3": "TIER_3_STRUCTURE"}
                tier_label = tier_map.get(tier_arg)
                if not tier_label:
                    send_telegram("📈 _Usage: `/shadow ic tier1`, `tier2`, or `tier3`._")
                else:
                    send_telegram(format_ic_report(tier_label))

            elif arg.startswith("investigate"):
                trade_id_arg = arg[len("investigate"):].strip()
                if not trade_id_arg:
                    send_telegram("🕵️ _Usage: `/shadow investigate <trade_id>` — "
                                   "find an id via `/shadow recent`._")
                else:
                    send_telegram(format_failure_investigation(trade_id_arg))

            elif arg in _SHADOW_ALIASES:
                key = _SHADOW_ALIASES[arg]
                if key == "EXPE_REJECTED_LIVE":
                    send_telegram(format_rejected_live_detail(shadow_stats))
                else:
                    send_telegram(format_shadow_detail(shadow_stats, key))

            else:
                send_telegram(
                    f"🔬 _Don't recognize '{arg}'._\n"
                    "Try: `/shadow`, `/shadow exp1`..`/shadow exp7`, "
                    "`/shadow rejected`, `/shadow blocked tier1|tier2|tier3`, "
                    "`/shadow recent`, or `/leaderboard`."
                )

        elif cmd in ("/leaderboard", "leaderboard"):
            board = format_shadow_leaderboard(load_shadow_stats())
            send_telegram(board or "🏆 _Nothing logged yet to rank._")

        elif cmd in ("/atrbands", "atrbands", "/atr", "atr"):
            table = format_atr_suitability_table()
            send_telegram(table or "📊 _No EXP7_TIER_ATR trades resolved yet — check back after a few hundred scans._")

        elif cmd in ("/biasab", "biasab"):
            send_telegram(format_bias_ab_summary(load_bias_ab_log()))

        elif cmd in ("/bias", "bias"):
            state        = load_state()
            confirmed    = state.get("macro_bias_confirmed", "?")
            stale        = state.get("macro_bias_stale", False)
            swing_high   = state.get("macro_swing_high")
            swing_low    = state.get("macro_swing_low")
            confirmed_at = state.get("macro_swing_confirmed_at", "?")
            leg_dir      = state.get("macro_leg_direction", "?")
            owner        = get_leg_owner(state)
            lines = [
                "🧭 *1H Bias — live state*",
                "─────────────────────",
                f"Confirmed: `{confirmed}`" + (" ⚠️ STALE" if stale else ""),
                f"Anchored leg direction: `{leg_dir}`",
            ]
            if swing_high is not None and swing_low is not None:
                lines.append(f"Swing H/L: `{swing_high:.5f}` / `{swing_low:.5f}`")
                lines.append(f"Confirmed at: `{confirmed_at}`")
            else:
                lines.append("Swing H/L: _no anchor — nothing has confirmed this bias yet_")
            if owner:
                lines.append(
                    f"Leg owner: `{owner.get('tier','?')}` — `{owner.get('status','?')}`"
                    + (" (upgraded)" if owner.get("upgraded") else "")
                )
            else:
                lines.append("Leg owner: _unclaimed_")
            if stale:
                lines.append(
                    "\n_STALE means the leg that last confirmed this bias has "
                    "since invalidated (origin break or 78.6%+ retrace) and "
                    "nothing fresh has replaced it yet. The direction shown is "
                    "a hold-over, not a live confirmation._"
                )
            send_telegram("\n".join(lines))

        elif cmd in ("/journal", "journal", "/j"):
            entries = stats.get("journal", [])
            if not entries:
                send_telegram("📓 _No journal entries yet. Log trades with /win or /loss._")
            else:
                recent = list(reversed(entries[-10:]))
                lines  = ["📓 *Trade Journal — Last 10 entries*", "─────────────────────"]
                for e in recent:
                    icon    = "✅" if e.get("result") == "WIN" else "❌"
                    sig     = e.get("signal", "?")
                    entry_p = e.get("entry", "?")
                    tier    = e.get("tier_label", "?")
                    score_v = e.get("score", "?")
                    note_t  = e.get("note", "—")
                    trade_time = e.get("time", "?")
                    lines.append(
                        f"{icon} `{trade_time}`\n"
                        f"   {sig} @ {entry_p} | {tier} | score {score_v}\n"
                        f"   📝 _{note_t}_"
                    )
                send_telegram("\n".join(lines))

        elif cmd in ("/last", "last"):
            ctx = _last_signal_context(stats)
            if ctx and ctx.get("signal") != "?":
                timeline_line = ""
                tl = stats.get("last_journal_timeline")
                if tl:
                    try:
                        timeline_line = "\n\n" + format_timeline_diagnostics(tl, datetime.now(timezone.utc))
                    except Exception:
                        timeline_line = ""
                send_telegram(
                    f"🔁 *Last signal sent:*\n"
                    f"Direction: `{ctx.get('signal','?')}`\n"
                    f"Entry: `{ctx.get('entry','?')}`\n"
                    f"Tier: `{ctx.get('tier_label','?')}` — `{ctx.get('tier_rating','?')}`\n"
                    f"Score: `{ctx.get('score','?')}`\n"
                    f"Time: `{ctx.get('signal_time','?')}`"
                    + timeline_line
                )
            else:
                send_telegram("_No signal recorded yet this session._")

    return stats


def check_trade_closed(active, c_last):
    """Assume SL if a single candle touches both levels (worse outcome,
    not a claim about intrabar sequencing)."""
    direction = active["direction"]
    sl, tp = active["sl"], active["tp"]
    high, low = c_last["High"], c_last["Low"]

    if direction == "BUY":
        hit_sl, hit_tp = low <= sl, high >= tp
    else:
        hit_sl, hit_tp = high >= sl, low <= tp

    if hit_sl:
        return "LOSS"
    if hit_tp:
        return "WIN"
    return None


def format_trade_status(active, current_price, now_utc):
    direction = active["direction"]
    entry, sl, tp = active["entry"], active["sl"], active["tp"]
    sign = 1 if direction == "BUY" else -1

    pl_pips      = (current_price - entry) * sign / PIP_SIZE
    dist_tp_pips = (tp - current_price) * sign / PIP_SIZE
    dist_sl_pips = (current_price - sl) * sign / PIP_SIZE

    opened_at = datetime.fromisoformat(active["opened_at"])
    elapsed   = now_utc - opened_at
    total_min = int(elapsed.total_seconds() // 60)
    hours, minutes = divmod(total_min, 60)
    time_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"

    pl_emoji = "🟢" if pl_pips >= 0 else "🔴"
    dir_emoji = "📈" if direction == "BUY" else "📉"

    return (
        "📊 *Trade Active — GBPUSD*\n\n"
        f"{dir_emoji} *Direction:* `{direction}` | *Tier:* `{active.get('tier_label','?')}`\n"
        f"📍 *Entry:* `{entry:.5f}`  →  *Now:* `{current_price:.5f}`\n"
        f"{pl_emoji} *P/L:* `{pl_pips:+.1f} pips`\n"
        f"🎯 *Distance to TP:* `{dist_tp_pips:.1f} pips`\n"
        f"🛡 *Distance to SL:* `{dist_sl_pips:.1f} pips`\n"
        f"⏱ *Time in trade:* `{time_str}`\n"
        "─────────────────────\n"
        f"_Signal-time score: {active.get('score', '?')} "
        f"({active.get('tier_rating', '?')}) — frozen, not recomputed._"
    )


def format_trade_query_response(stats, current_price, now_utc):
    """
    On-demand answer for /trade (ported from V6). Three cases:
      1. A live trade is open — same content as the periodic ping.
      2. No trade open, but the last one closed (auto SL/TP or manual
         /win|/loss) — say which level was hit / which result, and pips.
      3. Never had a trade this session.
    """
    active = stats.get("active_trade")
    if active:
        return format_trade_status(active, current_price, now_utc)

    last_closed = stats.get("last_closed_trade")
    if last_closed:
        hit    = last_closed.get("hit", "?")
        pips   = last_closed.get("pips")
        result = last_closed.get("result", "?")
        icon   = "✅" if result == "WIN" else "❌"
        try:
            pips_str = f"{pips:+.1f} pips"
        except (TypeError, ValueError):
            pips_str = "?"
        return (
            f"📭 *No trade in session* — {hit} hit {icon}\n"
            f"{last_closed.get('direction','?')} @ `{last_closed.get('entry','?')}` → "
            f"`{last_closed.get('exit','?')}` ({pips_str})\n"
            f"_Closed: {last_closed.get('closed_at','?')}_"
        )

    return "📭 *No trade in session.*"


def manage_active_trade(stats, state, df_5m, now_utc):
    """Runs BEFORE Macro Bias / Market Context / Rule of Law. If a trade
    is open, this is the ONLY thing scan() does this cycle. Returns True
    if a trade is open. Releases leg ownership on close either way."""
    active = stats.get("active_trade")
    if not active:
        return False

    c_last = df_5m.iloc[-1]
    outcome = check_trade_closed(active, c_last)

    if outcome is not None:
        exit_price = active["tp"] if outcome == "WIN" else active["sl"]
        sign = 1 if active["direction"] == "BUY" else -1
        pl_pips = (exit_price - active["entry"]) * sign / PIP_SIZE

        if outcome == "WIN":
            stats["wins"] = stats.get("wins", 0) + 1
        else:
            stats["losses"] = stats.get("losses", 0) + 1

        icon = "✅" if outcome == "WIN" else "❌"
        send_telegram(
            f"{icon} *Trade closed — {outcome}* (auto-detected, GBPUSD)\n\n"
            f"📍 *Entry:* `{active['entry']:.5f}`  →  *Exit:* `{exit_price:.5f}`\n"
            f"{'🟢' if pl_pips >= 0 else '🔴'} *P/L:* `{pl_pips:+.1f} pips`\n"
            f"_Tier: {active.get('tier_label','?')} | "
            f"Signal-time score: {active.get('score', '?')}_"
        )
        print(f"  [TRADE] Auto-closed as {outcome} @ {exit_price:.5f} ({pl_pips:+.1f} pips).")

        # ── Journal entry (ported from V6) — auto-close logs exactly like
        # a manual /win or /loss so /journal shows a complete history
        # regardless of how each trade was closed.
        journal = stats.setdefault("journal", [])
        journal.append({
            "time":            active.get("opened_at_display", "?"),
            "logged_at":       now_utc.strftime("%Y-%m-%d %H:%M UTC"),
            "result":          outcome,
            "note":            "auto-closed (SL/TP hit)",
            "signal":          active.get("direction", "?"),
            "entry":           f"{active['entry']:.5f}" if "entry" in active else "?",
            "tier_label":      active.get("tier_label", "?"),
            "score":           active.get("score", "?"),
            "tier_rating":     active.get("tier_rating", "?"),
        })
        stats["journal"] = journal[-JOURNAL_MAX_ENTRIES:]
        # Snapshot for /trade — same shape manual /win|/loss produces.
        stats["last_closed_trade"] = {
            "direction":  active.get("direction", "?"),
            "entry":      f"{active['entry']:.5f}" if "entry" in active else "?",
            "exit":       f"{exit_price:.5f}",
            "result":     outcome,
            "hit":        "TP" if outcome == "WIN" else "SL",
            "pips":       pl_pips,
            "closed_at":  now_utc.strftime("%Y-%m-%d %H:%M UTC"),
            "opened_at_display": active.get("opened_at_display", "?"),
        }
        stats.pop("active_trade", None)
        release_leg(state, f"trade closed ({outcome})")
        return False

    last_update = active.get("last_update_sent_at")
    send_update = True
    if last_update:
        try:
            gap = now_utc - datetime.fromisoformat(last_update)
            send_update = gap.total_seconds() >= TRADE_STATUS_UPDATE_MINUTES * 60
        except ValueError:
            send_update = True

    if send_update:
        send_telegram(format_trade_status(active, c_last["Close"], now_utc))
        active["last_update_sent_at"] = now_utc.isoformat()
        stats["active_trade"] = active
        print("  [TRADE] Status update sent — trade still open.")
    else:
        print("  [TRADE] Still open — skipping status ping.")

    return True


# =========================================================================
# MAIN SCAN
# =========================================================================
def scan():
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%H:%M UTC")
    print("\n[" + now_str + "] Scan starting (V3 Rule-of-Law, Stage 3)...")

    stats = load_stats()

    # ── Telegram commands — processed BEFORE the data fetch so a fetch
    # failure never silently drops a /win, /undo, /trade, etc. ───────────
    try:
        stats = check_result_commands(stats)
    except Exception as e:
        print("[COMMANDS ERROR] " + str(e))

    stats["total_scans"] += 1
    if stats["first_scan"] is None:
        stats["first_scan"] = now_utc.strftime("%Y-%m-%d")
    stats["last_scan"] = now_utc.strftime("%Y-%m-%d %H:%M")

    diag = new_diagnostic() if DIAGNOSTIC_MODE else None

    # ── Fetch data ──────────────────────────────────────────────────────
    df_5m  = fetch_ohlc("5min",  outputsize=100)
    df_15m = fetch_ohlc("15min", outputsize=SWING_LOOKBACK_15 + 10)
    df_1h  = fetch_ohlc("1h",    outputsize=HTF_BIAS_MIN_BARS + 20)

    if df_5m is None or df_15m is None or df_1h is None:
        print("Data fetch failed. Exiting.")
        save_stats(stats)
        return

    # ── Data quality gates ───────────────────────────────────────────────
    fresh_5m  = check_data_freshness(df_5m,  5,  "5min",  now_utc)
    fresh_15m = check_data_freshness(df_15m, 15, "15min", now_utc)
    fresh_1h  = check_data_freshness(df_1h,  60, "1h",    now_utc)
    if not (fresh_5m and fresh_15m and fresh_1h):
        print("Data freshness check failed — exiting without trading on stale bars.")
        save_stats(stats)
        return

    if not (data_looks_sane(df_5m, "5min") and
            data_looks_sane(df_15m, "15min") and
            data_looks_sane(df_1h, "1h")):
        print("Data sanity check failed. Skipping this run.")
        save_stats(stats)
        return

    # ── /trade on-demand query — deferred from check_result_commands()
    # above until fresh 5M data (and therefore a current price) is on
    # hand. Answered regardless of whether a trade is open right now;
    # format_trade_query_response() branches on that itself. ─────────────
    if stats.get("_pending_trade_query"):
        current_price = df_5m["Close"].iloc[-1]
        send_telegram(format_trade_query_response(stats, current_price, now_utc))
        stats["_pending_trade_query"] = False

    # ── Active-trade freeze — runs BEFORE Macro Bias ─────────────────────
    state = load_state()
    trade_is_open = manage_active_trade(stats, state, df_5m, now_utc)
    save_state(state)
    if trade_is_open:
        save_stats(stats)
        return

    if len(df_1h) < HTF_BIAS_MIN_BARS:
        print(f"Only {len(df_1h)} 1H bars. Need {HTF_BIAS_MIN_BARS}. Skipping.")
        save_stats(stats)
        return

    # ── MACRO BIAS (pure) ─────────────────────────────────────────────────
    macro_bias, bias_updates = compute_macro_bias(df_1h, df_15m, state)
    apply_state_updates(state, bias_updates)
    bias_stale = state.get("macro_bias_stale", False)

    # Shadow A/B (bias-only, never trades) — same pure pattern.
    shadow_bias, shadow_updates = compute_macro_bias_shadow_old_rule(df_1h, state)
    apply_state_updates(state, shadow_updates)
    bias_agree = (shadow_bias == macro_bias)
    if not bias_agree:
        print(f"  [SHADOW] live={macro_bias}{' STALE' if bias_stale else ''} | "
              f"old-rule={shadow_bias} | DIVERGE")

    # Persist every scan's agreement (not just divergences) — /biasab
    # needs the full denominator to report an honest agreement rate.
    try:
        bias_ab_log = load_bias_ab_log()
        bias_ab_log.append({
            "time":        now_utc.strftime("%Y-%m-%d %H:%M UTC"),
            "live_bias":   macro_bias,
            "shadow_bias": shadow_bias,
            "agree":       bias_agree,
            "price":       float(df_5m["Close"].iloc[-1]),
        })
        save_bias_ab_log(bias_ab_log)
    except Exception as e:
        print("[BIAS AB LOG ERROR] " + str(e))

    save_state(state)

    if macro_bias == "CONSOLIDATION":
        stats["consolidation_skip"] += 1
        save_stats(stats)
        diag_set(diag, "macro_bias", False, "CONSOLIDATION — no directional edge")
        print("  1H Bias: CONSOLIDATION — no directional edge. No tier is evaluated.")
        if diag is not None:
            print(build_diagnostic_report(diag))
        return
    diag_set(diag, "macro_bias", True,
             "STALE — hold-over direction, no live 1H break backing it" if bias_stale else None)

    swing_high = state.get("macro_swing_high")
    swing_low  = state.get("macro_swing_low")
    if swing_high is None or swing_low is None:
        print("  No confirmed 1H swing points yet. Skipping.")
        save_stats(stats)
        return

    # ── MARKET CONTEXT (pure) ─────────────────────────────────────────────
    ctx, ctx_reason, ctx_updates = evaluate_market_context(df_5m, state, now_utc)
    apply_state_updates(state, ctx_updates)
    save_state(state)

    print(
        "  1H Bias: {}{} | ATR: {:.1f}p | Regime shift: {} (ratio {:.2f}) | "
        "Post-spike cooldown: {} | Session active: {}".format(
            macro_bias, " (STALE)" if bias_stale else "",
            ctx.current_atr_pips, ctx.regime_shifted, ctx.regime_ratio,
            ctx.post_spike_active, ctx.session_active,
        )
    )

    # ── MARKET FACTS (pure observation, built once, shared by every tier) ─
    # NOTE: built BEFORE the ATR gate below on purpose. The Shadow
    # Pipeline (specifically Experiment 7 — Tier ATR Mirror) needs facts
    # on every scan, including scans the live bot skips for being below
    # ATR_MIN_PIPS — otherwise the ATR-suitability dataset would only
    # ever contain ATR values that already clear the current threshold,
    # which makes the whole "find the real sweet spot" exercise circular.
    facts = MarketFacts(df_5m, df_15m, df_1h, macro_bias, swing_high, swing_low, now_utc)

    if not ctx.tradeable:
        stats["atr_too_low"] += 1
        diag_set(diag, "market_context", False, ctx_reason)
        print(f"  NO TRADE — {ctx_reason}. No tier is evaluated live.")

        # ── SHADOW PIPELINE still runs — ATR floor never gates research ──
        try:
            run_shadow_pipeline(facts, ctx, state, df_15m,
                                 TierResult(reason="ATR too low: " + ctx_reason), now_utc)
        except Exception as e:
            print("[SHADOW ERROR] pipeline crashed, live bot unaffected: " + str(e))

        save_stats(stats)
        if diag is not None:
            print(build_diagnostic_report(diag))
        return
    diag_set(diag, "market_context", True)

    # ── RULE OF LAW (arbitration, including ownership upgrades) ──────────
    result = evaluate_rule_of_law(facts, ctx, state, stats, now_utc)
    save_state(state)

    print(f"  [RULE OF LAW] {result.reason}")
    if result.conviction is not None:
        print(f"  [CONVICTION] {result.tier_label} score={result.score} "
              f"minimum={result.conviction['minimum']} decision={result.conviction['decision']} "
              f"band={result.conviction['band_label']}")
        if result.breakdown:
            print(f"  [CONVICTION BREAKDOWN] {result.breakdown}")
    diag_set(diag, "tier_evaluation", result.activated, None if result.activated else result.reason)

    # ── EVIDENCE ENGINE (Phase 4 — read-only, tier-isolated, cannot touch
    # `fired`/`decision`/sizing; see compute_evidence()'s docstring) ──────
    # NOTE: an activated-but-not-yet-firing tier (still WATCHING for its
    # rejection candle) has an empty breakdown by construction — see each
    # tier's evaluate() — so this naturally stays silent until a tier has
    # actually reached a scored FIRE/REJECT decision, exactly when there's
    # a real structural fact-set to compare.
    evidence = None
    if result.activated and result.tier_label:
        try:
            evidence = compute_evidence(result.tier_label, result.breakdown)
        except Exception as e:
            print("[EVIDENCE ERROR] " + str(e))
            evidence = None
        if evidence:
            print(f"  [EVIDENCE] Tier {TIER_NUMBER.get(result.tier_label)}: "
                  f"n={evidence['n']} win_rate={evidence['win_rate']}% "
                  f"avg_r={evidence['avg_r']} strength={evidence['strength']}")

    # Rule of Law said REJECT, but historical evidence on this exact
    # structural shape exists and clears the min-N gate — surface it as a
    # SEPARATE, clearly-labeled research note (never a signal, never
    # touches sizing/state["active_trade"]). Deduped per leg+tier so a
    # setup sitting rejected for hours doesn't resend the same note every
    # 5-minute scan.
    if evidence and not result.fired and result.activated:
        leg_key = compute_leg_id(facts.macro_bias, facts.swing_high, facts.swing_low)
        note_key = f"{result.tier_label}|{leg_key}"
        if state.get("evidence_last_note_key") != note_key:
            state["evidence_last_note_key"] = note_key
            save_state(state)
            note = format_evidence_note(result.tier_label, evidence)
            if note:
                send_telegram("🔬 *RESEARCH NOTE — Rule of Law said REJECT*\n\n" + note)

    # ── SHADOW PIPELINE (research only — never blocks or alters live flow) ─
    try:
        run_shadow_pipeline(facts, ctx, state, df_15m, result, now_utc)
        if stats["total_scans"] % STATS_SUMMARY_EVERY == 0:
            summary = format_shadow_summary(load_shadow_stats())
            if summary:
                send_telegram(summary)
    except Exception as e:
        print("[SHADOW ERROR] pipeline crashed, live bot unaffected: " + str(e))

    if not result.fired:
        save_stats(stats)
        diag_set(diag, "leg_ownership", False,
                  result.reason if result.activated else "no tier activated")
        if diag is not None:
            print(build_diagnostic_report(diag))
        return
    diag_set(diag, "leg_ownership", True)

    # ── TRADE MANAGEMENT (shared regardless of which tier fired) ────────
    sl_mult = sl_multiplier_for_context(ctx)
    sl_buffer = max(sl_mult * ctx.current_atr, SL_MIN_PIPS * PIP_SIZE)
    sl_final = (result.sl_raw - sl_buffer if result.direction == "BUY"
                else result.sl_raw + sl_buffer)

    risk_result = apply_risk_gate_and_finalize(
        result.entry, sl_final, result.direction, ctx.current_atr,
        stats, result.score, result.tier_label,
        conviction=result.conviction,
    )

    if not risk_result["fired"]:
        print(f"  [RISK GATE] Suppressed — {risk_result['risk_gate_reason']}")
        save_stats(stats)
        diag_set(diag, "risk_gate", False, risk_result["risk_gate_reason"])
        if diag is not None:
            print(build_diagnostic_report(diag, header="Signal suppressed at risk gate"))
        return
    diag_set(diag, "risk_gate", True)

    stats["signals_sent"] += 1
    tier_counter_key = {
        "TIER_1_POI": "tier1_signals",
        "TIER_2_FIB": "tier2_signals",
        "TIER_3_STRUCTURE": "tier3_signals",
    }.get(result.tier_label)
    if tier_counter_key:
        stats[tier_counter_key] = stats.get(tier_counter_key, 0) + 1

    stats["active_trade"] = {
        "direction":   result.direction,
        "entry":       risk_result["entry"],
        "sl":          risk_result["sl"],
        "tp":          risk_result["tp"],
        "score":       result.score,
        "tier_rating": result.tier_rating,
        "tier_label":  result.tier_label,
        "opened_at":   now_utc.isoformat(),
        "opened_at_display": now_utc.strftime("%Y-%m-%d %H:%M UTC"),
        "last_update_sent_at": None,
        # Phase 3 — conviction-derived management plan, informational
        # (this bot signals, it doesn't place sized live orders).
        "band_label":   risk_result["band_label"],
        "target_r":     risk_result["target_r"],
        "size_mult":    risk_result["size_mult"],
        "partial_r":    risk_result["partial_r"],
        "breakeven_r":  risk_result["breakeven_r"],
    }

    # ── Journal/result-tracking bookkeeping (ported from V6) — snapshot
    # this signal's context so /last, /win, /loss, and the timeline can
    # all reference it, and unlock result-logging for the NEW signal. ───
    signal_time = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    stats["last_journal_signal"]      = result.direction
    stats["last_journal_entry"]       = f"{risk_result['entry']:.5f}"
    stats["last_journal_tier_label"]  = result.tier_label
    stats["last_journal_score"]       = result.score
    stats["last_journal_tier_rating"] = result.tier_rating
    stats["last_journal_time"]        = signal_time
    stats["last_journal_timeline"]    = dict(state.get("signal_timeline", {}))
    stats["result_logged_for_signal"] = None   # new signal — lift any prior lock

    timeline_line = ""
    tl = stats.get("last_journal_timeline")
    if tl:
        try:
            timeline_line = "\n\n" + format_timeline_diagnostics(tl, now_utc)
        except Exception:
            timeline_line = ""

    direction_emoji = "📈" if result.direction == "BUY" else "📉"
    partial_line = (f"🎯 *Partial:* `{risk_result['partial_r']}R`\n"
                     if risk_result["partial_r"] else "")
    be_line = (f"🔒 *Breakeven at:* `{risk_result['breakeven_r']}R`\n"
               if risk_result["breakeven_r"] else "")
    # Evidence Engine — annotation only, appended after everything Rule of
    # Law / Trade Management already decided. Empty string (not shown) if
    # `evidence` is None, i.e. below EVIDENCE_MIN_N for this tier.
    evidence_line = ("\n\n" + format_evidence_note(result.tier_label, evidence)) if evidence else ""
    send_telegram(
        "🚨 *SMC SIGNAL — GBPUSD* 🚨\n\n"
        f"{direction_emoji} *Action:* `{result.direction}`\n"
        f"🏛 *Tier:* `{result.tier_label}` — `{result.tier_rating}`\n"
        f"🧠 *Conviction:* `{result.score}` — `{risk_result['band_label']}`\n"
        f"📊 *Bias:* `{macro_bias}` (1H structure)\n"
        "─────────────────────\n"
        f"📍 *Entry:* `{risk_result['entry']:.5f}`\n"
        f"🛡 *Stop:*  `{risk_result['sl']:.5f}` _({risk_result['risk_pips']:.1f} pips)_\n"
        f"🏆 *Target:* `{risk_result['tp']:.5f}` _({risk_result['reward_pips']:.1f} pips)_\n"
        f"⚖️ *RR:* `1:{risk_result['target_r']}`\n"
        f"📏 *Suggested size:* `{risk_result['size_mult']}x base risk`\n"
        + partial_line + be_line
        + timeline_line
        + evidence_line
    )
    save_stats(stats)
    save_state(state)


if __name__ == "__main__":
    scan()
  
            
