"""
GBPUSD SMC Scanner V2 - GitHub Actions Edition
================================================
- Data: Twelve Data API (800 calls/day free)
- Runner: GitHub Actions (free, never goes offline)
- Alerts: Telegram
- Each run recomputes everything fresh from market data, with one
  exception: a short, time-bounded memory of the last confirmed
  dominant leg (state.json, capped at STATE_MAX_AGE_HOURS old) so a
  run that can't redetect a leg in its short lookback window isn't
  forced to fall all the way back to weaker logic.

V2 changes from V1:
- Binary Signal/No-Signal replaced with a weighted confidence score
  (see SCORE_WEIGHTS / compute_confidence_score). A clean reversal
  (sweep + structure shift + fib + confirmation) outranks a valid
  continuation missing one input, rather than the continuation being
  hard-rejected for lacking a sweep.
- Every state transition (WATCHING set/cleared, signal cooldown
  released) is now caused by a market event — a new swing, a zone
  failing, price running away, a fresh sweep — never by a clock or
  scan count. The V1 WATCHING_TTL (a 10-25 min timer) is gone; the
  V1 signal cooldown (30 min / 5 pips) is gone. Both are replaced by
  structural checks: same dealing range = suppressed, new leg = fires.
- Adaptive Fib zone and ATR gating are unchanged from V1 — reviewed
  for this rewrite and still sound, no changes needed there.
"""

import os
import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# -----------------------------------------------
# CREDENTIALS - pulled from GitHub Secrets (never hardcoded)
# -----------------------------------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_DATA_KEY = os.environ["TWELVE_DATA_KEY"]

PAIR = "GBP/USD"
PIP_SIZE = 0.0001

# Risk params
RR_RATIO = 3.0
SL_ATR_MULT = 1.5
SL_MIN_PIPS = 5
ATR_ENGULF_MIN = 0.4

# Structure params
HTF_BIAS_MIN_BARS = 100
SWING_LOOKBACK_15 = 48
FRACTAL_WING = 2
INVALIDATION_RETRACE = 0.786

# Consolidation detection (1H)
HTF_EMA_SLOPE_BARS = 10
HTF_CONSOLIDATION_ATR_MULT = 0.5
HTF_EMA_FLAT_THRESHOLD = 0.15

# Macro bias confirmation (1H)
# HTF_STRUCTURE_WING: fractal wing size used to detect a real 1H BOS/CHoCH
#   (an actual macro MSS). This is the ONLY thing that flips macro bias —
#   a confirmed break of the last opposing 1H swing point. A bare EMA_100
#   cross with no matching break does not flip bias; see compute_macro_bias().
HTF_STRUCTURE_WING = 3

# Adaptive entry zone
FIB_ZONE_NEAR = 0.382
FIB_ZONE_FAR = 0.618

# Momentum-overshoot guard — price is meant to GRADUALLY work its way into
# the fib pocket across several candles, reacting as it goes. A single
# large 15M candle that spans the entire pocket in one bar is a different
# animal: that's displacement, not a pullback, and displacement candles
# very often keep going and break clean through the zone rather than
# reacting from it. This doesn't lower the score (it isn't a quality
# gradient) — it's a structural "was this actually a controlled approach"
# check, so like regime_shifted/fib_stale it suppresses the alert outright
# rather than discounting it.
MOMENTUM_OVERSHOOT_POCKET_MULT = 1.5   # 15M candle range must be at least this many
                                          # times the pocket's own width to count as
                                          # "moving with force" rather than the pocket
                                          # just being naturally narrow that day.

# Effort-invalidation guard — a second, independent definition of "momentum
# candle" alongside the pocket-span one above. Geometry (did it wick through
# the zone) isn't the only signature of a momentum candle; the other is
# EFFORT: a leg that took a long, gradual grind to build (many candles / a
# long time) can have most of that progress erased by one or two opposing
# candles. That's disproportionate regardless of where price ends up
# relative to the fib pocket, and it's the same underlying idea the person
# who wrote this described directly: momentum candles invalidate effort,
# not necessarily a fixed price zone or a fixed clock.
#
# Deliberately loose on all three knobs so this doesn't fire on ordinary
# pullbacks — it needs a genuinely slow build (min_build_bars), a genuinely
# large give-back (erase_min_fraction), AND genuinely fast erasure
# (time_max_fraction) all at once. Any one of the three alone is normal
# market behavior; only the combination is the "effort invalidated" pattern.
EFFORT_MIN_BUILD_BARS       = 4     # leg must have taken at least this many 15M bars
                                       # (~60 min) to build before this check applies at
                                       # all — a fast-built leg reversing fast isn't
                                       # disproportionate to anything.
EFFORT_ERASE_MIN_FRACTION   = 0.5   # the reversal must have given back at least half
                                       # the leg's total range — a minor pullback isn't
                                       # "invalidated effort."
EFFORT_TIME_MAX_FRACTION    = 0.25  # ...while taking no more than 25% of the bars the
                                       # leg took to build. A slow give-back over a
                                       # comparable stretch of time is just the market
                                       # reversing normally, not a momentum candle.
EFFORT_REVERSAL_WINDOW_BARS = 2     # candles counted as "the reversal" — matches the
                                       # person's own framing of "one or sometimes two
                                       # candles," not a longer counted losing streak.

# Data sanity
DATA_SPIKE_ATR_MULT = 8

# Data freshness — confirms the scanner is trading on CURRENT market data,
# not a cached/delayed/stuck feed. A "last closed candle" older than this
# many multiples of its own interval is treated as a broken feed, not real
# market silence — vendor outages and stuck caches don't announce
# themselves, they just quietly serve the same bar forever.
FRESHNESS_MAX_CANDLE_AGE_MULT = 3

# Margin of error — small pip buffers so near-misses don't get rejected for noise
ZONE_TOLERANCE_PIPS   = 3    # fib zone: wick can be this many pips shy of the level
ENGULF_TOLERANCE_PIPS = 1    # engulf body overlap: 1 pip of leniency on containment

# Liquidity sweep detection
SWEEP_LOOKBACK_CANDLES = 3   # 3×5M = 1×15M candle — sweep must resolve within one 15M bar
SWEEP_MAX_DISTANCE_PIPS = 6  # a sweep only counts toward in_zone if the actual entry
                              # price (c_last close) is still within this many pips of
                              # the zone. Without this, sweep_valid had no distance limit
                              # at all - a confirmed sweep 3 candles ago would validate
                              # an entry candle that had since drifted arbitrarily far
                              # from the zone, at a worse price with a tighter effective
                              # stop than the setup was meant to have. Sits between
                              # ZONE_TOLERANCE_PIPS (3, for direct touches) and
                              # WATCHING_EXIT_PIPS (8, a slower multi-scan give-up
                              # threshold) since a sweep already proved the level once.

# WATCHING state — event-driven invalidation only (V2)
# V1 used a clock-based TTL ("expire after 15 minutes"). V2 removes that:
# a WATCHING setup stays alive until a MARKET EVENT invalidates it, never
# just because time passed. The two guards that remain are both events:
#   - the zone structurally failing (15M close-through), and
#   - price running away from the zone without confirming.
# Neither depends on a clock.
WATCHING_EXIT_PIPS    = 8    # auto-clear if price runs this many pips away from the zone,
                              # in the direction of the original leg, without confirming.
                              # e.g. bullish watching at 1.32300 - if price rallies to
                              # 1.32380+ without ever printing the reversal candle, the
                              # pullback was missed and chasing from here is a worse trade.
                              # Distinct from the 15M close-through guard below, which
                              # catches the zone failing in the OPPOSITE direction.

# Signal "cooldown" — V2 replaces the V1 clock (30 min) + distance (5 pip)
# timer with a structural check: a new signal in the same direction is only
# suppressed if it's coming from the SAME dealing range (swing high/low)
# that produced the last signal. If a new swing/leg has formed since, it's
# a genuinely new setup and fires regardless of how little time has passed.
# The pip tolerance below is noise tolerance for "is this the same swing",
# not a timer — the same role ZONE_TOLERANCE_PIPS plays elsewhere.
STRUCTURE_MATCH_TOLERANCE_PIPS = 5

# ── Active-trade management (score freeze) ───────────────────────────────
# Once a signal fires, the scanner stops evaluating new setups on this pair
# entirely — no bias/zone/structure/scoring, no WATCHING messages — until
# the open trade closes (SL/TP hit, or manual /win /loss). The score at
# signal time is frozen into stats["active_trade"] and never recomputed;
# what gets reported while a trade is open is trade status (P/L, distance
# to TP/SL, time in trade), not a re-run of the confidence score. A score
# is a pre-trade evidence read; recomputing it mid-trade and reporting the
# drift as if it were new information is misleading — the inputs that
# built the original number (e.g. a sweep) are historical facts about how
# the trade was entered, not live conditions that can un-happen.
TRADE_STATUS_UPDATE_MINUTES = 30   # minimum gap between "still open" status
                                     # pings while a trade runs, so a live
                                     # trade doesn't spam an update every scan.
                                     # SL/TP-hit closes are still reported
                                     # immediately regardless of this gap.

# ATR minimum gate — hard floor on 5M ATR before any signal is allowed.
# At 4 pip ATR, broker spread (1.5-2.5p) consumes 37-62% of the average
# bar range, making the effective RR after spread far worse than the
# nominal 1:3. This gate refuses to trade when volatility is so thin
# that the spread itself is a structural headwind.
# 6 pips = approximately 2× typical GBPUSD spread during London session.
# Set to 0 to disable.
ATR_MIN_PIPS = 6

# Volatility regime shift detection
# Compares short-term ATR (current micro-regime) against long-term ATR
# (session baseline) to detect when a news spike or liquidity event has
# shifted the volatility environment the strategy was calibrated on.
#
# When short_atr / long_atr exceeds REGIME_SHIFT_THRESHOLD, the signal
# is suppressed but the checklist still prints and journal data is kept.
# You never lose diagnostic information — you just don't act on
# parameters that are now miscalibrated for the new volatility regime.
#
# SHORT_PERIOD = 5 bars  → last 25 minutes  (current micro-regime)
# LONG_PERIOD  = 50 bars → last ~4 hours    (session baseline)
# THRESHOLD    = 2.0     → short ATR 2× baseline = genuine regime shift
#
# OPEN_WARMUP_BARS: at session open, the long ATR still contains
# overnight low-liquidity data which makes the ratio artificially high
# even without a real news spike. Suppress regime detection for the
# first N 5M bars to avoid false positives from normal open expansion.
REGIME_SHIFT_SHORT_PERIOD = 5
REGIME_SHIFT_LONG_PERIOD  = 50
REGIME_SHIFT_THRESHOLD    = 2.0
REGIME_SHIFT_OPEN_WARMUP  = 6    # bars = ~30 min after session start
REGIME_SHIFT_ENABLED      = True

# Post-spike cooldown — continues suppressing for N bars after ratio
# drops below threshold. N scales with spike severity (tested 7/7 pass):
#   ratio=2.0 → 2 bars (10 min) | ratio=4.0 → 5 bars | ratio=8.0 → 10 bars (cap)
# Stored in state.json so it persists across the 5-minute scan boundary.
POST_SPIKE_COOLDOWN_BASE  = 2
POST_SPIKE_COOLDOWN_SCALE = 1.5
POST_SPIKE_COOLDOWN_MAX   = 10   # hard cap = 50 minutes maximum suppression

# STATE_MEMORY staleness guard — if current price is more than this many
# pips away from the midpoint of the saved leg's SwH/SwL range, treat the
# memory as stale and fall through to plain fractal detection instead.
# Rationale: a leg saved 5H ago at completely different price levels
# produces a geometrically valid but contextually meaningless Fib zone.
STATE_MEMORY_MAX_DRIFT_PIPS = 80

# -----------------------------------------------
# V2 — CONFIDENCE SCORING
# -----------------------------------------------
# Replaces the V1 binary "Signal / No Signal" output. Every setup that
# reaches the zone gets scored on the evidence actually present, rather
# than being hard-rejected for missing one input (e.g. no sweep). A clean
# reversal (sweep + CHoCH + fib + confirmation) scores higher than a
# continuation with no sweep, but the continuation still fires if it
# clears the ACCEPTABLE floor — the bot ranks evidence instead of
# hard-coding a single required path.
#
# htf_bias is intentionally NOT in this table. It used to be scored here
# (worth 20 pts, awarded automatically since CONSOLIDATION already forces
# an early return before any setup reaches scoring) which meant it was
# padding every score with 20 free points rather than measuring anything.
# It is now a hard binary pre-check (see htf_bias_gate()) that runs before
# a setup is allowed anywhere near compute_confidence_score at all — a
# regime question, not a quality question, so it can't be earned partial
# credit for or outweighed by a great fib+liquidity+confirmation read.
#
# The remaining categories are the old weights rescaled proportionally
# (old sum was 80 without htf_bias) so the total still runs 0-100 and the
# existing SCORE_TIER_* thresholds stay meaningful until they're replaced
# empirically per the threshold-calibration step.
SCORE_WEIGHTS = {
    "liquidity":    31,   # 31 = confirmed sweep, 15/16 = direct touch w/ no sweep, 0 = neither
    "structure":    25,   # 25 = fresh BOS/CHoCH aligned with bias, 12/13 = fallback/state-memory structure
    "fib":          19,   # price actually reached the discount/premium zone
    "atr":           6,   # 6 = healthy ATR, 3 = regime-shifted/thin but still tradeable
    "session":       6,   # scan occurred inside a liquid session window
    "confirmation": 13,   # engulf/rejection confirmation candle present
}
assert sum(SCORE_WEIGHTS.values()) == 100

SCORE_TIER_A_PLUS   = 90
SCORE_TIER_STRONG   = 80
SCORE_TIER_ACCEPTABLE = 70
# Below SCORE_TIER_ACCEPTABLE = IGNORE. This is the only place "should this
# fire" is decided post-zone; there's no separate hard engulf gate anymore
# because "confirmation" is just one of the seven weighed inputs.

# Session windows (UTC) — used only for the scoring bonus, not as a hard
# gate. London and New York are GBPUSD's two liquid sessions; the overlap
# (12:00-16:00 UTC) is the most liquid window of the day.
SESSION_WINDOWS_UTC = [
    (7, 16),   # London
    (12, 21),  # New York
]

# Result tracking — Telegram command to log trade outcomes manually.
# Send "/win" or "/loss" to your bot to record the last signal's result.
# The bot checks for incoming messages once per scan and updates stats.
# Set to True to enable (requires bot to have getUpdates permission).
RESULT_TRACKING_ENABLED = True

# State memory
STATE_FILE = "state.json"
STATE_MAX_AGE_HOURS = 6

# Funnel analytics — persists across runs in the repo
STATS_FILE = "stats.json"

# Shadow log — every scan, records what the OLD (ungated) bias rule would
# have decided side-by-side with the live CHoCH+BOS+EMA-gated rule. Exists
# purely to produce short-term A/B evidence on whether the stricter flip
# gate is removing bad trades or just removing trades — the two look
# identical from signal count alone, so this log is what actually answers
# it, e.g. by end of week.
SHADOW_LOG_FILE = "shadow_log.json"
SHADOW_LOG_MAX_ENTRIES = 2000   # ~7 days at one 5-min-cadence entry/scan

# Shadow TRADING pipeline (separate from the bias-only A/B log above) —
# a full independent, loose-rule mirror of the live pipeline that takes
# a meaningfully higher volume of paper trades for research. See the
# "SHADOW PIPELINE" section further down for the full design notes.
SHADOW_ATR_MIN_PIPS   = 5     # live floor is ATR_MIN_PIPS = 6
SHADOW_STATE_FILE     = "shadow_pipeline_state.json"
SHADOW_TRADES_FILE    = "shadow_pipeline_trades.json"
SHADOW_MAX_OPEN_PER_DIRECTION = 2   # a little overlap allowed, not unbounded
SHADOW_MAX_OPEN_TOTAL         = 6   # hard safety ceiling regardless of direction split
SHADOW_RESOLVED_MAX           = 1000   # rolling cap on resolved shadow-trade history

# How often to send a Telegram summary (every N scans)
# Set to 0 to disable periodic summaries entirely
STATS_SUMMARY_EVERY = 50


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


# -----------------------------------------------
# FUNNEL STATS
# -----------------------------------------------
_STATS_DEFAULTS = {
    "total_scans":         0,
    "consolidation_skip":  0,
    "bos_conflict":        0,
    "no_structure":        0,
    "atr_too_low":         0,
    "regime_shift_skip":   0,   # NEW: scans suppressed due to volatility regime shift
    "fib_reached":         0,
    "watching_alerts":     0,
    "watching_confirmed":  0,
    "pattern_passed":      0,
    "signals_sent":        0,
    "wins":                0,
    "losses":              0,
    "first_scan":          None,
    "last_scan":           None,
    "last_update_id":      0,
}


def load_stats():
    try:
        with open(STATS_FILE, "r") as f:
            saved = json.load(f)
        # Merge with defaults so new keys added in future versions
        # don't cause KeyErrors on old stats files
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


def load_shadow_log():
    try:
        with open(SHADOW_LOG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_shadow_log(log):
    try:
        with open(SHADOW_LOG_FILE, "w") as f:
            # Capped so this doesn't grow unbounded across months of runs —
            # short-term A/B evidence only needs a rolling recent window.
            json.dump(log[-SHADOW_LOG_MAX_ENTRIES:], f, indent=2)
    except Exception as e:
        print("[SHADOW LOG SAVE ERROR] " + str(e))


def format_stats_summary(stats):
    """
    Formats the funnel breakdown exactly as requested:
      Today's scans: 84
      Consolidation skips: 19
      BOS conflicts: 12
      ...
    Plus derived rates so you can immediately see which filter
    is the bottleneck without doing the maths yourself.
    """
    n = stats["total_scans"]
    if n == 0:
        return "No scans recorded yet."

    def pct(val):
        return f"{val/n*100:.1f}%" if n > 0 else "—"

    first = stats.get("first_scan", "?")
    last  = stats.get("last_scan",  "?")

    # Watching conversion rate
    watching   = stats.get("watching_alerts", 0)
    confirmed  = stats.get("watching_confirmed", 0)
    watch_conv = f"{confirmed}/{watching} ({confirmed/watching*100:.0f}%)" if watching > 0 else "—"

    # Win rate
    wins   = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    total_results = wins + losses
    win_rate = f"{wins}/{total_results} ({wins/total_results*100:.0f}%)" if total_results > 0 else "no results yet"

    # Expectancy (pips): (winrate × RR) - lossrate, normalised per pip risked
    if total_results > 0:
        wr = wins / total_results
        expectancy = (wr * RR_RATIO) - (1 - wr)
        exp_str = f"{expectancy:+.2f}R per trade"
    else:
        exp_str = "—"

    lines = [
        "",
        "📊 *SMC Scanner — Funnel Stats*",
        f"_Period: {first} → {last}_",
        "─────────────────────",
        f"🔍 Total scans:          `{n}`",
        f"➖ Consolidation skip:   `{stats['consolidation_skip']}` ({pct(stats['consolidation_skip'])})",
        f"⚠️ BOS conflict:         `{stats['bos_conflict']}` ({pct(stats['bos_conflict'])})",
        f"❌ No structure:         `{stats['no_structure']}` ({pct(stats['no_structure'])})",
        f"📉 ATR too low:          `{stats.get('atr_too_low', 0)}` ({pct(stats.get('atr_too_low', 0))})",
        f"⚡ Regime shift skip:    `{stats.get('regime_shift_skip', 0)}` ({pct(stats.get('regime_shift_skip', 0))})",
        f"🎯 Fib zone reached:     `{stats['fib_reached']}` ({pct(stats['fib_reached'])})",
        f"👀 Watching alerts:      `{stats['watching_alerts']}` ({pct(stats['watching_alerts'])})",
        f"🔄 Watch → Signal rate:  `{watch_conv}`",
        f"✅ Pattern passed:       `{stats['pattern_passed']}` ({pct(stats['pattern_passed'])})",
        f"🚨 Signals sent:         `{stats['signals_sent']}` ({pct(stats['signals_sent'])})",
        "─────────────────────",
        f"🏆 Win rate:             `{win_rate}`",
        f"📐 Expectancy:           `{exp_str}`",
        "─────────────────────",
    ]

    # Bottleneck diagnosis
    if n > 20:
        atr_skips = stats.get("atr_too_low", 0)
        if atr_skips / n > 0.3:
            lines.append("⚠️ _>30% of scans skipped — ATR_MIN_PIPS may be too high for current regime._")
        elif stats["fib_reached"] == 0:
            lines.append("⚠️ _Fib zone never reached — price not pulling back to zone._")
        elif stats["watching_alerts"] == 0:
            lines.append("⚠️ _Zone reached but WATCHING never triggered — check zone logic._")
        elif stats["pattern_passed"] == 0:
            lines.append("⚠️ _Pattern never passes — engulf filter may be too tight._")
        elif watching > 5 and confirmed == 0:
            lines.append("⚠️ _WATCHING fires but never converts — pattern too strict post-touch._")
        elif total_results >= 10 and wins / total_results < 0.35:
            lines.append("⚠️ _Win rate below 35% over 10+ trades — review entry logic._")
        else:
            lines.append("✅ _Funnel behaving as expected._")

    if RESULT_TRACKING_ENABLED:
        lines.append("_Send /win or /loss to log last trade result._")

    return "\n".join(lines)


def format_shadow_summary(shadow_log):
    """
    Summarizes the live CHoCH+BOS+EMA-gated bias vs the old ungated rule
    (any 1H break flips immediately), scan-by-scan, from shadow_log.json.

    This is the actual evidence for "is the stricter gate worth it" —
    agreement rate alone plus a list of recent divergence windows, so it
    can be read manually against how price actually moved during each
    divergence (did the old rule's earlier flip get run over, or did it
    call a real reversal sooner than the gated live rule did).
    """
    if not shadow_log:
        return "🕶️ _No shadow log entries yet — give it a few scans._"

    n = len(shadow_log)
    agree = sum(1 for e in shadow_log if e.get("agree"))
    diverge = n - agree
    agree_pct = f"{agree/n*100:.0f}%"

    first_t = shadow_log[0].get("time", "?")
    last_t  = shadow_log[-1].get("time", "?")

    lines = [
        "",
        "🕶️ *Shadow Log — Live (gated) vs Old Rule (ungated)*",
        f"_Period: {first_t} → {last_t}_",
        "─────────────────────",
        f"🔍 Scans logged:     `{n}`",
        f"🤝 Agreement:        `{agree}/{n}` ({agree_pct})",
        f"↔️ Divergence:       `{diverge}`",
        "─────────────────────",
    ]

    if diverge == 0:
        lines.append("_No divergences yet — nothing for the gate to have blocked or delayed so far._")
    else:
        # Collapse consecutive divergent scans into contiguous windows so
        # a multi-hour disagreement reads as one event, not dozens of
        # near-duplicate lines.
        windows = []
        current = None
        for e in shadow_log:
            if not e.get("agree"):
                if current is None:
                    current = {"start": e["time"], "end": e["time"],
                               "live": e["live_bias"], "shadow": e["shadow_bias"],
                               "price_start": e["price"], "price_end": e["price"]}
                else:
                    current["end"] = e["time"]
                    current["price_end"] = e["price"]
            else:
                if current is not None:
                    windows.append(current)
                    current = None
        if current is not None:
            windows.append(current)

        lines.append(f"*Recent divergence windows* (last {min(len(windows), 8)} of {len(windows)}):")
        for w in windows[-8:]:
            price_move = w["price_end"] - w["price_start"]
            lines.append(
                f"  `{w['start']}` → `{w['end']}`\n"
                f"     live=`{w['live']}` (held) vs old-rule=`{w['shadow']}`\n"
                f"     price moved {price_move:+.5f} during the window"
            )
        lines.append("─────────────────────")
        lines.append(
            "_Read each window against price direction: if price kept moving "
            "toward the old rule's call, the gate delayed/blocked a real move. "
            "If price reverted back toward the live bias, the gate avoided a whipsaw._"
        )

    return "\n".join(lines)


# -----------------------------------------------
# TELEGRAM
# -----------------------------------------------
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


# -----------------------------------------------
# ACTIVE-TRADE MANAGEMENT (score freeze)
# -----------------------------------------------
def format_trade_status(active, current_price, now_utc):
    """
    Trade-in-progress status message. Deliberately does NOT include a
    recomputed confidence score — the score line here is the frozen
    signal-time value, labeled as such, never a fresh read.
    """
    direction = active["direction"]
    entry, sl, tp = active["entry"], active["sl"], active["tp"]
    sign = 1 if direction == "BUY" else -1

    pl_pips      = (current_price - entry) * sign / PIP_SIZE
    dist_tp_pips = (tp - current_price) * sign / PIP_SIZE
    dist_sl_pips = (current_price - sl) * sign / PIP_SIZE  # cushion left before stop

    opened_at = datetime.fromisoformat(active["opened_at"])
    elapsed   = now_utc - opened_at
    total_min = int(elapsed.total_seconds() // 60)
    hours, minutes = divmod(total_min, 60)
    time_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"

    pl_emoji = "🟢" if pl_pips >= 0 else "🔴"
    dir_emoji = "📈" if direction == "BUY" else "📉"

    return (
        "📊 *Trade Active — GBPUSD*\n\n"
        f"{dir_emoji} *Direction:* `{direction}`\n"
        f"📍 *Entry:* `{entry:.5f}`  →  *Now:* `{current_price:.5f}`\n"
        f"{pl_emoji} *P/L:* `{pl_pips:+.1f} pips`\n"
        f"🎯 *Distance to TP:* `{dist_tp_pips:.1f} pips`\n"
        f"🛡 *Distance to SL:* `{dist_sl_pips:.1f} pips`\n"
        f"⏱ *Time in trade:* `{time_str}`\n"
        "─────────────────────\n"
        f"_Signal-time score: {active.get('score', '?')}/100 "
        f"({active.get('score_tier', '?')}) — frozen, not recomputed._"
    )


def format_trade_query_response(stats, current_price, now_utc):
    """
    On-demand answer for /trade. Three cases:
      1. A live trade is currently open — full status (same content as
         the periodic ping): direction, entry vs current, P/L, distance
         to TP/SL, time in trade, frozen signal-time score.
      2. No trade open, but the last one closed via SL/TP (auto-detected
         by manage_active_trade) or was logged via /win or /loss — say
         which level was hit (or which result was logged) and the pips.
      3. Never had a trade this session — say so plainly.
    """
    active = stats.get("active_trade")
    if active:
        return format_trade_status(active, current_price, now_utc)

    last_closed = stats.get("last_closed_trade")
    if last_closed:
        hit    = last_closed.get("hit", "?")
        pips   = last_closed.get("pips", 0)
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


def check_trade_closed(active, c_last):
    """
    Checks the latest closed 5M candle's High/Low against the frozen SL/TP.
    Returns "WIN", "LOSS", or None (still open).

    If a single candle's range touches BOTH levels (a gap/spike bar), we
    can't tell which was hit first from OHLC alone — we assume SL (the
    worse outcome) rather than assume the better one. That's a
    conservative approximation, not a claim about intrabar sequencing.
    """
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


def manage_active_trade(stats, df_5m, now_utc):
    """
    Runs BEFORE any bias/zone/scoring logic in scan(). If a trade is
    already open on this pair, this function is the ONLY thing that runs
    this cycle — no new signal can be generated and no score is
    recomputed while a position is live.

    Returns True if a trade is open (caller should stop scan() here after
    saving stats), False if there is no active trade (caller proceeds
    with normal signal detection).
    """
    active = stats.get("active_trade")
    if not active:
        return False

    c_last = df_5m.iloc[-1]
    outcome = check_trade_closed(active, c_last)

    if outcome is not None:
        # Auto-close: journal it exactly like a manual /win or /loss, then
        # clear active_trade so normal scanning resumes next cycle.
        exit_price = active["tp"] if outcome == "WIN" else active["sl"]
        sign = 1 if active["direction"] == "BUY" else -1
        pl_pips = (exit_price - active["entry"]) * sign / PIP_SIZE

        if "journal" not in stats:
            stats["journal"] = []
        stats["journal"].append({
            "time":      active.get("opened_at_display", "?"),
            "logged_at": now_utc.strftime("%Y-%m-%d %H:%M UTC"),
            "result":    outcome,
            "note":      "auto-closed (SL/TP hit)",
            "signal":    active["direction"],
            "entry":     f"{active['entry']:.5f}",
            "structure": active.get("structure_source", "?"),
            "score":     active.get("score", "?"),
            "score_breakdown": active.get("score_breakdown", "?"),
        })
        stats["journal"] = stats["journal"][-100:]
        if outcome == "WIN":
            stats["wins"] = stats.get("wins", 0) + 1
        else:
            stats["losses"] = stats.get("losses", 0) + 1
        # Lock this signal against a stray manual /win or /loss arriving after auto-close.
        stats["result_logged_for_signal"] = active.get("opened_at_display")

        # Snapshot for the /trade command — "no trade in session, TP/SL hit".
        stats["last_closed_trade"] = {
            "direction":  active["direction"],
            "entry":      f"{active['entry']:.5f}",
            "exit":       f"{exit_price:.5f}",
            "result":     outcome,
            "hit":        "TP" if outcome == "WIN" else "SL",
            "pips":       pl_pips,
            "closed_at":  now_utc.strftime("%Y-%m-%d %H:%M UTC"),
            "opened_at_display": active.get("opened_at_display", "?"),
        }

        icon = "✅" if outcome == "WIN" else "❌"
        send_telegram(
            f"{icon} *Trade closed — {outcome}* (auto-detected, GBPUSD)\n\n"
            f"📍 *Entry:* `{active['entry']:.5f}`  →  *Exit:* `{exit_price:.5f}`\n"
            f"{'🟢' if pl_pips >= 0 else '🔴'} *P/L:* `{pl_pips:+.1f} pips`\n"
            f"_Signal-time score: {active.get('score', '?')}/100 ({active.get('score_tier', '?')})_"
        )
        print(f"  [TRADE] Auto-closed as {outcome} @ {exit_price:.5f} ({pl_pips:+.1f} pips).")
        stats.pop("active_trade", None)
        return False

    # Still open — send a periodic status ping, not a rescored signal.
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
        print("  [TRADE] Still open — skipping status ping (update interval not elapsed).")

    return True


# -----------------------------------------------
# DATA - Twelve Data
# -----------------------------------------------
def fetch_ohlc(interval, outputsize=200):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": PAIR,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_KEY,
        "format": "JSON",
    }
    try:
        resp = requests.get(url, params=params, timeout=15).json()
    except Exception as e:
        print("[FETCH ERROR] " + interval + ": " + str(e))
        return None

    if "values" not in resp:
        msg = resp.get("message") or resp.get("code") or "Unknown error"
        print("[API ERROR] " + interval + ": " + str(msg))
        return None

    df = pd.DataFrame(resp["values"])
    df.index = pd.to_datetime(df["datetime"], utc=True)
    df = df[["open", "high", "low", "close"]].rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close"
    }).astype(float).sort_index()

    return df.iloc[:-1]


def is_forex_weekend(now_utc):
    """
    FX closes roughly Fri 21:00 UTC -> Sun 21:00 UTC. Checked loosely by
    weekday/hour, not to the minute — this only exists so the freshness
    check below doesn't false-positive on data that's legitimately stale
    because the market itself is shut, not because the feed is broken.
    """
    wd = now_utc.weekday()  # Mon=0 ... Sun=6
    if wd == 5:
        return True
    if wd == 6:
        return True
    if wd == 4 and now_utc.hour >= 21:
        return True
    return False


def check_data_freshness(df, interval_minutes, label, now_utc):
    """
    Confirms this is CURRENT market data, not a cached/delayed/stuck feed.
    Compares the last closed candle's timestamp against now_utc; if it's
    older than a lag budget of FRESHNESS_MAX_CANDLE_AGE_MULT × interval,
    something's wrong upstream (stuck cache, vendor outage, wrong
    symbol/interval) and the scan must not trade off stale bars as if
    they were live. Skipped during the FX weekend close, when staleness
    is expected and not a symptom of anything broken.
    """
    if df is None or df.empty:
        return True  # handled separately by the None/empty check upstream

    if is_forex_weekend(now_utc):
        return True

    last_candle_time = df.index[-1]
    age_minutes = (now_utc - last_candle_time).total_seconds() / 60
    max_age = interval_minutes * FRESHNESS_MAX_CANDLE_AGE_MULT

    if age_minutes > max_age:
        print(
            "[STALE DATA] {}: last closed candle is {:.0f} min old "
            "(max allowed {:.0f} min) — feed may be delayed or stuck, "
            "refusing to trade on this as current market data.".format(
                label, age_minutes, max_age)
        )
        return False

    return True


# -----------------------------------------------
# INDICATORS
# -----------------------------------------------
def atr(df, period=14):
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


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


def detect_bos_impulse(df, wing=2, invalidation_retrace=INVALIDATION_RETRACE):
    fractals = find_all_fractals(df, wing=wing)
    if len(fractals) < 2:
        return None

    closes = df["Close"].values
    highs = df["High"].values
    lows = df["Low"].values
    n = len(df)

    external_high = None
    external_low = None
    candidate_low_origin = None
    candidate_high_origin = None
    dominant = None
    # Counts discrete break events belonging to the CURRENT dominant leg.
    # The break that founds/flips a leg (a CHoCH when it reverses an
    # opposing dominant leg) counts as 1. Any further break in that SAME
    # direction — a genuine follow-through BOS — increments it. A caller
    # deciding whether to flip a held bias can require break_count >= 2
    # to demand "CHoCH AND a confirming BOS", not just a single reversal
    # wick, before trusting the new direction.
    leg_break_count = 0

    fractal_iter = iter(fractals)
    next_fractal = next(fractal_iter, None)

    for i in range(n):
        while next_fractal is not None and next_fractal["idx"] == i:
            if next_fractal["type"] == "high":
                candidate_high_origin = next_fractal
                if external_high is None:
                    external_high = next_fractal["price"]
            else:
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

        if external_high is not None and close > external_high and candidate_low_origin is not None:
            new_candidate = {
                "direction": "BULLISH",
                "origin": candidate_low_origin["price"],
                "origin_idx": candidate_low_origin["idx"],
                "extreme": close,
            }
            external_high = close
            external_low = None

        if external_low is not None and close < external_low and candidate_high_origin is not None:
            new_candidate = {
                "direction": "BEARISH",
                "origin": candidate_high_origin["price"],
                "origin_idx": candidate_high_origin["idx"],
                "extreme": close,
            }
            external_low = close
            external_high = None

        if new_candidate is not None:
            if dominant is None:
                dominant = new_candidate
                leg_break_count = 1
            elif new_candidate["direction"] == dominant["direction"]:
                # Same-direction break: the existing leg just produced a
                # genuine follow-through break — this is the BOS half of
                # a CHoCH-then-BOS pattern if the leg started as a flip.
                leg_break_count += 1
            else:
                # Opposite-direction break while a leg was still active —
                # this IS the CHoCH: character changed on a break of the
                # most recent opposing swing, before the old leg was ever
                # invalidated by origin/78.6% retrace. Promote the new leg
                # now instead of waiting for the old leg's own invalidation
                # to eventually catch up (which may lag several bars, or
                # never trigger at all if price just chops below the break
                # without also blowing through the old origin). This
                # founding break is the CHoCH itself, not yet a confirming
                # BOS — reset the counter to 1.
                dominant = new_candidate
                leg_break_count = 1

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


def _refresh_leg_anchor(state, prefix, window_df, invalidation_retrace=INVALIDATION_RETRACE):
    """
    Checks a standing leg anchor — state[prefix+"_direction"/"_origin"/
    "_extreme"] — directly against the low/high of window_df, independent
    of whatever a fresh fractal recompute on that same window does or
    doesn't find.

    This is what makes "the leg's origin fractal scrolled out of the
    fetch window" distinguishable from "the leg actually broke." Without
    it, a window-limited detect_bos_impulse recompute has no memory and
    treats both cases identically — silently picking a different origin/
    range/retrace-threshold with no real price event behind the change.

    On survival: updates the anchor's extreme in state and returns True.
    On invalidation (origin broken, or retraced past threshold): drops
    the anchor keys from state and returns False.
    Returns False immediately if no anchor is currently set.
    """
    anchor_dir     = state.get(prefix + "_direction")
    anchor_origin  = state.get(prefix + "_origin")
    anchor_extreme = state.get(prefix + "_extreme")

    if anchor_dir not in ("BULLISH", "BEARISH") or anchor_origin is None or anchor_extreme is None:
        return False

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
        state.pop(prefix + "_direction", None)
        state.pop(prefix + "_origin", None)
        state.pop(prefix + "_extreme", None)
        return False

    state[prefix + "_extreme"] = new_extreme
    return True


def _persist_macro_swing(state, direction, origin, extreme):
    """
    Saves the actual 1H swing points backing the current confirmed macro
    bias as explicit, auditable state — separate from macro_leg_origin/
    extreme (which are direction-agnostic anchor fields used for
    invalidation checks). These are labeled swing_high/swing_low the way
    a person reading state.json would expect, plus a timestamp, so it's
    possible to look at state.json at any time and see exactly which
    swing points and when last justified the current bias — the same gap
    that made "why is it reading bearish" require three rounds of manual
    digging earlier in this conversation.
    """
    if direction == "BULLISH":
        state["macro_swing_low"]  = origin
        state["macro_swing_high"] = extreme
    else:
        state["macro_swing_high"] = origin
        state["macro_swing_low"]  = extreme
    state["macro_swing_confirmed_at"] = datetime.now(timezone.utc).isoformat()


def compute_macro_bias(df_1h, state, slope_bars=HTF_EMA_SLOPE_BARS,
                        flat_atr_mult=HTF_CONSOLIDATION_ATR_MULT,
                        flat_slope_atr_mult=HTF_EMA_FLAT_THRESHOLD,
                        structure_wing=HTF_STRUCTURE_WING):
    """
    Bias is driven by a confirmed 1H structure break (a real macro MSS via
    detect_bos_impulse on the 1H series) or by a flat-EMA CONSOLIDATION
    read. A bare EMA_100 cross with no matching 1H break does NOT flip
    bias on its own — see the FLIP GATE below for the one specific case
    where EMA does play a role.

    FLIP GATE (CHoCH confirmed by BOS, plus EMA agreement):
    When a fresh 1H break points OPPOSITE to the currently confirmed
    bias, that break alone is a CHoCH candidate — a change of character,
    not yet a confirmed reversal. Flipping the live bias on a single
    reversal wick is exactly the kind of thing that produces a FALLBACK
    -quality flip-flop. So a flip only completes when ALL of:
      1. CHoCH — a break opposite the currently held bias occurred.
      2. BOS follow-through — detect_bos_impulse's break_count for that
         new leg is >= 2, i.e. the new direction broke a FURTHER swing
         point after the initial reversal break, not just the one wick.
      3. EMA agreement — close is on the correct side of EMA_100 for the
         new direction (extra confirmation layer, not a hard structural
         requirement on its own — see htf_bias_gate design).
    Until all three line up, the OLD bias is held (not flipped, and not
    marked "stale" either — a pending flip is a distinct, more specific
    state than a plain hold-over with nothing challenging it) and the
    candidate is recorded in state["macro_bias_pending_flip"] so it's
    visible in state.json and doesn't need to be re-detected from scratch
    if the window rolls before it fully confirms.

    Mutates state["macro_bias_confirmed"] every call — caller must
    save_state(state) right after, since several downstream code paths
    return early before any other save_state() runs.

    Also mutates state["macro_bias_stale"]:
      - False whenever a fresh 1H BOS/CHoCH+BOS is what produced this bias.
      - True whenever the bias is a hold-over — the leg that last
        confirmed it has since invalidated (origin break / 78.6% retrace)
        and nothing has replaced it yet, OR this is a raw EMA cold-start
        seed with no structural confirmation at all.
    This flag exists so callers (the 15M BOS-conflict gate, the WATCHING
    invalidation guard) can tell "bias is live and structurally backed"
    apart from "bias is a placeholder waiting on the next break" — before
    this flag existed the two looked identical downstream, which meant a
    genuine 15M reversal against a stale hold was indistinguishable from
    one against a freshly confirmed bias, and got suppressed as a
    "conflict" either way.

    Also persists state["macro_leg_direction"/"macro_leg_origin"/
    "macro_leg_extreme"] as a standing anchor for the confirmed leg,
    independent of the fetch window, plus state["macro_swing_high"/
    "macro_swing_low"/"macro_swing_confirmed_at"] as the explicit,
    human-readable swing points backing that leg (see _persist_macro_swing).
    detect_bos_impulse is re-run on only the last ~120 1H bars every scan
    and has no memory of its own — if the origin fractal that anchors the
    current dominant leg is older than that window, it silently ages out
    and detect_bos_impulse picks whatever fractal happens to still be
    visible as a new "origin," producing a different leg/range/retrace
    -threshold with no real price event behind the change. The anchor
    below is checked directly against whatever price data IS available
    each scan (which necessarily postdates the anchor's origin, in or out
    of window) so a leg that's still genuinely alive isn't misread as
    invalidated just because its origin fractal scrolled out of view.
    """
    df_1h = df_1h.copy()
    df_1h["EMA_100"] = df_1h["Close"].ewm(span=100, adjust=False).mean()
    df_1h["ATR_1H"] = atr(df_1h, period=14)

    close_now = df_1h["Close"].iloc[-1]
    ema_now = df_1h["EMA_100"].iloc[-1]
    atr_now = df_1h["ATR_1H"].iloc[-1]

    confirmed = state.get("macro_bias_confirmed")

    # --- Flatness / consolidation gate (unchanged criteria) -------------
    is_flat = False
    if not (pd.isna(atr_now) or atr_now == 0 or len(df_1h) <= slope_bars):
        ema_then = df_1h["EMA_100"].iloc[-1 - slope_bars]
        dist_in_atr = abs(close_now - ema_now) / atr_now
        slope_in_atr = abs(ema_now - ema_then) / atr_now
        is_flat = dist_in_atr < flat_atr_mult and slope_in_atr < flat_slope_atr_mult

    if is_flat:
        state["macro_bias_confirmed"] = "CONSOLIDATION"
        state["macro_bias_stale"] = False
        return "CONSOLIDATION"

    # --- Only bias driver: a confirmed 1H structure break (real MSS) ----
    bos_1h = detect_bos_impulse(df_1h, wing=structure_wing)
    if bos_1h is not None:
        structural_bias = bos_1h["direction"]

        if confirmed in ("BULLISH", "BEARISH") and structural_bias != confirmed:
            # A break AGAINST the currently confirmed bias — a CHoCH
            # candidate. Gate it: require BOS follow-through AND EMA
            # agreement before actually flipping.
            break_count = bos_1h.get("break_count", 1)
            choch_confirmed_by_bos = break_count >= 2
            ema_agrees = (close_now > ema_now) if structural_bias == "BULLISH" else (close_now < ema_now)

            if choch_confirmed_by_bos and ema_agrees:
                state["macro_bias_confirmed"] = structural_bias
                state["macro_bias_stale"] = False
                state["macro_bias_pending_flip"] = None
                state["macro_leg_direction"] = structural_bias
                state["macro_leg_origin"]    = bos_1h["impulse_start"]
                state["macro_leg_extreme"]   = bos_1h["impulse_end"]
                _persist_macro_swing(state, structural_bias, bos_1h["impulse_start"], bos_1h["impulse_end"])
                print(
                    "  [BIAS FLIP] {} -> {} CONFIRMED — CHoCH + BOS follow-through "
                    "({} breaks) + EMA agree.".format(confirmed, structural_bias, break_count)
                )
                return structural_bias
            else:
                reasons = []
                if not choch_confirmed_by_bos:
                    reasons.append(f"needs BOS follow-through (only {break_count} break so far)")
                if not ema_agrees:
                    reasons.append("price hasn't crossed EMA_100 yet")
                state["macro_bias_pending_flip"] = {
                    "direction": structural_bias,
                    "break_count": break_count,
                    "ema_agrees": ema_agrees,
                    "reason": ", ".join(reasons),
                }
                print("  [BIAS] CHoCH vs {} detected ({}) but NOT confirmed — {}".format(
                    confirmed, structural_bias, ", ".join(reasons)))
                # Old bias held — not flipped, not marked stale (a pending
                # challenger is more specific than a plain unchallenged hold).
                return confirmed

        # No flip in play: either this reconfirms the already-held bias,
        # or it's a true cold start with nothing to conflict against —
        # accept immediately, same as before the flip gate existed.
        state["macro_bias_confirmed"] = structural_bias
        state["macro_bias_stale"] = False
        state["macro_bias_pending_flip"] = None
        # Anchor this fresh leg so a future window-rollover doesn't get
        # mistaken for invalidation.
        state["macro_leg_direction"] = structural_bias
        state["macro_leg_origin"]    = bos_1h["impulse_start"]
        state["macro_leg_extreme"]   = bos_1h["impulse_end"]
        _persist_macro_swing(state, structural_bias, bos_1h["impulse_start"], bos_1h["impulse_end"])
        return structural_bias

    # detect_bos_impulse found nothing IN THE CURRENT WINDOW. Before
    # treating that as invalidation, check whether we already have a
    # standing anchor for the leg the window just lost track of — if so,
    # test it directly against the price data on hand instead of trusting
    # the window-limited fractal recompute.
    if _refresh_leg_anchor(state, "macro_leg", df_1h):
        anchor_dir = state["macro_leg_direction"]
        state["macro_bias_confirmed"] = anchor_dir
        state["macro_bias_stale"]     = False
        _persist_macro_swing(state, anchor_dir, state["macro_leg_origin"], state["macro_leg_extreme"])
        return anchor_dir

    # No 1H leg detected. Hold the last confirmed bias — do NOT flip on
    # EMA position alone. Mark it stale: the structure that backed this
    # bias has invalidated and nothing fresh has replaced it yet.
    if confirmed in ("BULLISH", "BEARISH"):
        state["macro_bias_stale"] = True
        return confirmed

    # True cold start: no prior confirmed bias and no 1H leg yet. Seed
    # from raw EMA position once so the bot isn't permanently bias-less;
    # this seed is provisional and will be immediately superseded the
    # moment a real 1H break occurs. Also marked stale — it's not a
    # structural confirmation, just a placeholder.
    raw_bias = "BULLISH" if close_now > ema_now else "BEARISH"
    state["macro_bias_confirmed"] = raw_bias
    state["macro_bias_stale"] = True
    return raw_bias


def compute_macro_bias_shadow_old_rule(df_1h, state, slope_bars=HTF_EMA_SLOPE_BARS,
                                        flat_atr_mult=HTF_CONSOLIDATION_ATR_MULT,
                                        flat_slope_atr_mult=HTF_EMA_FLAT_THRESHOLD,
                                        structure_wing=HTF_STRUCTURE_WING):
    """
    SHADOW ONLY — never used for trading decisions or signals. Mirrors the
    exact pre-gate compute_macro_bias behavior: any 1H break, in either
    direction, flips bias immediately (no CHoCH+BOS follow-through
    requirement, no EMA agreement check). Runs alongside the live gated
    rule purely to produce side-by-side evidence of whether the new gate
    is filtering out bad flips or just delaying/blocking good ones.

    Deliberately self-contained and reads/writes only shadow_-prefixed
    state keys — it must never share state with, or influence, the real
    macro_bias_confirmed/macro_leg_* keys the live system trades on.
    """
    df_1h = df_1h.copy()
    df_1h["EMA_100"] = df_1h["Close"].ewm(span=100, adjust=False).mean()
    df_1h["ATR_1H"] = atr(df_1h, period=14)

    close_now = df_1h["Close"].iloc[-1]
    ema_now = df_1h["EMA_100"].iloc[-1]
    atr_now = df_1h["ATR_1H"].iloc[-1]

    confirmed = state.get("shadow_macro_bias_confirmed")

    is_flat = False
    if not (pd.isna(atr_now) or atr_now == 0 or len(df_1h) <= slope_bars):
        ema_then = df_1h["EMA_100"].iloc[-1 - slope_bars]
        dist_in_atr = abs(close_now - ema_now) / atr_now
        slope_in_atr = abs(ema_now - ema_then) / atr_now
        is_flat = dist_in_atr < flat_atr_mult and slope_in_atr < flat_slope_atr_mult

    if is_flat:
        state["shadow_macro_bias_confirmed"] = "CONSOLIDATION"
        return "CONSOLIDATION"

    bos_1h = detect_bos_impulse(df_1h, wing=structure_wing)
    if bos_1h is not None:
        structural_bias = bos_1h["direction"]
        state["shadow_macro_bias_confirmed"]  = structural_bias
        state["shadow_macro_leg_direction"]   = structural_bias
        state["shadow_macro_leg_origin"]      = bos_1h["impulse_start"]
        state["shadow_macro_leg_extreme"]     = bos_1h["impulse_end"]
        return structural_bias

    if _refresh_leg_anchor(state, "shadow_macro_leg", df_1h):
        return state["shadow_macro_leg_direction"]

    if confirmed in ("BULLISH", "BEARISH"):
        return confirmed

    raw_bias = "BULLISH" if close_now > ema_now else "BEARISH"
    state["shadow_macro_bias_confirmed"] = raw_bias
    return raw_bias


def adaptive_fib_ratio(df_5m, current_atr, near=FIB_ZONE_NEAR, far=FIB_ZONE_FAR, lookback=50):
    avg_atr = df_5m["ATR"].rolling(lookback, min_periods=10).mean().iloc[-1]
    if pd.isna(avg_atr) or avg_atr == 0 or pd.isna(current_atr):
        return (near + far) / 2

    expansion = current_atr / avg_atr
    expansion = max(0.5, min(2.0, expansion))
    t = (expansion - 0.5) / (2.0 - 0.5)
    return far - (t * (far - near))


def fallback_structure(lookback_df, macro_bias, state, wing=2):
    if (
        state.get("status") == "ACTIVE_LEG"
        and state.get("direction") == macro_bias
        and "impulse_start" in state
        and "impulse_end" in state
    ):
        if macro_bias == "BULLISH":
            return state["impulse_end"], state["impulse_start"], "STATE_MEMORY"
        else:
            return state["impulse_start"], state["impulse_end"], "STATE_MEMORY"

    sh, sl = fractal_swings(lookback_df, wing=wing)
    return sh, sl, "FALLBACK_FRACTAL"


def detect_liquidity_sweep(df_5m, df_15m, fib_zone, macro_bias,
                            lookback_candles=SWEEP_LOOKBACK_CANDLES):
    """
    Checks whether a liquidity sweep into the fib zone occurred within
    the last `lookback_candles` closed 5M bars (not counting c_last or c_prev,
    since those are already handled by the direct zone check).

    A valid sweep requires two things:
      1. A 5M candle wicked through the zone but CLOSED back on the correct
         side — price hunted liquidity below/above the zone but rejected it.
      2. The last closed 15M candle also closed on the correct side — meaning
         at the 15M level it was only a wick, not a close through. This is the
         15M confirmation the user identified: 3×5M = 1×15M, so if the 15M
         didn't close through the zone, the sweep was just a wick on the higher
         timeframe and the zone is still institutionally respected.

    Returns (swept: bool, label: str) for use in the checklist and in_zone logic.
    """
    # Candles before c_prev — the sweep would have happened here, with c_prev
    # and c_last being the post-sweep reaction candles we're trading off.
    recent = df_5m.iloc[-(lookback_candles + 2):-2]

    if macro_bias == "BULLISH":
        # Wick below zone, close above it
        swept = recent[(recent["Low"] < fib_zone) & (recent["Close"] > fib_zone)]
        if swept.empty:
            return False, "no sweep"
        # 15M close confirmation: last closed 15M must have closed above zone
        if df_15m.iloc[-1]["Close"] <= fib_zone:
            return False, "15M closed below zone"
        return True, "SWEEP CONFIRMED ✅"

    else:  # BEARISH
        # Wick above zone, close below it
        swept = recent[(recent["High"] > fib_zone) & (recent["Close"] < fib_zone)]
        if swept.empty:
            return False, "no sweep"
        # 15M close confirmation: last closed 15M must have closed below zone
        if df_15m.iloc[-1]["Close"] >= fib_zone:
            return False, "15M closed above zone"
        return True, "SWEEP CONFIRMED ✅"


def is_duplicate_signal(state, trade_signal, swing_high, swing_low):
    """
    V2: "duplicate" is a STRUCTURAL question, not a clock/distance one.
    A new signal in the same direction is suppressed only if it's still
    anchored to the same dealing range (swing high/low, within noise
    tolerance) that produced the last signal. The moment a new swing or
    a fresh BOS/CHoCH redraws that range, this returns False regardless
    of how many minutes or pips separate the two signals — a new leg is
    definitionally a new setup.
    """
    last_dir = state.get("last_signal_direction")
    last_sh  = state.get("last_signal_swing_high")
    last_sl  = state.get("last_signal_swing_low")

    if not (last_dir and last_sh is not None and last_sl is not None):
        return False
    if last_dir != trade_signal:
        return False

    tol = STRUCTURE_MATCH_TOLERANCE_PIPS * PIP_SIZE
    same_high = abs(swing_high - float(last_sh)) <= tol
    same_low  = abs(swing_low  - float(last_sl)) <= tol
    return same_high and same_low


def is_active_session(now_utc, windows=SESSION_WINDOWS_UTC):
    """Returns True if the current UTC hour falls inside a liquid session
    window. Used only as a small scoring bonus, never as a hard gate —
    GBPUSD trades outside London/NY too, it's just thinner."""
    hour = now_utc.hour
    for start, end in windows:
        if start <= hour < end:
            return True
    return False


def htf_bias_gate(macro_bias, proposed_direction):
    """
    Hard binary pre-check — NOT a scored factor. A setup whose proposed
    direction disagrees with the 1H regime must never reach
    compute_confidence_score, no matter how clean its liquidity, structure,
    fib, or confirmation read. Bias answers "is there a regime to trade
    with, and which way" — that's a yes/no gate, not a spectrum, so it
    can't be partially satisfied or outscored by the other components.

    Structurally, scan() only ever proposes a BULLISH setup when
    macro_bias is BULLISH (same for BEARISH), so today this gate should
    never actually trip. It's kept as an explicit, callable, testable
    choke point anyway — if a future change (e.g. a counter-trend fade
    variant) ever proposes a setup direction independent of macro_bias,
    it is forced through here before it can touch the scoring system.

    Returns (passed: bool, reason: str).
    """
    if macro_bias not in ("BULLISH", "BEARISH"):
        return False, f"1H regime is {macro_bias} — no tradeable bias"
    if proposed_direction not in ("BULLISH", "BEARISH"):
        return False, f"invalid proposed direction {proposed_direction!r}"
    if proposed_direction != macro_bias:
        return False, f"setup direction {proposed_direction} runs against 1H bias {macro_bias}"
    return True, ""


def compute_confidence_score(sweep_usable, in_zone_direct,
                              structure_source, in_zone, atr_ok, regime_shifted,
                              session_active, confirmation_passed, bias_stale=False):
    """
    V2 confidence score. Replaces the V1 binary Signal/No-Signal decision
    with a weighted evaluation of the evidence actually present, so a
    clean reversal (sweep+CHoCH+fib+confirmation) naturally outranks a
    valid continuation missing one input (e.g. no sweep) instead of the
    continuation being hard-rejected outright.

    HTF bias DIRECTION is a pass/fail regime gate handled by
    htf_bias_gate() before this is ever called, not a component that
    contributes points here — this function scores QUALITY only.

    bias_stale, however, is not a direction question — it's a QUALITY
    question about the same 1H macro bias htf_bias_gate() already passed,
    and it belongs here. structure_source == "BOS" as passed into this
    function means "a fresh 15M leg aligned with the current macro bias",
    which says nothing about whether the 1H bias itself is a live,
    confirmed break or a day-old carryover nothing has re-tested. A 15M
    BOS riding a stale 1H hold is real evidence, but it is not the same
    thing as a 15M BOS riding a bias that a fresh 1H MSS just confirmed —
    treating them identically is exactly how a FALLBACK_FRACTAL-quality
    setup (unconfirmed higher timeframe, weaker lower timeframe read)
    ended up scoring the same as the system's actual best setup (both
    timeframes freshly confirmed). The two losses in the journal so far
    were both non-BOS structure; this makes sure a stale bias can't
    quietly borrow full BOS-tier structure credit either.

    Returns (score: int, breakdown: dict[str, int], tier: str, emoji: str,
    warnings: list[str]).
    """
    breakdown = {}

    # Liquidity — confirmed sweep beats a direct touch with no sweep,
    # which still beats nothing (price never reached the zone at all).
    if sweep_usable:
        breakdown["liquidity"] = SCORE_WEIGHTS["liquidity"]
    elif in_zone_direct:
        breakdown["liquidity"] = SCORE_WEIGHTS["liquidity"] // 2
    else:
        breakdown["liquidity"] = 0

    # Structure — a fresh BOS/CHoCH aligned with bias beats structure
    # pulled from fallback/state-memory, which beats no structure.
    # A stale macro bias caps this at fallback-tier credit regardless of
    # the 15M read: the higher timeframe itself is unconfirmed, so the
    # combined structure picture can't be graded as the system's best case
    # (BOS + confirmed bias) even when the lower timeframe looks clean.
    if structure_source == "BOS" and not bias_stale:
        breakdown["structure"] = SCORE_WEIGHTS["structure"]
    elif structure_source == "BOS" and bias_stale:
        breakdown["structure"] = SCORE_WEIGHTS["structure"] // 2
    elif structure_source in ("STATE_MEMORY", "FALLBACK_FRACTAL") or "memory stale" in str(structure_source):
        breakdown["structure"] = SCORE_WEIGHTS["structure"] // 2
    else:
        breakdown["structure"] = 0

    # Fib — did price actually reach the discount/premium zone.
    breakdown["fib"] = SCORE_WEIGHTS["fib"] if in_zone else 0

    # ATR — healthy vol beats a regime-shifted/thin reading (still logged,
    # already passed the hard ATR_MIN_PIPS floor upstream).
    if atr_ok and not regime_shifted:
        breakdown["atr"] = SCORE_WEIGHTS["atr"]
    elif atr_ok:
        breakdown["atr"] = SCORE_WEIGHTS["atr"] // 2
    else:
        breakdown["atr"] = 0

    # Session — bonus only, never a gate.
    breakdown["session"] = SCORE_WEIGHTS["session"] if session_active else 0

    # Confirmation candle — engulf/rejection present.
    breakdown["confirmation"] = SCORE_WEIGHTS["confirmation"] if confirmation_passed else 0

    score = sum(breakdown.values())

    # ── Critical-zero warning ──────────────────────────────────────────
    # A high total built by compensating for one dead-zero factor worth
    # 20+ points is a different (riskier) setup than one that scored
    # broadly across every component, even when the two totals match.
    # This flags that case; it does not change the score or the tier.
    warnings = []
    for component, weight in SCORE_WEIGHTS.items():
        if weight >= 20 and breakdown.get(component, 0) == 0:
            warnings.append(
                f"WARNING: {component} scored 0 (worth {weight} pts) — "
                f"total may be masking a critical gap"
            )

    if bias_stale:
        warnings.append(
            "WARNING: macro bias is STALE (no live 1H break confirms it) — "
            "this setup is riding a carried-over bias, not a freshly confirmed one"
        )

    if score >= SCORE_TIER_A_PLUS:
        tier, emoji = "A+ SETUP", "🟢"
    elif score >= SCORE_TIER_STRONG:
        tier, emoji = "STRONG SETUP", "🟢"
    elif score >= SCORE_TIER_ACCEPTABLE:
        tier, emoji = "ACCEPTABLE SETUP", "🟡"
    else:
        tier, emoji = "IGNORE", "🔴"

    return score, breakdown, tier, emoji, warnings


def check_result_commands(stats):
    """
    Polls Telegram for result commands sent by the user.
    Supports optional notes for trade journaling.

    Commands:
      /win [optional note]    — log a win,  e.g. /win clean engulf at zone
      /loss [optional note]   — log a loss, e.g. /loss news spike stopped me
      /undo                    — reverse the last journal entry, no questions asked
      /confirm                 — override the last result after a flip warning
      /stats                   — get full funnel summary on demand
      /trade                   — status of the live trade in session (or
                                  "no trade in session" / which level of
                                  the last one was hit, if none is open)
      /shadow                  — loose-rule shadow pipeline summary: funnel
                                  counts, win rate, broken down by BOS vs
                                  Fractal structure and by confidence score
      /biasab                  — old-rule vs live-gated 1H bias agreement
                                  rate + recent divergences (was /shadow)
      /journal                 — get the last 10 trade journal entries
      /last                    — show the last signal that was sent

    Notes are stored in stats["journal"] as a list of dicts so you can
    cross-reference with the GitHub Actions checklist logs later:
      {"time": "...", "logged_at": "...", "result": "WIN",
       "note": "clean engulf at zone", "signal": "BUY",
       "entry": 1.32400, "structure": "BOS"}

    IMPORTANT: "time" is the SIGNAL's time (when the setup actually
    fired, i.e. last_journal_time) — NOT the moment you happened to
    send /win or /loss. That moment is stored separately as
    "logged_at". Session analysis should key off "time"; if it used
    "logged_at" instead, a /loss sent two hours after the trade closed
    would misplace it in the timeline.

    Three safeguards against a fat-fingered /win vs /loss:
      1. Cooldown per signal — once a result is logged for a signal
         (identified by last_journal_time), a second /win or /loss for
         the SAME signal is refused until a new signal fires. This
         also stops _last_signal_context(stats) from silently pulling
         the same signal context into two journal entries with
         different results.
      2. /undo — unconditionally reverses the most recent journal
         entry and decrements the matching win/loss counter.
      3. Confirmation gate — sending the opposite result within 60
         seconds of the last logged result for the SAME signal holds
         the write and asks for /confirm before overriding it.

    Uses long-poll offset (last_update_id) so each message is processed
    exactly once. Safe to call every scan.
    """
    if not RESULT_TRACKING_ENABLED:
        return stats

    url    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = stats.get("last_update_id", 0) + 1

    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 2},
                            timeout=5).json()
    except Exception:
        return stats

    if not resp.get("ok") or not resp.get("result"):
        return stats

    # Ensure journal list exists
    if "journal" not in stats:
        stats["journal"] = []

    def _log_result(stats, result, note, now_str, sig_time):
        """Append a journal entry (keyed to the SIGNAL's time) and bump the counter."""
        entry_ctx = _last_signal_context(stats)
        journal_entry = {
            "time":      sig_time,   # when the signal fired — used for session analysis
            "logged_at": now_str,    # when you actually sent /win or /loss
            "result":    result,
            "note":      note or "—",
            **entry_ctx,
        }
        stats["journal"].append(journal_entry)
        stats["journal"] = stats["journal"][-100:]
        if result == "WIN":
            stats["wins"] = stats.get("wins", 0) + 1
        else:
            stats["losses"] = stats.get("losses", 0) + 1
        # Lock this signal — no second result until a new signal fires
        stats["result_logged_for_signal"] = sig_time
        # Manual result overrides auto-detection — clear the active trade
        # (if any) so trade-status pings stop and normal scanning resumes.
        active = stats.get("active_trade")
        if active:
            # Snapshot for /trade — manual /win or /loss closes it exactly
            # like an auto-detected SL/TP hit, just with the level unknown
            # (a person logged the result, not a candle touching a price).
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
        """Pop the last journal entry and decrement its counter. No questions asked."""
        j = stats.get("journal", [])
        if not j:
            return None
        last = j.pop()
        if last.get("result") == "WIN":
            stats["wins"] = max(0, stats.get("wins", 0) - 1)
        elif last.get("result") == "LOSS":
            stats["losses"] = max(0, stats.get("losses", 0) - 1)
        # Lift the per-signal lock so the same signal can be logged again
        stats["result_logged_for_signal"] = None
        stats.pop("pending_confirm", None)
        return last

    for update in resp["result"]:
        update_id = update.get("update_id", 0)
        stats["last_update_id"] = max(stats.get("last_update_id", 0), update_id)

        msg     = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        raw     = msg.get("text", "").strip()

        # Only accept commands from your own chat
        if chat_id != TELEGRAM_CHAT_ID:
            continue

        cmd   = raw.split()[0].lower() if raw else ""
        # Everything after the command word is the optional note
        note  = raw[len(cmd):].strip() if len(raw) > len(cmd) else ""

        now_utc_dt = datetime.now(timezone.utc)
        now_str    = now_utc_dt.strftime("%Y-%m-%d %H:%M UTC")
        sig_time   = stats.get("last_journal_time", "?")

        if cmd in ("/win", "win", "/w", "/loss", "loss", "/l"):
            result = "WIN" if cmd in ("/win", "win", "/w") else "LOSS"

            locked_sig = stats.get("result_logged_for_signal")
            if locked_sig is not None and sig_time != "?" and locked_sig == sig_time:
                # A result is already on record for THIS signal — block by default.
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
                    stats["pending_confirm"] = {
                        "cmd": cmd, "note": note, "sig_time": sig_time,
                    }
                    send_telegram(
                        f"⚠️ *Result already logged for this signal* "
                        f"(`{last_entry.get('result')}` at `{last_entry.get('logged_at')}`).\n"
                        f"You just sent `{cmd}` within 60s — looks like a fat-finger flip.\n"
                        f"Reply /confirm to override the last result, or ignore to leave it as-is."
                    )
                else:
                    prior = last_entry.get('result') if last_entry else '?'
                    prior_t = last_entry.get('logged_at') if last_entry else '?'
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
            # Reverse the entry that triggered the flip warning, then log the override.
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
            # Needs a live price to compute P/L and distance to SL/TP, and
            # df_5m isn't fetched yet at this point in scan() (commands are
            # processed before the data fetch so they're never dropped by a
            # fetch failure). Defer the actual reply to right after
            # manage_active_trade() runs later this same scan, when fresh
            # 5M data is on hand — see the pending-query check in scan().
            stats["_pending_trade_query"] = True

        elif cmd in ("/shadow", "shadow"):
            send_telegram(format_shadow_pipeline_summary(load_shadow_trades()))

        elif cmd in ("/biasab", "biasab"):
            # Old-rule-vs-live-gated 1H BIAS A/B log (unrelated to the
            # /shadow loose-trading pipeline above) — kept under its own
            # command name so the two don't collide.
            send_telegram(format_shadow_summary(load_shadow_log()))

        elif cmd in ("/bias", "bias"):
            # NOTE: this branch previously referenced a bare `state` name
            # that does not exist inside this function's scope (only
            # scan() has a local `state` from load_state()) — sending
            # /bias raised a NameError and silently dropped the command.
            # Fixed by loading state fresh here.
            state        = load_state()
            confirmed    = state.get("macro_bias_confirmed", "?")
            stale        = state.get("macro_bias_stale", False)
            pending      = state.get("macro_bias_pending_flip")
            swing_high   = state.get("macro_swing_high")
            swing_low    = state.get("macro_swing_low")
            confirmed_at = state.get("macro_swing_confirmed_at", "?")
            leg_dir      = state.get("macro_leg_direction", "?")
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
            if pending:
                lines.append(
                    "Pending flip: `{}` — {}".format(
                        pending.get("direction", "?"), pending.get("reason", "?"))
                )
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
                # Show last 10 entries, newest first
                recent  = list(reversed(entries[-10:]))
                lines   = ["📓 *Trade Journal — Last 10 entries*", "─────────────────────"]
                for e in recent:
                    icon    = "✅" if e["result"] == "WIN" else "❌"
                    sig     = e.get("signal", "?")
                    entry_p = e.get("entry",  "?")
                    struct  = e.get("structure", "?")
                    score_v = e.get("score", "?")
                    note_t  = e.get("note", "—")
                    trade_time = e.get("time", "?")
                    lines.append(
                        f"{icon} `{trade_time}`\n"
                        f"   {sig} @ {entry_p} | {struct} | score {score_v}\n"
                        f"   📝 _{note_t}_"
                    )
                send_telegram("\n".join(lines))

        elif cmd in ("/last", "last"):
            ctx = _last_signal_context(stats)
            if ctx:
                send_telegram(
                    f"🔁 *Last signal sent:*\n"
                    f"Direction: `{ctx.get('signal','?')}`\n"
                    f"Entry: `{ctx.get('entry','?')}`\n"
                    f"Structure: `{ctx.get('structure','?')}`\n"
                    f"Score: `{ctx.get('score','?')}/100`\n"
                    f"Time: `{ctx.get('signal_time','?')}`"
                )
            else:
                send_telegram("_No signal recorded yet this session._")

    return stats


def _last_signal_context(stats):
    """
    Pulls the most recently sent signal's context from stats for
    journal entry enrichment. Returns an empty dict if not available.
    """
    return {
        "signal":      stats.get("last_journal_signal",    "?"),
        "entry":       stats.get("last_journal_entry",     "?"),
        "structure":   stats.get("last_journal_structure", "?"),
        "score":       stats.get("last_journal_score",      "?"),
        "score_breakdown": stats.get("last_journal_score_breakdown", "?"),
        "signal_time": stats.get("last_journal_time",      "?"),
    }


def is_state_memory_stale(state, macro_bias):
    """
    Returns True if the saved leg in STATE_MEMORY is so far from current
    price that its Fib zone would be geometrically valid but contextually
    wrong — i.e. anchored to a leg from a completely different price level.

    Check: if current price is more than STATE_MEMORY_MAX_DRIFT_PIPS from
    the midpoint of the saved SwH/SwL, treat the memory as stale.
    """
    if state.get("status") != "ACTIVE_LEG":
        return False
    if state.get("direction") != macro_bias:
        return False

    imp_start = state.get("impulse_start")
    imp_end   = state.get("impulse_end")

    if imp_start is None or imp_end is None:
        return False

    midpoint     = (imp_start + imp_end) / 2
    # We don't have current price here directly — use impulse_end as
    # a proxy for "where the leg terminated" and check if it's stale
    # relative to the structural range. If the leg's range itself is
    # implausibly tiny (< 5 pips), it's likely corrupted data.
    leg_range = abs(imp_end - imp_start)
    if leg_range < 5 * PIP_SIZE:
        return True  # Corrupted or noise leg

    return False  # Staleness check by price happens in scan() where we have df_5m


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
    if spike.tail(5).any():
        print("[DATA WARNING] " + label + ": possible bad tick in recent bars.")
        return False

    return True


def _checklist(bias, bos_check, bos_bias_check, range_check, fib_check, atr_check, pattern_check, decision,
               bias_stale=None):
    """Prints the full filter checklist every scan with emojis."""
    def fmt(val):
        s = str(val)
        if "PASS" in s or "OK" in s or "BULLISH" in s or "BEARISH" in s:
            return s + " ✅"
        elif "FAIL" in s or "CONFLICT" in s or "invalid" in s or "compressed" in s:
            return s + " ❌"
        elif "WARN" in s or "fallback" in s.lower() or "memory" in s.lower() or "fractal" in s.lower():
            return s + " ⚠️"
        elif "N/A" in s or "CONSOLIDATION" in s or "no BOS" in s.lower() or "no dominant" in s.lower():
            return s + " ➖"
        return s

    bias_line = str(bias)
    if bias == "BULLISH" or bias == "BEARISH":
        # bias_stale=None means "not applicable" (paths that never reach
        # here with a directional bias anyway). Only a directional bias
        # needs the confirmed/stale distinction called out — this is the
        # fix for the exact confusion where a day-old carried-over bias
        # printed identically to a fresh 1H MSS.
        if bias_stale:
            bias_line += " ⚠️ (STALE — no live 1H break backing this)"
        else:
            bias_line += " ✅ (CONFIRMED — fresh 1H structure)"
    elif bias == "CONSOLIDATION":
        bias_line += " ➖ (flat — no edge)"
    else:
        bias_line += " ❌"

    lines = [
        "",
        "  1H Bias:       " + bias_line,
        "  BOS:           " + fmt(bos_check),
        "  BOS/Bias Sync: " + fmt(bos_bias_check),
        "  Range Filter:  " + fmt(range_check),
        "  Fib Zone:      " + str(fib_check),
        "  ATR Filter:    " + fmt(atr_check),
        "  Pattern Check: " + fmt(pattern_check),
        "  ─────────────────────────────────────",
        "  Decision:      " + str(decision),
    ]
    return "\n".join(lines)


def scaled_cooldown_bars(peak_ratio):
    """
    Returns the post-spike cooldown duration in 5M bars, scaled to the
    severity of the volatility spike that triggered the suppression.

    Verified against test suite (7/7 pass):
      ratio=2.0 → 2 bars (10 min)   ← minimum, marginal spike
      ratio=4.0 → 5 bars (25 min)   ← moderate news event
      ratio=6.0 → 8 bars (40 min)   ← strong surprise print
      ratio=8.0 → 10 bars (50 min)  ← cap, SNB-style event
    """
    excess = max(0.0, peak_ratio - REGIME_SHIFT_THRESHOLD)
    bars   = POST_SPIKE_COOLDOWN_BASE + (excess * POST_SPIKE_COOLDOWN_SCALE)
    return min(int(bars), POST_SPIKE_COOLDOWN_MAX)


def _pocket_span_overshoot(df_15m, swing_high, swing_low, macro_bias,
                            near_ratio=FIB_ZONE_NEAR, far_ratio=FIB_ZONE_FAR):
    """
    Geometric definition of a momentum candle: the most recent 15M candle
    traversed the ENTIRE fib pocket (near edge to far edge) in a single
    bar, at a range large relative to the pocket's own width. That's a
    displacement/momentum candle doing the "entering" instead of price
    gradually working into the pocket.

    Returns (is_overshoot: bool, reason: str).
    """
    structural_range = swing_high - swing_low
    if structural_range <= 0 or len(df_15m) < 1:
        return False, ""

    if macro_bias == "BULLISH":
        near_edge = swing_high - (near_ratio * structural_range)
        far_edge  = swing_high - (far_ratio  * structural_range)
    else:
        near_edge = swing_low + (near_ratio * structural_range)
        far_edge  = swing_low + (far_ratio  * structural_range)

    pocket_top    = max(near_edge, far_edge)
    pocket_bottom = min(near_edge, far_edge)
    pocket_width  = pocket_top - pocket_bottom
    if pocket_width <= 0:
        return False, ""

    c = df_15m.iloc[-1]
    candle_range = c["High"] - c["Low"]

    spans_whole_pocket = c["High"] >= pocket_top and c["Low"] <= pocket_bottom
    is_momentum_sized  = candle_range >= (MOMENTUM_OVERSHOOT_POCKET_MULT * pocket_width)

    if spans_whole_pocket and is_momentum_sized:
        return True, (
            f"15M candle range {candle_range/PIP_SIZE:.1f}p spans the whole "
            f"pocket ({pocket_width/PIP_SIZE:.1f}p) in one bar — displacement, "
            f"not a gradual approach; likely to break through rather than react"
        )
    return False, ""


def detect_effort_invalidation(df_15m, swing_high, swing_low, macro_bias,
                                lookback_bars=SWING_LOOKBACK_15,
                                min_build_bars=EFFORT_MIN_BUILD_BARS,
                                erase_min_fraction=EFFORT_ERASE_MIN_FRACTION,
                                time_max_fraction=EFFORT_TIME_MAX_FRACTION,
                                reversal_window_bars=EFFORT_REVERSAL_WINDOW_BARS):
    """
    Effort-based definition of a momentum candle, independent of the fib
    pocket entirely: a leg that took a long, gradual grind to build (many
    candles) had most of that progress erased by only one or two opposing
    candles. That's disproportionate regardless of where price ends up
    relative to any zone — the thing being invalidated is the TIME/EFFORT
    that built the leg, not a price level.

    Locates the leg's origin and extreme within the recent 15M window by
    nearest-price match (swing_high/swing_low are already known structural
    levels; this just finds where in time they occurred), measures how
    many bars separate them (the "build"), then checks how much of that
    leg's range was given back in just the last `reversal_window_bars`
    candles (the "erasure"). Flags only when the erasure is both large
    (>= erase_min_fraction of the leg) and fast (<= time_max_fraction of
    the bars it took to build) — either alone is normal market behavior.

    Returns (is_invalidated: bool, reason: str).
    """
    leg_range = swing_high - swing_low
    min_bars_needed = min_build_bars + reversal_window_bars
    if leg_range <= 0 or len(df_15m) < min_bars_needed:
        return False, ""

    lookback = df_15m.tail(lookback_bars).reset_index(drop=True)
    if len(lookback) < min_bars_needed:
        return False, ""

    if macro_bias == "BULLISH":
        idx_origin  = (lookback["Low"]  - swing_low).abs().idxmin()
        idx_extreme = (lookback["High"] - swing_high).abs().idxmin()
    else:
        idx_origin  = (lookback["High"] - swing_high).abs().idxmin()
        idx_extreme = (lookback["Low"]  - swing_low).abs().idxmin()

    bars_to_build = abs(idx_extreme - idx_origin)
    if bars_to_build < min_build_bars:
        # The leg itself formed quickly — a fast reversal isn't
        # disproportionate to anything; this check only means something
        # when the build was genuinely gradual.
        return False, ""

    reversal = lookback.tail(reversal_window_bars)
    if macro_bias == "BULLISH":
        reversal_distance = swing_high - reversal["Low"].min()
    else:
        reversal_distance = reversal["High"].max() - swing_low

    erase_fraction = reversal_distance / leg_range
    time_fraction  = len(reversal) / bars_to_build

    if erase_fraction >= erase_min_fraction and time_fraction <= time_max_fraction:
        return True, (
            f"leg took {bars_to_build} bars (~{bars_to_build * 15} min) to build "
            f"but {erase_fraction*100:.0f}% of its range was erased in just "
            f"{len(reversal)} bar(s) — effort invalidated, treat with caution "
            f"even if price reacts from here"
        )
    return False, ""


def detect_momentum_overshoot(df_15m, swing_high, swing_low, macro_bias,
                               near_ratio=FIB_ZONE_NEAR, far_ratio=FIB_ZONE_FAR):
    """
    A momentum candle can show up two different ways, and either one on
    its own is enough to distrust the setup:

      1. Pocket-span (geometry): one candle wicks straight through the
         whole fib pocket instead of price gradually working into it.
      2. Effort-invalidation (pace): a slow, many-candle build gets most
         of its progress erased by one or two opposing candles — a
         disproportionate give-back regardless of where price lands
         relative to any zone.

    Returns (is_overshoot: bool, reason: str) — whichever check fired
    first; if both fire, the pocket-span reason is reported since it's
    the more visually immediate one.
    """
    span_overshoot, span_reason = _pocket_span_overshoot(
        df_15m, swing_high, swing_low, macro_bias, near_ratio, far_ratio)
    if span_overshoot:
        return True, span_reason

    return detect_effort_invalidation(df_15m, swing_high, swing_low, macro_bias)


def is_fib_zone_stale(c_spike, swing_high, swing_low, fib_zone, current_price):
    """
    Tests whether the Fib zone should be considered stale after a spike.
    Three distinct failure modes, checked in order of severity:

    1. Spike range exceeded structural range — the whole swing repriced.
       ATR-scaled stop and engulf threshold calibrated on old range are wrong.

    2. Spike close broke SwH or SwL — structure invalidated by definition.
       Fib drawn from an origin that no longer acts as meaningful support.

    3. Current price drifted > 50% of structural range from zone — the zone
       is geometrically valid but contextually irrelevant. Even if price
       returns to the zone, it's doing so from a completely different setup.

    Returns (is_stale: bool, reason: str).
    """
    structural_range = swing_high - swing_low
    spike_range      = c_spike["High"] - c_spike["Low"]

    if spike_range > structural_range:
        return True, (
            f"spike range {spike_range/PIP_SIZE:.1f}p "
            f"> structural range {structural_range/PIP_SIZE:.1f}p"
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


def detect_regime_shift(df_5m, current_atr, now_utc):
    """
    Detects whether the current volatility environment has shifted away
    from the baseline the strategy parameters were calibrated on.

    Method: compare short-term ATR (last 25 min) against long-term ATR
    (last ~4 hours). When short/long ratio exceeds REGIME_SHIFT_THRESHOLD,
    a news spike or liquidity event has created a new volatility regime.

    In this new regime:
      - Stop loss buffer (1.5×ATR) is now too tight relative to actual moves
      - Engulf body threshold (0.4×ATR) may accept noise candles that look
        significant only relative to the now-stale long-term ATR
      - Entry zone (Fib) was calculated on structure formed in the old regime

    Returns: (is_shifted: bool, ratio: float, short_atr_pips: float)

    The open warmup guard prevents false positives at session open where
    the long ATR is contaminated by overnight low-liquidity data, making
    the ratio artificially elevated even without a genuine news event.

    now_utc is passed in from scan() rather than re-derived here. Three
    sequential data-fetch calls (each up to 15s) happen between scan()
    capturing its own now_utc and this function running; a fresh
    datetime.now() call here could drift away from that — most visibly
    right at the 08:00 UTC session-open boundary this warmup guard cares
    about, where the two timestamps could straddle the boundary and
    disagree about whether warmup should apply.
    """
    if not REGIME_SHIFT_ENABLED:
        return False, 0.0, 0.0

    if len(df_5m) < REGIME_SHIFT_LONG_PERIOD + 2:
        return False, 0.0, 0.0

    # Open warmup: skip regime detection for the first N bars of the
    # session to avoid false positives from normal open expansion
    session_open = now_utc.replace(hour=8, minute=0, second=0, microsecond=0)
    bars_since_open = int((now_utc - session_open).total_seconds() / 300)
    if 0 <= bars_since_open < REGIME_SHIFT_OPEN_WARMUP:
        return False, 0.0, 0.0

    short_atr = df_5m["ATR"].rolling(
        REGIME_SHIFT_SHORT_PERIOD,
        min_periods=REGIME_SHIFT_SHORT_PERIOD
    ).mean().iloc[-1]

    long_atr = df_5m["ATR"].rolling(
        REGIME_SHIFT_LONG_PERIOD,
        min_periods=REGIME_SHIFT_LONG_PERIOD // 2
    ).mean().iloc[-1]

    if pd.isna(short_atr) or pd.isna(long_atr) or long_atr == 0:
        return False, 0.0, 0.0

    ratio          = short_atr / long_atr
    short_atr_pips = short_atr / PIP_SIZE
    is_shifted     = ratio >= REGIME_SHIFT_THRESHOLD

    return is_shifted, ratio, short_atr_pips


# -----------------------------------------------
# SHADOW PIPELINE — loose-rule parallel paper-trading simulator
# -----------------------------------------------
# Runs a full, INDEPENDENT mirror of the live pipeline every scan, using
# deliberately looser thresholds so it takes a meaningfully higher volume
# of paper trades than the live bot for research purposes only. It never
# sends a live trading alert and never reads or writes state.json /
# stats.json — it has its own dedicated files (SHADOW_STATE_FILE /
# SHADOW_TRADES_FILE), so a bug here cannot corrupt or influence the live
# bot's trading decisions. It also computes its own 1H bias independently
# (via the SAME compute_macro_bias() the live bot uses, just against its
# own state), so it keeps evaluating and opening trades even while the
# live bot itself is mid-trade and frozen at manage_active_trade().
#
# Kept IDENTICAL to live (real structural hard-stops, not noise filters):
#   - 1H bias / CONSOLIDATION gate
#   - 15M BOS vs 1H bias conflict suppression (timeframe alignment)
#   - Structural range floor + ATR-invalid (NaN/0) guard
#   - htf_bias_gate + compute_confidence_score (identical scoring math)
#   - SCORE_TIER_ACCEPTABLE floor (70) — same bar to actually "trade"
#
# Loosened (filters that gate WHEN a real setup fires, not what counts
# as a real setup):
#   - ATR floor: SHADOW_ATR_MIN_PIPS (5) vs live's ATR_MIN_PIPS (6)
#   - Volatility regime-shift / post-spike suppression: detected and
#     tagged on the trade record, but does not block it from opening
#   - Momentum-overshoot suppression: detected and tagged, not enforced
#   - Live's full dealing-range duplicate-signal cooldown: replaced with
#     a much lighter "already have an open shadow trade near this entry,
#     in this direction" dedup — enough that a stalled price doesn't
#     spawn a new trade every 5 minutes, without throttling as hard as
#     live's full-leg-memory cooldown
#   - The two-stage WATCHING confirmation ping: not applicable — shadow
#     only records executed (or would-be) trades, not pre-alerts
def load_shadow_pipeline_state():
    try:
        with open(SHADOW_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_shadow_pipeline_state(state):
    try:
        with open(SHADOW_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print("[SHADOW STATE SAVE ERROR] " + str(e))


def load_shadow_trades():
    try:
        with open(SHADOW_TRADES_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.setdefault("funnel", {})
    data.setdefault("open_trades", [])
    data.setdefault("resolved_trades", [])
    funnel = data["funnel"]
    for k in ("total_scans", "consolidation_skip", "bos_conflict", "no_structure",
              "atr_too_low", "fib_reached", "pattern_passed", "trades_opened"):
        funnel.setdefault(k, 0)
    return data


def save_shadow_trades(data):
    data["resolved_trades"] = data["resolved_trades"][-SHADOW_RESOLVED_MAX:]
    try:
        with open(SHADOW_TRADES_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print("[SHADOW TRADES SAVE ERROR] " + str(e))


def _shadow_structure_group(structure_source):
    """
    Collapses the fine-grained structure_source label into the two
    buckets requested for the /shadow breakdown: BOS vs everything
    fractal-derived (FALLBACK_FRACTAL, STATE_MEMORY, and the
    memory-stale-fallback variant all ultimately trade off the same
    underlying fractal swing read, not a fresh confirmed break).
    """
    return "BOS" if structure_source == "BOS" else "FRACTAL"


def resolve_shadow_trades(df_5m, now_utc):
    """
    Checks every currently-open shadow trade against the latest closed
    5M candle and closes any that hit SL or TP, using the same
    conservative "assume SL if a single bar touches both levels" rule
    check_trade_closed() already applies to the live trade. Runs every
    scan regardless of anything else, so shadow trades keep resolving
    even on scans where a new one can't open (or where the live bot is
    itself mid-trade and frozen).
    """
    data = load_shadow_trades()
    if not data["open_trades"]:
        return
    c_last = df_5m.iloc[-1]
    still_open = []
    for t in data["open_trades"]:
        outcome = check_trade_closed(t, c_last)
        if outcome is None:
            still_open.append(t)
            continue
        exit_price = t["tp"] if outcome == "WIN" else t["sl"]
        sign = 1 if t["direction"] == "BUY" else -1
        pl_pips = (exit_price - t["entry"]) * sign / PIP_SIZE
        t["result"]    = outcome
        t["exit"]      = exit_price
        t["pl_pips"]   = pl_pips
        t["closed_at"] = now_utc.strftime("%Y-%m-%d %H:%M UTC")
        data["resolved_trades"].append(t)
    data["open_trades"] = still_open
    save_shadow_trades(data)


def maybe_open_shadow_trade(df_5m, df_15m, df_1h, now_utc):
    """
    Independent, loose-rule evaluation of whether a shadow (paper) trade
    should open this scan. Purely additive — only touches
    SHADOW_STATE_FILE / SHADOW_TRADES_FILE, never state.json/stats.json.
    """
    shadow_state = load_shadow_pipeline_state()
    data = load_shadow_trades()
    data["funnel"]["total_scans"] += 1

    if len(df_1h) < HTF_BIAS_MIN_BARS:
        save_shadow_trades(data)
        return

    macro_bias = compute_macro_bias(df_1h, shadow_state)
    bias_stale = shadow_state.get("macro_bias_stale", False)
    save_shadow_pipeline_state(shadow_state)

    if macro_bias == "CONSOLIDATION":
        data["funnel"]["consolidation_skip"] += 1
        save_shadow_trades(data)
        return

    df_5m = df_5m.copy()
    df_5m["ATR"] = atr(df_5m, period=14)
    current_atr = df_5m["ATR"].iloc[-1]
    if pd.isna(current_atr) or current_atr == 0:
        save_shadow_trades(data)
        return
    current_atr_pips = current_atr / PIP_SIZE

    if current_atr_pips < SHADOW_ATR_MIN_PIPS:
        data["funnel"]["atr_too_low"] += 1
        save_shadow_trades(data)
        return

    regime_shifted, regime_ratio, short_atr_pips = detect_regime_shift(df_5m, current_atr, now_utc)

    lookback = df_15m.tail(SWING_LOOKBACK_15)
    bos = detect_bos_impulse(lookback, wing=FRACTAL_WING)
    if bos is None:
        if _refresh_leg_anchor(shadow_state, "leg15", lookback):
            bos = {
                "direction":     shadow_state["leg15_direction"],
                "impulse_start": shadow_state["leg15_origin"],
                "impulse_end":   shadow_state["leg15_extreme"],
            }
    else:
        shadow_state["leg15_direction"] = bos["direction"]
        shadow_state["leg15_origin"]    = bos["impulse_start"]
        shadow_state["leg15_extreme"]   = bos["impulse_end"]

    if bos is not None:
        if bos["direction"] == macro_bias:
            structure_source = "BOS"
            if bos["direction"] == "BULLISH":
                swing_low, swing_high = bos["impulse_start"], bos["impulse_end"]
            else:
                swing_high, swing_low = bos["impulse_start"], bos["impulse_end"]
            shadow_state.update({
                "status":        "ACTIVE_LEG",
                "direction":     bos["direction"],
                "impulse_start": bos["impulse_start"],
                "impulse_end":   bos["impulse_end"],
            })
        else:
            # Real 15M-vs-1H timeframe conflict — kept as a hard stop for
            # shadow too (this is core SMC strategy alignment, not noise).
            data["funnel"]["bos_conflict"] += 1
            save_shadow_pipeline_state(shadow_state)
            save_shadow_trades(data)
            return
    else:
        swing_high, swing_low, structure_source = fallback_structure(
            lookback, macro_bias, shadow_state, wing=FRACTAL_WING)

    save_shadow_pipeline_state(shadow_state)

    structural_range = swing_high - swing_low
    if structural_range < (5 * PIP_SIZE):
        data["funnel"]["no_structure"] += 1
        save_shadow_trades(data)
        return

    fib_ratio = adaptive_fib_ratio(df_5m, current_atr)
    fib_zone = (swing_high - (fib_ratio * structural_range) if macro_bias == "BULLISH"
                else swing_low + (fib_ratio * structural_range))

    zone_tol   = ZONE_TOLERANCE_PIPS * PIP_SIZE
    engulf_tol = ENGULF_TOLERANCE_PIPS * PIP_SIZE
    sweep_valid, _sweep_label = detect_liquidity_sweep(df_5m, df_15m, fib_zone, macro_bias)
    momentum_overshoot, _momentum_reason = detect_momentum_overshoot(df_15m, swing_high, swing_low, macro_bias)

    c_last = df_5m.iloc[-1]
    c_prev = df_5m.iloc[-2]
    body_last     = abs(c_last["Close"] - c_last["Open"])
    atr_threshold = ATR_ENGULF_MIN * current_atr

    trade_signal = "HOLD"
    entry = sl = tp = None
    score = score_tier = None

    if macro_bias == "BULLISH":
        lowest_wick        = min(c_prev["Low"], c_last["Low"])
        in_zone_direct     = lowest_wick <= fib_zone + zone_tol
        sweep_distance_ok  = abs(c_last["Close"] - fib_zone) / PIP_SIZE <= SWEEP_MAX_DISTANCE_PIPS
        sweep_usable       = sweep_valid and sweep_distance_ok
        in_zone            = in_zone_direct or sweep_usable
        bear_prev          = c_prev["Close"] < c_prev["Open"]
        bull_last          = c_last["Close"] > c_last["Open"]
        engulfs            = (c_last["Close"] >= c_prev["Open"] - engulf_tol and
                               c_last["Open"]  <= c_prev["Close"] + engulf_tol)
        real_body          = body_last > atr_threshold
        confirmation_passed = bear_prev and bull_last and engulfs and real_body

        if in_zone:
            data["funnel"]["fib_reached"] += 1

        if in_zone and confirmation_passed:
            gate_ok, _ = htf_bias_gate(macro_bias, "BULLISH")
            if gate_ok:
                score, _bd, score_tier, _emoji, _warn = compute_confidence_score(
                    sweep_usable, in_zone_direct, structure_source, in_zone, True,
                    regime_shifted, is_active_session(now_utc), confirmation_passed,
                    bias_stale=bias_stale,
                )
                if score >= SCORE_TIER_ACCEPTABLE:
                    trade_signal = "BUY"
                    entry        = c_last["Close"]
                    sl_buffer    = max(SL_ATR_MULT * current_atr, SL_MIN_PIPS * PIP_SIZE)
                    sl           = lowest_wick - sl_buffer
                    risk         = entry - sl
                    tp           = entry + (RR_RATIO * risk)
                    data["funnel"]["pattern_passed"] += 1

    elif macro_bias == "BEARISH":
        highest_wick       = max(c_prev["High"], c_last["High"])
        in_zone_direct     = highest_wick >= fib_zone - zone_tol
        sweep_distance_ok  = abs(c_last["Close"] - fib_zone) / PIP_SIZE <= SWEEP_MAX_DISTANCE_PIPS
        sweep_usable       = sweep_valid and sweep_distance_ok
        in_zone            = in_zone_direct or sweep_usable
        bull_prev          = c_prev["Close"] > c_prev["Open"]
        bear_last          = c_last["Close"] < c_last["Open"]
        engulfs            = (c_last["Open"]  >= c_prev["Close"] - engulf_tol and
                               c_last["Close"] <= c_prev["Open"]  + engulf_tol)
        real_body          = body_last > atr_threshold
        confirmation_passed = bull_prev and bear_last and engulfs and real_body

        if in_zone:
            data["funnel"]["fib_reached"] += 1

        if in_zone and confirmation_passed:
            gate_ok, _ = htf_bias_gate(macro_bias, "BEARISH")
            if gate_ok:
                score, _bd, score_tier, _emoji, _warn = compute_confidence_score(
                    sweep_usable, in_zone_direct, structure_source, in_zone, True,
                    regime_shifted, is_active_session(now_utc), confirmation_passed,
                    bias_stale=bias_stale,
                )
                if score >= SCORE_TIER_ACCEPTABLE:
                    trade_signal = "SELL"
                    entry        = c_last["Close"]
                    sl_buffer    = max(SL_ATR_MULT * current_atr, SL_MIN_PIPS * PIP_SIZE)
                    sl           = highest_wick + sl_buffer
                    risk         = sl - entry
                    tp           = entry - (RR_RATIO * risk)
                    data["funnel"]["pattern_passed"] += 1

    if trade_signal == "HOLD" or entry is None:
        save_shadow_trades(data)
        return

    # Lightweight dedup — NOT live's full dealing-range memory, just
    # enough that a stalled price sitting in one zone for hours doesn't
    # spawn a near-identical "trade" every 5 minutes.
    same_dir_open = [t for t in data["open_trades"] if t["direction"] == trade_signal]
    if (len(same_dir_open) >= SHADOW_MAX_OPEN_PER_DIRECTION or
            len(data["open_trades"]) >= SHADOW_MAX_OPEN_TOTAL):
        save_shadow_trades(data)
        return
    if any(abs(t["entry"] - entry) / PIP_SIZE < 3 for t in same_dir_open):
        save_shadow_trades(data)
        return

    data["open_trades"].append({
        "direction":          trade_signal,
        "entry":              entry,
        "sl":                 sl,
        "tp":                 tp,
        "score":              score,
        "score_tier":         score_tier,
        "structure_source":   structure_source,
        "structure_group":    _shadow_structure_group(structure_source),
        "regime_shifted":     bool(regime_shifted),
        "momentum_overshoot":  bool(momentum_overshoot),
        "opened_at":          now_utc.isoformat(),
        "opened_at_display":  now_utc.strftime("%Y-%m-%d %H:%M UTC"),
    })
    data["funnel"]["trades_opened"] += 1
    save_shadow_trades(data)


def run_shadow_pipeline(df_5m, df_15m, df_1h, now_utc):
    """
    Single entry point called from scan(). Wrapped in a try/except that
    swallows and logs any error — the shadow pipeline must NEVER be able
    to raise into, break, or otherwise interrupt the live bot's own scan.
    """
    try:
        resolve_shadow_trades(df_5m, now_utc)
        maybe_open_shadow_trade(df_5m, df_15m, df_1h, now_utc)
    except Exception as e:
        print("[SHADOW PIPELINE ERROR] " + str(e))


def format_shadow_pipeline_summary(data):
    """
    /shadow response: funnel breakdown (mirrors format_stats_summary's
    style so it reads the same way), overall resolved win rate, then two
    fine-grained breakdowns — by structure source (BOS vs Fractal) and by
    confidence-score bucket (e.g. "80-84: 5 trades — 45% win rate") — so
    it's possible to see at a glance which kind of setup the looser rules
    are actually paying off on.
    """
    funnel = data.get("funnel", {})
    resolved = data.get("resolved_trades", [])
    open_trades = data.get("open_trades", [])
    n = funnel.get("total_scans", 0)
    if n == 0:
        return "🕶️ _Shadow pipeline — no scans recorded yet._"

    def pct(v):
        return f"{v/n*100:.1f}%" if n else "—"

    total_resolved = len(resolved)
    wins = sum(1 for t in resolved if t.get("result") == "WIN")
    wr = (f"{wins}/{total_resolved} ({wins/total_resolved*100:.0f}%)"
          if total_resolved else "no trades resolved yet")

    lines = [
        "",
        "🕶️ *Shadow Pipeline — Loose-Rule Simulator*",
        f"_ATR floor {SHADOW_ATR_MIN_PIPS}p (live {ATR_MIN_PIPS}p) · regime/momentum "
        "suppression OFF · duplicate cooldown light · 1H bias & BOS-conflict "
        "still hard stops_",
        "─────────────────────",
        f"🔍 Scans:                `{n}`",
        f"➖ Consolidation skip:    `{funnel.get('consolidation_skip',0)}` ({pct(funnel.get('consolidation_skip',0))})",
        f"⚠️ BOS conflict:          `{funnel.get('bos_conflict',0)}` ({pct(funnel.get('bos_conflict',0))})",
        f"❌ No structure:          `{funnel.get('no_structure',0)}` ({pct(funnel.get('no_structure',0))})",
        f"📉 ATR too low (<{SHADOW_ATR_MIN_PIPS}p):  `{funnel.get('atr_too_low',0)}` ({pct(funnel.get('atr_too_low',0))})",
        f"🎯 Fib zone reached:      `{funnel.get('fib_reached',0)}` ({pct(funnel.get('fib_reached',0))})",
        f"✅ Pattern passed:        `{funnel.get('pattern_passed',0)}` ({pct(funnel.get('pattern_passed',0))})",
        f"🚨 Trades opened:         `{funnel.get('trades_opened',0)}`",
        f"📬 Currently open:        `{len(open_trades)}`",
        "─────────────────────",
        f"🏆 Resolved win rate:     `{wr}`",
    ]

    if total_resolved:
        lines.append("─────────────────────")
        lines.append("*By structure source:*")
        for grp in ("BOS", "FRACTAL"):
            grp_trades = [t for t in resolved if t.get("structure_group") == grp]
            if not grp_trades:
                continue
            g_wins  = sum(1 for t in grp_trades if t.get("result") == "WIN")
            g_total = len(grp_trades)
            lines.append(f"  {grp}: `{g_total} trades` — `{g_wins/g_total*100:.0f}% win rate`")

        lines.append("─────────────────────")
        lines.append("*By confidence score:*")
        buckets = [(70, 74), (75, 79), (80, 84), (85, 89), (90, 94), (95, 100)]
        for lo, hi in buckets:
            b_trades = [t for t in resolved if t.get("score") is not None and lo <= t["score"] <= hi]
            if not b_trades:
                continue
            b_wins  = sum(1 for t in b_trades if t.get("result") == "WIN")
            b_total = len(b_trades)
            lines.append(f"  {lo}-{hi}: `{b_total} trades` — `{b_wins/b_total*100:.0f}% win rate`")

    lines.append("─────────────────────")
    lines.append(
        "_Research only — never a live trading alert. Send /trade for the "
        "live bot's actual open position, or /biasab for the older bias-only A/B log._"
    )
    return "\n".join(lines)


# -----------------------------------------------
# MAIN SCAN
# -----------------------------------------------
def scan():
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%H:%M UTC")
    print("\n[" + now_str + "] Scan starting...")

    # ── Load persistent stats ────────────────────────────────────────────
    stats = load_stats()
    stats["total_scans"] += 1
    if stats["first_scan"] is None:
        stats["first_scan"] = now_utc.strftime("%Y-%m-%d")
    stats["last_scan"] = now_utc.strftime("%Y-%m-%d %H:%M")

    # ── Result command check (non-blocking) ──────────────────────────────
    # Moved to the top of scan() — must run on EVERY invocation regardless
    # of data fetch failures, sanity checks, or consolidation state.
    # Previously this ran after four early-return gates further down, so
    # /stats, /win, /loss, /journal, and /last would silently queue in
    # Telegram and only get processed on a scan where all four gates
    # happened to pass (e.g. not consolidating, enough bars, data sane).
    stats = check_result_commands(stats)

    # ── Fetch data ───────────────────────────────────────────────────────
    df_5m  = fetch_ohlc("5min",  outputsize=100)
    df_15m = fetch_ohlc("15min", outputsize=SWING_LOOKBACK_15 + 10)
    df_1h  = fetch_ohlc("1h",    outputsize=HTF_BIAS_MIN_BARS + 20)

    if df_5m is None or df_15m is None or df_1h is None:
        print("Data fetch failed. Exiting.")
        save_stats(stats)
        return

    # ── Data freshness ───────────────────────────────────────────────────
    # Must run before ANYTHING structural — a stale feed masquerading as
    # live data is worse than a fetch failure, since a fetch failure is at
    # least loud. This is silent by default, which is exactly the danger.
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

    # ── Shadow pipeline (independent loose-rule paper-trade simulator) ────
    # Runs every scan with good data, regardless of whether the live bot
    # itself is about to freeze at manage_active_trade() below — it keeps
    # its own bias/state/trades entirely separate from the live bot's, so
    # it never stalls just because a live trade happens to be open.
    run_shadow_pipeline(df_5m, df_15m, df_1h, now_utc)

    # ── Active-trade check — runs BEFORE bias/zone/scoring ────────────────
    # If a trade is already open, this is the ONLY thing scan() does this
    # cycle: check for SL/TP hit, or send a status ping. No new bias read,
    # no zone/structure evaluation, no confidence score gets computed —
    # the signal-time score stays frozen in stats["active_trade"] and is
    # never touched again until the trade closes.
    trade_is_open = manage_active_trade(stats, df_5m, now_utc)

    # ── /trade on-demand query ────────────────────────────────────────────
    # Deferred here from check_result_commands() (which runs before the
    # data fetch, and so has no live price to report with) — now that
    # fresh 5M data exists and manage_active_trade() has had a chance to
    # auto-close a just-hit SL/TP this same cycle, answer with the
    # freshest possible picture.
    if stats.pop("_pending_trade_query", False):
        send_telegram(format_trade_query_response(stats, df_5m.iloc[-1]["Close"], now_utc))

    if trade_is_open:
        save_stats(stats)
        return

    if len(df_1h) < HTF_BIAS_MIN_BARS:
        print("Only " + str(len(df_1h)) + " 1H bars. Need " +
              str(HTF_BIAS_MIN_BARS) + ". Skipping.")
        save_stats(stats)
        return

    # ── 1H Bias ──────────────────────────────────────────────────────────
    state      = load_state()
    macro_bias = compute_macro_bias(df_1h, state)
    # compute_macro_bias mutates state's bias-confirmation fields on every
    # call (including the CONSOLIDATION/no-trade path below, which returns
    # early before any other save_state() call would run) — save here so
    # the confirmation counter survives across runs regardless of outcome.
    bias_stale = state.get("macro_bias_stale", False)

    # ── Shadow log (old ungated rule vs live CHoCH+BOS+EMA gated rule) ───
    # Never used for trading decisions — purely an A/B record. Runs on the
    # same state dict (writing only shadow_-prefixed keys) so it's saved
    # in the same save_state(state) call right below, then a separate
    # comparison entry gets appended to its own rolling log file.
    shadow_bias = compute_macro_bias_shadow_old_rule(df_1h, state)
    shadow_agrees = (shadow_bias == macro_bias)
    pending_flip = state.get("macro_bias_pending_flip")
    if not shadow_agrees or pending_flip:
        print(
            "  [SHADOW] live={} (gated){} | old-rule={} | {}".format(
                macro_bias,
                " STALE" if bias_stale else "",
                shadow_bias,
                "MATCH" if shadow_agrees else "DIVERGE — gate is currently holding back a flip the old rule would have taken"
            )
        )
    shadow_log = load_shadow_log()
    shadow_log.append({
        "time":            now_utc.isoformat(),
        "price":           float(df_1h["Close"].iloc[-1]),
        "live_bias":       macro_bias,
        "live_bias_stale": bias_stale,
        "shadow_bias":     shadow_bias,
        "agree":           shadow_agrees,
        "pending_flip":    pending_flip,
    })
    save_shadow_log(shadow_log)

    save_state(state)

    # WATCHING bias-mismatch guard — runs BEFORE the CONSOLIDATION early
    # return below, and before anything else, specifically so a bias flip
    # can't be missed. This is the third event-based WATCHING invalidator
    # (alongside the 15M close-through and price-runaway guards further
    # down): the bias recompute itself, done fresh from real 1H data every
    # run, is the market event here — not a timer, not a bar count. A
    # WATCHING state anchored to a bias that no longer holds (flipped
    # direction, or gone flat) is stale regardless of price action.
    #
    # Also clears on bias_stale even when the VALUE hasn't changed — a
    # stale-held bias means the leg that anchored this WATCHING zone has
    # already invalidated (origin break / 78.6% retrace); the zone was
    # built on structure that no longer exists, even though macro_bias
    # still reads the same direction it did when WATCHING was set.
    if state.get("watching") and state.get("watching_bias") and (
        state.get("watching_bias") != macro_bias or bias_stale
    ):
        reason = (
            "1H bias moved from {} to {}".format(state.get("watching_bias"), macro_bias)
            if state.get("watching_bias") != macro_bias
            else "1H structure backing '{}' has invalidated with nothing to replace it yet".format(macro_bias)
        )
        print("  [WATCHING] Cleared — " + reason + " (zone no longer valid).")
        state["watching"] = False
        state.pop("watching_zone",   None)
        state.pop("watching_bias",   None)
        state.pop("watching_set_at", None)
        save_state(state)

    if macro_bias == "CONSOLIDATION":
        stats["consolidation_skip"] += 1
        save_stats(stats)
        print(_checklist(macro_bias, "N/A", "N/A", "N/A", "N/A", "N/A", "N/A",
                          "NO TRADE — 1H is consolidating (no directional edge)"))
        return

    # ── Early stale-bias resolution via 15M structure ────────────────────
    # This is the SAME promotion logic that lives further down in the 15M
    # BOS/Structure section (a 15M break against a stale 1H hold gets
    # promoted rather than suppressed as a "conflict") — duplicated here,
    # ahead of the ATR gate below, specifically because the ATR gate
    # RETURNS before that later block ever runs. Bias bookkeeping has
    # nothing to do with whether this instant's 5M ATR clears the entry
    # floor, so a quiet-ATR scan must not be able to leave a stale bias
    # stuck reporting its OLD direction indefinitely just because it never
    # got a turn to check. The later block still runs its own copy of this
    # check when reached; it naturally no-ops here since bias_stale is
    # already False by then, so nothing fires twice.
    if bias_stale:
        early_lookback = df_15m.tail(SWING_LOOKBACK_15)
        early_bos = detect_bos_impulse(early_lookback, wing=FRACTAL_WING)
        if early_bos is None:
            if _refresh_leg_anchor(state, "leg15", early_lookback):
                early_bos = {
                    "direction":     state["leg15_direction"],
                    "impulse_start": state["leg15_origin"],
                    "impulse_end":   state["leg15_extreme"],
                }
        if early_bos is not None and early_bos["direction"] != macro_bias:
            print(
                "  [BIAS] 15M BOS ({}) reconfirms over stale 1H hold ({}) — "
                "promoting early, ahead of the ATR gate.".format(
                    early_bos["direction"], macro_bias)
            )
            macro_bias = early_bos["direction"]
            state["macro_bias_confirmed"] = macro_bias
            state["macro_bias_stale"]     = False
            state["macro_leg_direction"]  = macro_bias
            state["macro_leg_origin"]     = early_bos["impulse_start"]
            state["macro_leg_extreme"]    = early_bos["impulse_end"]
            bias_stale = False
            save_state(state)

    # ── ATR ───────────────────────────────────────────────────────────────
    df_5m["ATR"] = atr(df_5m, period=14)
    current_atr      = df_5m["ATR"].iloc[-1]
    current_atr_pips = current_atr / PIP_SIZE if not pd.isna(current_atr) else 0

    atr_valid_check = "PASS" if (not pd.isna(current_atr) and current_atr != 0) else "FAIL"
    if pd.isna(current_atr) or current_atr == 0:
        save_stats(stats)
        print(_checklist(macro_bias, "N/A", "N/A", "N/A", "N/A", atr_valid_check, "N/A",
                          "NO TRADE — ATR invalid", bias_stale=bias_stale))
        return

    # Hard minimum ATR gate
    if ATR_MIN_PIPS > 0 and current_atr_pips < ATR_MIN_PIPS:
        stats["atr_too_low"] += 1
        save_stats(stats)
        print(_checklist(
            macro_bias, "N/A", "N/A", "N/A", "N/A",
            f"FAIL — ATR {current_atr_pips:.1f}p < min {ATR_MIN_PIPS}p",
            "N/A", "NO TRADE — ATR below minimum threshold",
            bias_stale=bias_stale
        ))
        return

    # ── Volatility regime shift detection ────────────────────────────────
    # Runs AFTER the ATR minimum gate so the regime check only applies
    # to sessions where baseline volatility is already tradeable.
    # A regime shift doesn't skip the scan entirely — it runs the full
    # checklist and logs everything, but suppresses the actual signal.
    # This preserves journal data while preventing trades on miscalibrated
    # parameters.
    regime_shifted, regime_ratio, short_atr_pips = detect_regime_shift(
        df_5m, current_atr, now_utc)
    regime_note = ""

    if regime_shifted:
        # Spike just detected — record severity in state so cooldown
        # persists across the next N scan cycles even after ratio recovers
        stats["regime_shift_skip"] = stats.get("regime_shift_skip", 0) + 1
        cooldown_bars = scaled_cooldown_bars(regime_ratio)
        state["post_spike_cooldown_remaining"] = cooldown_bars
        state["post_spike_peak_ratio"]         = regime_ratio
        save_state(state)
        regime_note = (
            f"\n  ⚡ [REGIME SHIFT] Short ATR {short_atr_pips:.1f}p is "
            f"{regime_ratio:.1f}× session baseline — "
            f"suppressing for {cooldown_bars} bars (~{cooldown_bars*5} min). "
            f"Checklist logged for research."
        )
    else:
        # No active spike — but check if we're still in post-spike cooldown
        remaining = state.get("post_spike_cooldown_remaining", 0)
        if remaining > 0:
            regime_shifted = True   # still suppressed
            state["post_spike_cooldown_remaining"] = remaining - 1
            save_state(state)
            peak = state.get("post_spike_peak_ratio", regime_ratio)
            regime_note = (
                f"\n  ⏳ [POST-SPIKE COOLDOWN] {remaining} bar(s) remaining "
                f"(peak ratio was {peak:.1f}×). Signal suppressed."
            )

    # ── BOS / Structure ───────────────────────────────────────────────────
    lookback      = df_15m.tail(SWING_LOOKBACK_15)
    bos           = detect_bos_impulse(lookback, wing=FRACTAL_WING)
    bos_check     = "N/A"
    bos_bias_check = "N/A"
    bias_reconfirmed_15m = False
    bos_from_anchor = False

    if bos is not None:
        # Fresh 15M leg found — anchor it. SWING_LOOKBACK_15 is only 48
        # bars (12 hours), far shorter than the 1H side's ~120-bar window,
        # so the same window-aging bug (a still-alive leg's origin
        # fractal scrolling out of view and getting silently replaced or
        # lost) bites here more often, not less.
        state["leg15_direction"] = bos["direction"]
        state["leg15_origin"]    = bos["impulse_start"]
        state["leg15_extreme"]   = bos["impulse_end"]
    else:
        # Nothing in the current 48-bar window. Before falling all the
        # way back to STATE_MEMORY/fractals, check whether a standing 15M
        # anchor is still alive against the price data actually on hand.
        if _refresh_leg_anchor(state, "leg15", lookback):
            bos = {
                "direction":     state["leg15_direction"],
                "impulse_start": state["leg15_origin"],
                "impulse_end":   state["leg15_extreme"],
            }
            bos_from_anchor = True
            print(
                "  [15M] Window lost the origin fractal but the anchored {} "
                "leg is still intact — using it instead of falling back."
                .format(bos["direction"])
            )

    if bos is not None:
        # Stale-hold reconfirmation: the 1H side has no live structure of
        # its own right now (compute_macro_bias is coasting on a bias whose
        # supporting leg already invalidated — see bias_stale above). A
        # 15M break AGAINST that stale hold is exactly the "fresh break, in
        # either direction" that resolves the gap, not a timeframe
        # conflict — promote it instead of suppressing it. This branch is
        # unreachable when bias_stale is False, so a 15M break against a
        # freshly-confirmed 1H bias still hits the conflict-suppress path
        # below, unchanged from before.
        if bias_stale and bos["direction"] != macro_bias:
            print(
                "  [BIAS] 15M BOS ({}) reconfirms over stale 1H hold ({}) — promoting."
                .format(bos["direction"], macro_bias)
            )
            macro_bias = bos["direction"]
            state["macro_bias_confirmed"] = macro_bias
            state["macro_bias_stale"] = False
            # Anchor this promotion too — otherwise next scan's
            # compute_macro_bias finds no macro_leg_* anchor for the new
            # direction, marks it stale again for lack of one, and we're
            # right back to depending on 15M reconfirming every single
            # scan just to stand still. The 15M leg's own range is a
            # weaker anchor than a real 1H break, but it's the best
            # evidence on hand and strictly better than none.
            state["macro_leg_direction"] = macro_bias
            state["macro_leg_origin"]    = bos["impulse_start"]
            state["macro_leg_extreme"]   = bos["impulse_end"]
            bias_stale = False
            bias_reconfirmed_15m = True
            save_state(state)

        bos_check = bos["direction"] + (" OK" if bos["direction"] == macro_bias else " WARN")

        if bos["direction"] == macro_bias:
            if bias_reconfirmed_15m:
                bos_bias_check = "PASS (15M reconfirmed stale 1H)"
            elif bos_from_anchor:
                bos_bias_check = "PASS (anchor-held leg)"
            else:
                bos_bias_check = "PASS"
            structure_source = "BOS"
            if bos["direction"] == "BULLISH":
                swing_low  = bos["impulse_start"]
                swing_high = bos["impulse_end"]
            else:
                swing_high = bos["impulse_start"]
                swing_low  = bos["impulse_end"]
            state.update({
                "status":        "ACTIVE_LEG",
                "direction":     bos["direction"],
                "impulse_start": bos["impulse_start"],
                "impulse_end":   bos["impulse_end"],
            })
            save_state(state)
        else:
            # 15M structure has broken OPPOSITE to macro bias. Macro bias
            # is now itself gated on a confirmed 1H MSS (compute_macro_bias
            # no longer flips on a bare EMA cross), so this is a genuine
            # timeframe conflict, not noise — the two timeframes are
            # actively disagreeing about direction. This used to fall
            # back to weak fractal/state-memory structure and fire anyway;
            # that was the exact hole that produced the FALLBACK_FRACTAL
            # sell signal. Suppress instead of downgrading.
            stats["bos_conflict"] += 1
            save_stats(stats)
            print(_checklist(macro_bias, bos_check, "CONFLICT (suppressed)", "N/A",
                              "N/A", atr_valid_check, "N/A",
                              "NO TRADE — 15M structure conflicts with confirmed macro bias",
                              bias_stale=bias_stale))
            return
    else:
        bos_bias_check = "N/A (no dominant leg found)"
        swing_high, swing_low, structure_source = fallback_structure(
            lookback, macro_bias, state, wing=FRACTAL_WING)

    # ── STATE_MEMORY drift guard ──────────────────────────────────────────
    # If we're using a remembered leg, verify it isn't stale before
    # trusting it. Two independent conditions, either one triggers
    # fallback to a fresh fractal read:
    #   1. Corrupted/degenerate leg — the remembered range is implausibly
    #      tiny (< 5 pips), more likely bad data than real structure.
    #      (is_state_memory_stale existed for exactly this and was never
    #      actually called anywhere — wiring it in here instead of leaving
    #      it as dead code. Without this, a corrupted STATE_MEMORY leg
    #      would fall through unchecked to the Range Filter below and get
    #      an outright "NO TRADE — range too compressed" instead of a
    #      chance at a fresh, possibly perfectly tradeable, fractal read.)
    #   2. Price drift — current price has moved so far from the leg's
    #      midpoint that its Fib zone is no longer contextually
    #      meaningful. A leg from 5 hours ago at a completely different
    #      price level should not be anchoring today's entries.
    if structure_source == "STATE_MEMORY":
        memory_corrupted = is_state_memory_stale(state, macro_bias)
        current_price     = df_5m["Close"].iloc[-1]
        leg_mid           = (swing_high + swing_low) / 2
        drift_pips        = abs(current_price - leg_mid) / PIP_SIZE
        drifted           = STATE_MEMORY_MAX_DRIFT_PIPS > 0 and drift_pips > STATE_MEMORY_MAX_DRIFT_PIPS

        if memory_corrupted or drifted:
            reason = (
                "corrupted (remembered leg range < 5 pips)" if memory_corrupted
                else f"price drifted {drift_pips:.0f}p from leg midpoint"
            )
            print(f"  [STATE_MEMORY] Stale — {reason}. Falling back to fractal detection.")
            swing_high, swing_low = fractal_swings(lookback, wing=FRACTAL_WING)
            structure_source = "FALLBACK_FRACTAL (memory stale)"

    # ── Range Filter ──────────────────────────────────────────────────────
    structural_range = swing_high - swing_low
    range_check = "PASS" if structural_range >= (5 * PIP_SIZE) else "FAIL (range < 5 pips)"

    if structural_range < (5 * PIP_SIZE):
        stats["no_structure"] += 1
        save_stats(stats)
        print(_checklist(macro_bias, bos_check, bos_bias_check, range_check,
                          "N/A", atr_valid_check, "N/A",
                          "NO TRADE — range too compressed", bias_stale=bias_stale))
        return

    # ── Adaptive Fib Zone ─────────────────────────────────────────────────
    fib_ratio = adaptive_fib_ratio(df_5m, current_atr)
    fib_zone  = (
        swing_high - (fib_ratio * structural_range) if macro_bias == "BULLISH"
        else swing_low + (fib_ratio * structural_range)
    )

    # ── Sweep detection + zone tolerance ──────────────────────────────────
    # Run sweep check once here so both BULLISH and BEARISH branches can use it.
    zone_tol   = ZONE_TOLERANCE_PIPS * PIP_SIZE
    engulf_tol = ENGULF_TOLERANCE_PIPS * PIP_SIZE
    sweep_valid, sweep_label = detect_liquidity_sweep(df_5m, df_15m, fib_zone, macro_bias)

    fib_check = "{:.5f} (ratio {:.1f}%) | Zone: {}".format(
        fib_zone, fib_ratio * 100,
        sweep_label if sweep_valid else "—"
    )

    # ── Momentum-overshoot check ────────────────────────────────────────
    # Independent of the ATR-ratio regime-shift heuristic below: this asks
    # a narrower, always-on question — did the most recent 15M candle alone
    # carry price through the whole pocket, rather than price gradually
    # working into it? A regime shift can be absent (ATR ratio under
    # threshold) while a single 15M bar still blows clean through the zone,
    # so this runs on every scan, not just during a detected volatility spike.
    momentum_overshoot, momentum_reason = detect_momentum_overshoot(
        df_15m, swing_high, swing_low, macro_bias)
    momentum_note = (
        f"\n  💥 [MOMENTUM OVERSHOOT] {momentum_reason} — signal will be suppressed."
        if momentum_overshoot else ""
    )

    # c_last/c_prev are needed by the fib-staleness check right below, so
    # they're pulled here rather than further down where they used to live -
    # that ordering caused an UnboundLocalError any time regime_shifted or
    # a post-spike cooldown was active, since this block ran before the
    # variables existed yet.
    c_last = df_5m.iloc[-1]
    c_prev = df_5m.iloc[-2]

    # ── Fib zone staleness check (post-spike) ────────────────────────────
    # If a regime shift was active in any of the last few scans, verify
    # the spike candle (c_last or c_prev, whichever was more extreme)
    # didn't structurally invalidate the zone we're about to use.
    # A valid Fib zone from before a spike can be geometrically correct
    # but contextually meaningless — this check catches that.
    fib_stale      = False
    fib_stale_reason = ""
    if regime_shifted or state.get("post_spike_cooldown_remaining", 0) > 0:
        # Use the more extreme of the two recent candles as the spike proxy
        spike_proxy = {
            "High":  max(c_last["High"],  c_prev["High"]),
            "Low":   min(c_last["Low"],   c_prev["Low"]),
            "Close": c_last["Close"],
        }
        fib_stale, fib_stale_reason = is_fib_zone_stale(
            spike_proxy, swing_high, swing_low, fib_zone, c_last["Close"]
        )
        if fib_stale:
            regime_note += (
                f"\n  🗑 [FIB STALE] {fib_stale_reason} — "
                f"forcing fresh structure detection on next scan."
            )
            # Clear state memory so next scan redetects structure fresh
            # rather than continuing to use the now-invalid cached leg
            state.pop("impulse_start", None)
            state.pop("impulse_end",   None)
            if state.get("status") == "ACTIVE_LEG":
                state["status"] = "STALE_POST_SPIKE"
            save_state(state)

    body_last     = abs(c_last["Close"] - c_last["Open"])
    atr_threshold = ATR_ENGULF_MIN * current_atr

    trade_signal  = "HOLD"
    entry = sl = tp = risk_pips = reward_pips = None
    pattern_check = "N/A"
    score = score_breakdown = score_tier = score_emoji = None
    score_warnings = []
    in_zone       = False
    in_zone_direct = False

    if macro_bias == "BULLISH":
        lowest_wick    = min(c_prev["Low"], c_last["Low"])
        # Direct touch: wick at or below zone (+ tolerance buffer)
        # Sweep touch: confirmed liquidity sweep in the last 3 candles, but
        # only if the entry candle hasn't since drifted too far from the
        # zone - a sweep proves the level, it doesn't excuse chasing price
        # far away from it afterward.
        in_zone_direct = lowest_wick <= fib_zone + zone_tol
        sweep_distance_ok = abs(c_last["Close"] - fib_zone) / PIP_SIZE <= SWEEP_MAX_DISTANCE_PIPS
        sweep_usable   = sweep_valid and sweep_distance_ok
        in_zone        = in_zone_direct or sweep_usable
        bear_prev      = c_prev["Close"] < c_prev["Open"]
        bull_last      = c_last["Close"] > c_last["Open"]
        # 1 pip of leniency on body containment — near-perfect engulfs pass
        engulfs        = (c_last["Close"] >= c_prev["Open"] - engulf_tol and
                          c_last["Open"]  <= c_prev["Close"] + engulf_tol)
        real_body      = body_last > atr_threshold

        confirmation_passed = bear_prev and bull_last and engulfs and real_body

        if in_zone:
            stats["fib_reached"] += 1

        if in_zone and confirmation_passed:
            gate_ok, gate_reason = htf_bias_gate(macro_bias, "BULLISH")
            if not gate_ok:
                pattern_check = "FAIL (" + gate_reason + ")"
            else:
                score, score_breakdown, score_tier, score_emoji, score_warnings = compute_confidence_score(
                    sweep_usable, in_zone_direct, structure_source,
                    in_zone, True, regime_shifted, is_active_session(now_utc),
                    confirmation_passed, bias_stale=bias_stale,
                )
                if score >= SCORE_TIER_ACCEPTABLE:
                    trade_signal  = "BUY"
                    entry         = c_last["Close"]
                    sl_buffer     = max(SL_ATR_MULT * current_atr, SL_MIN_PIPS * PIP_SIZE)
                    sl            = lowest_wick - sl_buffer
                    risk          = entry - sl
                    tp            = entry + (RR_RATIO * risk)
                    risk_pips     = risk / PIP_SIZE
                    reward_pips   = (RR_RATIO * risk) / PIP_SIZE
                    pattern_check = "PASS — {} {}/100 ({})".format(score_emoji, score, score_tier) + (
                        " (post-sweep entry)" if sweep_usable and not in_zone_direct else "")
                    stats["pattern_passed"] += 1
                else:
                    pattern_check = "PASS mechanically but IGNORED — {} {}/100 < {} floor ({})".format(
                        score_emoji, score, SCORE_TIER_ACCEPTABLE,
                        ", ".join(f"{k}:{v}" for k, v in score_breakdown.items()))
        else:
            score = score_breakdown = score_tier = score_emoji = None
            score_warnings = []
            fails = []
            if not in_zone:
                if sweep_valid and not sweep_distance_ok:
                    fails.append("sweep confirmed but price drifted too far from zone")
                else:
                    fails.append("price not in discount zone (no direct touch or sweep)")
            if not bear_prev:  fails.append("prev candle not bearish")
            if not bull_last:  fails.append("last candle not bullish")
            if not engulfs:    fails.append("doesn't engulf prev body")
            if not real_body:  fails.append("body too small vs ATR")
            pattern_check = "FAIL (" + ", ".join(fails) + ")"

    elif macro_bias == "BEARISH":
        highest_wick   = max(c_prev["High"], c_last["High"])
        # Direct touch: wick at or above zone (- tolerance buffer)
        # Sweep touch: confirmed liquidity sweep in the last 3 candles, but
        # only if the entry candle hasn't since drifted too far from the zone.
        in_zone_direct = highest_wick >= fib_zone - zone_tol
        sweep_distance_ok = abs(c_last["Close"] - fib_zone) / PIP_SIZE <= SWEEP_MAX_DISTANCE_PIPS
        sweep_usable   = sweep_valid and sweep_distance_ok
        in_zone        = in_zone_direct or sweep_usable
        bull_prev      = c_prev["Close"] > c_prev["Open"]
        bear_last      = c_last["Close"] < c_last["Open"]
        # 1 pip of leniency on body containment
        engulfs        = (c_last["Open"]  >= c_prev["Close"] - engulf_tol and
                          c_last["Close"] <= c_prev["Open"]  + engulf_tol)
        real_body      = body_last > atr_threshold

        confirmation_passed = bull_prev and bear_last and engulfs and real_body

        if in_zone:
            stats["fib_reached"] += 1

        if in_zone and confirmation_passed:
            gate_ok, gate_reason = htf_bias_gate(macro_bias, "BEARISH")
            if not gate_ok:
                pattern_check = "FAIL (" + gate_reason + ")"
            else:
                score, score_breakdown, score_tier, score_emoji, score_warnings = compute_confidence_score(
                    sweep_usable, in_zone_direct, structure_source,
                    in_zone, True, regime_shifted, is_active_session(now_utc),
                    confirmation_passed, bias_stale=bias_stale,
                )
                if score >= SCORE_TIER_ACCEPTABLE:
                    trade_signal  = "SELL"
                    entry         = c_last["Close"]
                    sl_buffer     = max(SL_ATR_MULT * current_atr, SL_MIN_PIPS * PIP_SIZE)
                    sl            = highest_wick + sl_buffer
                    risk          = sl - entry
                    tp            = entry - (RR_RATIO * risk)
                    risk_pips     = risk / PIP_SIZE
                    reward_pips   = (RR_RATIO * risk) / PIP_SIZE
                    pattern_check = "PASS — {} {}/100 ({})".format(score_emoji, score, score_tier) + (
                        " (post-sweep entry)" if sweep_usable and not in_zone_direct else "")
                    stats["pattern_passed"] += 1
                else:
                    pattern_check = "PASS mechanically but IGNORED — {} {}/100 < {} floor ({})".format(
                        score_emoji, score, SCORE_TIER_ACCEPTABLE,
                        ", ".join(f"{k}:{v}" for k, v in score_breakdown.items()))
        else:
            score = score_breakdown = score_tier = score_emoji = None
            score_warnings = []
            fails = []
            if not in_zone:
                if sweep_valid and not sweep_distance_ok:
                    fails.append("sweep confirmed but price drifted too far from zone")
                else:
                    fails.append("price not in premium zone (no direct touch or sweep)")
            if not bull_prev:  fails.append("prev candle not bullish")
            if not bear_last:  fails.append("last candle not bearish")
            if not engulfs:    fails.append("doesn't engulf prev body")
            if not real_body:  fails.append("body too small vs ATR")
            pattern_check = "FAIL (" + ", ".join(fails) + ")"

    # ── Print checklist ───────────────────────────────────────────────────
    decision = ("🚨 SIGNAL — " + trade_signal) if trade_signal != "HOLD" \
               else "NO TRADE — pattern conditions not met"
    if regime_shifted and trade_signal != "HOLD":
        decision = "⚡ SIGNAL SUPPRESSED — volatility regime shift / post-spike cooldown"
    elif fib_stale and trade_signal != "HOLD":
        decision = "🗑 SIGNAL SUPPRESSED — Fib zone stale after spike"
    elif momentum_overshoot and trade_signal != "HOLD":
        decision = "💥 SIGNAL SUPPRESSED — momentum candle overshot the pocket"
    print(_checklist(macro_bias, bos_check, bos_bias_check, range_check,
                     fib_check, atr_valid_check, pattern_check, decision,
                     bias_stale=bias_stale))
    if state.get("macro_swing_high") is not None and state.get("macro_swing_low") is not None:
        print(
            "  [Detail] 1H macro swing: H {:.5f} / L {:.5f} (confirmed {})".format(
                state["macro_swing_high"], state["macro_swing_low"],
                state.get("macro_swing_confirmed_at", "unknown")
            )
        )
    print(
        "  [Detail] Structure: " + structure_source +
        " | Price: {:.5f}".format(c_last["Close"]) +
        " | Fib: {:.5f}".format(fib_zone) +
        " | ATR short: {:.1f}p / long: {:.1f}p (ratio {:.2f}x)".format(
            short_atr_pips if regime_shifted else current_atr_pips,
            current_atr_pips,
            regime_ratio if regime_shifted else 1.0
        ) +
        " | SwH: {:.5f}".format(swing_high) +
        " SwL: {:.5f}".format(swing_low)
    )
    if regime_note:
        print(regime_note)
    if momentum_note:
        print(momentum_note)

    # ── WATCHING state — invalidation check ───────────────────────────────
    # Run this before the alert logic so we know the current watching status
    # is still valid before deciding whether to set, keep, or clear it.
    is_watching      = state.get("watching", False)
    watching_zone_p  = state.get("watching_zone")
    watching_bias_s  = state.get("watching_bias")

    if is_watching:
        invalidate    = False
        inv_reason    = ""

        # V2: no clock-based TTL. A WATCHING setup lives until a market
        # event kills it — either of the two guards below — never because
        # a timer ran out. (V1's "Guard 1" TTL lived here; removed.)

        # Guard 1 (was Guard 2): 15M close through the zone — the zone structurally failed,
        # not just a wick. Same logic as the sweep confirmation check, inverted:
        # if the 15M candle CLOSED on the wrong side, the zone is gone.
        if not invalidate and watching_zone_p and watching_bias_s:
            wz             = float(watching_zone_p)
            last_15m_close = df_15m.iloc[-1]["Close"]
            if watching_bias_s == "BULLISH" and last_15m_close < wz:
                invalidate = True
                inv_reason = "15M closed below zone ({:.5f})".format(last_15m_close)
            elif watching_bias_s == "BEARISH" and last_15m_close > wz:
                invalidate = True
                inv_reason = "15M closed above zone ({:.5f})".format(last_15m_close)

        # Guard 2: Zone exit — price has run away from the zone, in the
        # direction of the original leg, by more than WATCHING_EXIT_PIPS
        # without the pattern ever confirming. The pullback window has
        # passed - this is a dead setup, not a live one, even though
        # nothing structurally "broke" (Guard 1 wouldn't catch this since
        # price never closed through the zone the wrong way).
        if not invalidate and watching_zone_p and watching_bias_s:
            wz            = float(watching_zone_p)
            current_close = df_5m.iloc[-1]["Close"]
            if watching_bias_s == "BULLISH":
                drift_pips = (current_close - wz) / PIP_SIZE
                if drift_pips > WATCHING_EXIT_PIPS:
                    invalidate = True
                    inv_reason = "price ran {:.1f} pips above zone — pullback missed".format(drift_pips)
            else:  # BEARISH
                drift_pips = (wz - current_close) / PIP_SIZE
                if drift_pips > WATCHING_EXIT_PIPS:
                    invalidate = True
                    inv_reason = "price ran {:.1f} pips below zone — pullback missed".format(drift_pips)

        # Guard 3: Momentum overshoot — the candle that just printed blew
        # clean through the whole pocket instead of price gradually working
        # into it. Same signal as the suppression at signal time, applied
        # here too: an already-WATCHING setup doesn't survive its own zone
        # getting run over by a displacement candle.
        if not invalidate and momentum_overshoot:
            invalidate = True
            inv_reason = momentum_reason

        if invalidate:
            print("  [WATCHING] Cleared — " + inv_reason)
            state["watching"] = False
            state.pop("watching_zone",   None)
            state.pop("watching_bias",   None)
            state.pop("watching_set_at", None)
            is_watching = False
            save_state(state)

    # ── Signal / WATCHING alert logic ─────────────────────────────────────
    if trade_signal != "HOLD" and entry is not None:
        if regime_shifted or fib_stale or momentum_overshoot:
            # Pattern fired but something structurally undermines this
            # entry — volatility regime shift, a stale post-spike Fib zone,
            # or (new) a single 15M candle that overshot the whole pocket
            # instead of price gradually working into it. Any of these on
            # their own is reason enough to log the setup for research but
            # withhold the Telegram alert rather than act on it.
            if momentum_overshoot and not (regime_shifted or fib_stale):
                print(
                    f"  [MOMENTUM OVERSHOOT] Signal {trade_signal} @ {entry:.5f} "
                    f"suppressed — {momentum_reason}. Logged to journal context only."
                )
            else:
                print(
                    f"  [REGIME SHIFT] Signal {trade_signal} @ {entry:.5f} "
                    f"suppressed — short/long ATR ratio {regime_ratio:.2f}× "
                    f"(threshold {REGIME_SHIFT_THRESHOLD}×). "
                    f"Logged to journal context only."
                )
            # Still save signal context so /last works for research review
            stats["last_journal_signal"]    = trade_signal + " (suppressed)"
            stats["last_journal_entry"]     = f"{entry:.5f}"
            stats["last_journal_structure"] = structure_source
            stats["last_journal_score"]     = score
            stats["last_journal_score_breakdown"] = score_breakdown
            stats["last_journal_score_warnings"]  = score_warnings
            stats["last_journal_time"]      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            # New signal identity — any pending flip-confirmation from a
            # previous signal is now stale and must not carry over.
            stats.pop("pending_confirm", None)

        elif is_duplicate_signal(state, trade_signal, swing_high, swing_low):
            print(
                "  [COOLDOWN] Signal suppressed — same direction, same dealing "
                "range (SwH {:.5f} / SwL {:.5f}) as the last signal. Needs a new "
                "swing/leg, not just time passing, to fire again.".format(swing_high, swing_low)
            )
        else:
            was_watching = is_watching
            # Clear watching state — setup resolved either way
            state["watching"] = False
            state.pop("watching_zone",   None)
            state.pop("watching_bias",   None)
            state.pop("watching_set_at", None)

            stats["signals_sent"] += 1
            if was_watching:
                stats["watching_confirmed"] = stats.get("watching_confirmed", 0) + 1

            # Save signal context for journal enrichment on /win or /loss
            stats["last_journal_signal"]    = trade_signal
            stats["last_journal_entry"]     = f"{entry:.5f}"
            stats["last_journal_structure"] = structure_source
            stats["last_journal_score"]     = score
            stats["last_journal_score_breakdown"] = score_breakdown
            stats["last_journal_score_warnings"]  = score_warnings
            stats["last_journal_time"]      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            # Freeze this signal as the active trade. From here until it
            # closes (SL/TP hit, or manual /win /loss), scan() short-circuits
            # at manage_active_trade() before any bias/zone/scoring logic —
            # this score is never recomputed, and no WATCHING/new-signal
            # message can fire for this pair while it's open.
            stats["active_trade"] = {
                "direction":            trade_signal,
                "entry":                entry,
                "sl":                   sl,
                "tp":                   tp,
                "score":                score,
                "score_tier":           score_tier,
                "score_breakdown":      score_breakdown,
                "structure_source":     structure_source,
                "opened_at":            datetime.now(timezone.utc).isoformat(),
                "opened_at_display":    stats["last_journal_time"],
                "last_update_sent_at":  None,
            }

            # New signal identity — any pending flip-confirmation from a
            # previous signal is now stale and must not carry over.
            stats.pop("pending_confirm", None)
            direction_emoji = "📈" if trade_signal == "BUY" else "📉"
            zone_tag        = " _(liquidity sweep)_" if sweep_usable and not in_zone_direct else ""
            confirm_tag     = "\n✅ _Zone was pre-flagged — entry confirmed._" if was_watching else ""
            reconfirm_tag   = (
                "\n🔁 _Bias reconfirmed via 15M BOS — 1H structure was pending._"
                if bias_reconfirmed_15m else ""
            )
            score_lines     = "\n".join(
                f"     {k.replace('_', ' ').title()}: {v}" for k, v in score_breakdown.items()
            )
            warning_block = (
                "\n⚠️ " + " / ".join(score_warnings) + "\n" if score_warnings else ""
            )

            msg = (
                "🚨 *SMC SIGNAL — GBPUSD* 🚨\n\n"
                + direction_emoji + " *Action:* `" + trade_signal + "`\n"
                f"{score_emoji} *Confidence:* `{score}/100 — {score_tier}`\n"
                "📊 *Bias:* `" + macro_bias + "` (1H structure)\n"
                "🏗 *Structure:* `" + structure_source + "`\n"
                "🎯 *Fib Zone* _(adaptive {:.1f}%):_ `{:.5f}`{}\n".format(fib_ratio * 100, fib_zone, zone_tag) +
                "📐 *SwH:* `{:.5f}` | *SwL:* `{:.5f}`\n".format(swing_high, swing_low) +
                "⚡ *5M ATR:* `{:.1f} pips`\n".format(current_atr / PIP_SIZE) +
                "─────────────────────\n"
                "📍 *Entry:*  `{:.5f}`\n".format(entry) +
                "🛡 *Stop:*   `{:.5f}` _({:.1f} pips)_\n".format(sl, risk_pips) +
                "🏆 *Target:* `{:.5f}` _({:.1f} pips)_\n".format(tp, reward_pips) +
                "⚖️ *RR:*     `1:" + str(RR_RATIO) + "`\n"
                "─────────────────────\n"
                "*Score breakdown:*\n" + score_lines + "\n"
                + warning_block +
                "─────────────────────\n"
                "⚠️ _Confirm higher-TF context before executing._"
                + confirm_tag
                + reconfirm_tag
            )
            send_telegram(msg)
            # Save signal to state for the structural cooldown check on next scan
            state["last_signal_direction"] = trade_signal
            state["last_signal_swing_high"] = swing_high
            state["last_signal_swing_low"]  = swing_low
            state["last_signal_price"]     = entry
            state["last_signal_time"]      = datetime.now(timezone.utc).isoformat()
            save_state(state)

    elif in_zone and trade_signal == "HOLD":
        # CASE B: Price is in the zone but pattern hasn't confirmed yet.
        if momentum_overshoot:
            # Don't set WATCHING off a displacement candle — this "zone
            # entry" was a blow-through, not a gradual approach, so there's
            # nothing here worth watching for a confirmation candle on.
            print("  [WATCHING] Suppressed — " + momentum_reason)
        elif not is_watching:
            # First time price entered this zone — set WATCHING and alert.
            state["watching"]      = True
            state["watching_zone"] = fib_zone
            state["watching_bias"] = macro_bias
            state["watching_set_at"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
            stats["watching_alerts"] += 1

            direction_word = "discount" if macro_bias == "BULLISH" else "premium"
            # Live partial score — everything except the confirmation candle,
            # which by definition hasn't happened yet. Gives a read on how
            # strong the setup already is while still waiting.
            live_score, live_breakdown, live_tier, live_emoji, _live_warnings = compute_confidence_score(
                sweep_usable, in_zone_direct, structure_source,
                in_zone, True, regime_shifted, is_active_session(now_utc),
                confirmation_passed=False, bias_stale=bias_stale,
            )
            watch_msg = (
                "👀 *SMC WATCHING — GBPUSD*\n\n"
                "📊 *Bias:* `" + macro_bias + "` | *Structure:* `" + structure_source + "`\n"
                "🎯 *Price entered " + direction_word + " zone:* `{:.5f}`\n".format(fib_zone) +
                "📐 *SwH:* `{:.5f}` | *SwL:* `{:.5f}`\n".format(swing_high, swing_low) +
                "⚡ *ATR:* `{:.1f} pips`\n".format(current_atr / PIP_SIZE) +
                f"{live_emoji} *Current score:* `{live_score}/100 ex-confirmation ({live_tier})`\n"
                "─────────────────────\n"
                "⏳ _Waiting for engulf confirmation..._\n"
                "_(Clears only on 15M close-through the zone, or price running "
                + str(WATCHING_EXIT_PIPS) + "p away without confirming — no timer.)_"
                + ("\n🔁 _Bias reconfirmed via 15M BOS — 1H structure was pending._" if bias_reconfirmed_15m else "")
            )
            send_telegram(watch_msg)
            print("  [WATCHING] Set — price entered zone at {:.5f}".format(fib_zone))
        else:
            print("  [WATCHING] Active — price still in zone, no confirmation yet.")

    # ── Save stats and send periodic summary ─────────────────────────────
    save_stats(stats)

    if (STATS_SUMMARY_EVERY > 0 and
            stats["total_scans"] % STATS_SUMMARY_EVERY == 0):
        send_telegram(format_stats_summary(stats))
        print("  [STATS] Periodic summary sent to Telegram.")

    # Always print current funnel totals to the Actions log
    wins   = stats.get("wins",   0)
    losses = stats.get("losses", 0)
    total_results = wins + losses
    wr_str = f"{wins/total_results*100:.0f}%" if total_results > 0 else "—"
    print(
        "  [STATS] Scans: {total_scans} | ATR skip: {atr_too_low} | "
        "Regime shift: {regime_shift_skip} | "
        "Consolidation: {consolidation_skip} | BOS conflict: {bos_conflict} | "
        "No structure: {no_structure} | Fib reached: {fib_reached} | "
        "Watching: {watching_alerts} | Confirmed: {watching_confirmed} | "
        "Pattern passed: {pattern_passed} | Signals: {signals_sent} | "
        "W/L: {wins}W/{losses}L ({wr})".format(**stats, wr=wr_str)
    )


if __name__ == "__main__":
    scan()
 
