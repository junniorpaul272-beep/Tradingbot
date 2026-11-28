"""
btc_scan.py: what the chart is doing, technically and fundamentally. FACTS ONLY.

Observation layer. It never gates a signal and never writes a sentence for a human: every function here returns
plain numbers and codes (JSON-serialisable), and btc_report.py owns all wording. Anything that could not be
checked is returned as {"status": "unavailable", "reason": ...}, never guessed, so "not checked" can never be
mistaken for "checked and nothing there".

Timeframes: 1H is built from the 30-minute candles the strategy already uses (no extra data call);
15M and 5M come from their own series when supplied.

Definitions that are my reading of the "technical / fundamental" report spec (tune the constants below):
  * Control per timeframe = who made the LAST break of a confirmed swing (a candle CLOSE beyond it), and
    whether price has stayed on that side of the level since.
  * 2-week bias = the 2-week % change judged against the daily ATR (flat if it is smaller than FLAT_BAND_ATR
    ordinary days of movement).
  * Fast / medium / slow activity = hourly ATR, daily ATR, and the 2-week average daily range, each ranked
    against its own history (bottom 20% = unusually quiet, top 20% = unusually active).
"""
from datetime import timedelta

from btc_engine import Bar, ny

PIVOT_K = 2            # a swing needs K lower/higher candles on each side
FRAME_WINDOW = 200     # candles per timeframe used for structure
MIN_FRAME_BARS = 40
EMA_FAST, EMA_SLOW = 20, 50
STRETCH_ATR = 2.0      # |close - EMA50| in ATRs that counts as "stretched"
FLAT_BAND_ATR = 2.0    # a 2-week move smaller than this many daily ATRs counts as flat
QUIET_RANK, ACTIVE_RANK = 0.20, 0.80
MIN_HISTORY = 20       # history values needed before an activity label is given
FAST_HISTORY_BARS = 720  # ~30 days of hourly ATR values

# windows in 30-minute candles
WINDOWS = (("1H", 2), ("1D", 48), ("2D", 96), ("4D", 192), ("2W", 672))
RANGES = (("2D", 96), ("4D", 192), ("2W", 672))
BASE_S = 1800


# --------------------------------------------------------------------------- building blocks
def atr_series(bars, n=14):
    """Wilder ATR aligned to `bars` (None until seeded with the simple mean of the first n true ranges)."""
    out, prev, atr, seed = [None] * len(bars), None, None, []
    for i, b in enumerate(bars):
        tr = b.h - b.l if prev is None else max(b.h - b.l, abs(b.h - prev), abs(b.l - prev))
        prev = b.c
        if atr is None:
            seed.append(tr)
            if len(seed) == n:
                atr = sum(seed) / n
                out[i] = atr
        else:
            atr = (atr * (n - 1) + tr) / n
            out[i] = atr
    return out


def ema_series(values, n):
    k, e, out = 2 / (n + 1), None, []
    for v in values:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def resample(bars, target_s, base_s=BASE_S):
    """Aggregate candles into bigger ones. Only COMPLETE buckets are returned."""
    per = target_s // base_s
    buckets = {}
    for b in bars:
        buckets.setdefault(b.t // target_s * target_s, []).append(b)
    out = []
    for t0 in sorted(buckets):
        grp = sorted(buckets[t0], key=lambda x: x.t)
        if len(grp) == per:
            out.append(Bar(t0, grp[0].o, max(x.h for x in grp), min(x.l for x in grp), grp[-1].c))
    return out


def sessions(bars):
    """Completed NY 17:00 -> 17:00 daily candles. The first (maybe partial) and the last (still open) are dropped."""
    out, key, cur = [], None, None
    for b in bars:
        loc = ny(b.t)
        mins = loc.hour * 60 + loc.minute
        k = loc.date() - timedelta(days=1) if mins < 17 * 60 else loc.date()
        if k != key:
            if cur:
                out.append(cur)
            key, cur = k, [b.t, b.o, b.h, b.l, b.c]
        else:
            cur[2], cur[3], cur[4] = max(cur[2], b.h), min(cur[3], b.l), b.c
    return [Bar(*c) for c in out[1:]]


def structure_read(bars, k=PIVOT_K):
    """One pass over the candles: confirmed swings, the still-unbroken last swing high/low, and the last break."""
    n = len(bars)
    sh, sl, cur_sh, cur_sl, last_break = [], [], None, None, None
    for i in range(n):
        p = i - k
        if p >= k:
            h, l = bars[p].h, bars[p].l
            if all(h >= bars[p - j].h for j in range(1, k + 1)) and all(h > bars[p + j].h for j in range(1, k + 1)):
                sh.append((p, h))
                cur_sh = (p, h)
            if all(l <= bars[p - j].l for j in range(1, k + 1)) and all(l < bars[p + j].l for j in range(1, k + 1)):
                sl.append((p, l))
                cur_sl = (p, l)
        c = bars[i].c
        if cur_sh is not None and c > cur_sh[1]:
            last_break, cur_sh = {"dir": "up", "level": cur_sh[1], "idx": i}, None
        if cur_sl is not None and c < cur_sl[1]:
            last_break, cur_sl = {"dir": "down", "level": cur_sl[1], "idx": i}, None
    if last_break is not None:
        after = [x.c for x in bars[last_break["idx"] + 1:]]
        lvl = last_break["level"]
        last_break["held"] = all(c >= lvl for c in after) if last_break["dir"] == "up" else all(c <= lvl for c in after)
        last_break["bars_ago"] = n - 1 - last_break["idx"]
    return {"swing_highs": sh, "swing_lows": sl, "open_high": cur_sh, "open_low": cur_sl, "last_break": last_break}


def _structure_label(sh, sl):
    if len(sh) < 2 or len(sl) < 2:
        return "unclear"
    hh, hl = sh[-1][1] > sh[-2][1], sl[-1][1] > sl[-2][1]
    lh, ll = sh[-1][1] < sh[-2][1], sl[-1][1] < sl[-2][1]
    if hh and hl:
        return "up"
    if lh and ll:
        return "down"
    return "mixed"


def _control(last_break):
    if last_break is None:
        return "none"
    if last_break["dir"] == "up":
        return "buyers" if last_break["held"] else "contested"
    return "sellers" if last_break["held"] else "contested"


def rank_of(history, current):
    """Share of history values at or below the current value (0..1)."""
    return sum(1 for v in history if v <= current) / len(history)


def activity(history, current):
    """{'pct','rank','label'}; label is None when there is not enough history to judge."""
    if len(history) < MIN_HISTORY:
        return {"pct": current, "rank": None, "label": None, "history_n": len(history)}
    r = rank_of(history, current)
    label = "quiet" if r <= QUIET_RANK else "active" if r >= ACTIVE_RANK else "normal"
    return {"pct": current, "rank": r, "label": label, "history_n": len(history)}


# --------------------------------------------------------------------------- technical
def frame_read(bars, name, bar_s, asof):
    """Technical read of one timeframe. `bars` are CLOSED candles."""
    if not bars:
        return {"status": "unavailable", "reason": "no candles"}
    if len(bars) < MIN_FRAME_BARS:
        return {"status": "unavailable", "reason": f"only {len(bars)} candles, need {MIN_FRAME_BARS}"}
    if asof - (bars[-1].t + bar_s) > 2 * bar_s:
        return {"status": "unavailable", "reason": "data is stale"}
    w = bars[-FRAME_WINDOW:]
    st = structure_read(w)
    closes = [b.c for b in w]
    atr = atr_series(w, 14)[-1]
    ema_f, ema_s = ema_series(closes, EMA_FAST)[-1], ema_series(closes, EMA_SLOW)[-1]
    ext = (closes[-1] - ema_s) / atr if atr else None
    lb = st["last_break"]
    return {
        "status": "ok", "name": name, "bar_minutes": bar_s // 60, "last_close": closes[-1], "candles": len(w),
        "structure": _structure_label(st["swing_highs"], st["swing_lows"]),
        "control": _control(lb),
        "last_break": None if lb is None else {k: lb[k] for k in ("dir", "level", "bars_ago", "held")},
        "swing_high": st["open_high"][1] if st["open_high"] else None,
        "swing_low": st["open_low"][1] if st["open_low"] else None,
        "ema20_side": "above" if closes[-1] >= ema_f else "below",
        "extension_atr": None if ext is None else round(ext, 2),
        "stretched": None if ext is None else ("above" if ext >= STRETCH_ATR else "below" if ext <= -STRETCH_ATR else None),
    }


# --------------------------------------------------------------------------- fundamental
def fundamental_read(b30, h1, daily):
    """The underlying buyer/seller tug-of-war from price alone. Sentiment is reported as unavailable."""
    if len(b30) < 100 or len(h1) < 30:
        return {"status": "unavailable", "reason": "not enough candles"}
    price = b30[-1].c
    changes = {}
    for name, n in WINDOWS:
        if len(b30) > n:
            chg = (price / b30[-1 - n].c - 1) * 100
            changes[name] = {"pct": chg, "pace_per_day": chg / (n / 48) if n >= 48 else None}
        else:
            changes[name] = None
    ranges = {}
    for name, n in RANGES:
        if len(b30) >= n:
            w = b30[-n:]
            hi, lo = max(b.h for b in w), min(b.l for b in w)
            pos = (price - lo) / (hi - lo) if hi > lo else 0.5
            ranges[name] = {"high": hi, "low": lo, "position": pos,
                            "zone": "upper" if pos >= 2 / 3 else "lower" if pos <= 1 / 3 else "middle"}
        else:
            ranges[name] = None

    # activity: each measure against its own history
    fast_pct = [a / b.c * 100 for a, b in zip(atr_series(h1, 14), h1) if a is not None][-FAST_HISTORY_BARS:]
    fast = activity(fast_pct[:-1], fast_pct[-1]) if fast_pct else None
    datr = atr_series(daily, 14)
    med_pct = [a / b.c * 100 for a, b in zip(datr, daily) if a is not None]
    medium = activity(med_pct[:-1], med_pct[-1]) if med_pct else None
    rng = [(d.h - d.l) / d.c * 100 for d in daily]
    roll = [sum(rng[i - 13:i + 1]) / 14 for i in range(13, len(rng))]
    slow = activity(roll[:-1], roll[-1]) if roll else None
    daily_atr_abs = next((a for a in reversed(datr) if a is not None), None)

    # 2-week bias (overarching) and 1H push (entry timing)
    chg2w = changes["2W"]["pct"] if changes["2W"] else None
    bias = None
    if chg2w is not None and medium:
        band = FLAT_BAND_ATR * medium["pct"]
        bias = "up" if chg2w > band else "down" if chg2w < -band else "flat"
    push = None
    if changes["1H"] and fast and fast["pct"]:
        mult = abs(changes["1H"]["pct"]) / fast["pct"]
        push = {"pct": changes["1H"]["pct"], "vs_typical": mult,
                "size": "quiet" if mult < 0.5 else "normal" if mult < 1.5 else "strong",
                "direction": "up" if changes["1H"]["pct"] > 0 else "down" if changes["1H"]["pct"] < 0 else "flat"}
        if bias in ("up", "down") and push["direction"] != "flat":
            push["vs_bias"] = "with" if push["direction"] == bias else "against"
        else:
            push["vs_bias"] = "neutral"
    signs = {("up" if changes[k]["pct"] > 0 else "down") for k in ("1D", "2D", "4D", "2W")
             if changes.get(k) and changes[k]["pct"] != 0}
    flow = None if not signs else ("consistent" if len(signs) == 1 else "mixed")
    return {
        "status": "ok", "price": price, "changes": changes, "ranges": ranges,
        "activity": {"fast": fast, "medium": medium, "slow": slow, "daily_atr_abs": daily_atr_abs},
        "bias_2w": bias, "push_1h": push, "flow": flow,
        "sentiment": {"status": "unavailable", "reason": "no sentiment feed is connected"},
    }


# --------------------------------------------------------------------------- the scan
def _rnd(x):
    return round(x, 2) if isinstance(x, float) else x


def _levels(price, one_h, strategy, daily_atr):
    """The levels worth watching, as facts (kind + price). Wording lives in btc_report."""
    out = []

    def add(kind, level):
        if level is None:
            return
        out.append({"kind": kind, "price": level, "pct_from_price": (level / price - 1) * 100,
                    "atr_away": abs(level - price) / daily_atr if daily_atr else None})

    if one_h and one_h.get("status") == "ok":
        add("swing_high", one_h["swing_high"])
        add("swing_low", one_h["swing_low"])
        if one_h["last_break"] and one_h["control"] in ("buyers", "sellers"):
            add("break_level", one_h["last_break"]["level"])
    if strategy:
        lg, sh = strategy.get("LONG"), strategy.get("SHORT")
        if lg and lg.get("ready"):
            add("long_trigger", lg["entry_level"])
        if sh and sh.get("ready"):
            add("short_trigger", sh["entry_level"])
        for side, s in (("LONG", lg), ("SHORT", sh)):
            if s and s.get("signal_on"):
                add("active_exit_long" if side == "LONG" else "active_exit_short", s["active_stop"])
    return out


def build_scan(bars30, bars15, bars5, asof, strategy=None):
    """Technical + fundamental scan as of `asof` (epoch close time of the latest candle).
    bars30 are required; bars15 / bars5 may be None (reported as unavailable)."""
    b30 = [b for b in (bars30 or []) if b.t + BASE_S <= asof]
    if not b30:
        return {"asof": asof, "status": "unavailable", "reason": "no candles"}
    h1 = resample(b30, 3600)
    daily = sessions(b30)
    price = b30[-1].c

    frames = {"1H": frame_read(h1, "1H", 3600, asof)}
    for name, bars, step in (("15M", bars15, 900), ("5M", bars5, 300)):
        if not bars:
            frames[name] = {"status": "unavailable", "reason": "no data was fetched"}
        else:
            frames[name] = frame_read([b for b in bars if b.t + step <= asof], name, step, asof)

    fund = fundamental_read(b30, h1, daily)
    datr = fund["activity"]["daily_atr_abs"] if fund.get("status") == "ok" else None
    scan = {
        "asof": asof, "status": "ok", "price": price,
        "technical": {"frames": frames, "levels": _levels(price, frames["1H"], strategy, datr)},
        "fundamental": fund,
        "strategy": None if not strategy else {
            side: {k: s.get(k) for k in ("ready", "signal_on", "held", "entry_level", "exit_level", "active_stop")}
            for side, s in strategy.items()},
    }
    return _round_tree(scan)


def _round_tree(x):
    if isinstance(x, dict):
        return {k: _round_tree(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_round_tree(v) for v in x]
    return _rnd(x)
