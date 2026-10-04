"""
observatory/clean.py
====================
OBSERVATORY — canonical clean candles. ISOLATED from the live bot.

(The Observatory is the independent market-research body. It is deliberately
NOT called "research_*": the live bot already has research_lab.py /
research_dataset.py / edge_finder*.py for the User Catalog, and those names
must not be confused with this body.)

WHAT THIS FILE IS
-----------------
Turns raw candle files into canonical clean candles, with a full audit trail.
Two raw SOURCES are supported:
  * "observatory" (default): observatory_data/raw/GBPUSD_{5m,15m,1h}.jsonl,
    written by observatory/fetch.py with full-day coverage. This is the real
    research source.
  * "live-logs": the live scanner's ohlc_{tf}_log.jsonl files. Only for
    sanity-checking the cleaner on the scanner's partial data. The live
    scanner's schedule must never define the research dataset.

HARD RULES (enforced in code, not just stated)
----------------------------------------------
  * RAW LOGS ARE NEVER MODIFIED. They are only opened for reading. Output goes
    to a separate directory (default: observatory_data/clean/). write_clean() refuses
    to write anywhere that resolves to a raw-log path.
  * Imports: stdlib + pandas + numpy ONLY. It does not import scanner_common,
    scanner_observation, scanner_live, min_scanner, research_dataset or
    research_lab. Nothing in the live scanner may import the observatory package.
    (check_layer_imports.py does not list it, so it is not inspected and cannot
    break the live scan job. Do NOT edit that checker without testing it: it runs
    before every live scan.)
  * No trading concepts here. No FVG/BOS/CHoCH/IFVG/premium-discount.

WHAT THE REAL LOGS TAUGHT US (measured 2026-10-03, see catalog Section 8)
-------------------------------------------------------------------------
  * 1H log: 2,937 lines for 848 unique hours (identical copies).
  * 1H + 15M logs contain bars inside the FX closed window (weekend).
  * 5M log: 43 identical duplicate lines, and a systematic daily blind spot
    (almost no bars ~16:00-22:00 UTC) — those are unexplained gaps, not market
    closures, and are recorded as gaps.

CLEANING POLICY (each step is counted in the report)
----------------------------------------------------
  1. Parse. Malformed lines are counted, never silently skipped.
  2. De-duplicate on timestamp. The writer's own docstring says a closed candle
     is immutable, so copies should be identical. If copies DISAGREE on any
     OHLC value that is recorded as a `conflict`, and the FIRST copy is kept.
  3. Drop off-grid timestamps (minute/second not aligned to the timeframe).
  4. Drop impossible candles (high < low, high < max(open, close), etc.).
  5. Drop bars inside the closed window (see is_market_closed: provider convention,
     Friday 17:00 New York close, all Saturday, Sunday before 22:00 UTC).
  6. Detect gaps: consecutive kept bars further apart than one step. The number
     of OPEN-market grid slots missing between them is `gap_before_missing`.
     If that number is 0 the gap is a pure market closure (`closure_before`).

PROVENANCE
----------
Every clean row keeps `raw_line` (1-based line number of the copy that was kept)
and `dup_count` (how many raw lines carried that timestamp).

OUTCOME-WINDOW CENSORING (used by observatory.measure.first_touch)
---------------------------------------------------------------
`gap_before_missing` > 0 on any bar inside an outcome window  =>  DATA_GAP.
`closure_before` (weekend) is NOT a gap; it is flagged separately
(`crosses_closure`) so an analysis can exclude it if it wants.
"""
import json
import os
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")

STEP_MINUTES = {"5m": 5, "15m": 15, "1h": 60}
TIMEFRAMES = tuple(STEP_MINUTES)
DEFAULT_RAW_DIR = "observatory_data/raw"
DEFAULT_CLEAN_DIR = "observatory_data/clean"
SYMBOL_TAG = "GBPUSD"


def raw_filename(tf, source="observatory"):
    if source == "observatory":
        return f"{SYMBOL_TAG}_{tf}.jsonl"
    if source == "live-logs":
        return f"ohlc_{tf}_log.jsonl"
    raise ValueError(f"unknown source {source!r}")


def raw_path(tf, raw_dir=DEFAULT_RAW_DIR, source="observatory"):
    return os.path.join(raw_dir, raw_filename(tf, source))
CLEAN_COLUMNS = ["open", "high", "low", "close", "raw_line", "dup_count",
                 "gap_before_missing", "closure_before"]


# ---------------------------------------------------------------------------
# market hours
# ---------------------------------------------------------------------------
# Provider holiday windows: periods in which Twelve Data returned NO quotes at all
# (observed Dec 24 13:00 -> Dec 25 13:00 and Dec 31 13:00 -> Jan 1 13:00 UTC, 2025/26).
# Used ONLY to classify missing slots as a closure rather than a data gap; no bar is
# ever dropped because of them. Other holidays are unobserved and stay as gaps.
HOLIDAY_WINDOWS = (((12, 24, 13), (12, 25, 13)), ((12, 31, 13), (1, 1, 13)))


def _in_holiday_windows(index_utc):
    out = np.zeros(len(index_utc), dtype=bool)
    if len(index_utc) == 0:
        return out
    for year in range(index_utc.min().year - 1, index_utc.max().year + 1):
        for (m1, d1, h1), (m2, d2, h2) in HOLIDAY_WINDOWS:
            start = pd.Timestamp(year, m1, d1, h1, tz="UTC")
            end = pd.Timestamp(year + (1 if (m2, d2) < (m1, d1) else 0), m2, d2, h2, tz="UTC")
            out |= np.asarray((index_utc >= start) & (index_utc < end))
    return out


def is_market_closed(index_utc, holidays=False):
    """
    Vectorised. True for timestamps the market is closed, using THIS PROVIDER'S
    observed convention (verified on 12 months of GBP/USD, Oct 2025 - Oct 2026):
      * Friday close = 17:00 New York (21:00 UTC in US summer, 22:00 UTC in winter).
      * All Saturday (UTC).
      * Sunday before 22:00 UTC, all year. (The provider's first real Sunday bar is
        22:00-22:15 UTC even in US summer, when the NY rule alone would say 21:00.)
    The provider also emits low-liquidity quotes through the weekend; those are not
    tradeable, so they are dropped. holidays=True also treats the provider's
    holiday no-quote windows as closed (for gap classification only).
    """
    ny = index_utc.tz_convert(NY)
    ny_wd = np.asarray(ny.dayofweek)
    ny_minute = np.asarray(ny.hour) * 60 + np.asarray(ny.minute)
    utc_wd = np.asarray(index_utc.dayofweek)
    utc_hour = np.asarray(index_utc.hour)
    closed = ((ny_wd == 4) & (ny_minute >= 17 * 60)) | (utc_wd == 5) | ((utc_wd == 6) & (utc_hour < 22))
    if holidays:
        closed = closed | _in_holiday_windows(index_utc)
    return closed


# ---------------------------------------------------------------------------
# step 0: raw load (read-only)
# ---------------------------------------------------------------------------
def load_raw(path):
    """Returns (DataFrame with columns t, open, high, low, close, raw_line),
    malformed_line_count. Never writes. Missing file -> empty frame."""
    rows, bad = [], 0
    if not os.path.exists(path):
        return pd.DataFrame(columns=["t", "open", "high", "low", "close", "raw_line"]), 0
    with open(path, "r") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                rows.append({
                    "t": pd.Timestamp(r["datetime"]),
                    "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "raw_line": line_no,
                })
            except Exception:
                bad += 1
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "raw_line"])
    if not df.empty:
        df["t"] = pd.to_datetime(df["t"], utc=True)
    return df, bad


# ---------------------------------------------------------------------------
# steps 1-6: clean
# ---------------------------------------------------------------------------
def clean_candles(raw_df, step_minutes, malformed=0):
    """
    Returns (clean_df, gaps_df, report).
    clean_df index = UTC timestamps, columns = CLEAN_COLUMNS.
    gaps_df   = one row per unexplained gap (gap_start = last bar before gap).
    report    = every count the cleaning policy produced.
    """
    step = pd.Timedelta(minutes=step_minutes)
    report = {
        "raw_lines_parsed": int(len(raw_df)), "malformed_lines": int(malformed),
        "unique_timestamps": 0, "duplicate_lines": 0, "conflicting_duplicates": 0,
        "dropped_off_grid": 0, "dropped_invalid_ohlc": 0, "dropped_market_closed": 0,
        "kept_bars": 0, "unexplained_gaps": 0, "missing_open_bars": 0,
        "closure_gaps": 0, "first": None, "last": None,
    }
    empty_gaps = pd.DataFrame(columns=["gap_start", "gap_end", "missing_open_bars"])
    if raw_df.empty:
        return pd.DataFrame(columns=CLEAN_COLUMNS), empty_gaps, report

    df = raw_df.sort_values(["t", "raw_line"], kind="mergesort")

    # 2. duplicates: keep first copy, count conflicts
    grp = df.groupby("t", sort=True)
    dup_count = grp.size()
    ohlc_unique = grp[["open", "high", "low", "close"]].nunique().max(axis=1)
    report["unique_timestamps"] = int(len(dup_count))
    report["duplicate_lines"] = int((dup_count - 1).sum())
    report["conflicting_duplicates"] = int((ohlc_unique > 1).sum())
    first = grp.first()
    first["dup_count"] = dup_count
    first["raw_line"] = grp["raw_line"].first()
    d = first

    # 3. off-grid
    ts = d.index
    on_grid = (np.asarray(ts.second) == 0) & ((np.asarray(ts.hour) * 60 + np.asarray(ts.minute)) % step_minutes == 0)
    report["dropped_off_grid"] = int((~on_grid).sum())
    d = d[on_grid]

    # 4. impossible candles
    bad = ((d["high"] < d["low"]) | (d["high"] < d[["open", "close"]].max(axis=1)) |
           (d["low"] > d[["open", "close"]].min(axis=1)))
    report["dropped_invalid_ohlc"] = int(bad.sum())
    d = d[~bad.values]

    # 5. FX closed window
    closed = is_market_closed(d.index)
    report["dropped_market_closed"] = int(closed.sum())
    d = d[~closed]

    # 6. gaps
    d = d[["open", "high", "low", "close", "raw_line", "dup_count"]].copy()
    d["raw_line"] = d["raw_line"].astype(int)
    d["dup_count"] = d["dup_count"].astype(int)
    missing = np.zeros(len(d), dtype=int)
    closure = np.zeros(len(d), dtype=bool)
    gap_rows = []
    idx = d.index
    if len(d) > 1:
        diffs = idx[1:] - idx[:-1]
        for pos in np.where(diffs > step)[0]:
            a, b = idx[pos], idx[pos + 1]
            slots = pd.date_range(a + step, b - step, freq=step)
            n_open = int((~is_market_closed(slots, holidays=True)).sum()) if len(slots) else 0
            missing[pos + 1] = n_open
            if n_open == 0:
                closure[pos + 1] = True
                report["closure_gaps"] += 1
            else:
                gap_rows.append({"gap_start": a, "gap_end": b, "missing_open_bars": n_open})
    d["gap_before_missing"] = missing
    d["closure_before"] = closure
    gaps_df = pd.DataFrame(gap_rows, columns=["gap_start", "gap_end", "missing_open_bars"]) if gap_rows else empty_gaps
    report["kept_bars"] = int(len(d))
    report["unexplained_gaps"] = int(len(gap_rows))
    report["missing_open_bars"] = int(missing.sum())
    if len(d):
        report["first"], report["last"] = d.index[0].isoformat(), d.index[-1].isoformat()
    return d[CLEAN_COLUMNS], gaps_df, report


# ---------------------------------------------------------------------------
# convenience: one timeframe from a base directory
# ---------------------------------------------------------------------------
def clean_timeframe(tf, raw_dir=DEFAULT_RAW_DIR, source="observatory"):
    step_min = STEP_MINUTES[tf]
    path = raw_path(tf, raw_dir, source)
    raw, bad = load_raw(path)
    clean, gaps, report = clean_candles(raw, step_min, malformed=bad)
    report["timeframe"] = tf
    report["step_minutes"] = step_min
    report["source"] = source
    report["raw_file"] = path
    return clean, gaps, report


def write_clean(tf, clean, gaps, report, out_dir, raw_dir=DEFAULT_RAW_DIR, source="observatory"):
    """Writes clean candles / gaps / report into out_dir. Refuses to write
    over any raw file."""
    out_dir_abs = os.path.abspath(out_dir)
    raw_abs = {os.path.abspath(raw_path(t, raw_dir, src)) for t in STEP_MINUTES for src in ("observatory", "live-logs")}
    os.makedirs(out_dir_abs, exist_ok=True)
    targets = {
        "clean": os.path.join(out_dir_abs, f"clean_{tf}.csv"),
        "gaps": os.path.join(out_dir_abs, f"gaps_{tf}.csv"),
        "report": os.path.join(out_dir_abs, f"clean_report_{tf}.json"),
    }
    for p in targets.values():
        if os.path.abspath(p) in raw_abs:
            raise RuntimeError(f"refusing to write over a raw file: {p}")
    clean.to_csv(targets["clean"], index_label="t")
    gaps.to_csv(targets["gaps"], index=False)
    with open(targets["report"], "w") as f:
        json.dump(report, f, indent=2, default=str)
    return targets


def load_clean(tf, raw_dir=DEFAULT_RAW_DIR, source="observatory", clean_dir=None):
    """Returns clean candles from clean_dir if present, else cleans in memory."""
    if clean_dir:
        p = os.path.join(clean_dir, f"clean_{tf}.csv")
        if os.path.exists(p):
            df = pd.read_csv(p, parse_dates=["t"], index_col="t")
            df.index = pd.to_datetime(df.index, utc=True)
            df["closure_before"] = df["closure_before"].astype(bool)
            return df
    return clean_timeframe(tf, raw_dir, source)[0]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Clean raw candles into canonical candles (read-only on raw).")
    ap.add_argument("--raw-dir", default=DEFAULT_RAW_DIR)
    ap.add_argument("--source", default="observatory", choices=["observatory", "live-logs"])
    ap.add_argument("--out", default=DEFAULT_CLEAN_DIR)
    args = ap.parse_args()
    for label in TIMEFRAMES:
        c, g, r = clean_timeframe(label, args.raw_dir, args.source)
        write_clean(label, c, g, r, args.out, args.raw_dir, args.source)
        print(f"[{label}] raw={r['raw_lines_parsed']} unique={r['unique_timestamps']} "
              f"dups={r['duplicate_lines']} conflicts={r['conflicting_duplicates']} "
              f"closed_dropped={r['dropped_market_closed']} kept={r['kept_bars']} "
              f"gaps={r['unexplained_gaps']} missing_open_bars={r['missing_open_bars']}")
