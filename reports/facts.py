"""facts.py - layer 1. Raw candles in, plain numbers out (contracts.py shapes). No sentences, no interpretation."""
import math
import pandas as pd
from .contracts import (Break, TFFacts, BiasFacts, RocRow, FlowFacts, VolFacts, MeasureFacts, ContractError, Interaction, StoryFacts)

PIP = 0.0001
TF_MIN = {"5M": 5, "15M": 15, "1H": 60}
BREAK_WINDOW = {"5M": 96, "15M": 64, "1H": 72}      # bars searched for the last close through a swing level
CONTAIN_BARS = {"5M": 36, "15M": 24, "1H": 12}
PUSH_BARS = {"5M": 36, "15M": 24, "1H": 24}
MOM_BARS = 8


class FactError(Exception):
    """Missing or unusable input. The caller turns it into an explicit 'not available' message."""


def market_closed(ts):
    """The forex weekend, by the same rule the scanner uses (scanner_common.is_forex_weekend; a test keeps the two identical):
    all of Saturday, Friday from 21:00 UTC, Sunday until 22:00 UTC. ts is a UTC timestamp or index."""
    wd, hr = ts.weekday, ts.hour
    return (wd == 5) | ((wd == 6) & (hr < 22)) | ((wd == 4) & (hr >= 21))


def open_hours(start, end):
    """Number of whole hours between start and end when the market is open."""
    h = pd.date_range(start.ceil("h"), end.floor("h"), freq="h", inclusive="left")
    return int((~market_closed(h)).sum()) if len(h) else 0


def closed(df, tf, now):
    """Fully closed candles as of `now`, with the weekend's candles removed. The 1H and 15M logs hold quotes from when the market was shut
    (about 29% of the 1H rows, averaging half the size of a trading candle); counting them would shrink every average and every ATR."""
    if df is None or len(df) == 0:
        raise FactError(f"no {tf} candles")
    d = df[df.index + pd.Timedelta(minutes=TF_MIN[tf]) <= now]
    return d[~market_closed(d.index)]


def atr(df, n=14):
    pc = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def swings(df, wing=2):
    out = []
    h, l = df["High"].values, df["Low"].values
    for i in range(wing, len(df) - wing):
        if all(h[i] > h[i - k] for k in range(1, wing + 1)) and all(h[i] >= h[i + k] for k in range(1, wing + 1)):
            out.append((i, float(h[i]), "high", i + wing))
        if all(l[i] < l[i - k] for k in range(1, wing + 1)) and all(l[i] <= l[i + k] for k in range(1, wing + 1)):
            out.append((i, float(l[i]), "low", i + wing))
    return out


def breaks(df, wing=2):
    sw = swings(df, wing)
    by_conf = {}
    for s in sw:
        by_conf.setdefault(s[3], []).append(s)
    hi = lo = None
    ev = []
    c = df["Close"].values
    for j in range(len(df)):
        for s in by_conf.get(j, []):
            if s[2] == "high": hi = s
            else: lo = s
        if hi is not None and c[j] > hi[1]:
            ev.append((j, Break("UP", hi[1], df.index[j].to_pydatetime(), df.index[hi[0]].to_pydatetime()))); hi = None
        if lo is not None and c[j] < lo[1]:
            ev.append((j, Break("DOWN", lo[1], df.index[j].to_pydatetime(), df.index[lo[0]].to_pydatetime()))); lo = None
    return ev, sw


def _last_swing(d, sw, kind):
    s = [x for x in sw if x[2] == kind]
    return (s[-1][1], d.index[s[-1][0]].to_pydatetime()) if s else None


def tf_facts(df, tf, now) -> TFFacts:
    d = closed(df, tf, now)
    nb = BREAK_WINDOW[tf]
    if len(d) < nb // 2 + 20:
        raise FactError(f"not enough {tf} candles ({len(d)})")
    a = float(atr(d).iloc[-1])
    ev, sw = breaks(d)
    close = float(d["Close"].iloc[-1])
    recent = [e for e in ev if e[0] >= len(d) - nb]
    lb_pos, lb = recent[-1] if recent else (None, None)
    held = beyond = since = None
    if lb:
        held = close >= lb.level if lb.direction == "UP" else close <= lb.level
        beyond = ((close - lb.level) if lb.direction == "UP" else (lb.level - close)) / a
        since = len(d) - 1 - lb_pos
    m = d.iloc[-MOM_BARS:]
    net = float(m["Close"].iloc[-1] - m["Open"].iloc[0])
    cb = CONTAIN_BARS[tf]; w = d.iloc[-cb:]
    pb = PUSH_BARS[tf]; p = d.iloc[-pb:]
    hi, lo = float(w["High"].max()), float(w["Low"].min())
    tests_top = int(((w["High"] >= hi - 0.25 * a) & (w["Close"] <= hi - 0.5 * a)).sum())
    tests_bottom = int(((w["Low"] <= lo + 0.25 * a) & (w["Close"] >= lo + 0.5 * a)).sum())
    third = max(1, len(w) // 3)
    drift = float((w["Close"].iloc[-third:].mean() - w["Close"].iloc[:third].mean()) / (hi - lo)) if hi > lo else 0.0
    return TFFacts(tf, (d.index[-1] + pd.Timedelta(minutes=TF_MIN[tf])).to_pydatetime(), close, a / PIP, lb, held, since, beyond,
                   len(m), net / PIP, net / a, int((m["Close"] > m["Open"]).sum()),
                   cb, (hi - lo) / a, (hi - lo) / PIP, pb, float(p["Close"].iloc[-1] - p["Open"].iloc[0]) / a,
                   _last_swing(d, sw, "high"), _last_swing(d, sw, "low"), hi, lo, tests_top, tests_bottom, drift,
                   (_sh[0] - close) / PIP if (_sh := _last_swing(d, sw, "high")) else None, (close - _sl[0]) / PIP if (_sl := _last_swing(d, sw, "low")) else None)


def bias_facts(inp, d1, now, tf1: TFFacts) -> BiasFacts:
    d = closed(d1, "1H", now)
    e = d["Close"].ewm(span=100, adjust=False).mean()
    a = float(atr(d).iloc[-1])
    dist = float(d["Close"].iloc[-1] - e.iloc[-1])
    w = d.iloc[-CONTAIN_BARS["1H"]:]
    hi, lo = float(w["High"].max()), float(w["Low"].min())
    rev = None
    if inp.bias == "BULLISH": rev = tf1.swing_low
    elif inp.bias == "BEARISH": rev = tf1.swing_high
    return BiasFacts(inp.bias, inp.leg_extreme, inp.leg_origin, tf1.last_break, tf1.held, rev, len(w), hi, lo,
                     (hi - lo) <= 3.0 * a, "ABOVE" if dist > 0 else "BELOW", dist / PIP, dist / a, a / PIP,
                     float(d["Close"].iloc[-1]), (float(d["Close"].iloc[-1]) - inp.leg_extreme) / PIP)


WINDOWS = [("1 hour", 1), ("4 hours", 4), ("1 day", 24), ("2 days", 48), ("4 days", 96), ("1 week", 168), ("2 weeks", 336)]


def measure_facts(d1, now) -> MeasureFacts:
    d = closed(d1, "1H", now)
    if len(d) < 24:
        raise FactError(f"not enough 1H candles ({len(d)})")
    t_last = d.index[-1] + pd.Timedelta(hours=1)
    last = float(d["Close"].iloc[-1])
    rows = []
    for name, hrs in WINDOWS:
        start = t_last - pd.Timedelta(hours=hrs)
        w = d[d.index >= start]
        if d.index[0] > start or len(w) < max(1, int(open_hours(start, t_last) * 0.5)):
            rows.append(RocRow(name, hrs, False)); continue
        o = float(w["Open"].iloc[0]); hi, lo = float(w["High"].max()), float(w["Low"].min())
        rows.append(RocRow(name, hrs, True, (last - o) / PIP, (last / o - 1) * 100, (hi - lo) / PIP, (hi - lo) / lo * 100, (last - lo) / (hi - lo) if hi > lo else .5))
    flow = None
    two = next(r for r in rows if r.hours == 336)
    if two.available:
        w = d[d.index >= t_last - pd.Timedelta(hours=336)]
        body = w["Close"] - w["Open"]
        up, dn = body[body > 0], -body[body < 0]
        path = float(up.sum() + dn.sum())
        net = float(w["Close"].iloc[-1] - w["Open"].iloc[0])
        flow = FlowFacts(336, len(w), len(up), len(dn), float(up.sum()) / PIP, float(dn.sum()) / PIP,
                         float(up.mean()) / PIP if len(up) else 0.0, float(dn.mean()) / PIP if len(dn) else 0.0, net / PIP,
                         abs(net) / path if path else 0.0)
    sh = d.copy(); sh.index = sh.index + pd.Timedelta(hours=2)      # forex day/week start Sunday 22:00 UTC
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    fast = atr(d).dropna()
    daily = sh.resample("D").agg(agg).dropna(); daily = daily[(daily["High"] - daily["Low"]) > 0]
    d_atr = atr(daily, 14).dropna()
    weekly = sh.resample("W-FRI").agg(agg).dropna(); wr = weekly["High"] - weekly["Low"]
    if len(fast) < 50 or len(d_atr) < 10 or len(wr) < 3:
        raise FactError("not enough history to judge how big the moves are")
    pct = lambda s, v: float((s <= v).mean())
    wk = wr.iloc[:-1].tail(8)
    vol = VolFacts(float(fast.iloc[-1]) / PIP, pct(fast, fast.iloc[-1]), max(1, round((d.index[-1] - d.index[0]).days / 7)),
                   float(d_atr.iloc[-1]) / PIP, pct(d_atr, d_atr.iloc[-1]), len(d_atr), float(wk.mean()) / PIP, len(wk), float(wr.iloc[-1]) / PIP,
                   float(fast.iloc[-1]) / last * 100, float(d_atr.iloc[-1]) / last * 100, float(wk.mean()) / last * 100, float(wr.iloc[-1]) / last * 100)
    return MeasureFacts((d.index[-1] + pd.Timedelta(hours=1)).to_pydatetime(), tuple(rows), flow, vol, (d.index[-1] - d.index[0]).days)


STORY_MIN = 30
MIN_STORY_BARS = 4          # shortest window still reported: 20 minutes of unbroken 5M candles


EARLIER_MIN = 180
TF_RANK = {"5M": 0, "15M": 1, "1H": 2}


def story_facts(d5, now, minutes=STORY_MIN, d15=None, d1=None) -> StoryFacts:
    """The last `minutes` of fully closed 5M candles, as numbers."""
    d = closed(d5, "5M", now)
    n = minutes // 5
    if len(d) < n + 20:
        raise FactError(f"not enough 5M candles ({len(d)})")
    a = float(atr(d).iloc[-1])
    # newest unbroken run of 5M candles, up to the n asked for; a shorter run is used only if it still has MIN_STORY_BARS candles
    run = 1
    while run < n and d.index[-run] - d.index[-run - 1] == pd.Timedelta(minutes=5): run += 1
    shortened = run < n
    if shortened:
        if run < MIN_STORY_BARS: raise FactError(f"there are only {run * 5} minutes of unbroken 5M candles")
        n = run; minutes = 5 * run
    w = d.iloc[-n:]
    hi, lo = float(w["High"].max()), float(w["Low"].min())
    t_hi, t_lo = w["High"].idxmax(), w["Low"].idxmin()
    close = float(w["Close"].iloc[-1]); o = float(w["Open"].iloc[0])
    start = w.index[0]; end = w.index[-1] + pd.Timedelta(minutes=5)
    evs = []; tfs = []
    for tf, df in (("5M", d), ("15M", d15)):
        if df is None: continue
        dd = closed(df, tf, now)
        if len(dd) < 60: continue
        ev, _ = breaks(dd)
        for pos, b in ev:
            if b.time + pd.Timedelta(minutes=TF_MIN[tf]) > start + pd.Timedelta(0) and b.time >= start - pd.Timedelta(minutes=TF_MIN[tf]) + pd.Timedelta(minutes=0):
                evs.append(b); tfs.append(tf)
    # the most important close through a swing level in the 3 hours before the window: higher timeframe first, then the most recent
    earlier = None
    cutoff = start - pd.Timedelta(minutes=EARLIER_MIN)
    cands = []
    for tf, df in (("5M", d), ("15M", d15), ("1H", d1)):
        if df is None: continue
        dd = closed(df, tf, now).tail(400)
        if len(dd) < 60: continue
        ev, _ = breaks(dd)
        for pos, b in ev:
            done = pd.Timestamp(b.time) + pd.Timedelta(minutes=TF_MIN[tf])
            if cutoff <= done <= start: cands.append((TF_RANK[tf], done, b, tf))
    if cands:
        _, done, b, tf = max(cands, key=lambda x: (x[0], x[1]))
        earlier = (b, tf); earlier_min = int((end - done).total_seconds() // 60)
    else:
        earlier_min = 0
    bodies = (w["Close"] - w["Open"]).abs().sum()
    return StoryFacts(start.to_pydatetime(), end.to_pydatetime(), minutes, o, close, hi, t_hi.to_pydatetime(), lo, t_lo.to_pydatetime(),
                      (close - o) / PIP, (close - o) / a, (hi - lo) / a, (close - lo) / (hi - lo) if hi > lo else .5,
                      int((w["Close"] > w["Open"]).sum()), int((w["Close"] < w["Open"]).sum()), tuple(evs), tuple(tfs), (hi - lo) / PIP,
                      float(abs(close - o) / bodies) if bodies else 0.0, earlier, earlier_min, shortened)


def interaction(d5, now, price, minutes=STORY_MIN) -> Interaction:
    d = closed(d5, "5M", now); n = minutes // 5
    a = float(atr(d).iloc[-1]); w = d.iloc[-n:]
    close = float(w["Close"].iloc[-1]); side_above = close > price      # the side price is on NOW
    # approach from the side price started the window on
    start_side_above = float(w["Open"].iloc[0]) > price
    near = 0.25 * a
    touched = (w["Low"] <= price + near) & (w["High"] >= price - near)
    touches = int(touched.sum())
    through = bool(((w["Close"] < price) if start_side_above else (w["Close"] > price)).any())
    rej = int((touched & ((w["Close"] > price + near) if start_side_above else (w["Close"] < price - near))).sum())
    last = w.index[touched][-1].to_pydatetime() if touches else None
    return Interaction(price, touches, rej, through, last, abs(close - price) / a)
