"""render.py - layer 3. Words only. It receives contracts.* objects (numbers, times, fixed codes) and returns Telegram HTML.
It does not read candles and does no market maths: every number comes from a fact, printed through R so the guard can prove it.
Tone: plain, a bit conversational, short blocks. Labels (Why / Right now / ...) keep it scannable."""
import re
from datetime import timedelta
from html import escape
from .contracts import Report

LAGOS = timedelta(hours=1)
SIDE_WORD = {"BUYERS": "buyers", "SELLERS": "sellers"}
OTHER = {"BUYERS": "sellers", "SELLERS": "buyers"}
DOT = {"BUYERS": "\U0001F7E2", "SELLERS": "\U0001F534", None: "⚪"}
BAND = {"UNUSUALLY_QUIET": "unusually quiet", "QUIETER": "quieter than usual", "NORMAL": "about normal", "BUSIER": "busier than usual",
        "UNUSUALLY_ACTIVE": "unusually busy"}
POS = {"NEAR_BOTTOM": "near the low", "LOWER_PART": "lower part", "MIDDLE": "middle", "UPPER_PART": "upper part", "NEAR_TOP": "near the high"}


class R:
    """Number/time printer. Records every figure it prints."""
    def __init__(self): self.tokens = set()
    def _reg(self, s):
        self.tokens.update(re.findall(r"\d+(?:\.\d+)?", s)); return s
    def n(self, x, nd=1, signed=False, unit=""):
        sign = ("+" if x > 0 else "−" if x < 0 else "") if signed else ("−" if x < 0 else "")
        return self._reg(f"{sign}{abs(x):.{nd}f}{unit}")
    def pips(self, x, nd=1, signed=False): return self.n(x, nd, signed, " pips")
    def p(self, x): return self._reg(f"{x:.5f}")
    def t(self, ts): d = ts + LAGOS; return self._reg(f"{d:%a} {d:%H:%M}")
    def c(self, ts): return self._reg(f"{ts + LAGOS:%H:%M}")
    def k(self, s): return self._reg(s)          # a constant label such as "1H" or "100-hour"


class Doc:
    """Builds a message from paragraphs; each paragraph records where it came from (the trace)."""
    def __init__(self): self.parts, self.trace, self.n = [], [], 0
    def head(self, text): self.parts.append(f"<b>{text}</b>")
    def sub(self, text): self.parts.append(text)
    def para(self, source, *lines):
        self.n += 1; self.trace.append((f"p{self.n}", source)); self.parts.append("\n".join(lines))
    def blank(self): self.parts.append("")
    def text(self):
        out = "\n".join(self.parts)
        return re.sub(r"\n{3,}", "\n\n", out).strip()


def _lbl(s): return f"<i>{s}</i>"


def _mom_line(r, o_m):
    f = o_m.facts
    s = f"{'up' if f['mom_net_pips'] > 0 else 'down'} {r.pips(abs(f['mom_net_pips']), 1)} over the last {r.k(str(int(f['mom_bars'])))} candles ({r.n(f['mom_up'], 0)} up, {r.n(f['mom_bars'] - f['mom_up'], 0)} down)"
    if o_m.code == "MOMENTUM_AGAINST": return f"Lately: {s}. {SIDE_WORD[o_m.side].capitalize()} have been gaining momentum."
    if o_m.code == "MOMENTUM_WITH": return f"Lately: {s}. Momentum is behind {SIDE_WORD[o_m.side]}."
    return f"Lately: {s}. No real momentum either way."


def _clk(r, t): return r.c(t)


def _tf_block(d, r, tf_facts, o_s, o_m):
    tf = tf_facts.tf; lb = tf_facts.last_break
    side = o_s.side; dot = DOT[side]
    last = (f"{'above' if lb.direction == 'UP' else 'below'} {r.p(lb.level)} at {r.t(lb.time)}" if lb else None)
    lastline = f" Last close through a swing level: {last}." if lb else ""
    if o_s.code == "STRUCTURE_HOLDS":
        head = f"{SIDE_WORD[side].capitalize()} have the stronger position"
        if o_m.code == "MOMENTUM_AGAINST": head += f", but {SIDE_WORD[o_m.side]} are gaining momentum"
        ev = f"Evidence: closed {last} (a {'high' if lb.direction=='UP' else 'low'} from {r.t(lb.level_time)}) and hasn't closed back through it."
    elif o_s.code == "NO_FOLLOW_THROUGH":
        head = f"{SIDE_WORD[side].capitalize()} broke a level but price is struggling to follow through"
        ev = f"Evidence: closed {last}, but it has only travelled {r.n(o_s.facts['beyond_atr'], 1)} average candles past it in {r.n(o_s.facts['bars_since'], 0)} candles."
    elif o_s.code == "STRUCTURE_NOT_HOLDING":
        head = f"Contested: {SIDE_WORD[side]} broke a level and price has since closed back through it"
        ev = f"Evidence: the break was {last}, but it isn't holding."
    elif o_s.code in ("CONTAINED", "CONTAINED_DEFENDED", "CONTAINED_DRIFTING"):
        f = o_s.facts
        box = f"Evidence: price has stayed between {r.p(f['range_low'])} and {r.p(f['range_high'])} for {r.k(str(int(f['bars'])))} candles."
        if o_s.code == "CONTAINED_DEFENDED":
            top = o_s.side == "SELLERS"
            n = f["tests_top"] if top else f["tests_bottom"]; other = f["tests_bottom"] if top else f["tests_top"]
            head = f"Price is contained, and {SIDE_WORD[o_s.side]} keep defending the {'top' if top else 'bottom'}"
            ev = box + f" The {'top' if top else 'bottom'} was tested and turned back {r.n(n, 0)} times, the {'bottom' if top else 'top'} {r.n(other, 0)}."
        elif o_s.code == "CONTAINED_DRIFTING":
            head = f"Price is contained, but {SIDE_WORD[o_s.side]} are gaining ground"
            ev = box + f" Closes have drifted toward the {'top' if o_s.side == 'BUYERS' else 'bottom'} of the range."
        else:
            head = "Price is bouncing inside a range, and neither edge has been defended more than the other"
            ev = box
        ev += lastline
    elif o_s.code == "PUSHING_NO_BREAK":
        head = f"{SIDE_WORD[side].capitalize()} are pushing, but no swing level has gone yet"
        ev = f"Evidence: net {r.n(abs(o_s.facts['push_net_atr']), 1)} average candles {'up' if side == 'BUYERS' else 'down'} over {r.k(str(int(o_s.facts['bars'])))} candles, with no close through a swing level."
    else:
        head = "No clear direction in this stretch"
        ev = "Evidence: no close through a swing level and no tight range."
    d.para(f"{tf}:{o_s.code}+{o_m.code}", f"{dot} <b>{r.k(tf)}</b> \u2014 {head}", ev, _mom_line(r, o_m))


def _level_text(r, it, up):
    """What a level is and what a close through it means. Wording depends on the role and on which side price is on now."""
    if it.role == "CONTINUATION": return f"The {'high' if up else 'low'} of the 1H move. Closing {'above' if up else 'below'} it carries the move further."
    if it.role == "CHANGE": return f"1H {'low' if up else 'high'} from {r.t(it.made_at)}. Closing {'below' if up else 'above'} it would start to change the 1H bias."
    if it.role == "BACKGROUND": return "Where the 1H move began. Price back through it would undo the whole bias. (Background, not an immediate level.)"
    if it.source == "1H": return f"{'Top' if it.kind == 'HIGH' else 'Bottom'} of the recent 1H range. A close outside it would give the 1H a direction."
    kind = "high" if it.kind == "HIGH" else "low"
    broken = (it.kind == "LOW" and it.direction == "ABOVE") or (it.kind == "HIGH" and it.direction == "BELOW")
    if not broken: return f"15M swing {kind} from {r.t(it.made_at)}. Closing {'below' if kind == 'low' else 'above'} it would show {'sellers' if kind == 'low' else 'buyers'} pushing on the 15M."
    return (f"15M swing {kind} from {r.t(it.made_at)}, already broken. Price is now {'below' if kind == 'low' else 'above'} it; "
            f"closing back {'above' if kind == 'low' else 'below'} it would show {'sellers' if kind == 'low' else 'buyers'} losing their grip.")


def _short(o):
    return {"STRUCTURE_HOLDS": f"{SIDE_WORD.get(o.side, '')} ahead", "NO_FOLLOW_THROUGH": f"{SIDE_WORD.get(o.side, '')} stalled", "STRUCTURE_NOT_HOLDING": "contested",
            "CONTAINED": "in a range", "CONTAINED_DEFENDED": f"in a range, {SIDE_WORD.get(o.side, '')} defending", "CONTAINED_DRIFTING": f"in a range, {SIDE_WORD.get(o.side, '')} gaining",
            "PUSHING_NO_BREAK": f"{SIDE_WORD.get(o.side, '')} pushing", "NO_CLEAR_DIRECTION": "no clear direction"}[o.code]


def _gap(r, now, as_of):
    """How old the newest candle is at the moment of the reply, in whole minutes. 'Answered' is when this reply was produced; the data stops at the candle time."""
    return r.n(max(0, int((now - as_of).total_seconds() // 60)), 0)


def _bias_section(d, r, bias):
    word = {"BULLISH": "bullish", "BEARISH": "bearish", "CONSOLIDATION": "no direction yet"}[bias.bias]
    d.head(f"The 1H picture: {word}")
    if bias.bias in ("BULLISH", "BEARISH"):
        up = bias.bias == "BULLISH"; ev = bias.evidence
        if ev:
            d.para("bias:evidence", f"{_lbl('Why:')} the last 1H close through a swing level was {'above' if ev.direction=='UP' else 'below'} {r.p(ev.level)} "
                   f"({r.t(ev.time)}), and price {'has held' if bias.held else 'has not held'} beyond it.")
        else:
            d.para("bias:evidence", f"{_lbl('Why:')} there has been no 1H close through a swing level in the last {r.k('72')} hours.")
        near = abs(bias.dist_extreme_pips)
        rngtxt = (f"price has been in a tight range for {r.k(str(bias.range_bars))} hours" if bias.is_range else "price is moving freely")
        d.para("bias:now", f"{_lbl('Right now:')} {rngtxt}, {r.pips(near, 1)} {'above' if bias.dist_extreme_pips > 0 else 'below'} the {'low' if not up else 'high'} of the move ({r.p(bias.extreme)}).")
        if bias.reversal:
            d.para("bias:change", f"{_lbl('What would change it:')} a 1H close {'below' if up else 'above'} {r.p(bias.reversal[0])} (the {'low' if up else 'high'} from {r.t(bias.reversal[1])}). "
                   f"A close {'above' if up else 'below'} {r.p(bias.extreme)} would carry the move further.")
        else:
            d.para("bias:change", f"{_lbl('What would change it:')} no confirmed 1H swing against the bias is on record yet.")
        d.para("bias:background", f"{_lbl('Background:')} price is {r.pips(abs(bias.ema_dist_pips), 0)} {bias.ema_side.lower()} its {r.k('100')}-hour average ({r.n(abs(bias.ema_dist_atr), 1)} average candles).")
    else:
        d.para("bias:none", f"The 1H has no confirmed direction. Last {r.k(str(bias.range_bars))} hours ranged {r.p(bias.range_low)} to {r.p(bias.range_high)}; a 1H close outside that would set one.")
    return word


def technical(now, bias, tf1, tf15, tf5, obs, levels):
    r, d = R(), Doc()
    d.head("GBPUSD \u00B7 technical read")
    d.sub(_lbl(f"Answered {r.c(now)} Lagos \u00B7 data up to {r.c(tf5.as_of)} (5M), {r.c(tf15.as_of)} (15M), {r.c(tf1.as_of)} (1H), so {_gap(r, now, tf5.as_of)} min old"))
    d.blank()
    word = _bias_section(d, r, bias)
    d.blank()
    d.head("What each timeframe is doing")
    d.blank()
    for tf, f in (("1H", tf1), ("15M", tf15), ("5M", tf5)):
        _tf_block(d, r, f, *obs[tf]); d.blank()
    d.head("Levels worth watching")
    up = bias.bias == "BULLISH"
    d.para("levels", *[f"\u2022 <code>{r.p(it.price)}</code> \u2014 {_level_text(r, it, up)}" for it in levels])
    d.blank()
    d.para("summary", f"<b>Bottom line:</b> {r.k('1H')} bias {word}. {r.k('1H')}: {_short(obs['1H'][0])}; {r.k('15M')}: {_short(obs['15M'][0])}; {r.k('5M')}: {_short(obs['5M'][0])}.")
    return d.text(), r, d.trace


SIZE_WORD = {"SMALL": "a small move", "MODEST": "a modest move", "SOLID": "a solid move", "SHARP": "a sharp move"}


def _what(r, it):
    if it.role == "CONTINUATION": return "the end of the 1H move"
    if it.role == "CHANGE": return f"1H swing from {r.t(it.made_at)}"
    if it.role == "BACKGROUND": return "where the 1H move began"
    if it.source == "1H": return f"{'top' if it.kind == 'HIGH' else 'bottom'} of the 1H range"
    return f"15M swing {'high' if it.kind == 'HIGH' else 'low'} from {r.t(it.made_at)}"


def _meaning(it, up):
    """What a close through the level would mean, as a short clause."""
    if it.role == "CONTINUATION": return f"a close {'above' if up else 'below'} it carries the move further"
    if it.role == "CHANGE": return f"a close {'below' if up else 'above'} it would start to change the 1H bias"
    if it.role == "BACKGROUND": return "price back through it would undo the whole bias"
    if it.source == "1H": return "a close outside the 1H range would give the 1H a direction"
    low = it.kind == "LOW"; broken = (low and it.direction == "ABOVE") or (not low and it.direction == "BELOW")
    if not broken: return f"a close {'below' if low else 'above'} it would show {'sellers' if low else 'buyers'} pushing on the 15M"
    return f"a close back {'above' if low else 'below'} it would show {'sellers' if low else 'buyers'} losing their grip"


def _why(r, it, ia, up):
    """Why this level is on the list, in terms of what price just did around it."""
    if ia.closed_through:
        return f"Price closed {'above' if it.direction == 'BELOW' else 'below'} it in the last half hour; {_meaning(it, up)}."
    if ia.touches and ia.rejections:
        return f"Price tested it {'once' if ia.touches == 1 else r.n(ia.touches, 0) + ' times'} in the last half hour and was turned back; {_meaning(it, up)}."
    if ia.touches:
        return f"Price touched it in the last half hour; {_meaning(it, up)}."
    if ia.distance_atr <= 2.0:
        return f"Price is close to it right now; {_meaning(it, up)}."
    m = _meaning(it, up)
    return m[0].upper() + m[1:] + "."


def now_report(now, bias, tf1, tf15, tf5, obs, levels, story, labels, inter):
    r, d = R(), Doc()
    d.head("GBPUSD \u00B7 right now")
    d.sub(_lbl(f"Answered {r.c(now)} Lagos \u00B7 data up to {r.c(tf5.as_of)}, so {_gap(r, now, tf5.as_of)} min old"))
    d.blank()
    d.head(f"The last {r.k(str(story.minutes))} minutes ({r.c(story.start)}\u2013{r.c(story.end)})")
    if story.shortened:
        d.para("story:shortened", f"<i>Some {r.k('5M')} candles are missing before {r.c(story.start)}, so this covers {r.k(str(story.minutes))} minutes instead of the usual {r.k('30')}.</i>")
    sz = SIZE_WORD[labels.size]; sh = labels.shape
    quiet = sh in ("SIDEWAYS", "CHOPPY")
    if sh == "SIDEWAYS":
        line1 = f"Price has barely moved. It stayed between {r.p(story.low)} and {r.p(story.high)} and ended at {r.p(story.close)}."
        lines = [line1]
    elif sh == "CHOPPY":
        line1 = f"Price went back and forth without picking a direction, between {r.p(story.low)} and {r.p(story.high)}, and ended at {r.p(story.close)}."
        lines = [line1, f"{r.n(story.n_up, 0)} of the {r.n(story.n_up + story.n_down, 0)} {r.k('5M')} candles closed up, and most of the movement cancelled out."]
    else:
        if sh == "UP":
            line1 = f"Price pushed up from {r.p(story.low)} ({r.c(story.low_time)}) to {r.p(story.high)} ({r.c(story.high_time)}) and is sitting near the top of that move at {r.p(story.close)}."
        elif sh == "UP_THEN_FADE":
            line1 = f"Price rose to {r.p(story.high)} at {r.c(story.high_time)} after starting near {r.p(story.low)}, then gave back most of it and is now at {r.p(story.close)}."
        elif sh == "DOWN":
            line1 = f"Price slid from {r.p(story.high)} ({r.c(story.high_time)}) to {r.p(story.low)} ({r.c(story.low_time)}) and is sitting near the bottom of that move at {r.p(story.close)}."
        else:
            line1 = f"Price dropped to {r.p(story.low)} at {r.c(story.low_time)} after starting near {r.p(story.high)}, then bounced back and is now at {r.p(story.close)}."
        lines = [line1, f"It was {sz} (about {r.n(story.range_pips, 0)} pips top to bottom), and {r.n(story.n_up, 0)} of the {r.n(story.n_up + story.n_down, 0)} {r.k('5M')} candles closed up."]
    d.para(f"story:{sh}+{labels.size}", *lines)
    if story.breaks:
        bl = sorted(zip(story.breaks, story.break_tfs), key=lambda x: x[0].time)[-2:]
        d.para("story:breaks", *[f"{r.k(tf)} closed {'above' if b.direction == 'UP' else 'below'} {r.p(b.level)} at {r.c(b.time)}." for b, tf in bl])
    if story.earlier and (quiet or not story.breaks):
        b, tf = story.earlier
        hrs, mins = divmod(story.earlier_minutes, 60)
        ago = (f"{r.n(hrs, 0)} h {r.n(mins, 0)} min" if hrs else f"{r.n(mins, 0)} min")
        d.para("story:earlier", f"The bigger event came earlier: {r.k(tf)} closed {'above' if b.direction == 'UP' else 'below'} {r.p(b.level)} at {r.c(b.time + timedelta(minutes={'5M': 5, '15M': 15, '1H': 60}[tf]))}, {ago} before the end of this window.")
    elif quiet and not story.breaks:
        d.para("story:none", f"No close through a swing level in the last {r.k('3')} hours either.")
    d.blank()
    d.head("Where that leaves things")
    word = {"BULLISH": "bullish", "BEARISH": "bearish", "CONSOLIDATION": "no direction yet"}[bias.bias]
    d.para("now:bias+tf", f"{r.k('1H')} bias: {word}. {r.k('1H')}: {_short(obs['1H'][0])}.", f"{r.k('15M')}: {_short(obs['15M'][0])}.", f"{r.k('5M')}: {_short(obs['5M'][0])}.")
    d.blank()
    d.head("What I'm watching, and why")
    up = bias.bias == "BULLISH"
    d.para("now:levels", *[f"\u2022 <code>{r.p(it.price)}</code> \u2014 {_what(r, it)}. {_why(r, it, ia, up)}" for it, ia in zip(levels, inter)])
    return d.text(), r, d.trace


PLAIN_EFF = {"CHOPPY": "That means price has been going back and forth a lot: only {e} of everything it moved became real progress.",
             "SOMEWHAT_DIRECTIONAL": "That means price has made some real progress, but with plenty of back-and-forth: {e} of everything it moved became progress.",
             "DIRECTIONAL": "That means a fairly clean move: {e} of everything it moved became real progress."}
PLAIN_BAND = {"UNUSUALLY_QUIET": "unusually small", "QUIETER": "smaller than usual", "NORMAL": "about normal", "BUSIER": "bigger than usual", "UNUSUALLY_ACTIVE": "unusually big"}
PLAIN_POS = {"ALL_LOW": "Price has been sliding: it sits near the bottom of its range over every window from a day to two weeks.",
             "ALL_HIGH": "Price has been climbing: it sits near the top of its range over every window from a day to two weeks.",
             "MIXED": "There's no single pattern: where price sits in its range depends on how far back you look."}


def measures(now, m, lab, plain):
    r, d = R(), Doc()
    d.head("GBPUSD \u00B7 market behaviour")
    d.sub(_lbl(f"Answered {r.c(now)} Lagos \u00B7 1H data up to {r.c(m.as_of)} \u00B7 {r.n(m.history_days / 7, 0)} weeks of history"))
    d.blank()
    d.head("How far price has moved")
    posd = dict(lab.positions)
    rows = ["<pre>window     move      range    sits in range"]
    for x in m.roc:
        if not x.available:
            rows.append(f"{r.k(x.window):<10} not enough history"); continue
        rows.append(f"{r.k(x.window):<10} {r.n(x.change_pct, 2, True, '%'):<9} {r.n(x.range_pct, 2, False, '%'):<8} {POS[posd[x.window]]}")
    rows.append("</pre>")
    d.para("roc", "\n".join(rows))
    two = next((x for x in m.roc if x.hours == 336 and x.available), None)
    pl = [f"{_lbl('In plain words:')} {PLAIN_POS[plain.positions]}"]
    if two:
        pl.append(f"Over 2 weeks price has moved {r.n(abs(two.change_pct), 2, False, '%')} (about {r.n(abs(two.change_pips), 0)} pips) {'down' if two.change_pips < 0 else 'up'}.")
    d.para("roc:plain", *pl)
    d.blank()
    d.head("How big the moves are")
    v = m.vol
    d.para("vol", f"\u2022 {_lbl('Hourly candles:')} about {r.n(v.hour_atr_pct_price, 2, False, '%')} ({r.n(v.hour_atr_pips, 0)} pips) each. That is {PLAIN_BAND[lab.hour_band]} for the last {r.n(v.hour_span_weeks, 0)} weeks.",
           f"\u2022 {_lbl('Daily range:')} about {r.n(v.day_atr_pct_price, 2, False, '%')} ({r.n(v.day_atr_pips, 0)} pips). That is {PLAIN_BAND[lab.day_band]} for the last {r.n(v.day_n, 0)} days.",
           f"\u2022 {_lbl('Weekly range:')} {r.n(v.week_avg_pct_price, 2, False, '%')} on average over {r.n(v.week_n, 0)} full weeks; {r.n(v.week_now_pct_price, 2, False, '%')} so far this week" + (", which is already more than a typical full week." if plain.week_now == "PAST_TYPICAL" else " (the week isn't finished)."))
    d.para("vol:plain", f"{_lbl('In plain words:')} when the daily number is bigger than usual, price is covering more ground each day than it normally does. When it is smaller, the market is quieter than normal.")
    d.blank()
    d.head(f"Who has been stronger (last {r.k('2')} weeks)")
    fl = m.flow
    if fl:
        go = "fell" if fl.net_pips < 0 else "rose"
        d.para("flow", f"Price {go} over the 2 weeks. {r.n(fl.n_up, 0)} hourly candles closed up and {r.n(fl.n_down, 0)} closed down.",
               f"Up candles averaged {r.pips(fl.up_body_mean_pips, 1)}; down candles averaged {r.pips(fl.down_body_mean_pips, 1)}.",
               {"DOWN_LARGER": "Down candles were bigger, so sellers have had the stronger side.", "UP_LARGER": "Up candles were bigger, so buyers have had the stronger side.",
                "SIMILAR": "Up and down candles were about the same size."}[lab.flow])
        d.para("flow:plain", f"{_lbl('In plain words:')} " + PLAIN_EFF[plain.efficiency].format(e=r.n(fl.efficiency * 100, 0, False, "%")))
    else:
        d.para("flow", "Not enough history for a 2-week read.")
    d.para("sentiment", f"{_lbl('Sentiment:')} not measured. No sentiment data is connected, so nothing is claimed about it.")
    d.blank()
    dir2 = {"UP": "up", "DOWN": "down", None: None}[lab.direction_2w]
    eff = {"CHOPPY": "choppy rather than a clean move", "SOMEWHAT_DIRECTIONAL": "somewhat directional", "DIRECTIONAL": "a clean, directional move"}[plain.efficiency]
    d.para("summary", "<b>Overall:</b> " + (f"over 2 weeks price is {dir2}, and it has been {eff}. " if dir2 else "") +
           f"Daily swings are {PLAIN_BAND[lab.day_band]} and hourly ones are {PLAIN_BAND[lab.hour_band]}. The last hour is {({'UP': 'up', 'DOWN': 'down', 'FLAT': 'flat'})[lab.direction_1h]}.")
    d.blank()
    d.sub(_lbl("Quick guide:"))
    d.sub(_lbl(f"\u2022 1 pip is {r.k('0.0001')}, the usual smallest price step. It is shown in brackets next to the % so you can see both."))
    d.sub(_lbl(f"\u2022 'Hourly candles' = the average size of one hourly candle, high to low, over the last {r.k('14')} (daily and weekly use the forex day, which opens Sunday {r.k('22:00')} UTC)."))
    d.sub(_lbl(f"\u2022 'Real progress' = net move divided by all the up and down candle movement added together."))
    return d.text(), r, d.trace


# ---------------------------------------------------------------- close-ups: /thesis 1h | 15m | 5m
CMD = {"gen": "/thesis_gen", "1H": "/thesis_1h", "15M": "/thesis_15m", "5M": "/thesis_5m"}


def thesis_menu(now):
    r, d = R(), Doc()
    d.head("GBPUSD \u00B7 close-ups")
    d.blank()
    d.para("menu:intro", "Pick the one you want to look at closely (tap it):")
    d.para("menu:items",
           f"{CMD['gen']} \u2014 the overall picture: the {r.k('1H')} direction and what each timeframe is doing",
           f"{CMD['1H']} \u2014 the {r.k('1H')} on its own: the direction, the proof and what would change it",
           f"{CMD['15M']} \u2014 the {r.k('15M')} on its own",
           f"{CMD['5M']} \u2014 the {r.k('5M')} on its own")
    d.para("menu:alt", f"You can also type it with a space, like /thesis {r.k('15m')}.")
    return d.text(), r, d.trace


def _holding_back(o_s, o_m):
    """Why the reading isn't stronger: one plain sentence per code."""
    c = o_s.code; s = SIDE_WORD.get(o_s.side, "")
    if c == "STRUCTURE_HOLDS":
        if o_m.code == "MOMENTUM_AGAINST": return f"The structure still favours {s}, but the latest candles have pushed the other way. That is the first thing to watch."
        return f"Nothing is holding it back right now: the structure and the latest push point the same way."
    if c == "NO_FOLLOW_THROUGH": return "The break hasn't proven much yet. Price has barely moved past the level, so it hasn't shown real commitment."
    if c == "STRUCTURE_NOT_HOLDING": return "Price came back through the level it broke, so that break is no longer standing."
    if c in ("CONTAINED", "CONTAINED_DEFENDED", "CONTAINED_DRIFTING"): return "No level has broken, so nothing is confirmed. The edges of the range are what matter."
    if c == "PUSHING_NO_BREAK": return "Price is pushing, but it hasn't closed through a swing level yet, so nothing is confirmed."
    return "Nothing here is confirmed either way."


def _changes(r, tf, f, o_s):
    """(what would change it, what would carry it further), as short clauses built from levels in the facts."""
    lb = f.last_break; c = o_s.code; side = o_s.side
    if c in ("STRUCTURE_HOLDS", "NO_FOLLOW_THROUGH") and lb:
        up = lb.direction == "UP"
        sw = f.swing_high if not up else f.swing_low      # the swing on the other side of price
        change = f"a {r.k(tf)} close back {'below' if up else 'above'} {r.p(lb.level)} would undo the break"
        if sw: change += f"; a close {'below' if up else 'above'} {r.p(sw[0])} (the {'low' if up else 'high'} from {r.t(sw[1])}) would turn the {r.k(tf)} the other way"
        far = f.swing_high if up else f.swing_low
        gap = f.dist_swing_high_pips if up else f.dist_swing_low_pips      # still ahead of price? (positive = not reached yet)
        if gap is None or gap <= 0: far = None
        further = (f"a {r.k(tf)} close {'above' if up else 'below'} {r.p(far[0])} (the {'high' if up else 'low'} from {r.t(far[1])}) would carry it further" if far else None)
        return change, further
    if c == "STRUCTURE_NOT_HOLDING" and lb:
        up = lb.direction == "UP"
        return None, f"a {r.k(tf)} close {'above' if up else 'below'} {r.p(lb.level)} again would put the break back in charge"
    if c in ("CONTAINED", "CONTAINED_DEFENDED", "CONTAINED_DRIFTING"):
        return None, (f"a {r.k(tf)} close above {r.p(o_s.facts['range_high'])} would favour buyers; a close below {r.p(o_s.facts['range_low'])} would favour sellers")
    parts = []
    if f.swing_high: parts.append(f"a close above {r.p(f.swing_high[0])} would favour buyers")
    if f.swing_low: parts.append(f"a close below {r.p(f.swing_low[0])} would favour sellers")
    return None, ("; ".join(parts) if parts else None)


def thesis_tf(now, bias, f, o_s, o_m, tf):
    r, d = R(), Doc()
    d.head(f"GBPUSD \u00B7 {r.k(tf)} close-up")
    d.sub(_lbl(f"Answered {r.c(now)} Lagos \u00B7 data up to {r.c(f.as_of)} ({r.k(tf)}), so {_gap(r, now, f.as_of)} min old"))
    d.blank()
    if tf == "1H":
        _bias_section(d, r, bias); d.blank()
    d.head("What it's saying")
    _tf_block(d, r, f, o_s, o_m); d.blank()
    d.head("The numbers behind it")
    lines = [f"\u2022 An average {r.k(tf)} candle is about {r.pips(f.atr_pips, 1)}. Price is at {r.p(f.close)}."]
    if f.swing_high and f.dist_swing_high_pips is not None:
        lines.append(f"\u2022 Latest swing high {r.p(f.swing_high[0])} ({r.t(f.swing_high[1])}): price is {r.pips(abs(f.dist_swing_high_pips), 1)} {'below' if f.dist_swing_high_pips >= 0 else 'above'} it.")
    if f.swing_low and f.dist_swing_low_pips is not None:
        lines.append(f"\u2022 Latest swing low {r.p(f.swing_low[0])} ({r.t(f.swing_low[1])}): price is {r.pips(abs(f.dist_swing_low_pips), 1)} {'above' if f.dist_swing_low_pips >= 0 else 'below'} it.")
    lines.append(f"\u2022 The last {r.k(str(f.contain_bars))} candles covered {r.pips(f.contain_range_pips, 0)} top to bottom, about {r.n(f.contain_range_atr, 1)} average candles.")
    d.para("tf:numbers", *lines); d.blank()
    d.head("What's holding it back")
    d.para("tf:holding", _holding_back(o_s, o_m)); d.blank()
    change, further = _changes(r, tf, f, o_s)
    if change or further:
        d.head("What would change it" if change else "If it breaks out")
        ls = []
        if change: ls.append(f"{_lbl('Weaken:')} {change}.")
        if further: ls.append(f"{_lbl('Strengthen:' if change else 'Next:')} {further}.")
        d.para("tf:changes", *ls)
    return d.text(), r, d.trace
