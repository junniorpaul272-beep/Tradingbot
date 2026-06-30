"""
GBPUSD SMC Scanner — GitHub Actions Edition
============================================
- Data: Twelve Data API (800 calls/day free)
- Runner: GitHub Actions (free, never goes offline)
- Alerts: Telegram
- Stateless: each run is independent, no server needed
"""



CHANGELOG (this version):
  - Structure detection now tracks the DOMINANT impulse leg (the one a
    discretionary SMC trader would actually draw a Fib on), not just the
    most recent external BOS. A leg stays dominant until it's invalidated
    by an origin violation or a deep retracement — a new same-direction
    break extends it, a smaller opposite-direction break inside it is
    ignored as internal structure. (Merged from the earlier dominant-leg
    state machine + the single-pass fractal ingestion from the BOS-only
    version.)
  - Fixed origin drift: same-direction continuations no longer reassign
    the impulse origin — only a genuine new dominant leg does.
  - Fixed engulfing check: was comparing body SIZE only (body_last >
    body_prev), which let candles that don't actually cover the prior
    candle's range pass as "engulfing." Now checks real open/close
    containment.
  - Every scan now prints a full checklist (bias, BOS, sync, range, ATR,
    pattern) regardless of outcome, so a NO TRADE always shows exactly
    which gate rejected it instead of a single opaque skip line.
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
HTF_BIAS_MIN_BARS   = 100
SWING_LOOKBACK_15    = 48
FRACTAL_WING         = 2
INVALIDATION_RETRACE = 0.786   # depth at which the dominant leg is considered dead


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


def find_all_fractals(df: pd.DataFrame, wing: int = 2) -> list[dict]:
    """
    Finds ALL confirmed fractal swing points in chronological order,
    not just the most recent. Each entry is tagged as 'high' or 'low'.
    This is the raw material BOS detection walks through — the full
    sequence of swings, not just whatever is most recent.
    """
    highs = df["High"].values
    lows  = df["Low"].values
    n     = len(highs)
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


def detect_bos_impulse(
    df: pd.DataFrame,
    wing: int = 2,
    invalidation_retrace: float = INVALIDATION_RETRACE,
) -> dict | None:
    """
    Tracks the DOMINANT impulse leg, not just "the latest external BOS."

    Every confirmed external break is a CANDIDATE leg. The dominant leg
    only changes when the current dominant leg is INVALIDATED:

      - Origin violation: price trades back through the dominant leg's
        origin point. The leg has been fully round-tripped — dead.
      - Retracement violation: price retraces beyond `invalidation_retrace`
        (default 78.6%) of the leg's range. A pullback into the 61.8%
        entry zone alone does NOT invalidate the leg — only a deeper
        retrace does.

    Until invalidated, the dominant leg stays dominant even if a smaller,
    more recent break has technically formed in the opposite direction
    (internal structure / a pullback) or even the same direction (in
    which case the leg just extends — its origin never moves).

    This is the A→B vs C→D distinction: C→D only takes over once A→B is
    invalidated by one of the two rules above, never just because D is
    the newest break. It's a heuristic approximation of discretionary
    "this still looks like the same leg" judgment, not a perfect replica
    of it.

    Returns dict with: direction, impulse_start, impulse_end (current
    extreme of the dominant leg), or None if no leg has ever qualified.
    """
    fractals = find_all_fractals(df, wing=wing)
    if len(fractals) < 2:
        return None

    closes = df["Close"].values
    highs  = df["High"].values
    lows   = df["Low"].values
    n      = len(df)

    # Running external extremes — used only to detect NEW candidate legs,
    # not to immediately promote them to dominant.
    external_high = None
    external_low  = None
    candidate_low_origin  = None
    candidate_high_origin = None

    # The currently DOMINANT leg, if any.
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
        high  = highs[i]
        low   = lows[i]

        # ── Check invalidation of the current dominant leg FIRST ────────
        # A leg must be invalidated before any new candidate can replace it.
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

        # ── New candidate external break — only adopt as dominant if ────
        # ── there currently is none; same-direction just extends it ─────
        new_candidate = None

        if external_high is not None and close > external_high and candidate_low_origin is not None:
            new_candidate = {
                "direction":  "BULLISH",
                "origin":     candidate_low_origin["price"],
                "origin_idx": candidate_low_origin["idx"],
                "extreme":    close,
            }
            external_high = close
            external_low  = None

        if external_low is not None and close < external_low and candidate_high_origin is not None:
            new_candidate = {
                "direction":  "BEARISH",
                "origin":     candidate_high_origin["price"],
                "origin_idx": candidate_high_origin["idx"],
                "extreme":    close,
            }
            external_low  = close
            external_high = None

        if new_candidate is not None:
            if dominant is None:
                # No dominant leg active (none ever formed, or it was
                # just invalidated this same bar) — this candidate
                # becomes dominant by default.
                dominant = new_candidate
            elif new_candidate["direction"] == dominant["direction"]:
                # Same-direction continuation — the dominant leg
                # extending further, not a new leg. Keep the ORIGINAL
                # origin; the extreme ratchets below regardless.
                pass
            # else: opposite-direction candidate while dominant leg is
            # still valid — internal structure (a pullback that hasn't
            # invalidated the dominant leg). Ignored.

        # Ratchet the dominant leg's extreme forward as price continues
        if dominant is not None:
            if dominant["direction"] == "BULLISH":
                dominant["extreme"] = max(dominant["extreme"], high)
            else:
                dominant["extreme"] = min(dominant["extreme"], low)

    if dominant is None:
        return None

    return {
        "direction":     dominant["direction"],
        "impulse_start": dominant["origin"],
        "impulse_end":   dominant["extreme"],
    }


def fractal_swings(df: pd.DataFrame, wing: int = 2) -> tuple[float, float]:
    """
    LEGACY fallback — kept only for cases where no dominant leg can be
    confirmed (e.g. choppy/ranging conditions). Most recent confirmed
    fractal swing high and low, found independently.
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


def _checklist(bias, bos_check, bos_bias_check, range_check, fib_check, atr_check, pattern_check, decision):
    """
    Full filter checklist for every scan, win or lose — so it's always
    clear exactly which gate passed or rejected the setup, rather than
    a single opaque skip message.
    """
    return (
        f"\n"
        f"  1H Bias:       {bias} {'✅' if bias in ('BULLISH','BEARISH') else '❌'}\n"
        f"  BOS:           {bos_check}\n"
        f"  BOS/Bias Sync: {bos_bias_check}\n"
        f"  Range Filter:  {range_check}\n"
        f"  Fib Zone:      {fib_check}\n"
        f"  ATR Filter:    {atr_check}\n"
        f"  Pattern Check: {pattern_check}\n"
        f"  Decision:      {decision}\n"
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

    # ── Structure: Dominant Impulse Leg (preferred) ─────────────────────────
    lookback = df_15m.tail(SWING_LOOKBACK_15)
    bos = detect_bos_impulse(lookback, wing=FRACTAL_WING)

    bos_check         = "❌ N/A"
    bos_bias_check    = "❌ N/A"
    structure_source  = "FALLBACK_FRACTAL"

    if bos is not None:
        bos_check = f"{bos['direction']} {'✅' if bos['direction'] == macro_bias else '⚠️'}"

        if bos["direction"] == macro_bias:
            # Dominant leg agrees with 1H bias — use it as structure.
            bos_bias_check    = "PASS ✅"
            structure_source  = "BOS"
            if bos["direction"] == "BULLISH":
                swing_low  = bos["impulse_start"]
                swing_high = bos["impulse_end"]
            else:
                swing_high = bos["impulse_start"]
                swing_low  = bos["impulse_end"]
        else:
            # Dominant leg disagrees with 1H bias — useful context
            # (counter-trend structure forming) but not a hard veto.
            # Fall back to fractal detection rather than refusing to
            # scan entirely.
            bos_bias_check = "CONFLICT ⚠️ (using fallback structure)"
            swing_high, swing_low = fractal_swings(lookback, wing=FRACTAL_WING)
    else:
        bos_bias_check = "N/A (no dominant leg found)"
        swing_high, swing_low = fractal_swings(lookback, wing=FRACTAL_WING)

    structural_range = swing_high - swing_low

    range_check = "PASS ✅" if structural_range >= (5 * PIP_SIZE) else "FAIL ❌ (range < 5 pips)"
    if structural_range < (5 * PIP_SIZE):
        print(_checklist(macro_bias, bos_check, bos_bias_check, range_check, "N/A", "N/A", "NO TRADE — range too compressed"))
        return

    # ── Fibonacci Zone ──────────────────────────────────────────────────────
    fib_zone = (
        swing_high - (0.618 * structural_range) if macro_bias == "BULLISH"
        else swing_low + (0.618 * structural_range)
    )

    # ── ATR ─────────────────────────────────────────────────────────────────
    df_5m["ATR"] = atr(df_5m, period=14)
    current_atr  = df_5m["ATR"].iloc[-1]

    atr_valid_check = "PASS ✅" if (not pd.isna(current_atr) and current_atr != 0) else "FAIL ❌"
    if pd.isna(current_atr) or current_atr == 0:
        print(_checklist(macro_bias, bos_check, bos_bias_check, range_check, atr_valid_check, "N/A", "NO TRADE — ATR invalid"))
        return

    # ── Execution Matrix ─────────────────────────────────────────────────────
    # -1 = last closed, -2 = candle before that
    c_last = df_5m.iloc[-1]
    c_prev = df_5m.iloc[-2]

    body_last     = abs(c_last["Close"] - c_last["Open"])
    atr_threshold = ATR_ENGULF_MIN * current_atr

    trade_signal = "HOLD"
    entry = sl = tp = risk_pips = reward_pips = None

    if macro_bias == "BULLISH":
        lowest_wick = min(c_prev["Low"], c_last["Low"])
        in_zone     = lowest_wick <= fib_zone
        bear_prev   = c_prev["Close"] < c_prev["Open"]
        bull_last   = c_last["Close"] > c_last["Open"]
        # Real bullish engulf: last candle's body fully contains prev's
        # open/close range, not just a bigger body somewhere on the chart.
        engulfs     = c_last["Close"] >= c_prev["Open"] and c_last["Open"] <= c_prev["Close"]
        real_body   = body_last > atr_threshold

        if in_zone and bear_prev and bull_last and engulfs and real_body:
            trade_signal = "BUY"
            entry        = c_last["Close"]
            sl_buffer    = max(SL_ATR_MULT * current_atr, SL_MIN_PIPS * PIP_SIZE)
            sl           = lowest_wick - sl_buffer
            risk         = entry - sl
            tp           = entry + (RR_RATIO * risk)
            risk_pips    = risk / PIP_SIZE
            reward_pips  = (RR_RATIO * risk) / PIP_SIZE
            pattern_check = "PASS ✅"
        else:
            fails = []
            if not in_zone:   fails.append("price not in discount zone")
            if not bear_prev: fails.append("prev candle not bearish")
            if not bull_last: fails.append("last candle not bullish")
            if not engulfs:   fails.append("doesn't engulf prev body")
            if not real_body: fails.append("body too small vs ATR")
            pattern_check = f"FAIL ❌ ({', '.join(fails)})"

    elif macro_bias == "BEARISH":
        highest_wick = max(c_prev["High"], c_last["High"])
        in_zone      = highest_wick >= fib_zone
        bull_prev    = c_prev["Close"] > c_prev["Open"]
        bear_last    = c_last["Close"] < c_last["Open"]
        # Real bearish engulf: last candle's body fully contains prev's
        # open/close range.
        engulfs      = c_last["Open"] >= c_prev["Close"] and c_last["Close"] <= c_prev["Open"]
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
            pattern_check = "PASS ✅"
        else:
            fails = []
            if not in_zone:   fails.append("price not in premium zone")
            if not bull_prev: fails.append("prev candle not bullish")
            if not bear_last: fails.append("last candle not bearish")
            if not engulfs:   fails.append("doesn't engulf prev body")
            if not real_body: fails.append("body too small vs ATR")
            pattern_check = f"FAIL ❌ ({', '.join(fails)})"
    else:
        pattern_check = "N/A"

    # ── Log: full checklist every scan, regardless of outcome ───────────────
    decision = f"TRADE — {trade_signal}" if trade_signal != "HOLD" else "NO TRADE — pattern conditions not met"
    fib_check = f"{fib_zone:.5f}"
    print(_checklist(macro_bias, bos_check, bos_bias_check, range_check, fib_check, atr_valid_check, pattern_check, decision))
    print(
        f"  [Detail] Structure: {structure_source} | Price: {c_last['Close']:.5f} | "
        f"SwH: {swing_high:.5f} SwL: {swing_low:.5f}"
    )

    # ── Alert ─────────────────────────────────────────────────────────────
    if trade_signal != "HOLD" and entry is not None:
        msg = (
            f"🚨 *SMC SIGNAL — GBPUSD* 🚨\n\n"
            f"*Action:* `{trade_signal}`\n"
            f"*Bias:* `{macro_bias}` (1H EMA-100)\n"
            f"*Structure:* `{structure_source}`\n"
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
