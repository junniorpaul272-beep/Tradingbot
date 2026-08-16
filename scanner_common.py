"""
scanner_common.py
==================
Shared config constants, math primitives, and persistence helpers used
by BOTH scanner_live.py and min_scanner.py. Import *, no side effects,
and (deliberately) no Twelve Data / Telegram network calls of its own —
those live in the file that actually needs them.

File/constant NAMES are left exactly as in the original scanner.py on
purpose (still say "shadow" in places, etc.) so nothing already on
disk gets orphaned by the split.
"""

import os
import json
import math
import time
import shutil
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone


def _json_default(o):
    """Fallback encoder for json.dump — makes numpy scalars/arrays safe.

    Pandas/numpy comparisons (e.g. `close > open`) return numpy scalar
    types (np.bool_, np.int64, np.float64, ...), not native Python
    bool/int/float. json.dump doesn't know how to serialize those.
    Note: on numpy>=2.0, np.bool_'s __name__ was renamed to "bool", so
    the resulting TypeError misleadingly reads "Object of type bool is
    not JSON serializable" even though it's not a real Python bool.
    """
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def atomic_write_json(path, data, **dump_kwargs):
    """Write JSON to `path` without ever leaving it truncated/corrupted.

    Writes to a temp file first, then atomically renames it over the
    real path (os.replace is atomic on POSIX and Windows). Also keeps
    one `.bak` copy of the previous good file before the swap, so a bad
    write never means total data loss. Used by every save_* function
    (audit fix — these all used to do a raw `open(path, "w")`, which
    means a crash mid-write, or two scans running concurrently, could
    truncate or silently clobber state).
    """
    dump_kwargs.setdefault("default", _json_default)
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if os.path.exists(path):
        bak = path + ".bak"
        try:
            shutil.copy2(path, bak)
        except OSError:
            pass
    os.replace(tmp, path)

# =========================================================================
# CREDENTIALS
# =========================================================================
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
# NOTE: TWELVE_DATA_KEY is deliberately NOT read here. Only scanner_live.py
# needs it (it's the only one calling fetch_ohlc), and reading it via
# os.environ[...] here would hard-crash min_scanner.py on import in any
# environment where that secret isn't set — which is exactly the point of
# MIN not touching the API at all.


# =========================================================================
# CONFIG — grouped by the architectural layer that owns each constant.
# =========================================================================

# ---- GLOBAL ---------------------------------------------------------------
PAIR      = "GBP/USD"
PIP_SIZE  = 0.0001
RR_RATIO  = 3.0
BASE_DIR  = os.path.dirname(os.path.abspath(globals().get("__file__", "pasted-text.txt")))

DIAGNOSTIC_MODE = os.environ.get("DIAGNOSTIC_MODE", "1") != "0"

STATE_FILE           = os.path.join(BASE_DIR, "state.json")
STATE_MAX_AGE_HOURS   = 6
STATS_FILE            = os.path.join(BASE_DIR, "stats.json")
SCAN_LOCK_FILE        = os.path.join(BASE_DIR, "scanner.lock")
SCAN_LOCK_MAX_AGE_SEC = 15 * 60

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
# ATR is NOT a hard gate anymore (removed — a signal at any ATR can now
# fire). ATR_MIN_PIPS/ATR_WARN_PIPS are purely LABEL thresholds so a
# thin-volatility signal is clearly tagged in the Telegram alert instead
# of being silently blocked or silently treated like a normal one:
#   current_atr_pips <  ATR_MIN_PIPS                      -> "very low ATR"
#   ATR_MIN_PIPS <= current_atr_pips < ATR_WARN_PIPS       -> "low ATR"
#   current_atr_pips >= ATR_WARN_PIPS                      -> normal
# (see very_low_atr_warning / low_atr_warning on MarketContext)
ATR_MIN_PIPS  = 4
ATR_WARN_PIPS = 6

# ---- ATR HISTOGRAM (pure counting, additive, zero gating impact) ----------
# Fixed-width pip buckets used by the scan-level ATR histogram (see
# atr_histogram_bucket() below and stats["atr_histogram"] in
# scanner_live.py). Every scan drops one tally into one bucket regardless
# of whether a signal fires, is skipped, or is gated for any other reason
# — this is purely descriptive of the volatility regime scans are running
# in, never used to decide anything.
ATR_HIST_BUCKET_PIPS = 1
ATR_HIST_MAX_PIPS     = 10   # anything >= this lands in one "10p+" overflow bucket

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

# ---- SUPPORT / RESISTANCE (EXP3 slot reuse, shadow-only) -------------------
# Per chat, 2026-08-15. EXP3_POI (OB vs FVG comparison) retired — see
# flush_exp3.py — once FVG graduated to a live Tier 1 fallback POI.
# This is a genuinely different idea from EXP3_POI, not a rename: not a
# range-trading strategy, structural S/R that assists the CURRENT trend
# via flip+retest. Deliberately NOT range-restricted — birth is keyed on
# the Market Phase Engine's EXHAUSTION/TRANSITION states (see
# discover_sr_candidates()), which happen inside trends too. Shadow-only
# — same graduation path FVG itself just completed; nothing here reads
# these constants from a live gate.
SR_ZONE_ATR_MULT          = 0.5    # zone half-width either side of the swing extreme, 15M-ATR-sized — a zone, never an exact price
SR_MIN_CONTAINMENT_BARS   = 12     # leg must have aged (EXHAUSTION/TRANSITION) at least this many bars before a level is born
SR_FLIP_COOLDOWN_BARS     = 6      # bars a just-flipped level can't flip again — debounce, the actual fix for the "infinite S/R loop" failure mode
SR_MAX_FLIPS              = 3      # terminal cap — a level that's flipped this many times is retired, not tracked forever
SR_MAX_AGE_CANDLES        = 200    # outer staleness safeguard, same idea as FVG_MAX_AGE_CANDLES above
SR_METHODOLOGY_VERSION    = 1

# ---- BANK — simulated capital per experiment, shadow-only ------------------
# Per chat, 2026-08-16. "The Bank doesn't control the other departments. It
# gives them a financial environment in which their decisions have
# consequences." Turns each experiment's raw R-multiple history into a
# simulated equity curve — P&L, drawdown, return% — so /bank and
# /leaderboard can show which experiments "repay confidence" in capital
# terms, not just average R.
#
# UPDATED 2026-08-16 (later same day): the /bank REPORT (simulate_bank_
# account/compute_bank_accounts) is still a stateless recompute off the
# permanent shadow/leg-obs logs, same as before — that part was correct
# and stays. What's NEW is BANK_TRANSACTIONS_FILE below: a permanent,
# append-only ledger that settles each resolved outcome into a real
# transaction record (its own bank_id, equity_before/after, the risk_pct/
# cost_r actually in effect when it settled) the FIRST time it's seen,
# via settle_bank_transactions() in min_scanner.py. Reason: a pure
# recompute silently rewrites ALL historical Bank numbers the moment
# BANK_RISK_PCT/BANK_COST_R/an account's risk% ever changes — there was
# no durable record of what actually would have happened at the time.
# The ledger is additive and MIN-owned, same ownership rule as every
# other permanent log in this file — and yes, it MUST be checked directly
# in min-scan.yml's own commit step (validate + save steps both), same
# lesson as ACCOUNT_PROFILES_FILE below and Market Intent/S/R before it —
# a third occurrence of "add it to the workflow list, don't just assume."
BANK_STARTING_CAPITAL = 10_000.0
BANK_RISK_PCT         = 0.005   # 0.5% — the user's REAL risk tolerance, not
                                 # the friend's 1% example, so simulated and
                                 # real stay apples-to-apples if these ever
                                 # connect to a real account profile later
BANK_COST_R           = 0.05    # flat per-trade cost in R terms, bundling
                                 # spread + slippage + commission + allocation
                                 # fee into ONE number (per the friend's
                                 # "0.05R" example) rather than separately
                                 # modeling three costs this codebase has no
                                 # real broker execution data to estimate
                                 # honestly — a single, clearly-labeled
                                 # simplification beats fabricated precision
BANK_MIN_N            = 10      # same "is this sample big enough" floor as
                                 # EVIDENCE_MIN_N elsewhere in this file —
                                 # duplicated as a literal, not a reference,
                                 # since EVIDENCE_MIN_N is defined later in
                                 # this same module (forward reference isn't
                                 # possible at module-load time); keep these
                                 # two in sync by hand if either ever changes

BANK_TRANSACTIONS_FILE = os.path.join(BASE_DIR, "bank_transactions.jsonl")   # permanent, append-only ledger — see note above
BANK_LEDGER_METHODOLOGY_VERSION = 1

# ---- ACCOUNT PROFILES — real balances, not simulated. Per chat, 2026-08-16.
# Genuinely persistent (unlike Bank's report above) — a balance the user
# sets has to survive across scans. MUST be in live-scan.yml's commit
# step (checked directly — same lesson as BANK_TRANSACTIONS_FILE above).
# NOTE: these two names were referenced by min_scanner.py's load/save_
# account_profiles() from the day Account Profiles shipped, but never
# actually defined here — every import of min_scanner.py has been raising
# ImportError since. Found and fixed 2026-08-16.
ACCOUNT_PROFILES_FILE = os.path.join(BASE_DIR, "account_profiles.json")
ACCOUNT_PROFILES_METHODOLOGY_VERSION = 1

# ---- RULE OF LAW (arbitration — routing only, no quality judgment) --------
TIER_PRIORITY = ["TIER_1_POI", "TIER_2_FIB", "TIER_3_STRUCTURE"]
LEG_MATCH_TOLERANCE_PIPS = 5

# ---- CONVICTION (RETROSPECTIVE/RESEARCH ONLY — see the note right below
# CONVICTION_MANAGEMENT_BANDS for the 2026-08-11 Risk-at-Hand change).
# UNVALIDATED PLACEHOLDERS. Every number below is a first guess, not a
# derived threshold — do not treat these as final. Calibrate against
# resolved journal/shadow R-outcomes (Experiment 5 ablation + Experiment E
# rejected-live hypothetical R) before trusting them with real sizing.
# Activation (mandatory gates, per tier's own evaluate()) is UNCHANGED and
# stays a hard pass/fail — that part was always true. What's NO LONGER
# true: conviction does NOT decide what an activated setup is allowed to
# do anymore. Every activated setup fires; Risk-at-Hand (classify_tierN_
# risk + stop_recommendation, scanner_observation.py) is what actually
# describes it to the live alert now. These constants only feed
# classify_conviction() for EXP7/shadow telemetry.
CONVICTION_MIN_BY_TIER = {
    "TIER_1_POI":       65,
    "TIER_2_FIB":        60,
    "TIER_3_STRUCTURE":  55,
}

# Management bands keyed by score, independent of which tier. Historically
# (pre-Risk-at-Hand) only applied after a tier cleared CONVICTION_MIN_BY_TIER;
# now purely retrospective — classify_conviction() still runs the same
# below/band logic on every activated setup's score for EXP7/shadow
# telemetry, it just no longer feeds a live REJECT/size decision.
#   size_mult    — suggested position-size multiplier (informational; this
#                  bot sends signals, it doesn't place sized orders, so
#                  this rides in the Telegram message for you to apply)
#   target_r     — replaces the flat RR_RATIO for TP placement
#   partial_r    — take partials at this R (None = no partial)
#   breakeven_r  — move SL to breakeven at this R (None = don't)
#
# SHARPE AWARENESS GAP (audit fix — size_mult flattened to 1.0 everywhere):
# size_mult used to be tied to conviction score (a win-rate proxy), not to
# volatility-normalised or Kelly-derived sizing. Win-rate-based sizing
# optimises hit rate, NOT the return/variance tradeoff Sharpe measures — a
# high-conviction, high-variance tier can carry a WORSE Sharpe than a
# lower-conviction, low-variance one at the same size_mult, and a score-only
# band has no way to tell them apart. The old bands (1.0/1.0/1.0/0.5) implied
# we already knew CONSERVATIVE-band setups deserved half size — we don't;
# that number was a guess, not a derived result, same as every other
# UNVALIDATED PLACEHOLDER in this file. Guessing new numbers now would just
# swap one unvalidated placeholder for another that LOOKS more considered.
# Flattened to 1.0 across every band instead, which is the honest reflection
# of what we currently know about relative sizing: nothing.
#
# RE-ENABLE PATH (do not hand-tune size_mult back in before this):
# Per-tier, per-band realised Sharpe is computed by compute_tier_sharpe()
# (see below) and surfaced in /thesis's Confidence line whenever a tier
# currently owns the leg. Once EXP7_TIER_ATR volume clears n=30 resolved trades in a
# given (tier, band) — same significance floor the dashboard project uses
# elsewhere — size_mult for THAT band can be set from realised volatility
# (e.g. inversely scaled to realised R stdev, capped in a sane range like
# 0.5x-1.5x so one noisy stretch can't blow sizing up), not from score.
# target_r/partial_r/breakeven_r are TP/management placement, not risk
# sizing, and are left untouched — lower stakes if imperfect.
CONVICTION_MANAGEMENT_BANDS = [
    # (score_floor, label,          size_mult, target_r, partial_r, breakeven_r)
    (90,  "FULL_EXTENDED",  1.0, 3.0, 2.0, 1.0),
    (80,  "FULL",           1.0, 3.0, 2.0, 1.0),
    (70,  "NORMAL",         1.0, 2.5, 1.5, 1.0),
    (0,   "CONSERVATIVE",   1.0, 2.0, None, 0.5),
]
# RETROSPECTIVE/RESEARCH ONLY as of the risk-at-hand change (per chat,
# 2026-08-11) — CONVICTION_MIN_BY_TIER and these bands no longer gate
# live `fired` or live trade management (see classify_tierN_risk in
# scanner_observation.py for what replaced them, and apply_risk_gate_
# and_finalize's conviction=None live call site in this file). Kept
# exactly as-is, untouched, purely so min_scanner.py's existing audit
# tools (compute_conviction_buckets, format_conviction_audit,
# /shadow conviction tierN) can keep asking "would these old bands have
# separated winners from losers" against the growing shadow sample.

# LIVE_SIZE_MULT_DEFAULT: fixed size multiplier for every live fire now
# that conviction no longer varies it — the value the CONVICTION_
# MANAGEMENT_BANDS size_mult column was already flattened to above, just
# no longer looked up from a score band at all.
LIVE_SIZE_MULT_DEFAULT = 1.0


# ---- MARKET PHASE (read-only narrative layer) ------------------------------
# Relabels observations the scanner ALREADY makes (macro bias, its
# staleness/break_count, EMA distance, liquidity sweeps at the swing
# boundary) into a phase taxonomy — it detects NOTHING new. Purely a
# synthesis layer so a human/tier can ask "how old is this trend" in one
# call instead of re-deriving it from three separate fields each time.
#
# UNVALIDATED PLACEHOLDERS, same caveat as CONVICTION_MIN_BY_TIER above:
# first guesses, not derived thresholds. DO NOT gate any live tier on
# Phase until MIN has run enough shadow-logged scans to check these
# actually separate winning setups from losing ones. Until then this is
# log/narrative only.
PHASE_EXHAUSTION_MIN_BREAK_COUNT   = 4     # Nth+ same-direction 1H BOS = aging leg
PHASE_EXHAUSTION_EMA_DIST_ATR_MULT = 2.5   # price this many ATRs from EMA_100 = overextended
PHASE_HISTORY_MAX_LEN              = 8     # how many past phases the rolling story keeps

# ---- MEASURED MOVE (classic AB=CD current-leg-vs-prior-leg extension) -----
# Compares the CURRENT confirmed 1H leg's length to the leg immediately
# before it (the one it replaced at the last CHoCH/promotion) — the
# textbook "does this leg equal 100-200% of the move that preceded it"
# measured-move check. Bucketed for logging/tagging only, never a live
# gate. Bucket edges are round numbers, not derived from data — same
# unvalidated-placeholder caveat as PHASE_EXHAUSTION_* above. Whether any
# of these buckets actually correlate with reversal is exactly what MIN's
# scenario report (leg_obs bucketed by phase + extension) is for — this
# constant just defines the buckets, it makes no claim about them.
MEASURED_MOVE_BUCKETS = [
    (0.0, 1.0, "UNDER_100"),
    (1.0, 1.5, "EXT_100_150"),
    (1.5, 2.0, "EXT_150_200"),
    (2.0, float("inf"), "EXT_OVER_200"),
]

# ---- MARKET THESIS ENGINE (replaces Advisory Council, per chat) -----------
# Turns the Phase taxonomy above from "what state is this" into "why did it
# become this state, and what does that textbook-imply comes next." Same
# discipline as PHASE_*: detects NOTHING new. Every transition_cause below
# is read off values compute_market_phase()/detect_bos_impulse() already
# compute (was_choch, the MANIPULATION sweep overlay, the two existing
# aging triggers) plus ONE new addition — a before/after snapshot of
# macro_bias itself, same pattern capture_prior_leg_snapshot() already uses
# for the leg (see scanner_live.py).
THESIS_METHODOLOGY_VERSION = 1

# Independent 1H volatility comparison local to compute_market_phase() —
# mirrors the SPIRIT of compute_market_state()'s volatility_state (MIN-
# owned, 15M-frame) but is its own 1H-frame computation so scanner_
# observation.py never has to import from min_scanner.py (wrong direction
# — MIN imports from common/observation, never the reverse).
PHASE_VOLATILITY_HINT_LOOKBACK_BARS = 10
PHASE_VOLATILITY_HINT_THRESHOLD     = 0.15

# Fraction of PHASE_EXHAUSTION_MIN_BREAK_COUNT / PHASE_EXHAUSTION_EMA_DIST_
# ATR_MULT at which a still-EXPANSION leg gets flagged MEDIUM failure risk
# instead of LOW — "approaching" the same aging thresholds Phase already
# uses, not a new invented number. UNVALIDATED PLACEHOLDER, same caveat as
# everything else in this file: a first guess, not a derived boundary.
FAILURE_RISK_APPROACHING_FRACTION = 0.75

# (phase_value, transition_cause_value) -> textbook expectation. This is
# the FALSIFIABLE part of the thesis (per chat) — logged at signal time,
# meant to eventually be checked against what the leg actually did later
# (leg_obs fate / next phase transition — that comparison is Phase 2, not
# built yet). Same reliability-diagram principle as CALIBRATION_BUCKETS.
# A combination with no entry here returns None from expected_next_event()
# rather than guessing — that gap is itself useful signal (which real
# (phase, cause) pairs actually occur but have no textbook expectation
# defined yet, per _build_market_thesis's docstring).
EXPECTED_NEXT_EVENT_MAP = {
    ("expansion", "FRESH_BOS"):             "Continuation BOS, same direction",
    ("expansion", "SWEEP_RECLAIM"):         "Continuation BOS toward opposing liquidity",
    ("expansion", "CHOCH"):                 "Follow-through BOS confirming the new direction",
    ("expansion", "VOLATILITY_EXPANSION"):  "Continuation BOS while displacement holds",
    ("exhaustion", "EMA_EXHAUSTION"):       "CHoCH or range — leg showing age",
    ("exhaustion", "FAILED_CONTINUATION"):  "CHoCH or extended consolidation",
    ("transition", "BIAS_FLIP"):            "Fresh BOS in the new direction, or reversion to range",
    ("transition", "VOLATILITY_COLLAPSE"):  "Range persists until a break with fresh displacement",
    ("transition", "UNKNOWN"):              "No confirmed directional edge — awaiting a fresh BOS either way",
    ("manipulation", "SWEEP_RECLAIM"):      "Reversal away from the swept level",
}


def classify_conviction(tier_label, score):
    """
    Pure function: (tier, score) -> a decision/band label. RETROSPECTIVE
    ONLY as of the Risk-at-Hand change (per chat, 2026-08-11) — its
    `decision` field (REJECT/FIRE) is NEVER read to gate whether a live
    setup fires anymore; every tier fires unconditionally once its own
    mandatory structural conditions pass (see each tier's evaluate() in
    scanner_observation.py and TierResult's docstring). This function is
    called purely to populate TierResult.conviction for EXP7/shadow
    telemetry (console print, /shadow conviction tierN, EXP4_POLICY_LAB)
    — it describes what the OLD conviction gate would have decided, not
    what actually happens live. Risk-at-Hand (classify_tierN_risk +
    stop_recommendation, also in scanner_observation.py) is what the
    live alert actually shows instead.

    Returns a dict, always populated with `reason` so a hypothetical
    REJECT/WATCH is just as legible in EXP7/console output as a FIRE.

    CONTRACT (per chat): `score` is a hand-weighted heuristic, not a
    validated statistic — see /shadow conviction tierN for whether it
    actually correlates with outcomes. Treat CONVICTION_MIN_BY_TIER as a
    retired policy still under study, not as ground truth about setup
    quality — and never as something to reintroduce as a live gate
    without evidence backing it.
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
SHADOW_STATE_FILE = os.path.join(BASE_DIR, "shadow_state.json")
SHADOW_STATS_FILE = os.path.join(BASE_DIR, "shadow_stats.json")
SHADOW_METHODOLOGY_VERSION = 2
# BUMP POLICY (per chat, prompted by "research contamination" concern —
# experiment definitions changing while historical samples stay mixed).
# Written down explicitly so a future change doesn't have to guess:
#   BUMP when a change would make an existing variant's OLD and NEW
#   records genuinely incomparable if pooled together — e.g. the R-
#   multiple/outcome math changes, a tag's MEANING changes under the
#   same key name, or a variant's entry/SL logic changes such that
#   "TIER_1_POI" before the change and "TIER_1_POI" after no longer
#   describe the same thing. A bump resets shadow_state (see
#   load_shadow_state below) and every report that reads the permanent
#   log filters to CURRENT-version records only — old records aren't
#   deleted, just excluded from being pooled with incompatible ones.
#   DO NOT bump for a purely additive change — a new tag key on an
#   EXISTING variant (old records simply lack it; every reader must
#   treat a missing/None tag as "predates this field," never as a
#   default value — see the thesis_tags / predicted_win_prob precedent
#   in experiment_7_tier_atr_mirror), or a brand-new variant name (it's
#   naturally isolated by variant-name bucketing and can never get
#   pooled with a differently-defined variant sharing its old name).
# When genuinely unsure which case a change falls into: bump. A false
# reset costs some continuity; a false skip risks a silently
# contaminated sample, which is the more expensive mistake by far.
# Permanent, append-only, NEVER overwritten wholesale (unlike the two
# files above, which are full-state snapshots rewritten every scan).
# Every resolved shadow trade — win, loss, or timeout — gets one line
# appended here forever. This IS the Intelligence Database: the actual
# raw dataset the ATR suitability analysis (compute_atr_suitability) and
# the Evidence & Research Department both read from, and it survives
# even if shadow_state.json/shadow_stats.json were ever lost, reset, or
# corrupted.
SHADOW_TRADE_LOG_FILE = os.path.join(BASE_DIR, "shadow_trade_log.jsonl")
# Write-hardening for the permanent append (per chat, "safety against
# duplication"): _append_shadow_trade_log() retries a failed disk write
# this many times, SHADOW_TRADE_LOG_WRITE_RETRY_DELAY_SEC apart, before
# giving up and reporting failure to its caller. This is deliberately
# small — a genuinely full disk won't be fixed by retrying, this only
# covers transient failures (a momentary lock, a brief permission
# hiccup). See resolve_pending() in min_scanner.py for what happens on
# the caller side when even that fails: the trade is NOT marked as
# logged (logged_trade_ids only records confirmed successful writes,
# never attempts) and stays in shadow_state["pending"] to retry on the
# next scan, rather than being silently dropped.
SHADOW_TRADE_LOG_WRITE_RETRIES = 3
SHADOW_TRADE_LOG_WRITE_RETRY_DELAY_SEC = 0.25
# Permanent, append-only live trade log — one line per closed live trade,
# written by _append_live_trade_log() at both auto-close (SL/TP hit) and
# manual close (/win, /loss). Never truncated or rewritten.
LIVE_TRADE_LOG_FILE = os.path.join(BASE_DIR, "live_trade_log.jsonl")
BIAS_AB_LOG_FILE  = os.path.join(BASE_DIR, "bias_ab_log.json")    # live (gated) vs shadow (old-rule) 1H bias, for /biasab
BIAS_AB_LOG_MAX_ENTRIES = 500

# Maps a live tier label to the plain tier number used in ATR-band
# analysis and Telegram output (Tier: 1 / Tier: 2 / Tier: 3).
TIER_NUMBER = {"TIER_1_POI": 1, "TIER_2_FIB": 2, "TIER_3_STRUCTURE": 3}
ATR_SUITABILITY_BAND_WIDTH_PIPS = 1.0   # bucket width for the ATR band table

JOURNAL_MAX_ENTRIES = 100

SHADOW_MAX_PENDING_BARS   = 60    # ~5h of 5M bars before a stale shadow setup is force-resolved
                                  # (was 200/~16.6h — shortened per request to the
                                  # 48-72 bar range; picked the midpoint)
SHADOW_MAX_PENDING_PER_EXPERIMENT = 20   # safety valve against runaway logging

# Per (experiment, variant) key, bounded seen-set of recently-logged leg_ids
# (audit fix). log_shadow_setup() used to compare only against the single
# most-recently-logged leg per key ("last_leg"), so a leg that recurred
# after being displaced by a different leg — A -> B -> A — got silently
# re-logged as new on its second appearance, inflating shadow_stats
# ['logged'] and duplicating shadow_state['pending']. This previously
# affected EXP2_FIB and EXP5_ABLATION (confirmed) and, structurally, every
# other experiment routed through log_shadow_setup. Same bounded-history
# idea as LEG_OBS_CLOSED_MAX / DRAINED_LEG_ID_CAP elsewhere in this file —
# small and per-key here since flapping between two legs is the failure
# mode being guarded against, not a long replay window.
SHADOW_SEEN_LEG_CAP = 20

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

# ---- POSITION SIZING (structural stop -> $ risk -> lots) -------------------
# Per chat, 2026-08-16. Feeds calculate_position_size() below.
# PIP_VALUE_PER_STANDARD_LOT assumes a USD-QUOTED pair (XXX/USD — true for
# both GBPUSD and EURUSD, this codebase's only two pairs): for a 100,000-
# unit standard lot, 1 pip (PIP_SIZE) of movement is worth exactly
# STANDARD_LOT_UNITS * PIP_SIZE in USD, no cross-rate conversion needed.
# CAVEAT: this identity breaks for a non-USD-quoted pair (e.g. USDJPY) —
# if this bot ever trades one, pip value would need the current exchange
# rate, not this flat constant.
STANDARD_LOT_UNITS          = 100_000
PIP_VALUE_PER_STANDARD_LOT  = STANDARD_LOT_UNITS * PIP_SIZE   # = $10.00/pip for GBPUSD/EURUSD
MIN_LOT_SIZE                = 0.01    # typical retail broker floor — UNVALIDATED, not pulled from a real broker spec
LOT_STEP                    = 0.01    # typical retail broker increment — same caveat
MAX_LOT_SIZE_SANITY_CAP     = 5.0     # defensive ceiling only, NOT a real broker/margin limit — guards
                                       # against a garbage near-zero stop_pips silently producing an
                                       # absurd size; calculate_position_size() flags sanity_capped=True
                                       # rather than ever recommending past this quietly

WATCHING_EXIT_PIPS             = 8
TRADE_STATUS_UPDATE_MINUTES    = 30
NEUTRAL_WATCH_COOLDOWN_MINUTES = 60
NEUTRAL_WATCH_MIN_RETRACE      = FIB_ZONE_FAR

RESULT_TRACKING_ENABLED = True
STATS_SUMMARY_EVERY     = 50

# Daily Market Thesis push (per chat — "quiet mode risks users thinking the
# bot is broken; daily no-signal updates risk notification fatigue").
# Deliberately a WALL-CLOCK schedule, not a scan-count one — STATS_SUMMARY_
# EVERY above is scan-count-based, which is exactly why it lands at a
# different real-world time every cycle (gaps, restarts, and any downtime
# all shift it). This is the fix pattern applied to a genuine daily cadence:
# state carries the LOCAL date and which of today's slots have already
# fired, so a scan any time after a slot's hour sends it once and only
# once, regardless of how often scan() itself runs.
# TZ_OFFSET is a fixed UTC offset, not an IANA zone — chosen deliberately
# simple since the offset was confirmed stable (no DST) at build time
# (per chat, UTC+1). If that ever changes, this constant is the one place
# to update — see THESIS_UPDATE_HOURS_LOCAL below for the actual schedule.
THESIS_UPDATE_TZ_OFFSET_HOURS = 1
THESIS_UPDATE_HOURS_LOCAL     = [9, 12, 15]   # 9am / 12pm / 3pm local. The code itself
# doesn't skip weekends (see _maybe_send_daily_thesis_update's docstring) — but in
# practice this only ever fires Mon-Fri, because the external Cronjob.com trigger
# that calls scan() is itself configured Mon-Fri only (confirmed per chat). If that
# trigger schedule ever changes to include weekends, this code needs no changes —
# it already handles that case correctly.

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
# is shown at all. Lowered from 50 -> 10 now that the Bayesian layer
# (BAYES_PRIOR_ALPHA/BETA below) reports a shrunk posterior + credible
# interval instead of raw wins/n — an n=7 case no longer prints a bare
# "71% win rate" that overstates itself, it prints something like "60%
# (CI 35-82%)", and that wide CI IS the honesty the old hard gate existed
# to enforce. 10 is still a real floor, not zero: below it there's less
# actual data than the prior's own ~8 pseudo-observations, so the
# posterior is mostly the prior talking, not the tier's own history —
# that's still worth staying silent on.
EVIDENCE_MIN_N = 10

EVIDENCE_STRENGTH_BANDS = [
    (200, "VERY_STRONG"),
    (100, "STRONG"),
    (50,  "MODERATE"),
    (10,  "WEAK"),   # new floor == EVIDENCE_MIN_N, so this always matches
                      # once n clears the dormancy gate above — no case
                      # falls through to a default label anymore.
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


# ---- MARKET INTELLIGENCE NETWORK: Market Evolution (Markov regime model) --
# "What state is the market IN, and what has it historically done NEXT
# from here?" — separate question from the Evidence Engine above (which
# asks "what happened to setups shaped like this one"). This tracks the
# 1H directional regime itself, scan to scan, as a small discrete Markov
# chain. Read-only annotation layer, same as Evidence: it NEVER feeds
# back into fired/decision/sizing — see record_markov_transition() and
# how it's wired into scan() (before any gate, exactly like the shadow
# pipeline) for why that can't happen even by accident.
#
# 5 states, not the full bias x ATR x regime-shift x post-spike x session
# cross-product: CONSOLIDATION plus a FRESH/STALE split on each
# directional bias. Kept deliberately small — a 96-state chain would
# need far more scans than this bot will see in months before any row
# has enough n to mean anything; ATR/regime-shift/post-spike are already
# tracked elsewhere (MarketContext, Evidence Engine) and don't need a
# second, sparser home here.
MARKOV_STATES = ["BULLISH_FRESH", "BULLISH_STALE", "BEARISH_FRESH", "BEARISH_STALE", "CONSOLIDATION"]

# Dirichlet/Laplace smoothing per outgoing transition — same shrinkage
# idea as BAYES_PRIOR_ALPHA/BETA above, generalized from 2 outcomes to 5.
# alpha=1 per state (5 pseudo-observations total per row) so a
# just-seen state with 1-2 real transitions doesn't report a false-
# confident 100% to whichever neighbor it happened to visit first.
MARKOV_PRIOR_ALPHA = 1.0

MARKOV_STATE_FILE = os.path.join(BASE_DIR, "markov_transitions.json")

# ---- FORWARD OBSERVATION + VALIDATION ENGINE --------------------------------
LEG_OBS_STATE_FILE = os.path.join(BASE_DIR, "leg_obs_state.json")   # current open per-leg record
LEG_OBS_LOG_FILE   = os.path.join(BASE_DIR, "leg_obs_log.jsonl")    # permanent resolved records (append-only)
LEG_OBS_CLOSED_MAX = 20   # rolling cap on closed_ids set — prevents reopen-after-invalidation bug
# Bumped 2 -> 3 (Phase 2b, per chat — Market Story / break-by-break timeline):
# open records now carry `timeline` + `_tl_*` scratch fields that older
# records don't have. load_leg_obs_state() already resets to a clean slate
# on any version mismatch (see min_scanner.py) rather than crashing on a
# missing key, so bumping this is the same discipline as the v1->v2 bump —
# any leg mid-flight when this ships just restarts tracking fresh.
LEG_OBS_METHODOLOGY_VERSION = 3
# Rolling cap on the open record's timeline list — same idea as
# LEG_OBS_CLOSED_MAX above. A leg aging for hundreds of bars shouldn't grow
# state.json/leg_obs_state.json unboundedly; the oldest events fall off.
LEG_OBS_TIMELINE_MAX = 20

CALIBRATION_LOG_FILE = os.path.join(BASE_DIR, "calibration_log.jsonl")   # reserved for future per-event logging
# Reliability-diagram buckets: stated probability → empirical frequency check.
# Same principle a weather forecaster uses: if we said "70% chance of regime X",
# how often did regime X actually happen over all such predictions?
CALIBRATION_BUCKETS = [
    (0.0,  0.3,  "<30%"),
    (0.3,  0.4,  "30-40%"),
    (0.4,  0.5,  "40-50%"),
    (0.5,  0.6,  "50-60%"),
    (0.6,  0.7,  "60-70%"),
    (0.7,  0.8,  "70-80%"),
    (0.8,  0.9,  "80-90%"),
    (0.9,  1.01, "90%+"),
]
CALIBRATION_MIN_N = 10   # same gate discipline as EVIDENCE_MIN_N

# ---- FAILURE INVESTIGATION BUREAU -------------------------------------------
# Per chat: opens a "case" whenever a signal that WOULD have fired live
# (EXP7_TIER_ATR, tags.would_have_fired_live=True) resolves as a loss.
# Compares that trade's formation tags against this tier's own historical
# winners/losers, ranks which tag differs most, and forwards a research
# QUESTION — never a strategy change. See open_failure_case() below.
FAILURE_CASE_LOG_FILE = os.path.join(BASE_DIR, "failure_case_log.jsonl")   # permanent, append-only
# Minimum records needed in EACH of the winners/losers groups before a tag
# comparison is shown at all. Same floor as EVIDENCE_MIN_N/CALIBRATION_MIN_N
# — below this, "OB freshness differs by 31%" is noise wearing a number,
# exactly the failure mode flagged in chat for the original steps 5-6.
CASE_MIN_GROUP_N = EVIDENCE_MIN_N
# Tags that are administrative/derived rather than structural facts about
# the market — excluded from tag-by-tag comparison so the bureau compares
# "what the market looked like," not "what the bot already concluded
# about it" (comparing conviction_score against itself would be circular
# the same way audit fix #3 was).
CASE_EXCLUDED_TAGS = {
    "would_have_fired_live", "atr_floor_pips", "predicted_win_prob",
    "conviction_score",
}

# ---- MARKET INTENT ----------------------------------------------------------
# Per chat, 2026-08-13. "Given what I'm seeing right now, what kind of
# setup would I actually be interested in?" — pre-tier, read-only, never
# a signal. See build_market_intent() (scanner_observation.py) for the
# engine itself; the constants below back its own tracking/calibration/
# failure-case lifecycle, kept DELIBERATELY SEPARATE from Forward
# Observation and the Failure Investigation Bureau (explicit choice,
# 2026-08-13 chat) rather than folded into either — Market Intent's
# "tags" are its own watched-condition vocabulary (WatchCode/CautionCode),
# not a leg's formation_state or a tier's conviction breakdown, so mixing
# them into either existing log would compare apples to a different fruit
# entirely.
MARKET_INTENT_STATE_FILE = os.path.join(BASE_DIR, "market_intent_state.json")   # current open tracked instance
MARKET_INTENT_LOG_FILE   = os.path.join(BASE_DIR, "market_intent_log.jsonl")    # permanent resolved records
MARKET_INTENT_CASE_LOG_FILE = os.path.join(BASE_DIR, "market_intent_case_log.jsonl")   # permanent, append-only
MARKET_INTENT_METHODOLOGY_VERSION = 1
# Same gate discipline as CALIBRATION_MIN_N / CASE_MIN_GROUP_N throughout
# this file — reused rather than given independent tuning, since it's the
# same underlying question ("is this sample big enough to say anything").
MARKET_INTENT_CALIBRATION_MIN_N = CALIBRATION_MIN_N
MARKET_INTENT_CASE_MIN_GROUP_N = EVIDENCE_MIN_N

# Beta-Binomial prior for the Bayesian evidence layer below. Beta(4,4) —
# centered on a coin-flip (50%), worth about 8 pseudo-observations. Big
# enough to visibly discipline the exact failure mode EVIDENCE_MIN_N's
# comment above warns about (an n=7 "71% win rate" reads as noise, not a
# real edge, once shrunk toward 50%); small enough that it's swamped by
# anything past a few dozen real resolved trades. EVIDENCE_MIN_N is left
# untouched on purpose — this replaces WHAT gets reported (posterior
# mean + credible interval instead of raw wins/n), not WHEN it's shown.
BAYES_PRIOR_ALPHA = 4.0
BAYES_PRIOR_BETA  = 4.0
BAYES_CI_LEVEL    = 0.95   # width of the reported credible interval


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
        atomic_write_json(STATE_FILE, state)
    except Exception as e:
        print("[STATE SAVE ERROR] " + str(e))


def load_markov_data():
    """Persisted transition COUNTS (the sufficient statistic — same role
    wins/losses play for the Bayesian evidence layer). Never ages out
    like load_state() does; a regime chain is only useful cumulative."""
    try:
        with open(MARKOV_STATE_FILE, "r") as f:
            data = json.load(f)
        data.setdefault("transitions", {})
        return data
    except Exception:
        return {"transitions": {}}


def save_markov_data(markov_data):
    try:
        atomic_write_json(MARKOV_STATE_FILE, markov_data, indent=2)
    except Exception as e:
        print("[MARKOV SAVE ERROR] " + str(e))


_STATS_DEFAULTS = {
    "total_scans":          0,
    "consolidation_skip":   0,
    # RENAMED from "atr_too_low" — ATR is no longer a hard gate, so this
    # now only counts scans where the ATR reading itself was unusable
    # (NaN/0), not "ATR was below the floor" (there is no floor anymore).
    "atr_invalid":          0,
    # Pure counts of how many SIGNALS (not skipped scans) fired while
    # tagged very-low/low ATR — see very_low_atr_warning/low_atr_warning
    # on MarketContext. Additive, zero effect on whether the signal fired.
    "signals_very_low_atr": 0,
    "signals_low_atr":      0,
    "regime_shift_skip":    0,
    "session_skip":         0,
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
    # NOTE: "journal", "pending_confirm", and "atr_histogram" are
    # deliberately NOT given mutable defaults here (dict/list defaults
    # shared across dict(_STATS_DEFAULTS) calls would alias between loads
    # that never got saved in between). All three are lazily created with
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
        atomic_write_json(STATS_FILE, stats, indent=2)
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
        atomic_write_json(BIAS_AB_LOG_FILE, log[-BIAS_AB_LOG_MAX_ENTRIES:], indent=2)
    except Exception as e:
        print("[BIAS AB LOG SAVE ERROR] " + str(e))


# =========================================================================
# TELEGRAM
# =========================================================================
def send_telegram(message):
    """
    Sends `message` to TELEGRAM_CHAT_ID. Tries Markdown first (existing
    behaviour — every call site's intentional *bold*/_italic_ formatting
    still works). If Telegram rejects it — most commonly "can't parse
    entities" from an unbalanced _ * ` [ that arrived via some
    dynamically-inserted enum/constant string rather than the call
    site's own deliberate formatting (e.g. a measured-move bucket label
    like UNDER_100 landing in a narrative sentence, per the 2026-08-12
    incident: this exact failure mode silently killed every /thesis
    reply with zero trace outside a console log nobody was watching) —
    retries ONCE as plain text, no parse_mode.

    A message a person is waiting on going missing entirely, with no
    signal anywhere that it tried and failed, is worse than the same
    message arriving unformatted. This does not attempt to distinguish
    "why" the first attempt failed (bad entities vs. something else) —
    a second attempt costs one extra HTTP call and is harmless even when
    the real cause wasn't Markdown-related, since that attempt will just
    fail too and both failures get logged.

    Returns True if the message was delivered by EITHER attempt, False
    only if both failed.
    """
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise Exception(f"Telegram API error: {data}")
        print("Telegram alert sent.")
        return True
    except Exception as e:
        print("[TELEGRAM ERROR] " + str(e))

    # Fallback attempt — plain text, no parse_mode, so a bad entity can't
    # reject it a second time. Kept in its own try/except: a failure here
    # must still return False cleanly rather than raising past this
    # function into a scan()/command-handler call site that isn't
    # expecting send_telegram() to ever throw.
    try:
        r2 = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        }, timeout=10)
        r2.raise_for_status()
        data2 = r2.json()
        if not data2.get("ok"):
            raise Exception(f"Telegram API error: {data2}")
        print("Telegram alert sent (plain-text fallback after Markdown parse failure).")
        return True
    except Exception as e2:
        print("[TELEGRAM ERROR] Plain-text fallback also failed: " + str(e2))
        return False

# =========================================================================
# DATA-QUALITY / MATH PRIMITIVES — used by both processes
# (fetch_ohlc itself stays in scanner_live.py; only the checks below and
#  the structural math are shared)
# =========================================================================
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


def atr_histogram_bucket(current_atr_pips, bucket_pips=ATR_HIST_BUCKET_PIPS,
                          max_pips=ATR_HIST_MAX_PIPS):
    """PURE. Maps a raw ATR-in-pips reading to a fixed-width bucket label
    for the scan-level ATR histogram (e.g. "0-1p", "1-2p", ... "10p+").
    No judgment call here — just bucketing for the histogram; the
    very-low/low ATR WARNING labels are a separate, coarser classification
    (see ATR_MIN_PIPS/ATR_WARN_PIPS) used on fired signals specifically.
    """
    if current_atr_pips < 0:
        current_atr_pips = 0.0
    if current_atr_pips >= max_pips:
        return f"{max_pips}p+"
    lo = int(current_atr_pips // bucket_pips) * bucket_pips
    hi = lo + bucket_pips
    return f"{lo}-{hi}p"


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


def calculate_position_size(stop_pips, balance, risk_pct,
                             pip_value_per_lot=PIP_VALUE_PER_STANDARD_LOT,
                             min_lot=MIN_LOT_SIZE, lot_step=LOT_STEP,
                             sanity_cap=MAX_LOT_SIZE_SANITY_CAP):
    """
    PURE. Turns a structural stop distance (in pips) + an account balance/
    risk% into a concrete lot-size recommendation. Per chat, 2026-08-16 —
    this is the "Risk Engine" piece of the friend's Structural Stop Engine
    -> Risk Engine -> Bank layering: the structural stop itself is NOT
    this function's job (that's each tier's own sl_raw, already computed
    upstream) — this only answers "how big," never "should this trade
    happen at all." Does not reject anything; MAX_RISK_ATR_MULT/
    MAX_RISK_PIPS remain the only hard gate for now (per chat — that's a
    separate discussion still pending).

    risk_dollars = balance * risk_pct, floored at 0 (never negative).
    lots_raw = risk_dollars / (stop_pips * pip_value_per_lot), then
    floored (never rounded UP — that would silently risk more than
    risk_pct asked for) to the nearest lot_step.

    Returns None if stop_pips isn't a usable positive number (mirrors the
    same "finite and on the loss side of entry" guard
    apply_risk_gate_and_finalize already uses) — the caller decides how
    to degrade, same wrap-and-report pattern as everywhere else here.

    Returns a dict: risk_dollars, lots (the number to actually use — 0.0
    if even min_lot can't be reached within budget), lots_raw (pre-floor,
    for transparency), below_min_lot (True if the risk-accurate size
    rounds to less than min_lot — trading min_lot anyway means risking
    MORE than risk_pct, which the caller must decide, not this function),
    sanity_capped (True if lots_raw exceeded sanity_cap — capped, not
    silently trusted; a near-zero stop_pips is the realistic way this
    fires, e.g. a state-bleed bug producing a degenerate stop).
    """
    if stop_pips is None or not math.isfinite(stop_pips) or stop_pips <= 0:
        return None
    if balance is None or not math.isfinite(balance) or balance <= 0:
        return None

    risk_dollars = max(0.0, balance * risk_pct)
    lots_raw = risk_dollars / (stop_pips * pip_value_per_lot)

    sanity_capped = lots_raw > sanity_cap
    lots_pre_floor = min(lots_raw, sanity_cap)

    # Floor to lot_step (never round up — see docstring).
    steps = math.floor(lots_pre_floor / lot_step + 1e-9)
    lots = round(steps * lot_step, 2)

    below_min_lot = 0 < lots_raw < min_lot

    return {
        "risk_dollars": round(risk_dollars, 2),
        "lots_raw": round(lots_raw, 4),
        "lots": lots if lots >= min_lot else 0.0,
        "below_min_lot": below_min_lot,
        "sanity_capped": sanity_capped,
        "pip_value_per_lot": pip_value_per_lot,
    }


def close_location(candle):
    """0 (closed at the low) to 1 (closed at the high)."""
    rng = candle["High"] - candle["Low"]
    if rng <= 0:
        return 0.5
    return (candle["Close"] - candle["Low"]) / rng


def find_all_fractals(df, wing=2):
    """Return all confirmed fractal highs and lows in `df`.

    STRUCTURAL LAG NOTE: wing=2 confirmation requires 2 bars on each side,
    meaning every confirmed fractal is at minimum 2 bars stale at the moment
    it can first be observed — worse in choppy conditions where the
    confirmation bar itself is the new dominant candle. That 2-bar lag flows
    directly into every BOS/CHoCH detection and hence into every tier's
    activation timing. It is not lookahead (the wing correctly excludes the
    unconfirmed edge bars), but it IS a real execution-timing cost relative
    to any lower-latency confirmation method. Worth noting when comparing
    backtested timing vs live fill quality."""
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

    CONTRACTS on the two swing-point fields in the returned dict (added
    per chat, after a real bug where a caller assumed the wrong one):
      impulse_start / origin
          This leg's FOUNDING swing point — where the current dominant
          leg began. Does NOT move on a same-direction continuation
          break; it can be many bars and several continuations old.
          Correct for: fib anchors meant to span the whole dominant leg
          (that's the actual SMC convention — Tier 2/Tier 3 use this on
          purpose), SL placement past the leg's true structural invalidation.
          WRONG for: anything meant to represent "the most recent swing
          point" or "internal/tactical structure" — see latest_swing_origin.
      latest_swing_origin
          The swing point behind this leg's MOST RECENT break —
          continuation included. Equals impulse_start when the leg
          hasn't continued yet (break_count == 1); a genuinely different,
          more recent price once it has. Correct for: anything wanting
          "the latest swing low/high," e.g. a trailing/tactical fib
          anchor (EXP2_FIB Group D uses this).
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
    last_dominant_direction = None
    leg_break_count = 0
    # Per chat — "the latest swing point, not the extreme/origin one": on a
    # same-direction continuation break, `dominant["origin"]` is
    # deliberately left untouched below (leg_break_count += 1, no
    # reassignment) so the leg's founding origin — and its formation
    # story (was_choch) — never changes. But a fresh, more recent swing
    # point (candidate_low_origin / candidate_high_origin) IS captured at
    # every break, continuation included; it was just being computed and
    # thrown away. latest_swing_origin keeps it: the swing point behind
    # the MOST RECENT break of the current dominant leg, not its first
    # one. In an uptrend that's the latest higher-low, not the leg's
    # original low. Purely additive, same as origin_idx/was_choch above —
    # existing callers reading only direction/impulse_start/impulse_end
    # are unaffected.
    latest_swing_origin = None
    latest_swing_origin_idx = None
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
                "break_idx": i,
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
                "break_idx": i,
                "extreme": close,
            }
            external_low = close
            external_high = None
            candidate_high_origin = None

        if new_candidate is not None:
            latest_swing_origin = new_candidate["origin"]
            latest_swing_origin_idx = new_candidate["origin_idx"]
            if dominant is None:
                dominant = new_candidate
                leg_break_count = 1
                was_choch = (
                    last_dominant_direction is not None and
                    new_candidate["direction"] != last_dominant_direction
                )
            elif new_candidate["direction"] == dominant["direction"]:
                leg_break_count += 1
            else:
                dominant = new_candidate
                leg_break_count = 1
                was_choch = True  # genuine flip from an opposite-direction dominant
            last_dominant_direction = dominant["direction"]

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
        "break_idx": dominant.get("break_idx"),
        # Per chat — "was this leg's formation a genuine CHoCH, or a
        # fresh leg with no prior direction to flip from?" Purely
        # additive, same as origin_idx above: existing callers that
        # don't read this key are completely unaffected.
        "was_choch": was_choch,
        # Per chat — the swing point behind this leg's MOST RECENT break
        # (continuation included), not its founding one. Equals
        # impulse_start when the leg hasn't continued yet (break_count
        # == 1); diverges from it after continuation breaks, which is the
        # whole point. Falls back to impulse_start/origin_idx in the
        # (should-never-happen) case a dominant leg exists with no
        # recorded break — keeps this key always populated for callers
        # that don't want to special-case None.
        "latest_swing_origin": (
            latest_swing_origin if latest_swing_origin is not None else dominant["origin"]
        ),
        "latest_swing_origin_idx": (
            latest_swing_origin_idx if latest_swing_origin_idx is not None else dominant.get("origin_idx")
        ),
    }

