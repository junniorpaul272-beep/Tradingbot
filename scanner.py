"""
GBPUSD SMC Scanner - GitHub Actions Edition
============================================
- Data: Twelve Data API (800 calls/day free)
- Runner: GitHub Actions (free, never goes offline)
- Alerts: Telegram
- Each run recomputes everything fresh from market data, with one
  exception: a short, time-bounded memory of the last confirmed
  dominant leg (state.json, capped at STATE_MAX_AGE_HOURS old) so a
  run that can't redetect a leg in its short lookback window isn't
  forced to fall all the way back to weaker logic.
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

# Adaptive entry zone
FIB_ZONE_NEAR = 0.382
FIB_ZONE_FAR = 0.618

# Data sanity
DATA_SPIKE_ATR_MULT = 8

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

# WATCHING state — two-stage alert system
WATCHING_TTL_MINUTES  = 15   # auto-clear if no confirmation within this window (= one 15M bar)
WATCHING_EXIT_PIPS    = 8    # auto-clear if price runs this many pips away from the zone,
                              # in the direction of the original leg, without confirming.
                              # e.g. bullish watching at 1.32300 - if price rallies to
                              # 1.32380+ without ever printing the reversal candle, the
                              # pullback was missed and chasing from here is a worse trade.
                              # Distinct from the 15M close-through guard below, which
                              # catches the zone failing in the OPPOSITE direction.

# Signal cooldown — suppress duplicate entry alerts
SIGNAL_COOLDOWN_PIPS  = 5
SIGNAL_COOLDOWN_HOURS = 0.5  # 30 minutes

# ATR minimum gate — hard floor on 5M ATR before any signal is allowed.
# At 4 pip ATR, broker spread (1.5-2.5p) consumes 37-62% of the average
# bar range, making the effective RR after spread far worse than the
# nominal 1:3. This gate refuses to trade when volatility is so thin
# that the spread itself is a structural headwind.
# 6 pips = approximately 2× typical GBPUSD spread during London session.
# Set to 0 to disable.
ATR_MIN_PIPS = 6

# WATCHING TTL — adaptive bounds
# Base TTL scales with current ATR: thin markets get more time because
# reversal candles form more slowly when bars are small. Fast markets
# get less time because if the zone rejects and runs, you've already
# missed it by the time a slow TTL expires.
WATCHING_TTL_MIN_MINUTES = 10   # floor: fastest possible expiry (high ATR)
WATCHING_TTL_MAX_MINUTES = 25   # ceiling: slowest possible expiry (low ATR)
WATCHING_TTL_ATR_PIVOT   = 8    # pips — ATR at which TTL = midpoint of range

# STATE_MEMORY staleness guard — if current price is more than this many
# pips away from the midpoint of the saved leg's SwH/SwL range, treat the
# memory as stale and fall through to plain fractal detection instead.
# Rationale: a leg saved 5H ago at completely different price levels
# produces a geometrically valid but contextually meaningless Fib zone.
STATE_MEMORY_MAX_DRIFT_PIPS = 80

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
    "atr_too_low":         0,   # NEW: scans skipped due to ATR below minimum
    "fib_reached":         0,
    "watching_alerts":     0,
    "watching_confirmed":  0,   # NEW: watching alerts that converted to signals
    "pattern_passed":      0,
    "signals_sent":        0,
    "wins":                0,   # NEW: manually logged wins
    "losses":              0,   # NEW: manually logged losses
    "first_scan":          None,
    "last_scan":           None,
    "last_update_id":      0,   # NEW: Telegram update offset for result tracking
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
            elif new_candidate["direction"] == dominant["direction"]:
                pass

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


def compute_macro_bias(df_1h, slope_bars=HTF_EMA_SLOPE_BARS,
                        flat_atr_mult=HTF_CONSOLIDATION_ATR_MULT,
                        flat_slope_atr_mult=HTF_EMA_FLAT_THRESHOLD):
    df_1h = df_1h.copy()
    df_1h["EMA_100"] = df_1h["Close"].ewm(span=100, adjust=False).mean()
    df_1h["ATR_1H"] = atr(df_1h, period=14)

    close_now = df_1h["Close"].iloc[-1]
    ema_now = df_1h["EMA_100"].iloc[-1]
    atr_now = df_1h["ATR_1H"].iloc[-1]

    if pd.isna(atr_now) or atr_now == 0 or len(df_1h) <= slope_bars:
        return "BULLISH" if close_now > ema_now else "BEARISH"

    ema_then = df_1h["EMA_100"].iloc[-1 - slope_bars]
    dist_in_atr = abs(close_now - ema_now) / atr_now
    slope_in_atr = abs(ema_now - ema_then) / atr_now

    if dist_in_atr < flat_atr_mult and slope_in_atr < flat_slope_atr_mult:
        return "CONSOLIDATION"

    return "BULLISH" if close_now > ema_now else "BEARISH"


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


def is_duplicate_signal(state, trade_signal, entry_price):
    """
    Returns True if this signal is too similar to the last one sent —
    same direction, within SIGNAL_COOLDOWN_PIPS, within SIGNAL_COOLDOWN_HOURS.
    Prevents the bot from spamming the same trade alert on consecutive scans
    when price hasn't meaningfully moved.
    """
    last_dir   = state.get("last_signal_direction")
    last_price = state.get("last_signal_price")
    last_time  = state.get("last_signal_time")

    if not (last_dir and last_price and last_time):
        return False
    if last_dir != trade_signal:
        return False

    try:
        sent_at   = datetime.fromisoformat(last_time)
        age_hours = (datetime.now(timezone.utc) - sent_at).total_seconds() / 3600
        if age_hours > SIGNAL_COOLDOWN_HOURS:
            return False
    except Exception:
        return False

    pip_diff = abs(entry_price - float(last_price)) / PIP_SIZE
    return pip_diff <= SIGNAL_COOLDOWN_PIPS


def adaptive_watching_ttl(current_atr_pips):
    """
    Returns the WATCHING TTL in minutes, scaled to current 5M ATR.

    Fast market (ATR > pivot): shorter TTL — if the zone rejects hard,
    the move runs fast and a long TTL just keeps a dead setup alive.

    Slow market (ATR < pivot): longer TTL — reversal candles take more
    time to form when bars are small; 15 min kills valid setups that
    are simply developing slowly in thin conditions.

    Clipped to [WATCHING_TTL_MIN_MINUTES, WATCHING_TTL_MAX_MINUTES].
    """
    pivot = WATCHING_TTL_ATR_PIVOT
    lo    = WATCHING_TTL_MIN_MINUTES
    hi    = WATCHING_TTL_MAX_MINUTES
    mid   = (lo + hi) / 2

    if current_atr_pips <= 0:
        return mid

    # Linear interpolation: ATR=0 → hi, ATR=2×pivot → lo
    t   = min(current_atr_pips / (2 * pivot), 1.0)
    ttl = hi - t * (hi - lo)
    return max(lo, min(hi, ttl))


def check_result_commands(stats):
    """
    Polls Telegram for result commands sent by the user.
    Supports optional notes for trade journaling.

    Commands:
      /win [optional note]    — log a win,  e.g. /win clean engulf at zone
      /loss [optional note]   — log a loss, e.g. /loss news spike stopped me
      /stats                  — get full funnel summary on demand
      /journal                — get the last 10 trade journal entries
      /last                   — show the last signal that was sent

    Notes are stored in stats["journal"] as a list of dicts so you can
    cross-reference with the GitHub Actions checklist logs later:
      {"time": "...", "result": "WIN", "note": "clean engulf at zone",
       "signal": "BUY", "entry": 1.32400, "structure": "BOS"}

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

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        wins    = stats.get("wins",   0)
        losses  = stats.get("losses", 0)

        if cmd in ("/win", "win", "/w"):
            stats["wins"] = wins + 1
            wins          = stats["wins"]
            total         = wins + losses
            wr            = f"{wins/total*100:.0f}%" if total > 0 else "—"

            # Journal entry — pulls last signal context from state if available
            entry_ctx = _last_signal_context(stats)
            journal_entry = {
                "time":      now_str,
                "result":    "WIN",
                "note":      note or "—",
                **entry_ctx,
            }
            stats["journal"].append(journal_entry)
            # Keep journal to last 100 entries so stats.json doesn't grow unbounded
            stats["journal"] = stats["journal"][-100:]

            note_line = f"\n📝 _{note}_" if note else ""
            send_telegram(
                f"✅ *WIN logged*{note_line}\n"
                f"Running record: `{wins}W / {losses}L` ({wr} win rate)\n"
                f"_Send /journal to see recent entries._"
            )
            print(f"  [RESULT] WIN logged. Note: '{note}'. Running: {wins}W / {losses}L")

        elif cmd in ("/loss", "loss", "/l"):
            stats["losses"] = losses + 1
            losses          = stats["losses"]
            total           = wins + losses
            wr              = f"{wins/total*100:.0f}%" if total > 0 else "—"

            entry_ctx = _last_signal_context(stats)
            journal_entry = {
                "time":   now_str,
                "result": "LOSS",
                "note":   note or "—",
                **entry_ctx,
            }
            stats["journal"].append(journal_entry)
            stats["journal"] = stats["journal"][-100:]

            note_line = f"\n📝 _{note}_" if note else ""
            send_telegram(
                f"❌ *LOSS logged*{note_line}\n"
                f"Running record: `{wins}W / {losses}L` ({wr} win rate)\n"
                f"_Send /journal to see recent entries._"
            )
            print(f"  [RESULT] LOSS logged. Note: '{note}'. Running: {wins}W / {losses}L")

        elif cmd in ("/stats", "stats"):
            send_telegram(format_stats_summary(stats))

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
                    note_t  = e.get("note", "—")
                    lines.append(
                        f"{icon} `{e['time']}`\n"
                        f"   {sig} @ {entry_p} | {struct}\n"
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


def _checklist(bias, bos_check, bos_bias_check, range_check, fib_check, atr_check, pattern_check, decision):
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
    if bias == "BULLISH":
        bias_line += " ✅"
    elif bias == "BEARISH":
        bias_line += " ✅"
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

    # ── Fetch data ───────────────────────────────────────────────────────
    df_5m  = fetch_ohlc("5min",  outputsize=100)
    df_15m = fetch_ohlc("15min", outputsize=SWING_LOOKBACK_15 + 10)
    df_1h  = fetch_ohlc("1h",    outputsize=HTF_BIAS_MIN_BARS + 20)

    if df_5m is None or df_15m is None or df_1h is None:
        print("Data fetch failed. Exiting.")
        save_stats(stats)
        return

    if not (data_looks_sane(df_5m, "5min") and
            data_looks_sane(df_15m, "15min") and
            data_looks_sane(df_1h, "1h")):
        print("Data sanity check failed. Skipping this run.")
        save_stats(stats)
        return

    if len(df_1h) < HTF_BIAS_MIN_BARS:
        print("Only " + str(len(df_1h)) + " 1H bars. Need " +
              str(HTF_BIAS_MIN_BARS) + ". Skipping.")
        save_stats(stats)
        return

    # ── 1H Bias ──────────────────────────────────────────────────────────
    state      = load_state()
    macro_bias = compute_macro_bias(df_1h)

    if macro_bias == "CONSOLIDATION":
        stats["consolidation_skip"] += 1
        save_stats(stats)
        print(_checklist(macro_bias, "N/A", "N/A", "N/A", "N/A", "N/A", "N/A",
                          "NO TRADE — 1H is consolidating (no directional edge)"))
        return

    # ── Result command check (non-blocking) ──────────────────────────────
    stats = check_result_commands(stats)

    # ── ATR ───────────────────────────────────────────────────────────────
    df_5m["ATR"] = atr(df_5m, period=14)
    current_atr      = df_5m["ATR"].iloc[-1]
    current_atr_pips = current_atr / PIP_SIZE if not pd.isna(current_atr) else 0

    atr_valid_check = "PASS" if (not pd.isna(current_atr) and current_atr != 0) else "FAIL"
    if pd.isna(current_atr) or current_atr == 0:
        save_stats(stats)
        print(_checklist(macro_bias, "N/A", "N/A", "N/A", "N/A", atr_valid_check, "N/A",
                          "NO TRADE — ATR invalid"))
        return

    # Hard minimum ATR gate — refuses to trade when spread eats too much
    # of the average bar range. At ATR < ATR_MIN_PIPS, nominal 1:3 RR
    # degrades significantly after spread and slippage.
    if ATR_MIN_PIPS > 0 and current_atr_pips < ATR_MIN_PIPS:
        stats["atr_too_low"] += 1
        save_stats(stats)
        print(_checklist(
            macro_bias, "N/A", "N/A", "N/A", "N/A",
            f"FAIL — ATR {current_atr_pips:.1f}p < min {ATR_MIN_PIPS}p",
            "N/A", "NO TRADE — ATR below minimum threshold"
        ))
        return

    # ── BOS / Structure ───────────────────────────────────────────────────
    lookback      = df_15m.tail(SWING_LOOKBACK_15)
    bos           = detect_bos_impulse(lookback, wing=FRACTAL_WING)
    bos_check     = "N/A"
    bos_bias_check = "N/A"

    if bos is not None:
        bos_check = bos["direction"] + (" OK" if bos["direction"] == macro_bias else " WARN")

        if bos["direction"] == macro_bias:
            bos_bias_check   = "PASS"
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
            stats["bos_conflict"] += 1
            bos_bias_check = "CONFLICT (using fallback structure)"
            swing_high, swing_low, structure_source = fallback_structure(
                lookback, macro_bias, state, wing=FRACTAL_WING)
    else:
        bos_bias_check = "N/A (no dominant leg found)"
        swing_high, swing_low, structure_source = fallback_structure(
            lookback, macro_bias, state, wing=FRACTAL_WING)

    # ── STATE_MEMORY drift guard ──────────────────────────────────────────
    # If we're using a remembered leg, verify current price hasn't drifted
    # so far from that leg's midpoint that the Fib zone it produces is
    # contextually meaningless. A leg from 5 hours ago at a completely
    # different price level should not be anchoring today's entries.
    if structure_source == "STATE_MEMORY" and STATE_MEMORY_MAX_DRIFT_PIPS > 0:
        current_price = df_5m["Close"].iloc[-1]
        leg_mid       = (swing_high + swing_low) / 2
        drift_pips    = abs(current_price - leg_mid) / PIP_SIZE
        if drift_pips > STATE_MEMORY_MAX_DRIFT_PIPS:
            print(f"  [STATE_MEMORY] Stale — price drifted {drift_pips:.0f}p from leg midpoint. "
                  f"Falling back to fractal detection.")
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
                          "NO TRADE — range too compressed"))
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

    # ── Execution Matrix ──────────────────────────────────────────────────
    c_last = df_5m.iloc[-1]
    c_prev = df_5m.iloc[-2]

    body_last     = abs(c_last["Close"] - c_last["Open"])
    atr_threshold = ATR_ENGULF_MIN * current_atr

    trade_signal  = "HOLD"
    entry = sl = tp = risk_pips = reward_pips = None
    pattern_check = "N/A"
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

        if in_zone:
            stats["fib_reached"] += 1

        if in_zone and bear_prev and bull_last and engulfs and real_body:
            trade_signal  = "BUY"
            entry         = c_last["Close"]
            sl_buffer     = max(SL_ATR_MULT * current_atr, SL_MIN_PIPS * PIP_SIZE)
            sl            = lowest_wick - sl_buffer
            risk          = entry - sl
            tp            = entry + (RR_RATIO * risk)
            risk_pips     = risk / PIP_SIZE
            reward_pips   = (RR_RATIO * risk) / PIP_SIZE
            pattern_check = "PASS" + (" (post-sweep entry)" if sweep_usable and not in_zone_direct else "")
            stats["pattern_passed"] += 1
        else:
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

        if in_zone:
            stats["fib_reached"] += 1

        if in_zone and bull_prev and bear_last and engulfs and real_body:
            trade_signal  = "SELL"
            entry         = c_last["Close"]
            sl_buffer     = max(SL_ATR_MULT * current_atr, SL_MIN_PIPS * PIP_SIZE)
            sl            = highest_wick + sl_buffer
            risk          = sl - entry
            tp            = entry - (RR_RATIO * risk)
            risk_pips     = risk / PIP_SIZE
            reward_pips   = (RR_RATIO * risk) / PIP_SIZE
            pattern_check = "PASS" + (" (post-sweep entry)" if sweep_usable and not in_zone_direct else "")
            stats["pattern_passed"] += 1
        else:
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
    print(_checklist(macro_bias, bos_check, bos_bias_check, range_check,
                     fib_check, atr_valid_check, pattern_check, decision))
    print(
        "  [Detail] Structure: " + structure_source +
        " | Price: {:.5f}".format(c_last["Close"]) +
        " | Fib: {:.5f}".format(fib_zone) +
        " | ATR: {:.1f}p".format(current_atr / PIP_SIZE) +
        " | SwH: {:.5f}".format(swing_high) +
        " SwL: {:.5f}".format(swing_low)
    )

    # ── WATCHING state — invalidation check ───────────────────────────────
    # Run this before the alert logic so we know the current watching status
    # is still valid before deciding whether to set, keep, or clear it.
    is_watching      = state.get("watching", False)
    watching_zone_p  = state.get("watching_zone")
    watching_bias_s  = state.get("watching_bias")
    watching_set_at  = state.get("watching_set_at")

    if is_watching:
        invalidate    = False
        inv_reason    = ""

        # Guard 1: Adaptive TTL — scales with current ATR so thin markets
        # get more time (slow candles need more bars to confirm) and fast
        # markets expire sooner (missed pullbacks run quickly).
        if watching_set_at:
            try:
                set_at      = datetime.fromisoformat(watching_set_at)
                age_min     = (datetime.now(timezone.utc) - set_at).total_seconds() / 60
                ttl_minutes = adaptive_watching_ttl(current_atr_pips)
                if age_min > ttl_minutes:
                    invalidate = True
                    inv_reason = "TTL expired ({:.0f}/{:.0f} min, ATR={:.1f}p)".format(
                        age_min, ttl_minutes, current_atr_pips)
            except Exception:
                invalidate = True
                inv_reason = "invalid watching timestamp"

        # Guard 2: 15M close through the zone — the zone structurally failed,
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

        # Guard 3: Zone exit — price has run away from the zone, in the
        # direction of the original leg, by more than WATCHING_EXIT_PIPS
        # without the pattern ever confirming. The pullback window has
        # passed - this is a dead setup, not a live one, even though
        # nothing structurally "broke" (Guard 2 wouldn't catch this since
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
        # CASE A: Pattern confirmed (same-scan or post-watching).
        # Same-scan priority: if zone touched AND pattern fires in one scan,
        # skip WATCHING entirely and go straight to Entry Confirmed.
        if is_duplicate_signal(state, trade_signal, entry):
            print(
                "  [COOLDOWN] Signal suppressed — same direction within "
                + str(SIGNAL_COOLDOWN_PIPS) + " pips / "
                + str(int(SIGNAL_COOLDOWN_HOURS * 60)) + " min of last alert."
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
            stats["last_journal_time"]      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            direction_emoji = "📈" if trade_signal == "BUY" else "📉"
            zone_tag        = " _(liquidity sweep)_" if sweep_usable and not in_zone_direct else ""
            confirm_tag     = "\n✅ _Zone was pre-flagged — entry confirmed._" if was_watching else ""

            msg = (
                "🚨 *SMC SIGNAL — GBPUSD* 🚨\n\n"
                + direction_emoji + " *Action:* `" + trade_signal + "`\n"
                "📊 *Bias:* `" + macro_bias + "` (1H EMA-100)\n"
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
                "⚠️ _Confirm higher-TF context before executing._"
                + confirm_tag
            )
            send_telegram(msg)
            # Save signal to state for cooldown check on next scan
            state["last_signal_direction"] = trade_signal
            state["last_signal_price"]     = entry
            state["last_signal_time"]      = datetime.now(timezone.utc).isoformat()
            save_state(state)

    elif in_zone and trade_signal == "HOLD":
        # CASE B: Price is in the zone but pattern hasn't confirmed yet.
        if not is_watching:
            # First time price entered this zone — set WATCHING and alert.
            state["watching"]      = True
            state["watching_zone"] = fib_zone
            state["watching_bias"] = macro_bias
            state["watching_set_at"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
            stats["watching_alerts"] += 1

            direction_word = "discount" if macro_bias == "BULLISH" else "premium"
            watch_msg = (
                "👀 *SMC WATCHING — GBPUSD*\n\n"
                "📊 *Bias:* `" + macro_bias + "` | *Structure:* `" + structure_source + "`\n"
                "🎯 *Price entered " + direction_word + " zone:* `{:.5f}`\n".format(fib_zone) +
                "📐 *SwH:* `{:.5f}` | *SwL:* `{:.5f}`\n".format(swing_high, swing_low) +
                "⚡ *ATR:* `{:.1f} pips`\n".format(current_atr / PIP_SIZE) +
                "─────────────────────\n"
                "⏳ _Waiting for engulf confirmation..._\n"
                "_(Auto-clears in " + str(WATCHING_TTL_MINUTES) + " min or on 15M close-through)_"
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
        "Consolidation: {consolidation_skip} | BOS conflict: {bos_conflict} | "
        "No structure: {no_structure} | Fib reached: {fib_reached} | "
        "Watching: {watching_alerts} | Confirmed: {watching_confirmed} | "
        "Pattern passed: {pattern_passed} | Signals: {signals_sent} | "
        "W/L: {wins}W/{losses}L ({wr})".format(**stats, wr=wr_str)
    )


if __name__ == "__main__":
    scan()
