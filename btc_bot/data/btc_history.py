"""
btc_history.py: download a long stretch of candles and keep it as a CSV, so a replay can start from months of data
instead of waiting for the live record to build up.

Twelve Data returns at most 5000 candles per request (about 104 days of 30-minute candles), so longer history is
fetched in pages walking backwards with end_date. Each page is one request; a free plan allows 8 a minute, so the
download pauses between pages. The CSV is UTC: datetime,open,high,low,close.

Note: the LIVE bot does not need this to start. Every live run already pulls the latest 5000 candles (about 104 days),
far more than the ~22 completed trading days the bands and ATR need. This is for backtesting over longer periods.
History replays go to their own files and never touch the live ledger, so the live record stays clean.
"""
import csv
import os
import time
from datetime import datetime, timezone

from btc_engine import Bar

PAGE = 5000
_FMT = "%Y-%m-%d %H:%M:%S"


def load_csv(path):
    bars = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            raw = row.get("datetime") or row.get("time")
            dt = datetime.strptime(raw[:19].replace("T", " "), _FMT).replace(tzinfo=timezone.utc)
            bars.append(Bar(int(dt.timestamp()), float(row["open"]), float(row["high"]),
                            float(row["low"]), float(row["close"])))
    bars.sort(key=lambda b: b.t)
    return bars


def merge(*bar_lists):
    """Combine candle lists, one candle per timestamp (later lists win), oldest first."""
    by_t = {}
    for lst in bar_lists:
        for b in lst:
            by_t[b.t] = b
    return [by_t[t] for t in sorted(by_t)]


def save_csv(path, bars):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "open", "high", "low", "close"])
        for b in bars:
            w.writerow([datetime.fromtimestamp(b.t, timezone.utc).strftime(_FMT), b.o, b.h, b.l, b.c])
    os.replace(tmp, path)  # never leave a half-written history behind


def download(fetch_page, days, now=None, max_pages=40, pause=8, sleep=None):
    """Walk backwards from now until `days` of candles are covered.

    fetch_page(end_date, outputsize) -> list[Bar] (any order; end_date is None for 'latest' or a UTC string).
    Returns (bars oldest first, report) where report says how far back it got and why it stopped."""
    now = int(now if now is not None else time.time())
    want_from = now - int(days * 86400)
    got_by_t, end, pages, why = {}, None, 0, "reached the number of days asked for"
    for _ in range(max_pages):
        page = fetch_page(end, PAGE)
        pages += 1
        if not page:
            why = "the data provider returned nothing older"
            break
        new = 0
        for b in page:
            if b.t not in got_by_t:
                got_by_t[b.t] = b
                new += 1
        earliest = min(b.t for b in page)
        if earliest <= want_from:
            break
        if new == 0 or len(page) < PAGE:
            why = "the data provider has no candles older than this"
            break
        end = datetime.fromtimestamp(earliest - 1, timezone.utc).strftime(_FMT)
        (sleep or time.sleep)(pause)
    else:
        why = f"stopped after {max_pages} pages"
    bars = [got_by_t[t] for t in sorted(got_by_t) if t >= want_from]
    covered = (bars[-1].t - bars[0].t) / 86400 if len(bars) > 1 else 0.0
    return bars, {"pages": pages, "candles": len(bars), "days_covered": covered, "why": why}
