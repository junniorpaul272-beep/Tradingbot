"""
GBPUSD SMC Scanner — GitHub Actions Edition
============================================
- Data: Twelve Data API (800 calls/day free)
- Runner: GitHub Actions (free, never goes offline)
- Alerts: Telegram
- Stateless: each run is independent, no server needed
"""

import os
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# CREDENTIALS — pulled from GitHub Secrets (never hardcoded)
# ─────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_DATA_KEY   = os.environ["TWELVE_DATA_KEY"]

PAIR     = "GBP/USD"
PIP_SIZE = 0.0001

# Risk params
RR_RATIO       = 3.0
SL_ATR_MULT    = 1.5
SL_MIN_PIPS    = 5
ATR_ENGULF_MIN = 0.5

# Structure params
HTF_BIAS_MIN_BARS = 100
SWING_LOOKBACK_15 = 48
FRACTAL_WING      = 2


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "Markdown",
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print("Telegram alert sent.")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")


# ─────────────────────────────────────────────
# DATA — Twelve Data
# ─────────────────────────────────────────────
def fetch_ohlc(interval: str, outputsize: int = 200) -> pd.DataFrame | None:
    """
    Fetches OHLC data from Twelve Data.
    interval: '5min' | '15min' | '1h'
    """
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     PAIR,
        "interval":   interval,
        "outputsize": outputsize,
        "apikey":     TWELVE_DATA_KEY,
        "format":     "JSON",
    }
    try:
        resp = requests.get(url, params=params, timeout=15).json()
    except Exception as e:
        print(f"[FETCH ERROR] {interval}: {e}")
        return None

    if "values" not in resp:
        msg = resp.get("message") or resp.get("code") or "Unknown error"
        print(f"[API ERROR] {interval}: {msg}")
        return None

    df = pd.DataFrame(resp["values"])
    df.index = pd.to_datetime(df["datetime"], utc=True)
    df = df[["open", "high", "low", "close"]].rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close"
    }).astype(float).sort_index()

    # Drop last bar — it's the currently forming candle
    return df.iloc[:-1]


# ─────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def fractal_swings(df: pd.DataFrame, wing: int = 2) -> tuple[float, float]:
    """
    Most recent confirmed fractal swing high and low.
    Walks backwards so we always get the most recent structural pivots.
    """
    highs = df["High"].values
    lows  = df["Low"].values
    n     = len(highs)
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


# ─────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────
def scan() -> None:
    now_utc = datetime.now(timezone.utc)
    print(f"\n[{now_utc.strftime('%H:%M UTC')}] Scan starting...")

    # ── Fetch all three timeframes ──────────────────────────────────────────
    df_5m  = fetch_ohlc("5min",  outputsize=100)
    df_15m = fetch_ohlc("15min", outputsize=SWING_LOOKBACK_15 + 10)
    df_1h  = fetch_ohlc("1h",    outputsize=HTF_BIAS_MIN_BARS + 20)

    if df_5m is None or df_15m is None or df_1h is None:
        print("Data fetch failed. Exiting.")
        return

    # ── EMA Bias Guard ──────────────────────────────────────────────────────
    if len(df_1h) < HTF_BIAS_MIN_BARS:
        print(f"Only {len(df_1h)} 1H bars. Need {HTF_BIAS_MIN_BARS}. Skipping.")
        return

    df_1h["EMA_100"] = df_1h["Close"].ewm(span=100, adjust=False).mean()
    macro_bias = "BULLISH" if df_1h["Close"].iloc[-1] > df_1h["EMA_100"].iloc[-1] else "BEARISH"

    # ── Fractal Structure ───────────────────────────────────────────────────
    lookback = df_15m.tail(SWING_LOOKBACK_15)
    swing_high, swing_low = fractal_swings(lookback, wing=FRACTAL_WING)
    structural_range = swing_high - swing_low

    if structural_range < (5 * PIP_SIZE):
        print("Range too compressed. No setup.")
        return

    # ── Fibonacci Zone ──────────────────────────────────────────────────────
    fib_zone = (
        swing_high - (0.618 * structural_range) if macro_bias == "BULLISH"
        else swing_low + (0.618 * structural_range)
    )

    # ── ATR ─────────────────────────────────────────────────────────────────
    df_5m["ATR"] = atr(df_5m, period=14)
    current_atr  = df_5m["ATR"].iloc[-1]

    if pd.isna(current_atr) or current_atr == 0:
        print("ATR invalid. Skipping.")
        return

    # ── Execution Matrix ─────────────────────────────────────────────────────
    # -1 = last closed, -2 = candle before that
    c_last = df_5m.iloc[-1]
    c_prev = df_5m.iloc[-2]

    body_prev     = abs(c_prev["Close"] - c_prev["Open"])
    body_last     = abs(c_last["Close"] - c_last["Open"])
    atr_threshold = ATR_ENGULF_MIN * current_atr

    trade_signal = "HOLD"
    entry = sl = tp = risk_pips = reward_pips = None

    if macro_bias == "BULLISH":
        lowest_wick  = min(c_prev["Low"], c_last["Low"])
        in_zone      = lowest_wick <= fib_zone
        bear_prev    = c_prev["Close"] < c_prev["Open"]
        bull_last    = c_last["Close"] > c_last["Open"]
        engulfs      = body_last > body_prev
        real_body    = body_last > atr_threshold

        if in_zone and bear_prev and bull_last and engulfs and real_body:
            trade_signal = "BUY"
            entry        = c_last["Close"]
            sl_buffer    = max(SL_ATR_MULT * current_atr, SL_MIN_PIPS * PIP_SIZE)
            sl           = lowest_wick - sl_buffer
            risk         = entry - sl
            tp           = entry + (RR_RATIO * risk)
            risk_pips    = risk / PIP_SIZE
            reward_pips  = (RR_RATIO * risk) / PIP_SIZE

    elif macro_bias == "BEARISH":
        highest_wick = max(c_prev["High"], c_last["High"])
        in_zone      = highest_wick >= fib_zone
        bull_prev    = c_prev["Close"] > c_prev["Open"]
        bear_last    = c_last["Close"] < c_last["Open"]
        engulfs      = body_last > body_prev
        real_body    = body_last > atr_threshold

        if in_zone and bull_prev and bear_last and engulfs and real_body:
            trade_signal = "SELL"
            entry        = c_last["Close"]
            sl_buffer    = max(SL_ATR_MULT * current_atr, SL_MIN_PIPS * PIP_SIZE)
            sl           = highest_wick + sl_buffer
            risk         = sl - entry
            tp           = entry - (RR_RATIO * risk)
            risk_pips    = risk / PIP_SIZE
            reward_pips  = (RR_RATIO * risk) / PIP_SIZE

    # ── Log ─────────────────────────────────────────────────────────────────
    print(
        f"Bias: {macro_bias} | Price: {c_last['Close']:.5f} | "
        f"Fib: {fib_zone:.5f} | ATR: {current_atr/PIP_SIZE:.1f}p | "
        f"SwH: {swing_high:.5f} SwL: {swing_low:.5f} | Signal: {trade_signal}"
    )

    # ── Alert ────────────────────────────────────────────────────────────────
    if trade_signal != "HOLD" and entry is not None:
        msg = (
            f"🚨 *SMC SIGNAL — GBPUSD* 🚨\n\n"
            f"*Action:* `{trade_signal}`\n"
            f"*Bias:* `{macro_bias}` (1H EMA-100)\n"
            f"*Fib 61.8% Zone:* `{fib_zone:.5f}`\n"
            f"*SwH:* `{swing_high:.5f}` | *SwL:* `{swing_low:.5f}`\n"
            f"*5M ATR:* `{current_atr/PIP_SIZE:.1f} pips`\n"
            f"─────────────────────\n"
            f"📍 *Entry:*  `{entry:.5f}`\n"
            f"🛡️ *Stop:*   `{sl:.5f}` ({risk_pips:.1f} pips)\n"
            f"🎯 *Target:* `{tp:.5f}` ({reward_pips:.1f} pips)\n"
            f"📊 *RR:*     `1:{RR_RATIO}`\n"
            f"─────────────────────\n"
            f"⚠️ _Confirm higher-TF context before executing._"
        )
        send_telegram(msg)


if __name__ == "__main__":
    scan()
