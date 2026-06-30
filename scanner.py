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
# How big the 5M candle body must be vs current ATR to count as a "real"
# engulfing candle rather than noise. Lowered from 0.5 - that was filtering
# out almost everything on a pair that often makes valid moves on smaller
# bodies. If you're still seeing zero signals over a few days, try 0.3.
# If you start getting weak/marginal signals, push it back up toward 0.5.
ATR_ENGULF_MIN = 0.4

# Structure params
HTF_BIAS_MIN_BARS = 100
SWING_LOOKBACK_15 = 48
FRACTAL_WING = 2
INVALIDATION_RETRACE = 0.786  # depth at which the dominant leg is considered dead

# --- Consolidation detection (1H) ---
# Price-vs-EMA alone forces a BULLISH/BEARISH call even when the market
# is just chopping sideways around the EMA. These two checks have to BOTH
# be true for the bot to instead call it CONSOLIDATION and skip the run:
#   - price sits within HTF_CONSOLIDATION_ATR_MULT * (1H ATR) of the EMA
#   - the EMA itself has moved less than HTF_EMA_FLAT_THRESHOLD * (1H ATR)
#     over the last HTF_EMA_SLOPE_BARS bars (i.e. it's flat, not sloping)
HTF_EMA_SLOPE_BARS = 10
HTF_CONSOLIDATION_ATR_MULT = 0.5
HTF_EMA_FLAT_THRESHOLD = 0.15

# --- Adaptive entry zone ---
# Instead of one fixed pullback ratio, the entry zone now slides between
# a shallow ratio (catches continuation early) and a deep ratio (waits for
# a fuller retrace), based on whether 5M volatility is currently running
# hot or cold relative to its own recent average. See adaptive_fib_ratio().
FIB_ZONE_NEAR = 0.382  # shallow end - used when the market is moving fast
FIB_ZONE_FAR = 0.618   # deep end - used when the market is calm

# How many times bigger than its own 20-bar average range a single candle
# has to be before it's treated as a probable bad tick / API glitch and
# the run is skipped rather than traded on.
DATA_SPIKE_ATR_MULT = 8

# File used to remember "I'm currently watching a setup" between runs.
# GitHub Actions runs are stateless by default - each run starts fresh -
# so this file gets committed back to the repo at the end of every run
# (see the workflow yml) to carry that memory forward to the next run.
STATE_FILE = "state.json"

# Bounded memory: a saved leg older than this is treated as stale and
# ignored, so the bot never leans on structure from a much earlier
# session. It only bridges short gaps between runs, not "remember
# everything forever".
STATE_MAX_AGE_HOURS = 6


def load_state():
    """Reads the saved watch state. Returns a default 'nothing watched'
    state if the file doesn't exist yet, can't be read, or has expired
    past STATE_MAX_AGE_HOURS."""
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
    """Writes the watch state back to disk, stamped with the current
    time so load_state() can tell later whether it's gone stale."""
    state = dict(state)
    state["timestamp"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print("[STATE SAVE ERROR] " + str(e))


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
    """
    Fetches OHLC data from Twelve Data.
    interval: '5min' | '15min' | '1h'
    """
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

    # Drop last bar - it's the currently forming candle
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
    """
    Finds ALL confirmed fractal swing points in chronological order,
    not just the most recent. Each entry is tagged as 'high' or 'low'.
    """
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
    """
    Tracks the DOMINANT impulse leg, not just the latest external BOS.

    Every confirmed external break is a CANDIDATE leg. The dominant leg
    only changes when the current dominant leg is INVALIDATED:

      - Origin violation: price trades back through the dominant leg's
        origin point. The leg has been fully round-tripped - dead.
      - Retracement violation: price retraces beyond `invalidation_retrace`
        (default 78.6%) of the leg's range.

    Until invalidated, the dominant leg stays dominant even if a smaller,
    more recent break has technically formed in the opposite direction
    (internal structure) or the same direction (in which case the leg
    just extends - its origin never moves).

    Returns dict with: direction, impulse_start, impulse_end, or None
    if no leg has ever qualified.
    """
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

    dominant = None  # {"direction", "origin", "origin_idx", "extreme"}

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

        # Check invalidation of the current dominant leg first.
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
            else:  # BEARISH
                origin_violated = high >= dominant["origin"]
                leg_range = dominant["origin"] - dominant["extreme"]
                retrace_violated = (
                    leg_range > 0 and
                    (high - dominant["extreme"]) / leg_range >= invalidation_retrace
                )
                if origin_violated or retrace_violated:
                    dominant = None

        # New candidate external break.
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
                # Same-direction continuation - keep original origin.
                pass
            # else: opposite-direction candidate while still valid - ignored.

        # Ratchet the dominant leg's extreme forward.
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
    """
    LEGACY fallback - kept only for cases where no dominant leg can be
    confirmed (e.g. choppy/ranging conditions).
    """
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
    """
    Classifies the 1H market as BULLISH, BEARISH, or CONSOLIDATION instead
    of forcing a directional call every single run.

    Two distances are measured in units of 1H ATR (so the check scales
    with whatever volatility regime GBPUSD is currently in, rather than
    a fixed pip distance):
      - how far price currently sits from the EMA-100
      - how far the EMA itself has moved over the last `slope_bars` bars

    Both have to be small for the market to count as CONSOLIDATION -
    price merely poking to one side of a flat EMA isn't a trend.
    """
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
    """
    Slides the entry zone between `near` (shallow) and `far` (deep) based
    on whether 5M volatility is currently running hot or cold relative to
    its own recent average, instead of using one fixed ratio for every
    market condition.

    expansion = current_atr / (average 5M ATR over the last `lookback` bars)
      - expansion > 1: price is moving faster than usual right now (often
        the start of a fresh impulsive leg) -> lean toward the shallow
        ratio so a continuation isn't missed waiting for a deep retrace
        a fast market may never give.
      - expansion < 1: volatility is calm/below-average -> lean toward
        the deep ratio and wait for a fuller pullback, since there's
        less urgency.
    """
    avg_atr = df_5m["ATR"].rolling(lookback, min_periods=10).mean().iloc[-1]
    if pd.isna(avg_atr) or avg_atr == 0 or pd.isna(current_atr):
        return (near + far) / 2

    expansion = current_atr / avg_atr
    expansion = max(0.5, min(2.0, expansion))  # clip to a sane band
    t = (expansion - 0.5) / (2.0 - 0.5)  # 0 (calm) .. 1 (hot)
    return far - (t * (far - near))


def fallback_structure(lookback_df, macro_bias, state, wing=2):
    """
    Used whenever no fresh dominant leg can be confirmed inside the
    current (short) lookback window.

    First choice: the bounded memory carried over from a previous run
    (state.json). If it's recent enough (see STATE_MAX_AGE_HOURS) and
    matches the current bias, that's a real leg the bot already tracked -
    it's just fallen outside this run's short lookback slice, not gone.

    Only drops to the older plain-fractal-swing fallback if there's no
    usable memory either.

    Returns (swing_high, swing_low, structure_source_label).
    """
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


def data_looks_sane(df, label, max_spike_mult=DATA_SPIKE_ATR_MULT):
    """
    Lightweight guard against bad ticks / API glitches. This doesn't try
    to clean the data - it just refuses to trade on a candle that looks
    physically implausible (NaNs, non-positive prices, or a single bar's
    range many times larger than its recent neighbors). Note this can
    also occasionally flag a real, legitimate high-impact-news candle -
    that's an acceptable trade-off for a bot that should rather skip a
    run than act on garbage data.
    """
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
        print("[DATA WARNING] " + label + ": possible bad tick (abnormal candle range) in recent bars.")
        return False

    return True


def _checklist(bias, bos_check, bos_bias_check, range_check, fib_check, atr_check, pattern_check, decision):
    """
    Full filter checklist for every scan, win or lose.
    """
    lines = []
    lines.append("")
    if bias in ("BULLISH", "BEARISH"):
        bias_suffix = " OK"
    elif bias == "CONSOLIDATION":
        bias_suffix = " (flat)"
    else:
        bias_suffix = " X"
    lines.append("  1H Bias:       " + str(bias) + bias_suffix)
    lines.append("  BOS:           " + str(bos_check))
    lines.append("  BOS/Bias Sync: " + str(bos_bias_check))
    lines.append("  Range Filter:  " + str(range_check))
    lines.append("  Fib Zone:      " + str(fib_check))
    lines.append("  ATR Filter:    " + str(atr_check))
    lines.append("  Pattern Check: " + str(pattern_check))
    lines.append("  Decision:      " + str(decision))
    return "\n".join(lines)


# -----------------------------------------------
# MAIN SCAN
# -----------------------------------------------
def scan():
    now_utc = datetime.now(timezone.utc)
    print("\n[" + now_utc.strftime("%H:%M UTC") + "] Scan starting...")

    df_5m = fetch_ohlc("5min", outputsize=100)
    df_15m = fetch_ohlc("15min", outputsize=SWING_LOOKBACK_15 + 10)
    df_1h = fetch_ohlc("1h", outputsize=HTF_BIAS_MIN_BARS + 20)

    if df_5m is None or df_15m is None or df_1h is None:
        print("Data fetch failed. Exiting.")
        return

    if not (data_looks_sane(df_5m, "5min") and data_looks_sane(df_15m, "15min") and data_looks_sane(df_1h, "1h")):
        print("Data sanity check failed. Skipping this run rather than trading on it.")
        return

    if len(df_1h) < HTF_BIAS_MIN_BARS:
        print("Only " + str(len(df_1h)) + " 1H bars. Need " + str(HTF_BIAS_MIN_BARS) + ". Skipping.")
        return

    state = load_state()

    macro_bias = compute_macro_bias(df_1h)

    if macro_bias == "CONSOLIDATION":
        print(_checklist(macro_bias, "N/A", "N/A", "N/A", "N/A", "N/A", "N/A",
                          "NO TRADE - 1H is consolidating (no directional edge)"))
        return

    df_5m["ATR"] = atr(df_5m, period=14)
    current_atr = df_5m["ATR"].iloc[-1]

    atr_valid_check = "PASS" if (not pd.isna(current_atr) and current_atr != 0) else "FAIL"
    if pd.isna(current_atr) or current_atr == 0:
        print(_checklist(macro_bias, "N/A", "N/A", "N/A", "N/A", atr_valid_check, "N/A", "NO TRADE - ATR invalid"))
        return

    lookback = df_15m.tail(SWING_LOOKBACK_15)
    bos = detect_bos_impulse(lookback, wing=FRACTAL_WING)

    bos_check = "X N/A"
    bos_bias_check = "X N/A"

    if bos is not None:
        bos_check = bos["direction"] + (" OK" if bos["direction"] == macro_bias else " WARN")

        if bos["direction"] == macro_bias:
            bos_bias_check = "PASS"
            structure_source = "BOS"
            if bos["direction"] == "BULLISH":
                swing_low = bos["impulse_start"]
                swing_high = bos["impulse_end"]
            else:
                swing_high = bos["impulse_start"]
                swing_low = bos["impulse_end"]
            # Remember this leg so a future run that can't redetect it
            # within its short lookback window can still pick it up -
            # bounded by STATE_MAX_AGE_HOURS so it can't go stale forever.
            save_state({
                "status": "ACTIVE_LEG",
                "direction": bos["direction"],
                "impulse_start": bos["impulse_start"],
                "impulse_end": bos["impulse_end"],
            })
        else:
            bos_bias_check = "CONFLICT (using fallback structure)"
            swing_high, swing_low, structure_source = fallback_structure(lookback, macro_bias, state, wing=FRACTAL_WING)
    else:
        bos_bias_check = "N/A (no dominant leg found)"
        swing_high, swing_low, structure_source = fallback_structure(lookback, macro_bias, state, wing=FRACTAL_WING)

    structural_range = swing_high - swing_low

    range_check = "PASS" if structural_range >= (5 * PIP_SIZE) else "FAIL (range < 5 pips)"
    if structural_range < (5 * PIP_SIZE):
        print(_checklist(macro_bias, bos_check, bos_bias_check, range_check, "N/A", atr_valid_check, "N/A", "NO TRADE - range too compressed"))
        return

    fib_ratio = adaptive_fib_ratio(df_5m, current_atr)
    fib_zone = (
        swing_high - (fib_ratio * structural_range) if macro_bias == "BULLISH"
        else swing_low + (fib_ratio * structural_range)
    )
    fib_check = "{:.5f} (ratio {:.1f}%)".format(fib_zone, fib_ratio * 100)

    c_last = df_5m.iloc[-1]
    c_prev = df_5m.iloc[-2]

    body_last = abs(c_last["Close"] - c_last["Open"])
    atr_threshold = ATR_ENGULF_MIN * current_atr

    trade_signal = "HOLD"
    entry = sl = tp = risk_pips = reward_pips = None
    pattern_check = "N/A"

    if macro_bias == "BULLISH":
        lowest_wick = min(c_prev["Low"], c_last["Low"])
        in_zone = lowest_wick <= fib_zone
        bear_prev = c_prev["Close"] < c_prev["Open"]
        bull_last = c_last["Close"] > c_last["Open"]
        engulfs = c_last["Close"] >= c_prev["Open"] and c_last["Open"] <= c_prev["Close"]
        real_body = body_last > atr_threshold

        if in_zone and bear_prev and bull_last and engulfs and real_body:
            trade_signal = "BUY"
            entry = c_last["Close"]
            sl_buffer = max(SL_ATR_MULT * current_atr, SL_MIN_PIPS * PIP_SIZE)
            sl = lowest_wick - sl_buffer
            risk = entry - sl
            tp = entry + (RR_RATIO * risk)
            risk_pips = risk / PIP_SIZE
            reward_pips = (RR_RATIO * risk) / PIP_SIZE
            pattern_check = "PASS"
        else:
            fails = []
            if not in_zone:
                fails.append("price not in discount zone")
            if not bear_prev:
                fails.append("prev candle not bearish")
            if not bull_last:
                fails.append("last candle not bullish")
            if not engulfs:
                fails.append("doesn't engulf prev body")
            if not real_body:
                fails.append("body too small vs ATR")
            pattern_check = "FAIL (" + ", ".join(fails) + ")"

    elif macro_bias == "BEARISH":
        highest_wick = max(c_prev["High"], c_last["High"])
        in_zone = highest_wick >= fib_zone
        bull_prev = c_prev["Close"] > c_prev["Open"]
        bear_last = c_last["Close"] < c_last["Open"]
        engulfs = c_last["Open"] >= c_prev["Close"] and c_last["Close"] <= c_prev["Open"]
        real_body = body_last > atr_threshold

        if in_zone and bull_prev and bear_last and engulfs and real_body:
            trade_signal = "SELL"
            entry = c_last["Close"]
            sl_buffer = max(SL_ATR_MULT * current_atr, SL_MIN_PIPS * PIP_SIZE)
            sl = highest_wick + sl_buffer
            risk = sl - entry
            tp = entry - (RR_RATIO * risk)
            risk_pips = risk / PIP_SIZE
            reward_pips = (RR_RATIO * risk) / PIP_SIZE
            pattern_check = "PASS"
        else:
            fails = []
            if not in_zone:
                fails.append("price not in premium zone")
            if not bull_prev:
                fails.append("prev candle not bullish")
            if not bear_last:
                fails.append("last candle not bearish")
            if not engulfs:
                fails.append("doesn't engulf prev body")
            if not real_body:
                fails.append("body too small vs ATR")
            pattern_check = "FAIL (" + ", ".join(fails) + ")"

    decision = ("TRADE - " + trade_signal) if trade_signal != "HOLD" else "NO TRADE - pattern conditions not met"
    print(_checklist(macro_bias, bos_check, bos_bias_check, range_check, fib_check, atr_valid_check, pattern_check, decision))
    print(
        "  [Detail] Structure: " + structure_source +
        " | Price: {:.5f}".format(c_last["Close"]) +
        " | Fib: {:.5f}".format(fib_zone) +
        " | ATR: {:.1f}p".format(current_atr / PIP_SIZE) +
        " | SwH: {:.5f}".format(swing_high) +
        " SwL: {:.5f}".format(swing_low)
    )

    if trade_signal != "HOLD" and entry is not None:
        msg = (
            "SMC SIGNAL - GBPUSD\n\n"
            "Action: " + trade_signal + "\n"
            "Bias: " + macro_bias + " (1H EMA-100)\n"
            "Structure: " + structure_source + "\n"
            "Fib Zone (adaptive {:.1f}%): {:.5f}\n".format(fib_ratio * 100, fib_zone) +
            "SwH: {:.5f} | SwL: {:.5f}\n".format(swing_high, swing_low) +
            "5M ATR: {:.1f} pips\n".format(current_atr / PIP_SIZE) +
            "---------------------\n"
            "Entry:  {:.5f}\n".format(entry) +
            "Stop:   {:.5f} ({:.1f} pips)\n".format(sl, risk_pips) +
            "Target: {:.5f} ({:.1f} pips)\n".format(tp, reward_pips) +
            "RR:     1:" + str(RR_RATIO) + "\n"
            "---------------------\n"
            "Confirm higher-TF context before executing."
        )
        send_telegram(msg)


if __name__ == "__main__":
    scan()
