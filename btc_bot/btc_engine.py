"""
btc_engine.py: Turtle Breakout engine, long AND short. Pure logic: no network, no files, no wording.

Rules (a faithful port of the "Turtle Breakout - Revelio" Pine v6 indicator):
  * Trading days run 17:00 -> 17:00 New York. Bands and ATR use COMPLETED days only.
  * LONG:  price trades above the highest high of the last N (20) days.
           Stop = entry reference - 2 ATR (20-day, Wilder), fixed. Trailing exit = lowest low of the last M (10) days.
           The trade exits at whichever of the two is HIGHER.
  * SHORT: the exact mirror. Price trades below the lowest low of the last N days.
           Stop = entry reference + 2 ATR. Trailing exit = highest high of the last M days.
           The trade exits at whichever of the two is LOWER.
  * Day-trading mode: close at 16:30 NY, re-enter from 18:00 NY if the signal is still valid, flat at the weekend.

The short side is not a second implementation: it is the tested long engine fed mirrored prices
(p -> -p, highs and lows swapped), then flipped back. A new rule can therefore never drift between sides.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
ROLLOVER_M = 17 * 60  # trading day rolls at 17:00 New York


# --------------------------------------------------------------------------- types
@dataclass
class Config:
    n: int = 20
    m: int = 10
    atr_len: int = 20
    atr_mult: float = 2.0
    day_trading: bool = True
    close_h: int = 16
    close_min: int = 30
    open_h: int = 18
    open_min: int = 0
    interval_min: int = 30

    def validate(self):
        if self.interval_min not in (1, 5, 15, 30):
            raise ValueError("INTERVAL_MIN must be 1, 5, 15 or 30 (same as the Pine script).")
        if self.day_trading:
            c, o = self.close_h * 60 + self.close_min, self.open_h * 60 + self.open_min
            if c >= o:
                raise ValueError("The close time must be earlier than the re-entry time.")
            if c % self.interval_min or o % self.interval_min:
                raise ValueError("Close and re-entry times must fall on a candle boundary.")


@dataclass
class Bar:
    t: int  # candle OPEN time, epoch seconds (UTC)
    o: float
    h: float
    l: float
    c: float


@dataclass
class Event:
    kind: str          # ENTRY | REENTRY | CLOSE | EXIT
    t: int             # bar open time
    close_time: int    # bar close time (when the alert is confirmed)
    price: float
    entry_level: float
    exit_level: float
    stop: float
    atr: float
    active_stop: float
    late: bool = False   # CLOSE caught up after a missed close
    flat: bool = False   # EXIT while already flat by schedule
    side: str = "LONG"   # LONG | SHORT

    @property
    def event_id(self):
        return f"{self.side}:{self.kind}:{self.t}"


def ny(t):
    return datetime.fromtimestamp(t, NY)


def deadline_for(t, cfg):
    """Next scheduled close (cfg.close_h:close_min NY) on a weekday, as epoch seconds."""
    loc = ny(t)
    mins = loc.hour * 60 + loc.minute
    d = loc.date() + timedelta(days=1 if mins >= cfg.close_h * 60 + cfg.close_min else 0)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return int(datetime(d.year, d.month, d.day, cfg.close_h, cfg.close_min, tzinfo=NY).timestamp())


# --------------------------------------------------------------------------- the long engine
class Engine:
    """Bar-by-bar port of the Pine script (long side). Feed CLOSED bars in time order with step()."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.close_m = cfg.close_h * 60 + cfg.close_min
        self.open_m = cfg.open_h * 60 + cfg.open_min
        self.step_s = cfg.interval_min * 60
        self.highs, self.lows = [], []
        self.atr = None
        self._atr_seed, self._atr_count = 0.0, 0
        self.d_high = self.d_low = self.d_close = None
        self.prev_close = None
        self.skip_first = True
        self.sess_key = None
        self.signal_long = False
        self.held = False
        self.stop = None
        self.deadline = None
        self.entry_level = None
        self.exit_level = None
        self.trades = []
        self._open = None
        self.last_bar = None

    def _start(self, t, price):
        self._open = {"entry_t": t, "entry_price": price}

    def _finish(self, t, price, kind):
        if self._open is not None:
            self.trades.append({**self._open, "exit_t": t, "exit_price": price, "kind": kind, "side": "LONG"})
            self._open = None

    def step(self, b):
        cfg = self.cfg
        evs = []
        loc = ny(b.t)
        nymin = loc.hour * 60 + loc.minute
        dow = loc.weekday()  # Mon=0 ... Fri=4, Sat=5, Sun=6
        sess = loc.date() - timedelta(days=1) if nymin < ROLLOVER_M else loc.date()
        new_session = self.sess_key is None or sess != self.sess_key
        self.sess_key = sess
        self.last_bar = b

        # Daily aggregation: only COMPLETED days feed the bands and the ATR.
        if new_session:
            if self.d_high is not None:
                if self.skip_first:  # the first loaded day is usually incomplete
                    self.skip_first = False
                else:
                    self.highs.append(self.d_high)
                    if len(self.highs) > cfg.n:
                        self.highs.pop(0)
                    self.lows.append(self.d_low)
                    if len(self.lows) > cfg.m:
                        self.lows.pop(0)
                    rng = self.d_high - self.d_low
                    if self.prev_close is None:
                        tr = rng
                    else:
                        tr = max(rng, abs(self.d_high - self.prev_close), abs(self.d_low - self.prev_close))
                    if self.atr is None:
                        self._atr_seed += tr
                        self._atr_count += 1
                        if self._atr_count == cfg.atr_len:
                            self.atr = self._atr_seed / cfg.atr_len
                    else:
                        self.atr = (self.atr * (cfg.atr_len - 1) + tr) / cfg.atr_len
                    self.prev_close = self.d_close
            self.d_high, self.d_low, self.d_close = b.h, b.l, b.c
        else:
            self.d_high = max(self.d_high, b.h)
            self.d_low = min(self.d_low, b.l)
            self.d_close = b.c

        self.entry_level = max(self.highs) if len(self.highs) == cfg.n else None
        self.exit_level = min(self.lows) if len(self.lows) == cfg.m else None
        allowed = (not cfg.day_trading) or (
            (nymin < self.close_m or nymin >= self.open_m)
            and dow != 5
            and not (dow == 6 and nymin < self.open_m)
            and not (dow == 4 and nymin >= self.close_m)
        )

        if self.entry_level is None or self.exit_level is None or self.atr is None:
            return evs  # still warming up

        def mk(kind, price, **kw):
            active = max(self.stop, self.exit_level) if self.stop is not None else self.exit_level
            return Event(kind, b.t, b.t + self.step_s, price, self.entry_level, self.exit_level,
                         self.stop, self.atr, active, **kw)

        was_long = self.signal_long
        breakout = (not was_long) and b.h > self.entry_level
        exit_event = False
        signal_exit = None
        exit_ev_active = None
        if breakout:
            self.stop = max(self.entry_level, b.o) - cfg.atr_mult * self.atr
            self.signal_long = True
            exit_event = b.l < self.stop
        elif was_long:
            exit_event = b.l < max(self.stop, self.exit_level)
        if exit_event:
            signal_exit = min(b.o, max(self.stop, self.exit_level)) if was_long else self.stop
            exit_ev_active = max(self.stop, self.exit_level)
            self.signal_long = False

        # Missed scheduled close (gap in the data, weekend, etc.)
        if cfg.day_trading and self.held and b.t >= self.deadline:
            self._finish(b.t, b.o, "CLOSE")
            self.held = False
            evs.append(mk("CLOSE", b.o, late=True))

        if not self.held and allowed:
            entry_event = breakout
            reentry_event = was_long and b.o >= max(self.stop, self.exit_level)
            if entry_event or reentry_event:
                price = max(self.entry_level, b.o) if breakout else b.o
                self.held = True
                self.deadline = deadline_for(b.t, cfg)
                self._start(b.t, price)
                evs.append(mk("ENTRY" if entry_event else "REENTRY", price))

        shade_held = self.held

        # Re-entry uses the open, so a stop hit later in the same candle still closes it.
        if self.held:
            if exit_event:
                self._finish(b.t, signal_exit, "EXIT")
                self.held = False
            elif cfg.day_trading and b.t + self.step_s == self.deadline:
                self._finish(b.t, b.c, "CLOSE")
                self.held = False
                evs.append(mk("CLOSE", b.c))

        if exit_event:
            price = signal_exit if shade_held else exit_ev_active
            evs.append(mk("EXIT", price, flat=not shade_held))
        return evs

    def status(self):
        active = None
        if self.signal_long and self.stop is not None and self.exit_level is not None:
            active = max(self.stop, self.exit_level)
        return {
            "side": "LONG",
            "ready": self.atr is not None and self.entry_level is not None and self.exit_level is not None,
            "signal_on": self.signal_long,
            "held": self.held,
            "entry_level": self.entry_level,   # the trigger: highest high of the last N days
            "exit_level": self.exit_level,     # the trailing band: lowest low of the last M days
            "atr": self.atr,
            "stop": self.stop if self.signal_long else None,
            "active_stop": active,
            "last_close": self.last_bar.c if self.last_bar else None,
            "completed_days": len(self.highs),
        }


# --------------------------------------------------------------------------- the short engine (mirror)
def _neg(x):
    return None if x is None else -x


def mirror_bar(b):
    return Bar(b.t, -b.o, -b.l, -b.h, -b.c)


class ShortEngine:
    """Short side: the long engine run on mirrored prices, flipped back on the way out."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._e = Engine(cfg)

    def step(self, b):
        return [self._flip(e) for e in self._e.step(mirror_bar(b))]

    @staticmethod
    def _flip(e):
        return Event(e.kind, e.t, e.close_time, _neg(e.price), _neg(e.entry_level), _neg(e.exit_level),
                     _neg(e.stop), e.atr, _neg(e.active_stop), e.late, e.flat, "SHORT")

    @property
    def trades(self):
        return [{**t, "entry_price": _neg(t["entry_price"]), "exit_price": _neg(t["exit_price"]), "side": "SHORT"}
                for t in self._e.trades]

    def status(self):
        s = self._e.status()
        return {
            "side": "SHORT", "ready": s["ready"], "signal_on": s["signal_on"], "held": s["held"],
            "entry_level": _neg(s["entry_level"]),   # the trigger: lowest low of the last N days
            "exit_level": _neg(s["exit_level"]),     # the trailing band: highest high of the last M days
            "atr": s["atr"], "stop": _neg(s["stop"]), "active_stop": _neg(s["active_stop"]),
            "last_close": _neg(s["last_close"]), "completed_days": s["completed_days"],
        }


# --------------------------------------------------------------------------- running both sides
_EXITISH = ("CLOSE", "EXIT")


def _order(el, es):
    """Same-candle events from both sides: a side that is leaving goes first, so reversals read naturally."""
    if es and es[0].kind in _EXITISH and not (el and el[0].kind in _EXITISH):
        return es + el
    return el + es


def run_both(bars, cfg, keep_last=300):
    """Replay `bars` through both engines. Returns (long_engine, short_engine, events, snaps).
    snaps maps a candle's close time -> {"LONG": status, "SHORT": status} for the last `keep_last` candles."""
    lg, sh = Engine(cfg), ShortEngine(cfg)
    step_s = cfg.interval_min * 60
    events, snaps = [], {}
    n = len(bars)
    for i, b in enumerate(bars):
        events.extend(_order(lg.step(b), sh.step(b)))
        if i >= n - keep_last:
            snaps[b.t + step_s] = {"LONG": lg.status(), "SHORT": sh.status()}
    return lg, sh, events, snaps
