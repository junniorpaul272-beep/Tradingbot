"""
timeref.py — turn "by 8:30", "between 8 and 8:30", "after the 8:30 break", "Tuesday", "yesterday" into a time window.
Pure stdlib; no clock (the caller passes `now`). Lagos time unless the message says utc/gmt.

NEVER A SILENT GUESS: every choice made for the user (which day, 24-hour or am/pm, how wide a stretch) is returned in
`assumptions`, and the caller says them out loud. A time that cannot be read returns None, not a default.

WINDOW RULES
  between A and B / from A to B   exactly [A, B]
  by T / up to T / until T        the hour BEFORE T ending at T            (assumed; said aloud)
  after T / since T               T forward 2 hours, never past `now`      (assumed; said aloud)
  around T / at T / T alone       30 minutes either side of T              (assumed; said aloud)
  a day alone (tuesday, today)    that whole day, Lagos midnight to midnight
  this morning / last night       06:00-12:00 / 19:00-24:00 of the day before
A time with no am/pm is read on a 24-hour clock (08:30 is half past eight in the morning) and says so.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LAGOS = ZoneInfo("Africa/Lagos")
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_T = r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?"
_TIME = re.compile(rf"\b{_T}\b", re.I)
_CLOCK = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b|\b(\d{1,2})\s*(am|pm)\b", re.I)
_BETWEEN = re.compile(rf"\b(?:between|from)\s+{_T}\s+(?:and|to|till|until|-)\s+{_T}\b", re.I)


class Window:
    def __init__(self, start, end, kind, assumptions, label_day, zone_label):
        self.start, self.end, self.kind = start, end, kind
        self.assumptions, self.label_day, self.zone_label = assumptions, label_day, zone_label

    def as_dict(self):
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "kind": self.kind,
                "assumptions": list(self.assumptions)}


def _zone(text):
    if re.search(r"\b(utc|gmt|zulu)\b", text, re.I):
        return timezone.utc, "UTC"
    return LAGOS, "Lagos time"


def _day(text, now, zone):
    """-> (date, said, assumption or None). `said` is True when the message named a day."""
    local = now.astimezone(zone).date()
    low = text.lower()
    if "yesterday" in low:
        return local - timedelta(days=1), True, None
    if re.search(r"\b(today|this morning|tonight|this afternoon|this evening)\b", low):
        return local, True, None
    if re.search(r"\blast night\b", low):
        return local - timedelta(days=1), True, None
    for i, d in enumerate(_DAYS):
        if re.search(rf"\b{d}\b", low):
            back = (local.weekday() - i) % 7
            if re.search(rf"\blast {d}\b", low) and back == 0:
                back = 7
            return local - timedelta(days=back), True, (f"I took {d.capitalize()} to be {local - timedelta(days=back):%d %b}"
                                                         if back else None)
    return local, False, None


def _hm(h, m, ap, assumptions, zone_label):
    h = int(h)
    m = int(m) if m else 0
    if ap:
        ap = ap.lower()
        if h < 1 or h > 12:
            return None
        h = (h % 12) + (12 if ap == "pm" else 0)
    else:
        if h > 23:
            return None
        if h <= 12 and "24-hour" not in " ".join(assumptions):
            assumptions.append(f"I read times on a 24-hour clock, {zone_label}")
    return (h, m) if m <= 59 else None


def parse(text, now, tz_default=None):
    """-> Window or None. `now` is a timezone-aware datetime."""
    raw = text or ""
    zone, zlabel = _zone(raw)
    day, day_said, day_note = _day(raw, now, zone)
    assumptions = []
    if day_note:
        assumptions.append(day_note)

    def at(h_m):
        return datetime(day.year, day.month, day.day, h_m[0], h_m[1], tzinfo=zone).astimezone(timezone.utc)

    def settle(dt):
        """A time that has not happened yet today means the last time it did, unless the message named the day."""
        if not day_said and dt > now:
            assumptions.append("that time hasn't happened yet today, so I used yesterday's")
            return dt - timedelta(days=1)
        return dt

    m = _BETWEEN.search(raw)
    if m:
        a = _hm(m.group(1), m.group(2), m.group(3), assumptions, zlabel)
        b = _hm(m.group(4), m.group(5), m.group(6) or m.group(3), assumptions, zlabel)
        if a and b:
            s, e = at(a), at(b)
            if e <= s:
                e += timedelta(days=1)
            s = settle(s)
            e = s + (at(b) - at(a) if at(b) > at(a) else at(b) - at(a) + timedelta(days=1))
            if e > now:
                e = now
            return Window(s, e, "between", assumptions, day, zlabel)
        return None

    c = _CLOCK.search(raw)
    if c:
        if c.group(1) is not None:
            hm = _hm(c.group(1), c.group(2), c.group(3), assumptions, zlabel)
        else:
            hm = _hm(c.group(4), None, c.group(5), assumptions, zlabel)
        if not hm:
            return None
        t = settle(at(hm))
        low = raw.lower()
        if re.search(r"\b(by|up to|until|till|before)\b", low):
            s, e, kind = t - timedelta(hours=1), t, "up_to"
            assumptions.append("you said 'by', so I looked at the hour leading up to it")
        elif re.search(r"\b(after|since|following|from)\b", low):
            s, e, kind = t, min(t + timedelta(hours=2), now), "after"
            assumptions.append("I followed it for two hours, or up to now if that's sooner")
        else:
            s, e, kind = t - timedelta(minutes=30), min(t + timedelta(minutes=30), now), "around"
            assumptions.append("I took half an hour either side of it")
        return Window(s, e, kind, assumptions, day, zlabel)

    low = raw.lower()
    if "this morning" in low:
        return Window(at((6, 0)), min(at((12, 0)), now), "day", assumptions + ["this morning = 06:00 to 12:00"], day, zlabel)
    if "last night" in low:
        return Window(at((19, 0)), at((23, 59)) + timedelta(minutes=1), "day", assumptions + ["last night = 19:00 to midnight"], day, zlabel)
    if day_said:
        s = at((0, 0))
        return Window(s, min(s + timedelta(days=1), now), "day", assumptions, day, zlabel)
    return None


def lagos(dt):
    return dt.astimezone(LAGOS)


def hm(dt):
    return lagos(dt).strftime("%H:%M")
