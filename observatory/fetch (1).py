"""
observatory/fetch.py
====================
OBSERVATORY — independent historical candle fetcher (GBP/USD only). ISOLATED.

PURPOSE
-------
Get authoritative FULL-DAY GBP/USD 5M / 15M / 1H candles from Twelve Data into
observatory_data/raw/GBPUSD_{5m,15m,1h}.jsonl, independent of the live scanner's
schedule, cursor, state and logs. (The scanner only runs 07:00-15:55 UTC Mon-Fri
and its 5M log is missing 15:50-22:35 UTC every day; see the catalog, Section 13.)

IT DOES NOT: detect FVG/BOS/anything, read scanner state or logs, or import any
live-bot module. Imports: stdlib only.

1-MINUTE CANDLES (added): --tfs 1m. One file per month (observatory_data/raw/GBPUSD_1m_YYYY-MM.jsonl); never fetched at/after the sealed holdout (end clamped to 2026-06-17 23:59 UTC).
About 8 requests per month of history (5,000 bars per request, weekend quotes included): six months is about 50 credits (about 17 minutes at the default 20 s pace).

FREE-PLAN SAFETY (Twelve Data Basic = 8 credits/minute, 800 credits/day, 5,000
points per request; 1 credit per time_series request)
-----------------------------------------------------------------------------
The LIVE scanner uses the same API key: about 3 credits per run x 108 runs/day
(08:00-16:55 Lagos = 07:00-15:55 UTC, Mon-Fri) = roughly 324 credits/day, in
bursts of 3 every 5 minutes. A research run that exceeded 8 credits/minute or ate
the daily quota would make LIVE fetches fail. So:
  * Refuses to run inside the live window (07:00-15:55 UTC Mon-Fri) unless
    --allow-live-window is passed. Run it evenings (after 16:00 UTC) or weekends.
  * Paces requests (default 1 per 20 s = 3/min).
  * Hard credit budget per run: default 100, absolute ceiling 300.
  * Stops IMMEDIATELY on a rate-limit/credit error (no retry storm), keeps what it
    already fetched, and reports status RATE_LIMITED.
  * The API key is read from env TWELVE_DATA_KEY and is never printed or stored.

DATA HANDLING
-------------
  * Pages BACKWARD from --end using end_date (provider returns newest-first),
    outputsize up to 5,000, until --start or the provider's earliest data.
  * Drops the still-forming candle (open + step > now).
  * Merges into the raw file by timestamp (provider wins; revised bars are counted
    as `revised`). Writes atomically (temp file + rename). Idempotent.
  * Appends one JSON line per run to observatory_data/raw/fetch_log.jsonl.

Run:  python3 -m observatory.fetch --start 2025-10-01 --max-credits 60
      python3 -m observatory.fetch --dry-run --start 2025-10-01
      python3 -m observatory.fetch --selftest       (mock provider, no network, no key)
"""
import argparse
import json
import math
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import pandas as pd

API_URL = "https://api.twelvedata.com/time_series"
EARLIEST_URL = "https://api.twelvedata.com/earliest_timestamp"
SYMBOL = "GBP/USD"
SYMBOL_TAG = "GBPUSD"
INTERVALS = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h"}
STEP_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}
# 1-MINUTE candles are stored as ONE FILE PER MONTH (GBPUSD_1m_YYYY-MM.jsonl, ~4-5 MB each): a single 1m file for 20 months would be ~90 MB and GitHub rejects files over 100 MB.
# They are NEVER fetched at or after the sealed holdout start: the end of a 1m request is clamped to one minute before it (the 5m/15m/1h files are unchanged: sealed in code, not by absence).
HOLDOUT_START_UTC = pd.Timestamp("2026-06-18 00:00", tz="UTC")      # must equal observatory.clean.HOLDOUT_START_UTC (the selftest checks it)
MAX_POINTS = 5000
DEFAULT_RAW_DIR = "observatory_data/raw"
DEFAULT_MAX_CREDITS = 100
CREDIT_CEILING = 300            # absolute cap per run (live scanner needs ~324 of the 800/day)
DEFAULT_PACE_SECONDS = 20
# Live scanner window in UTC (cron-job.org: 08:00-16:55 Africa/Lagos, Mon-Fri)
LIVE_WINDOW_UTC = (7 * 60, 15 * 60 + 56)   # minutes of day, [start, end)


class RateLimited(Exception):
    pass


class ApiError(Exception):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def in_live_window(now):
    now = now.tz_convert("UTC") if now.tzinfo else now.tz_localize("UTC")
    if now.dayofweek > 4:
        return False
    m = now.hour * 60 + now.minute
    return LIVE_WINDOW_UTC[0] <= m < LIVE_WINDOW_UTC[1]


def estimate_requests(tf, start, end, page_size=MAX_POINTS):
    bars_per_week = 7 * 24 * 60 / STEP_MINUTES[tf]   # provider also emits weekend quotes (measured: ~6.3 days of bars per week)
    weeks = max(0.0, (end - start).total_seconds() / (7 * 86400))
    return int(math.ceil(weeks * bars_per_week / page_size)) + 1      # +1 safety


def _fmt(ts):
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def http_get(url, params, timeout=30):
    """Real HTTP. Never prints the URL (it contains the key)."""
    full = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _check_response(resp):
    if isinstance(resp, dict) and (resp.get("status") == "error" or ("values" not in resp and "datetime" not in resp)):
        code = resp.get("code")
        msg = str(resp.get("message", "")).lower()
        if code == 429 or "credit" in msg or "rate limit" in msg or "too many" in msg:
            raise RateLimited(str(resp.get("message", "rate limited"))[:200])
        if code == 400 and ("no data" in msg or "not available" in msg):
            return {"values": []}
        raise ApiError(f"code={code} {str(resp.get('message', ''))[:200]}")
    return resp


# ---------------------------------------------------------------------------
# fetch one timeframe
# ---------------------------------------------------------------------------
def fetch_timeframe(tf, start, end, key, get=http_get, state=None, pace=DEFAULT_PACE_SECONDS,
                    page_size=MAX_POINTS, now=None, sleep=time.sleep, checkpoint=None):
    """
    state = {"credits_used": int, "budget": int}  (shared across timeframes)
    Returns (rows dict[datetime_str] -> row dict, info dict).
    """
    step = pd.Timedelta(minutes=STEP_MINUTES[tf])
    now = now or pd.Timestamp.now(tz="UTC")
    info = {"timeframe": tf, "requests": 0, "status": "OK", "earliest_provider": None,
            "start_effective": _fmt(start), "dropped_forming": 0}

    def call(url, params):
        if state["credits_used"] >= state["budget"]:
            raise _BudgetStop()
        if state["credits_used"] > 0:
            sleep(pace)
        state["credits_used"] += 1
        return _check_response(get(url, params))

    rows = {}
    try:
        # earliest available data (1 credit): clamp start so we never ask for nothing
        er = call(EARLIEST_URL, {"symbol": SYMBOL, "interval": INTERVALS[tf], "apikey": key, "timezone": "UTC"})
        if isinstance(er, dict) and er.get("datetime"):
            earliest = pd.Timestamp(er["datetime"], tz="UTC")
            info["earliest_provider"] = _fmt(earliest)
            if earliest > start:
                start = earliest
                info["start_effective"] = _fmt(start)
        cursor_end = end
        prev_oldest = None
        while True:
            resp = call(API_URL, {"symbol": SYMBOL, "interval": INTERVALS[tf], "apikey": key,
                                  "outputsize": page_size, "timezone": "UTC",
                                  "start_date": _fmt(start), "end_date": _fmt(cursor_end), "order": "DESC"})
            info["requests"] += 1
            vals = resp.get("values", [])
            if not vals:
                break
            for v in vals:
                t = pd.Timestamp(v["datetime"], tz="UTC")
                if t < start or t > end:
                    continue
                if t + step > now:
                    info["dropped_forming"] += 1
                    continue
                rows[_fmt(t)] = {"datetime": _fmt(t), "open": v["open"], "high": v["high"],
                                 "low": v["low"], "close": v["close"]}
            if checkpoint is not None and rows:
                checkpoint(rows)                     # persist after EVERY page: a cancelled/timed-out job keeps what it fetched
            oldest = min(pd.Timestamp(v["datetime"], tz="UTC") for v in vals)
            if len(vals) < page_size or oldest <= start:
                break
            if prev_oldest is not None and oldest >= prev_oldest:
                info["status"] = "NO_PROGRESS"
                break
            prev_oldest = oldest
            cursor_end = oldest - step
    except _BudgetStop:
        info["status"] = "BUDGET_REACHED"
    except RateLimited as e:
        info["status"] = "RATE_LIMITED"
        info["message"] = str(e)
    except ApiError as e:
        info["status"] = "API_ERROR"
        info["message"] = str(e)
    except Exception as e:                                   # network etc. Never include the URL/key.
        info["status"] = "ERROR"
        info["message"] = type(e).__name__
    info["rows_fetched"] = len(rows)
    return rows, info


class _BudgetStop(Exception):
    pass


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
def _store_path(tf, raw_dir):
    return os.path.join(raw_dir, f"{SYMBOL_TAG}_{tf}.jsonl")


def _tf_end(tf, end):
    """The effective end of a request: 1m never reaches the sealed holdout."""
    return min(end, HOLDOUT_START_UTC - pd.Timedelta(minutes=1)) if tf == "1m" else end


def _shard_path(raw_dir, month):
    return os.path.join(raw_dir, f"{SYMBOL_TAG}_1m_{month}.jsonl")


def merge_into_store(tf, new_rows, raw_dir=DEFAULT_RAW_DIR):
    """Merge by timestamp (provider wins), atomic rewrite. Returns counts. 1m goes to one shard per calendar month (UTC)."""
    if tf != "1m":
        return _merge_file(_store_path(tf, raw_dir), new_rows, raw_dir)
    by_month = {}
    for k, r in new_rows.items():
        by_month.setdefault(k[:7], {})[k] = r
    tot = {"added": 0, "revised": 0, "total": 0, "shards": 0}
    for m in sorted(by_month):
        r = _merge_file(_shard_path(raw_dir, m), by_month[m], raw_dir)
        for k in ("added", "revised", "total"):
            tot[k] += r[k]
        tot["shards"] += 1
    tot["file"] = os.path.join(raw_dir, f"{SYMBOL_TAG}_1m_YYYY-MM.jsonl")
    return tot


def _merge_file(path, new_rows, raw_dir):
    os.makedirs(raw_dir, exist_ok=True)
    existing = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    existing[r["datetime"]] = r
    added = revised = 0
    for k, r in new_rows.items():
        if k not in existing:
            added += 1
        elif any(str(existing[k].get(c)) != str(r[c]) for c in ("open", "high", "low", "close")):
            revised += 1
        existing[k] = r
    if not added and not revised and os.path.exists(path):
        return {"added": 0, "revised": 0, "total": len(existing), "file": path}          # nothing changed: do not rewrite the file
    fd, tmp = tempfile.mkstemp(dir=raw_dir, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        for k in sorted(existing):
            f.write(json.dumps(existing[k]) + "\n")
    os.replace(tmp, path)
    return {"added": added, "revised": revised, "total": len(existing), "file": path}


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def run(start, end=None, tfs=("5m", "15m", "1h"), max_credits=DEFAULT_MAX_CREDITS,
        pace=DEFAULT_PACE_SECONDS, raw_dir=DEFAULT_RAW_DIR, dry_run=False, allow_live_window=False,
        get=http_get, key=None, now=None, page_size=MAX_POINTS, sleep=time.sleep):
    now = now or pd.Timestamp.now(tz="UTC")
    start = pd.Timestamp(start, tz="UTC") if not isinstance(start, pd.Timestamp) else start
    end = pd.Timestamp(end, tz="UTC") if end and not isinstance(end, pd.Timestamp) else (end or now)
    budget = min(int(max_credits), CREDIT_CEILING)
    ends = {tf: _tf_end(tf, end) for tf in tfs}
    plan = {tf: estimate_requests(tf, start, ends[tf], page_size) + 1 for tf in tfs}     # +1 earliest_timestamp call
    summary = {"started_utc": now.isoformat(), "start": _fmt(start), "end": _fmt(end), "timeframes": list(tfs),
               "budget_credits": budget, "estimated_credits": plan, "estimated_total": sum(plan.values()),
               "dry_run": dry_run, "results": {}, "status": "OK"}
    if any(ends[tf] != end for tf in tfs):
        summary["end_clamped_to_before_holdout"] = {tf: _fmt(ends[tf]) for tf in tfs if ends[tf] != end}
    if max_credits > CREDIT_CEILING:
        summary["note"] = f"max-credits capped to {CREDIT_CEILING}"
    if dry_run:
        summary["status"] = "DRY_RUN"
        return summary
    if in_live_window(now) and not allow_live_window:
        summary["status"] = "REFUSED_LIVE_WINDOW"
        summary["message"] = ("Inside the live scanner window (07:00-15:55 UTC, Mon-Fri). A research fetch could push the "
                              "shared API key over 8 credits/minute and break live fetches. Run after 16:00 UTC or on a weekend.")
        return summary
    if summary["estimated_total"] > budget:
        summary["status"] = "REFUSED_OVER_BUDGET"
        summary["message"] = f"estimated {summary['estimated_total']} credits > budget {budget}. Shorten the range or raise --max-credits (ceiling {CREDIT_CEILING})."
        return summary
    key = key or os.environ.get("TWELVE_DATA_KEY")
    if not key:
        summary["status"] = "NO_KEY"
        summary["message"] = "TWELVE_DATA_KEY not set"
        return summary
    state = {"credits_used": 0, "budget": budget}
    for tf in tfs:
        ck = {"added": 0, "revised": 0}

        def _cp(rows_so_far, tf=tf, ck=ck):
            r = merge_into_store(tf, rows_so_far, raw_dir)
            ck["added"] += r["added"]
            ck["revised"] += r["revised"]
        rows, info = fetch_timeframe(tf, start, ends[tf], key, get=get, state=state, pace=pace,
                                     page_size=page_size, now=now, sleep=sleep, checkpoint=_cp)
        if rows:
            r = merge_into_store(tf, rows, raw_dir)
            ck["added"] += r["added"]
            ck["revised"] += r["revised"]
            r["added"], r["revised"] = ck["added"], ck["revised"]
            info.update(r)
        summary["results"][tf] = info
        if info["status"] in ("RATE_LIMITED", "BUDGET_REACHED", "API_ERROR", "ERROR"):
            summary["status"] = info["status"]
            break
    summary["credits_used"] = state["credits_used"]
    os.makedirs(raw_dir, exist_ok=True)
    with open(os.path.join(raw_dir, "fetch_log.jsonl"), "a") as f:
        f.write(json.dumps(summary, default=str) + "\n")
    return summary


# ---------------------------------------------------------------------------
# self-test with a mock provider (no network, no key)
# ---------------------------------------------------------------------------
class _MockProvider:
    """Behaves like Twelve Data time_series: start/end inclusive, newest-first, outputsize cap."""

    def __init__(self, tf, n_bars, t_last, revise=None, rate_limit_after=None):
        step = pd.Timedelta(minutes=STEP_MINUTES[tf])
        self.idx = [t_last - step * i for i in range(n_bars)][::-1]
        self.rows = {t: {"datetime": _fmt(t), "open": "1.30000", "high": "1.30100", "low": "1.29900", "close": "1.30050"} for t in self.idx}
        if revise:
            for t, c in revise.items():
                self.rows[t] = dict(self.rows[t], close=c)
        self.calls = 0
        self.rate_limit_after = rate_limit_after

    def __call__(self, url, params):
        self.calls += 1
        if self.rate_limit_after is not None and self.calls > self.rate_limit_after:
            return {"code": 429, "message": "You have run out of API credits for the current minute.", "status": "error"}
        if url == EARLIEST_URL:
            return {"datetime": _fmt(self.idx[0]), "unix_time": 0}
        s = pd.Timestamp(params["start_date"], tz="UTC")
        e = pd.Timestamp(params["end_date"], tz="UTC")
        sel = [t for t in reversed(self.idx) if s <= t <= e][: int(params["outputsize"])]
        return {"meta": {}, "values": [self.rows[t] for t in sel], "status": "ok"}


def selftest():
    fails, n = [], 0

    def chk(label, got, want):
        nonlocal n
        n += 1
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")
    nosleep = lambda s: None
    t_last = pd.Timestamp("2026-10-03 12:00", tz="UTC")
    now = pd.Timestamp("2026-10-03 20:00", tz="UTC")          # outside live window (Saturday anyway)
    tmp = tempfile.mkdtemp()
    key = "SECRET_TEST_KEY_123"

    # 1. pagination over several small pages returns every bar exactly once
    mp = _MockProvider("5m", 1000, t_last)
    out = run("2026-09-25", "2026-10-03 12:00", ("5m",), max_credits=100, pace=0, raw_dir=tmp, get=mp, key=key, now=now, page_size=100, sleep=nosleep)
    want = [t for t in mp.idx if t >= pd.Timestamp("2026-09-25", tz="UTC")]
    got = [json.loads(l)["datetime"] for l in open(os.path.join(tmp, "GBPUSD_5m.jsonl"))]
    chk("pagination complete & ordered", got, [_fmt(t) for t in want])
    chk("status OK", out["status"], "OK")
    # 2. idempotent: second run adds nothing
    out2 = run("2026-09-25", "2026-10-03 12:00", ("5m",), max_credits=100, pace=0, raw_dir=tmp, get=_MockProvider("5m", 1000, t_last), key=key, now=now, page_size=100, sleep=nosleep)
    chk("idempotent add=0", out2["results"]["5m"]["added"], 0)
    # 3. forming candle dropped
    now2 = pd.Timestamp("2026-10-03 12:03", tz="UTC")
    tmp2 = tempfile.mkdtemp()
    run("2026-10-03 10:00", "2026-10-03 12:05", ("5m",), max_credits=50, pace=0, raw_dir=tmp2, get=_MockProvider("5m", 400, pd.Timestamp("2026-10-03 12:00", tz="UTC")), key=key, now=now2, page_size=100, sleep=nosleep, allow_live_window=True)
    last = [json.loads(l)["datetime"] for l in open(os.path.join(tmp2, "GBPUSD_5m.jsonl"))][-1]
    chk("forming bar (12:00 closes 12:05 > 12:03) dropped", last, "2026-10-03 11:55:00")
    # 4. budget cap
    tmp3 = tempfile.mkdtemp()
    o3 = run("2026-09-25", "2026-10-03 12:00", ("5m",), max_credits=3, pace=0, raw_dir=tmp3, get=_MockProvider("5m", 1000, t_last), key=key, now=now, page_size=100, sleep=nosleep)
    chk("over-budget plan refused", o3["status"], "REFUSED_OVER_BUDGET")
    # 4b. budget reached mid-run (estimate under budget, but pages exceed): tiny pages
    mpb = _MockProvider("5m", 1000, t_last)
    o3b = run("2026-10-03 00:00", "2026-10-03 12:00", ("5m",), max_credits=4, pace=0, raw_dir=tmp3, get=mpb, key=key, now=now, page_size=10, sleep=nosleep)
    chk("budget never exceeded", mpb.calls <= 4, True)
    # 5. rate limit stops immediately, keeps nothing wrong, no retry storm
    tmp4 = tempfile.mkdtemp()
    mpr = _MockProvider("5m", 1000, t_last, rate_limit_after=2)
    o4 = run("2026-09-25", "2026-10-03 12:00", ("5m", "15m"), max_credits=100, pace=0, raw_dir=tmp4, get=mpr, key=key, now=now, page_size=100, sleep=nosleep)
    chk("rate limited status", o4["status"], "RATE_LIMITED")
    chk("no calls after rate limit", mpr.calls, 3)
    chk("15m never attempted", "15m" in o4["results"], False)
    # 6. revised bar counted, provider wins
    tmp5 = tempfile.mkdtemp()
    run("2026-10-03 10:00", "2026-10-03 12:00", ("5m",), pace=0, raw_dir=tmp5, get=_MockProvider("5m", 300, t_last), key=key, now=now, page_size=100, sleep=nosleep)
    rev_t = pd.Timestamp("2026-10-03 11:00", tz="UTC")
    o6 = run("2026-10-03 10:00", "2026-10-03 12:00", ("5m",), pace=0, raw_dir=tmp5, get=_MockProvider("5m", 300, t_last, revise={rev_t: "1.30999"}), key=key, now=now, page_size=100, sleep=nosleep)
    chk("revised counted", o6["results"]["5m"]["revised"], 1)
    # 7. live-window refusal (Wednesday 10:00 UTC), allowed with flag
    wed = pd.Timestamp("2026-10-07 10:00", tz="UTC")
    o7 = run("2026-10-01", "2026-10-07 09:00", ("5m",), pace=0, raw_dir=tmp5, get=_MockProvider("5m", 300, t_last), key=key, now=wed)
    chk("refused in live window", o7["status"], "REFUSED_LIVE_WINDOW")
    chk("weekend not live window", in_live_window(pd.Timestamp("2026-10-03 10:00", tz="UTC")), False)
    chk("weekday 16:00 not live window", in_live_window(pd.Timestamp("2026-10-07 16:00", tz="UTC")), False)
    chk("weekday 15:55 is live window", in_live_window(pd.Timestamp("2026-10-07 15:55", tz="UTC")), True)
    # 8. earliest clamp
    tmp6 = tempfile.mkdtemp()
    o8 = run("2026-09-28", "2026-10-03 12:00", ("5m",), max_credits=300, pace=0, raw_dir=tmp6, get=_MockProvider("5m", 500, t_last), key=key, now=now, page_size=100, sleep=nosleep)
    chk("start clamped to provider earliest", o8["results"]["5m"]["start_effective"], _fmt(_MockProvider("5m", 500, t_last).idx[0]))
    # 9. key never leaks into outputs
    blob = json.dumps(out) + json.dumps(o4) + open(os.path.join(tmp, "fetch_log.jsonl")).read()
    chk("api key not in any output", key in blob, False)
    # 10. no key => refuses cleanly
    chk("no key refused", run("2026-10-01", "2026-10-02", ("5m",), pace=0, raw_dir=tmp, get=mp, key="", now=now)["status"], "NO_KEY") if not os.environ.get("TWELVE_DATA_KEY") else None
    # 11. dry run does no I/O
    o11 = run("2025-10-01", None, ("5m", "15m", "1h"), dry_run=True, now=now)
    chk("dry run status", o11["status"], "DRY_RUN")
    # 12. 1-minute candles: one shard per month, no single 1m file, idempotent, ordered, holdout never requested
    from . import clean as _rc
    chk("holdout constant equals clean.HOLDOUT_START_UTC", HOLDOUT_START_UTC, _rc.HOLDOUT_START_UTC)
    chk("1m end is clamped to one minute before the holdout", _tf_end("1m", pd.Timestamp("2026-10-04", tz="UTC")), pd.Timestamp("2026-06-17 23:59", tz="UTC"))
    chk("1m end before the holdout is untouched", _tf_end("1m", pd.Timestamp("2026-03-01", tz="UTC")), pd.Timestamp("2026-03-01", tz="UTC"))
    chk("5m end is never clamped here", _tf_end("5m", pd.Timestamp("2026-10-04", tz="UTC")), pd.Timestamp("2026-10-04", tz="UTC"))
    est = estimate_requests("1m", pd.Timestamp("2025-10-01", tz="UTC"), pd.Timestamp("2026-03-31 23:59", tz="UTC"))
    chk("six months of 1m is about 50 requests (not hundreds)", 45 <= est + 1 <= 60, True)
    tmp7 = tempfile.mkdtemp()
    t1 = pd.Timestamp("2026-03-02 01:00", tz="UTC")
    mp1 = _MockProvider("1m", 4000, t1)
    o12 = run("2026-02-27 00:00", "2026-03-02 01:00", ("1m",), max_credits=100, pace=0, raw_dir=tmp7, get=mp1, key=key, now=pd.Timestamp("2026-10-03 20:00", tz="UTC"), page_size=700, sleep=nosleep)
    files = sorted(os.listdir(tmp7))
    chk("1m written as monthly shards across the month boundary", [f for f in files if f.startswith("GBPUSD_1m")], ["GBPUSD_1m_2026-02.jsonl", "GBPUSD_1m_2026-03.jsonl"])
    chk("no single 1m file", "GBPUSD_1m.jsonl" in files, False)
    allt = []
    for f in ["GBPUSD_1m_2026-02.jsonl", "GBPUSD_1m_2026-03.jsonl"]:
        allt += [json.loads(l)["datetime"] for l in open(os.path.join(tmp7, f))]
    want1 = [_fmt(t) for t in mp1.idx if t >= pd.Timestamp("2026-02-27", tz="UTC")]
    chk("1m shards hold every bar once, in order, in the right month", allt, want1)
    chk("1m shard keys match their month", all(l.startswith('{"datetime": "2026-02') for l in open(os.path.join(tmp7, "GBPUSD_1m_2026-02.jsonl"))), True)
    o12b = run("2026-02-27 00:00", "2026-03-02 01:00", ("1m",), max_credits=100, pace=0, raw_dir=tmp7, get=_MockProvider("1m", 4000, t1), key=key, now=pd.Timestamp("2026-10-03 20:00", tz="UTC"), page_size=700, sleep=nosleep)
    chk("1m second run adds nothing", o12b["results"]["1m"]["added"], 0)
    # a request that reaches past the holdout is clamped (the provider is never asked for later bars)
    tmp8 = tempfile.mkdtemp()
    mp2 = _MockProvider("1m", 3000, pd.Timestamp("2026-06-18 12:00", tz="UTC"))
    o13 = run("2026-06-16 12:00", "2026-06-18 12:00", ("1m",), max_credits=100, pace=0, raw_dir=tmp8, get=mp2, key=key, now=pd.Timestamp("2026-10-03 20:00", tz="UTC"), page_size=700, sleep=nosleep)
    last1m = [json.loads(l)["datetime"] for l in open(os.path.join(tmp8, "GBPUSD_1m_2026-06.jsonl"))][-1]
    chk("no 1m bar at or after the holdout is stored", last1m, "2026-06-17 23:59:00")
    chk("the clamp is reported", "1m" in o13.get("end_clamped_to_before_holdout", {}), True)
    return {"checks": n, "failures": fails, "passed": not fails}


def main():
    ap = argparse.ArgumentParser(description="Independent GBP/USD candle fetcher (free-plan safe).")
    ap.add_argument("--start", help="YYYY-MM-DD (UTC)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD HH:MM (UTC), default now")
    ap.add_argument("--tfs", default="5m,15m,1h")
    ap.add_argument("--max-credits", type=int, default=DEFAULT_MAX_CREDITS)
    ap.add_argument("--pace-seconds", type=float, default=DEFAULT_PACE_SECONDS)
    ap.add_argument("--raw-dir", default=DEFAULT_RAW_DIR)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-live-window", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["passed"] else 1)
    if not a.start:
        ap.error("--start is required")
    out = run(a.start, a.end, tuple(a.tfs.split(",")), a.max_credits, a.pace_seconds, a.raw_dir, a.dry_run, a.allow_live_window)
    print(json.dumps(out, indent=2, default=str))
    sys.exit(0 if out["status"] in ("OK", "DRY_RUN") else 2)


if __name__ == "__main__":
    main()
