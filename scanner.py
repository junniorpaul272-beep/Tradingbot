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

# State memory
STATE_FILE = "state.json"
STATE_MAX_AGE_HOURS = 6


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
    print("\n[" + now_utc.strftime("%H:%M UTC") + "] Scan starting...")

    df_5m = fetch_ohlc("5min", outputsize=100)
    df_15m = fetch_ohlc("15min", outputsize=SWING_LOOKBACK_15 + 10)
    df_1h = fetch_ohlc("1h", outputsize=HTF_BIAS_MIN_BARS + 20)

    if df_5m is None or df_15m is None or df_1h is None:
        print("Data fetch failed. Exiting.")
        return

    if not (data_looks_sane(df_5m, "5min") and data_looks_sane(df_15m, "15min") and data_looks_sane(df_1h, "1h")):
        print("Data sanity check failed. Skipping this run.")
        return

    if len(df_1h) < HTF_BIAS_MIN_BARS:
        print("Only " + str(len(df_1h)) + " 1H bars. Need " + str(HTF_BIAS_MIN_BARS) + ". Skipping.")
        return

    state = load_state()
    macro_bias = compute_macro_bias(df_1h)

    if macro_bias == "CONSOLIDATION":
        print(_checklist(macro_bias, "N/A", "N/A", "N/A", "N/A", "N/A", "N/A",
                          "NO TRADE — 1H is consolidating (no directional edge)"))
        return

    df_5m["ATR"] = atr(df_5m, period=14)
    current_atr = df_5m["ATR"].iloc[-1]

    atr_valid_check = "PASS" if (not pd.isna(current_atr) and current_atr != 0) else "FAIL"
    if pd.isna(current_atr) or current_atr == 0:
        print(_checklist(macro_bias, "N/A", "N/A", "N/A", "N/A", atr_valid_check, "N/A",
                          "NO TRADE — ATR invalid"))
        return

    lookback = df_15m.tail(SWING_LOOKBACK_15)
    bos = detect_bos_impulse(lookback, wing=FRACTAL_WING)

    bos_check = "N/A"
    bos_bias_check = "N/A"

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
        print(_checklist(macro_bias, bos_check, bos_bias_check, range_check, "N/A", atr_valid_check, "N/A",
                          "NO TRADE — range too compressed"))
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
            if not in_zone:    fails.append("price not in discount zone")
            if not bear_prev:  fails.append("prev candle not bearish")
            if not bull_last:  fails.append("last candle not bullish")
            if not engulfs:    fails.append("doesn't engulf prev body")
            if not real_body:  fails.append("body too small vs ATR")
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
            if not in_zone:    fails.append("price not in premium zone")
            if not bull_prev:  fails.append("prev candle not bullish")
            if not bear_last:  fails.append("last candle not bearish")
            if not engulfs:    fails.append("doesn't engulf prev body")
            if not real_body:  fails.append("body too small vs ATR")
            pattern_check = "FAIL (" + ", ".join(fails) + ")"
            pattern_check = "FAIL (" + ", ".join(fails) + ")"

    decision = ("🚨 SIGNAL — " + trade_signal) if trade_signal != "HOLD" else "NO TRADE — pattern conditions not met"
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
        direction_emoji = "📈" if trade_signal == "BUY" else "📉"
        msg = (
            "🚨 *SMC SIGNAL — GBPUSD* 🚨\n\n"
            + direction_emoji + " *Action:* `" + trade_signal + "`\n"
            "📊 *Bias:* `" + macro_bias + "` (1H EMA-100)\n"
            "🏗 *Structure:* `" + structure_source + "`\n"
            "🎯 *Fib Zone* _(adaptive {:.1f}%):_ `{:.5f}`\n".format(fib_ratio * 100, fib_zone) +
            "📐 *SwH:* `{:.5f}` | *SwL:* `{:.5f}`\n".format(swing_high, swing_low) +
            "⚡ *5M ATR:* `{:.1f} pips`\n".format(current_atr / PIP_SIZE) +
            "─────────────────────\n"
            "📍 *Entry:*  `{:.5f}`\n".format(entry) +
            "🛡 *Stop:*   `{:.5f}` _({:.1f} pips)_\n".format(sl, risk_pips) +
            "🏆 *Target:* `{:.5f}` _({:.1f} pips)_\n".format(tp, reward_pips) +
            "⚖️ *RR:*     `1:" + str(RR_RATIO) + "`\n"
            "─────────────────────\n"
            "⚠️ _Confirm higher-TF context before executing._"
        )
        send_telegram(msg)


if __name__ == "__main__":
    scan()
