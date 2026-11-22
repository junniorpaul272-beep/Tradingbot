"""contracts.py - the ONLY shapes allowed to cross between layers. Numbers, times and fixed codes. No free-text field exists anywhere in here.
  raw candles -> facts.py -> (TFFacts, BiasFacts, MeasureFacts) -> interpret.py -> Observation -> render.py -> Report -> Telegram
Each layer imports this file and nothing from the layers on either side of it (enforced by test_report.py)."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple, Dict

SIDES = ("BUYERS", "SELLERS")
OBS_CODES = ("STRUCTURE_HOLDS", "STRUCTURE_NOT_HOLDING", "NO_FOLLOW_THROUGH", "CONTAINED", "CONTAINED_DEFENDED", "CONTAINED_DRIFTING", "PUSHING_NO_BREAK", "NO_CLEAR_DIRECTION",
             "MOMENTUM_WITH", "MOMENTUM_AGAINST", "MOMENTUM_FLAT")


class ContractError(Exception):
    pass


@dataclass(frozen=True)
class Break:
    direction: str          # UP | DOWN
    level: float
    time: datetime          # close that went through the level
    level_time: datetime    # when that level was made


@dataclass(frozen=True)
class TFFacts:
    tf: str                 # 5M | 15M | 1H
    as_of: datetime         # close time of the newest candle used
    close: float
    atr_pips: float
    last_break: Optional[Break]
    held: Optional[bool]
    bars_since_break: Optional[int]
    beyond_level_atr: Optional[float]   # how far past the broken level price is, in average candles (signed toward the break direction)
    mom_bars: int
    mom_net_pips: float
    mom_net_atr: float
    mom_up: int
    contain_bars: int
    contain_range_atr: float            # high-low range over contain_bars, in average candles
    contain_range_pips: float
    push_bars: int
    push_net_atr: float                 # net close-to-close move over push_bars, in average candles (signed)
    swing_high: Optional[Tuple[float, datetime]]
    swing_low: Optional[Tuple[float, datetime]]
    range_high: float = 0.0           # high/low over contain_bars
    range_low: float = 0.0
    tests_top: int = 0                # candles that reached the top quarter-candle of the range and closed back at least half a candle below it
    tests_bottom: int = 0
    drift: float = 0.0                # mean close of the last third minus the first third of the window, as a share of the range (-1..1)
    dist_swing_high_pips: Optional[float] = None   # swing high minus close, in pips (negative once price is above it)
    dist_swing_low_pips: Optional[float] = None    # close minus swing low, in pips (negative once price is below it)


@dataclass(frozen=True)
class BiasFacts:
    bias: str               # BULLISH | BEARISH | CONSOLIDATION  (the one value that comes from the engine)
    extreme: float          # engine: far end of the bias move
    origin: float           # engine: where the bias move began
    evidence: Optional[Break]
    held: Optional[bool]
    reversal: Optional[Tuple[float, datetime]]    # latest confirmed 1H swing against the bias
    range_bars: int
    range_high: float
    range_low: float
    is_range: bool
    ema_side: str           # ABOVE | BELOW
    ema_dist_pips: float
    ema_dist_atr: float
    atr_pips: float
    close: float
    dist_extreme_pips: float    # close minus the far end of the bias move, in pips (signed: price above = positive)


@dataclass(frozen=True)
class Observation:
    scope: str              # 1H | 15M | 5M
    code: str               # one of OBS_CODES
    side: Optional[str]     # BUYERS | SELLERS | None
    facts: Dict[str, float] = field(default_factory=dict)   # the numbers that justify it

    def __post_init__(self):
        if self.code not in OBS_CODES:
            raise ContractError(f"unknown observation code {self.code!r}")
        if self.side not in (None,) + SIDES:
            raise ContractError(f"bad side {self.side!r}")


@dataclass(frozen=True)
class RocRow:
    window: str
    hours: int
    available: bool
    change_pips: float = 0.0
    change_pct: float = 0.0
    range_pips: float = 0.0
    range_pct: float = 0.0
    position: float = 0.5   # 0 = at the low of the window, 1 = at the high


@dataclass(frozen=True)
class FlowFacts:
    hours: int
    n_candles: int
    n_up: int
    n_down: int
    up_body_total_pips: float
    down_body_total_pips: float
    up_body_mean_pips: float
    down_body_mean_pips: float
    net_pips: float
    efficiency: float       # |net| / (all up bodies + all down bodies); 1 = straight line, near 0 = lots of back and forth


@dataclass(frozen=True)
class VolFacts:
    hour_atr_pips: float
    hour_pct: float
    hour_span_weeks: int
    day_atr_pips: float
    day_pct: float
    day_n: int
    week_avg_pips: float
    week_n: int
    week_now_pips: float
    hour_atr_pct_price: float = 0.0     # the same sizes as a percentage of price
    day_atr_pct_price: float = 0.0
    week_avg_pct_price: float = 0.0
    week_now_pct_price: float = 0.0


@dataclass(frozen=True)
class MeasureFacts:
    as_of: datetime
    roc: Tuple[RocRow, ...]
    flow: Optional[FlowFacts]
    vol: VolFacts
    history_days: int


@dataclass(frozen=True)
class Report:
    kind: str
    messages: Tuple[str, ...]      # Telegram HTML, each under the length limit
    trace: Tuple[Tuple[str, str], ...]   # (paragraph id, source: observation code / fact name) for every paragraph
    status: str


@dataclass(frozen=True)
class BiasInput:
    """What passes from the engine into a report: one enum and two prices. Nothing else. (source.bias_from_state reads two more keys, the stale flag and the data time, but only to decide whether to refuse; they never pass this line.)"""
    bias: str
    leg_origin: float
    leg_extreme: float

    def __post_init__(self):
        import math
        if self.bias not in ("BULLISH", "BEARISH", "CONSOLIDATION"):
            raise ContractError(f"bias must be BULLISH/BEARISH/CONSOLIDATION, got {self.bias!r}")
        for v in (self.leg_origin, self.leg_extreme):
            if not isinstance(v, (int, float)) or isinstance(v, bool) or math.isnan(v) or not (0.5 < v < 5):
                raise ContractError(f"price out of range: {v!r}")
        # the two prices must agree with the direction: a bearish move began high and has gone lower, a bullish one the reverse
        if self.bias == "BEARISH" and not self.leg_origin > self.leg_extreme:
            raise ContractError("a bearish bias needs the start of the move above its far end")
        if self.bias == "BULLISH" and not self.leg_origin < self.leg_extreme:
            raise ContractError("a bullish bias needs the start of the move below its far end")


# Fixed label vocabularies produced by interpret.py and turned into words by render.py
BAND_CODES = ("UNUSUALLY_QUIET", "QUIETER", "NORMAL", "BUSIER", "UNUSUALLY_ACTIVE")
POS_CODES = ("NEAR_BOTTOM", "LOWER_PART", "MIDDLE", "UPPER_PART", "NEAR_TOP")
FLOW_CODES = ("DOWN_LARGER", "UP_LARGER", "SIMILAR")


@dataclass(frozen=True)
class MeasureLabels:
    hour_band: str
    day_band: str
    positions: Tuple[Tuple[str, str], ...]     # (window name, POS code) for available windows
    flow: Optional[str]
    direction_2w: Optional[str]                # UP | DOWN | None if the 2-week window is unavailable
    direction_1h: str                          # UP | DOWN | FLAT


@dataclass(frozen=True)
class LevelItem:
    price: float
    role: str               # BOUNDARY | CONTINUATION | CHANGE | BACKGROUND
    source: str             # 5M | 15M | 1H | MOVE
    made_at: Optional[datetime]
    direction: str          # ABOVE | BELOW  (relative to the latest close)
    kind: Optional[str] = None   # HIGH | LOW for swing levels


@dataclass(frozen=True)
class Interaction:
    """What the last window of 5M candles did around one level."""
    price: float
    touches: int            # candles whose range came within a quarter of an average 5M candle of the level (or through it)
    rejections: int         # touches that closed back on the original side
    closed_through: bool    # any close beyond the level in the window
    last_touch: Optional[datetime]
    distance_atr: float     # latest close to the level, in average 5M candles


@dataclass(frozen=True)
class StoryFacts:
    start: datetime         # open time of the first candle in the window
    end: datetime           # close time of the last candle
    minutes: int
    open: float
    close: float
    high: float
    high_time: datetime
    low: float
    low_time: datetime
    net_pips: float
    net_atr: float          # net move in average 5M candles
    range_atr: float
    close_pos: float        # where the last close sits in the window's range, 0 = low, 1 = high
    n_up: int
    n_down: int
    breaks: Tuple["Break", ...]       # closes through swing levels (5M or 15M) that happened inside the window
    break_tfs: Tuple[str, ...]
    range_pips: float = 0.0
    efficiency: float = 0.0           # |net| / sum of candle bodies inside the window
    earlier: Optional[Tuple["Break", str]] = None     # the most important close through a swing level in the 3 hours before the window
    earlier_minutes: int = 0          # how long before the end of the window it happened
    shortened: bool = False           # True when gaps in the 5M candles forced a window shorter than the 30 minutes asked for


@dataclass(frozen=True)
class StoryLabels:
    shape: str              # SIDEWAYS | CHOPPY | UP | UP_THEN_FADE | DOWN | DOWN_THEN_BOUNCE
    size: str               # SMALL | MODEST | SOLID | SHARP


@dataclass(frozen=True)
class PlainLabels:
    """Plain-language classes for the quantitative report."""
    efficiency: str         # CHOPPY | SOMEWHAT_DIRECTIONAL | DIRECTIONAL
    positions: str          # ALL_LOW | ALL_HIGH | MIXED  (windows of a day and longer)
    week_now: str           # PAST_TYPICAL | NOT_YET: this unfinished week's range so far against the average full week
