"""
momentum_read.py — momentum.state: a MEASURED description of recent price movement on one timeframe.

Four separate readings; there is no composite score and no bullish/bearish bias here:
  direction    net displacement over the window, in ATRs          D  = (C[t] - C[t-N]) / ATR[t]      (signed)
  strength     the size of that same displacement                 |D|                                 (NOT independent of direction)
  persistence  HISTORICAL PATH EFFICIENCY over the same window    ER = |C[t]-C[t-N]| / sum|C[i]-C[i-1]|   (0..1)
               - it describes how directly price travelled; it is NOT evidence the move will continue.
  expansion    recent range activity against its own baseline     X  = mean TR[t-3..t] / mean TR[t-51..t-4]  (direction-blind)

EXACT DEFINITIONS (bars are COMPLETED candles, oldest..newest, t = the newest; TR[i] needs C[i-1]):
  TR[i]   = max(H[i]-L[i], |H[i]-C[i-1]|, |L[i]-C[i-1]|)
  ATR[t]  = simple mean of TR[t-13..t]                (14 values, no smoothing, no warm-up; NOT scanner_common.atr)
  D       uses bars t-N..t                            (N+1 = 17 bars; the ATR window t-14..t lies inside it)
  ER      uses bars t-N..t                            (N close-to-close steps)
  X       recent = TR[t-3..t] (4 values), baseline = TR[t-51..t-4] (48 values): NO overlap, bar t counted once.
          Needs bars t-52..t (53 bars). A volatility shock raises X, then lowers it again as the shock enters the
          baseline: that is a property of the rolling ratio, not necessarily of the market.

DATA QUALITY (a reading that cannot be computed is UNKNOWN with a reason; never neutral, never zero):
  The candles themselves cannot tell a provider outage from a missing bar, so the module only distinguishes:
    bad_input             unusable candle / timestamp (naive, not on a 15-minute boundary, duplicate or out of order,
                          non-finite or inconsistent OHLC)
    no_candles            nothing completed
    market_closed         `now` falls inside the FX weekend closure (Fri 17:00 -> Sun 17:00 America/New_York)
    stale_feed            market open, but the newest completed candle ended more than 2 intervals before `now`
                          (feed lag or outage: indistinguishable here)
    insufficient_bars     fewer bars than that reading needs
    missing_bar           an interior gap while the market was open (data defect; also bank-holiday gaps: conservative)
    spans_market_closure  the reading's own window crosses the weekend closure, so the jump is not movement
    zero_atr / zero_path / zero_baseline   the denominator is exactly zero
  A candle still forming (its interval has not ended at `now`) is excluded, never used.
  ASSUMPTION: FX week = Sun 17:00 -> Fri 17:00 New York time, no daily break. A holiday closure shows as missing_bar.

LABELS: raw values are always returned. A label is returned ONLY when the thresholds are frozen (THRESHOLDS["frozen"]);
until then `label` is None and `provisional_label` shows what the PLACEHOLDER cut points would say. Persistence has no
placeholder at all. Nothing downstream may state a provisional label as fact.

PURE and OBSERVATION-ONLY: no I/O, no state, no clock, imports only the standard library, and nothing in the trading
engine reads it for a decision.
"""
import math
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _NY = ZoneInfo("America/New_York")
except Exception:                                  # pragma: no cover - zoneinfo missing: closure test degrades to UTC-5
    _NY = None

VERSION = "momentum_v0"
INTERVAL = timedelta(minutes=15)
TIMEFRAME = "15m"
N = 16                       # lookback steps (4 hours)
ATR_PERIOD = 14
EXP_RECENT = 4
EXP_BASELINE = 48
STALE_INTERVALS = 2

NEED_DIRECTION = max(N, ATR_PERIOD) + 1            # 17 bars
NEED_PERSISTENCE = N + 1                           # 17 bars
NEED_EXPANSION = EXP_RECENT + EXP_BASELINE + 1     # 53 bars

THRESHOLDS = {
    "version": "v0-unfrozen",
    "frozen": False,                               # set True only with cut points chosen on the first 60% of the archive
    "direction_neutral_atr": 0.5,                  # PLACEHOLDER
    "strength_moderate_atr": 0.5, "strength_strong_atr": 1.5,      # PLACEHOLDERS
    "expansion_expanded": 1.5, "expansion_contracted": 0.67,       # PLACEHOLDERS
    # persistence: deliberately no cut points (raw ER only) until history has been inspected
}


# ---------------------------------------------------------------- calendar
def market_closed_at(ts):
    """True if the instant is inside the FX weekend closure. `ts` must be timezone-aware."""
    loc = ts.astimezone(_NY) if _NY is not None else ts.astimezone(timezone(timedelta(hours=-5)))
    wd, hm = loc.weekday(), (loc.hour, loc.minute)
    return (wd == 4 and hm >= (17, 0)) or wd == 5 or (wd == 6 and hm < (17, 0))


def _aligned(ts):
    return ts.tzinfo is not None and ts.utcoffset() == timedelta(0) and ts.minute % 15 == 0 and ts.second == 0 and ts.microsecond == 0


# ---------------------------------------------------------------- input
def candles_from_frame(df):
    """Duck-typed adapter for a pandas frame (index = candle OPEN time, columns Open/High/Low/Close). No pandas import."""
    out = []
    for ts, row in df.iterrows():
        t = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        out.append({"t": t, "o": row["Open"], "h": row["High"], "l": row["Low"], "c": row["Close"]})
    return out


def _clean(candles):
    """-> (list of validated candles, error or None). Candles are dicts with t (OPEN time, tz-aware UTC), o,h,l,c."""
    out, prev = [], None
    for c in candles:
        try:
            t, o, h, l, cl = c["t"], float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])
        except (KeyError, TypeError, ValueError):
            return None, "bad_input"
        if not isinstance(t, datetime) or not _aligned(t):
            return None, "bad_input"
        if not all(math.isfinite(x) for x in (o, h, l, cl)) or h < l or not (l <= o <= h) or not (l <= cl <= h):
            return None, "bad_input"
        if prev is not None and t <= prev:
            return None, "bad_input"
        prev = t
        out.append({"t": t, "o": o, "h": h, "l": l, "c": cl})
    return out, None


def _span_problem(bars):
    """Contiguity of a window: None if every 15-minute slot is present, else the reason."""
    reason = None
    for a, b in zip(bars, bars[1:]):
        gap = b["t"] - a["t"]
        if gap == INTERVAL:
            continue
        slots, t = [], a["t"] + INTERVAL
        while t < b["t"]:
            slots.append(t); t += INTERVAL
        if all(market_closed_at(s) for s in slots):
            reason = reason or "spans_market_closure"
        else:
            return "missing_bar"                    # a defect outranks a closure
    return reason


def _tr(bars):
    """True ranges for bars[1:], oldest first."""
    return [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(bars, bars[1:])]


# ---------------------------------------------------------------- readings
def _unknown(reason):
    return {"status": "UNKNOWN", "value": None, "label": None, "provisional_label": None, "reason": reason}


def _ok(value, label, provisional):
    return {"status": "OK", "value": value, "label": label if THRESHOLDS["frozen"] else None,
            "provisional_label": provisional, "reason": None}


def _window(bars, need):
    if len(bars) < need:
        return None, "insufficient_bars"
    w = bars[-need:]
    p = _span_problem(w)
    return (w, None) if p is None else (None, p)


def _direction_strength(bars):
    w, why = _window(bars, NEED_DIRECTION)
    if why:
        return _unknown(why), _unknown(why)
    atr = sum(_tr(w[-(ATR_PERIOD + 1):])) / ATR_PERIOD
    if atr == 0:
        return _unknown("zero_atr"), _unknown("zero_atr")
    d = (w[-1]["c"] - w[-1 - N]["c"]) / atr
    th = THRESHOLDS
    dl = "NEUTRAL" if abs(d) < th["direction_neutral_atr"] else ("UP" if d > 0 else "DOWN")
    a = abs(d)
    sl = "WEAK" if a < th["strength_moderate_atr"] else ("MODERATE" if a < th["strength_strong_atr"] else "STRONG")
    return _ok(d, dl, dl), _ok(a, sl, sl)


def _persistence(bars):
    w, why = _window(bars, NEED_PERSISTENCE)
    if why:
        return _unknown(why)
    w = w[-(N + 1):]
    path = sum(abs(b["c"] - a["c"]) for a, b in zip(w, w[1:]))
    if path == 0:
        return _unknown("zero_path")
    return _ok(abs(w[-1]["c"] - w[0]["c"]) / path, None, None)


def _expansion(bars):
    w, why = _window(bars, NEED_EXPANSION)
    if why:
        return _unknown(why)
    tr = _tr(w)                                     # 52 values: baseline = tr[:48], recent = tr[48:]
    base = sum(tr[:EXP_BASELINE]) / EXP_BASELINE
    if base == 0:
        return _unknown("zero_baseline")
    x = (sum(tr[EXP_BASELINE:]) / EXP_RECENT) / base
    th = THRESHOLDS
    lab = "EXPANDED" if x >= th["expansion_expanded"] else ("CONTRACTED" if x <= th["expansion_contracted"] else "NORMAL")
    return _ok(x, lab, lab)


def compute(candles, now):
    """candles: dicts {t (open time, tz-aware UTC), o, h, l, c}, any length, oldest first. `now`: tz-aware UTC.
    Returns the full momentum.state record; never raises on bad data (it reports bad_input)."""
    rec = {"version": VERSION, "threshold_version": THRESHOLDS["version"], "labels_frozen": THRESHOLDS["frozen"],
           "timeframe": TIMEFRAME, "lookback_bars": N, "atr_period": ATR_PERIOD,
           "expansion_recent_bars": EXP_RECENT, "expansion_baseline_bars": EXP_BASELINE,
           "scan_time": None, "asof_candle_open": None, "asof_candle_close": None,
           "forming_bar_excluded": False, "bars_used": 0}

    def all_unknown(reason):
        for k in ("direction", "strength", "persistence", "expansion"):
            rec[k] = _unknown(reason)
        return rec

    if not isinstance(now, datetime) or now.tzinfo is None:
        return all_unknown("bad_input")
    now = now.astimezone(timezone.utc)
    rec["scan_time"] = now.isoformat()
    bars, err = _clean(candles)
    if err:
        return all_unknown(err)
    done = [b for b in bars if b["t"] + INTERVAL <= now]
    rec["forming_bar_excluded"] = len(done) != len(bars)
    rec["bars_used"] = len(done)
    if not done:
        return all_unknown("no_candles")
    last = done[-1]
    rec["asof_candle_open"] = last["t"].isoformat()
    rec["asof_candle_close"] = (last["t"] + INTERVAL).isoformat()
    if market_closed_at(now):
        return all_unknown("market_closed")
    if now - (last["t"] + INTERVAL) > STALE_INTERVALS * INTERVAL:
        return all_unknown("stale_feed")
    rec["direction"], rec["strength"] = _direction_strength(done)
    rec["persistence"] = _persistence(done)
    rec["expansion"] = _expansion(done)
    return rec
