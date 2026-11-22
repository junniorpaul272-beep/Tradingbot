"""interpret.py - layer 2. Relationships BETWEEN facts, as codes. No prose and no market data access: it only sees TFFacts.
Every observation carries the numbers that justify it. Thresholds are named constants so they can be reviewed and changed in one place."""
from .contracts import TFFacts, Observation, MeasureLabels, MeasureFacts, LevelItem, StoryFacts, StoryLabels, PlainLabels, BiasFacts

MOM_STRONG_ATR = 1.0        # last 8 candles moved a full average candle or more
STALL_ATR = 0.5             # price is less than half an average candle past the level it broke
STALL_AFTER_BARS = 4        # ...and at least this many candles have passed since the break
CONTAINED_ATR = 3.0         # the recent range is no more than 3 average candles tall
PUSH_ATR = 2.0              # net move of 2+ average candles with no swing level closed through
DEFEND_MIN_TESTS = 3        # inside a range: an edge counts as defended after 3+ turned-back tests...
DEFEND_RATIO = 2            # ...and at least twice as many as the opposite edge
DRIFT_SHARE = 0.25          # otherwise closes drifting 25%+ of the range toward one edge count as one side gaining

# Momentum, exactly: mom_net_atr = (close of the latest candle - open of the candle MOM_BARS back) / ATR14 of that timeframe.
# It is a net displacement, not a sum of bodies. |mom_net_atr| >= MOM_STRONG_ATR counts as real momentum.


def _side(direction):
    return "BUYERS" if direction == "UP" else "SELLERS"


def observe(f: TFFacts):
    """Returns (structure observation, momentum observation). Always exactly one of each."""
    lb = f.last_break
    window_start_bars = None
    contained = f.contain_range_atr <= CONTAINED_ATR
    old_break = lb is None or f.bars_since_break is None or f.bars_since_break >= f.contain_bars
    facts = {"mom_net_pips": f.mom_net_pips, "mom_net_atr": f.mom_net_atr, "mom_up": f.mom_up, "mom_bars": f.mom_bars}
    if lb is None or (contained and old_break):
        if contained:
            base = {"range_atr": f.contain_range_atr, "bars": f.contain_bars, "range_high": f.range_high, "range_low": f.range_low,
                    "tests_top": f.tests_top, "tests_bottom": f.tests_bottom, "drift": f.drift}
            t, b = f.tests_top, f.tests_bottom
            if t >= DEFEND_MIN_TESTS and t >= DEFEND_RATIO * b:
                s = Observation(f.tf, "CONTAINED_DEFENDED", "SELLERS", base)       # the top keeps getting turned back by sellers
            elif b >= DEFEND_MIN_TESTS and b >= DEFEND_RATIO * t:
                s = Observation(f.tf, "CONTAINED_DEFENDED", "BUYERS", base)        # the bottom keeps getting held by buyers
            elif abs(f.drift) >= DRIFT_SHARE:
                s = Observation(f.tf, "CONTAINED_DRIFTING", "BUYERS" if f.drift > 0 else "SELLERS", base)
            else:
                s = Observation(f.tf, "CONTAINED", None, base)
        elif abs(f.push_net_atr) >= PUSH_ATR:
            s = Observation(f.tf, "PUSHING_NO_BREAK", "BUYERS" if f.push_net_atr > 0 else "SELLERS", {"push_net_atr": f.push_net_atr, "bars": f.push_bars})
        else:
            s = Observation(f.tf, "NO_CLEAR_DIRECTION", None, {})
        mom_ref = None
    else:
        side = _side(lb.direction)
        if not f.held:
            s = Observation(f.tf, "STRUCTURE_NOT_HOLDING", side, {"level": lb.level})
        elif f.bars_since_break >= STALL_AFTER_BARS and f.beyond_level_atr < STALL_ATR:
            s = Observation(f.tf, "NO_FOLLOW_THROUGH", side, {"level": lb.level, "beyond_atr": f.beyond_level_atr, "bars_since": f.bars_since_break})
        else:
            s = Observation(f.tf, "STRUCTURE_HOLDS", side, {"level": lb.level, "beyond_atr": f.beyond_level_atr})
        mom_ref = lb.direction
    # momentum, described relative to the structural side if there is one
    mom_dir = "UP" if f.mom_net_atr > 0 else "DOWN"
    if abs(f.mom_net_atr) < MOM_STRONG_ATR:
        m = Observation(f.tf, "MOMENTUM_FLAT", None, facts)
    elif mom_ref is None or mom_dir == mom_ref:
        m = Observation(f.tf, "MOMENTUM_WITH", _side(mom_dir), facts)
    else:
        m = Observation(f.tf, "MOMENTUM_AGAINST", _side(mom_dir), facts)
    return s, m


def _band(p):
    return ("UNUSUALLY_QUIET" if p < .10 else "QUIETER" if p < .30 else "NORMAL" if p < .70 else "BUSIER" if p < .90 else "UNUSUALLY_ACTIVE")


def _pos(p):
    return "NEAR_TOP" if p >= .8 else "UPPER_PART" if p >= .6 else "MIDDLE" if p > .4 else "LOWER_PART" if p > .2 else "NEAR_BOTTOM"


SIMILAR_WITHIN = 0.10      # average up and down candle bodies within 10% of each other count as similar


def label_measures(m: MeasureFacts) -> MeasureLabels:
    flow = None
    if m.flow:
        u, d = m.flow.up_body_mean_pips, m.flow.down_body_mean_pips
        big = max(u, d)
        flow = "SIMILAR" if big == 0 or abs(u - d) / big <= SIMILAR_WITHIN else ("UP_LARGER" if u > d else "DOWN_LARGER")
    two = next((r for r in m.roc if r.hours == 336 and r.available), None)
    hr = next(r for r in m.roc if r.hours == 1)
    return MeasureLabels(_band(m.vol.hour_pct), _band(m.vol.day_pct), tuple((r.window, _pos(r.position)) for r in m.roc if r.available), flow,
                         None if two is None else ("UP" if two.change_pips > 0 else "DOWN"),
                         "FLAT" if (not hr.available or hr.change_pips == 0) else ("UP" if hr.change_pips > 0 else "DOWN"))


# ---------------- level selection ----------------
MERGE_ATR15 = 0.5     # a 15M swing within half a 15M candle of another selected level is the same level


def select_levels(bias: BiasFacts, tf15: TFFacts, close: float, detail=False):
    """The rule (documented, not a truncation): at most one level per ROLE, in this order.
      BOUNDARY     - the confirmed 15M swing (high or low) nearest to the latest close that is not the same level as the two below.
      CONTINUATION - 1H bias only: the far end of the bias move (a close through it carries the move further).
      CHANGE       - 1H bias only: the latest confirmed 1H swing against the bias (a close through it starts to change the 1H bias).
      BACKGROUND   - /levels only: where the bias move began.
    With no 1H direction, the two edges of the 1H range are the CONTINUATION/CHANGE stand-ins (ROLE BOUNDARY for both) and the nearest 15M swing is added.
    A role with no level is left out, never replaced by a different level."""
    out = []
    side = lambda p: "ABOVE" if p > close else "BELOW"
    tol = MERGE_ATR15 * tf15.atr_pips * 0.0001
    if bias.bias in ("BULLISH", "BEARISH"):
        fixed = []
        fixed.append(LevelItem(bias.extreme, "CONTINUATION", "MOVE", None, side(bias.extreme)))
        if bias.reversal: fixed.append(LevelItem(bias.reversal[0], "CHANGE", "1H", bias.reversal[1], side(bias.reversal[0])))
    else:
        fixed = [LevelItem(bias.range_high, "BOUNDARY", "1H", None, side(bias.range_high), "HIGH"), LevelItem(bias.range_low, "BOUNDARY", "1H", None, side(bias.range_low), "LOW")]
    cands = [(x[0], x[1], k) for x, k in ((tf15.swing_high, "HIGH"), (tf15.swing_low, "LOW")) if x]
    cands = [c for c in cands if all(abs(c[0] - l.price) > tol for l in fixed)]
    cands.sort(key=lambda c: abs(c[0] - close))
    if cands: out.append(LevelItem(cands[0][0], "BOUNDARY", "15M", cands[0][1], side(cands[0][0]), cands[0][2]))
    out += fixed
    if detail and bias.bias in ("BULLISH", "BEARISH"): out.append(LevelItem(bias.origin, "BACKGROUND", "MOVE", None, side(bias.origin)))
    return out


# ---------------- last-30-minutes story ----------------
SIDEWAYS_ATR = 1.5      # a 30-minute range under 1.5 average 5M candles is "not much happened"
SIZE_ATR = (3.0, 5.0)   # range in average candles: under 1.5 small / under 3 modest / under 5 solid / otherwise sharp
CHOPPY_EFF = 0.25       # net move under 25% of all candle movement in the window, with both up and down candles = back-and-forth, not a direction
NEAR_ATR = 1.0          # price within one average 5M candle of a level counts as "at" it


def label_story(s: StoryFacts) -> StoryLabels:
    if s.range_atr < SIDEWAYS_ATR: shape = "SIDEWAYS"
    elif s.efficiency < CHOPPY_EFF and s.n_up >= 2 and s.n_down >= 2 and 0.3 <= s.close_pos <= 0.7: shape = "CHOPPY"   # ended mid-range after going both ways; a clear rise-then-fade is a real story, not chop
    elif s.low_time < s.high_time: shape = "UP" if s.close_pos >= 0.6 else "UP_THEN_FADE"
    elif s.high_time < s.low_time: shape = "DOWN" if s.close_pos <= 0.4 else "DOWN_THEN_BOUNCE"
    else: shape = "UP" if s.close_pos >= .5 else "DOWN"
    r = s.range_atr
    size = "SMALL" if r < SIDEWAYS_ATR else "MODEST" if r < SIZE_ATR[0] else "SOLID" if r < SIZE_ATR[1] else "SHARP"
    return StoryLabels(shape, size)


# ---------------- plain-language classes for the quantitative report ----------------
EFF_CHOPPY = 0.15       # net progress under 15% of all movement = mostly back-and-forth
EFF_DIRECTIONAL = 0.35  # over 35% = a fairly clean move


def plain_labels(m: MeasureFacts) -> PlainLabels:
    eff = m.flow.efficiency if m.flow else 0.0
    e = "CHOPPY" if eff < EFF_CHOPPY else "SOMEWHAT_DIRECTIONAL" if eff < EFF_DIRECTIONAL else "DIRECTIONAL"
    long = [r for r in m.roc if r.available and r.hours >= 24]
    pos = "ALL_LOW" if long and all(r.position <= .3 for r in long) else "ALL_HIGH" if long and all(r.position >= .7 for r in long) else "MIXED"
    two = next((r for r in m.roc if r.hours == 336 and r.available), None)
    # an unfinished week's range can only grow, so "already past the average full week" is a fair comparison; "below" is not (it may not be finished)
    wk = "PAST_TYPICAL" if m.vol.week_now_pips > m.vol.week_avg_pips else "NOT_YET"
    return PlainLabels(e, pos, wk)
