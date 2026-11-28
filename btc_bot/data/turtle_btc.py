#!/usr/bin/env python3
"""
Turtle Breakout BTC bot: the runner.  Port of the "Turtle Breakout - Revelio" Pine v6 indicator, long AND short,
plus a technical / fundamental scan, a demo-account Bank that sizes every signal, and performance statistics.

Like the indicator, this bot only ALERTS. It places no orders.

Everything for this bot lives in this folder (btc_bot/). Runtime files go in btc_bot/data/ and are only ever written
by ONE program each, so two scheduled jobs can never collide:
  data/state.json, trades.jsonl, scans.jsonl, bank_ledger.jsonl, latest.json, history_30m.csv, backtest/  <- this runner
  data/journal.jsonl                                                                                       <- btc_commands.py

Usage (from anywhere: paths do not depend on the working directory)
  python btc_bot/turtle_btc.py                    live run: fetch, replay, alert new signals, store everything
  python btc_bot/turtle_btc.py --dry-run          same, but print instead of sending and write nothing
  python btc_bot/turtle_btc.py --status           current state of both sides and the demo accounts
  python btc_bot/turtle_btc.py --report           the technical and fundamental reports right now
  python btc_bot/turtle_btc.py --stats            profit factor, R, drawdown from the stored live record
  python btc_bot/turtle_btc.py --download-history 365     download a year of 30m candles to data/history_30m.csv
  python btc_bot/turtle_btc.py --backtest         replay data/history_30m.csv (or --csv file) with full statistics
  add --send to --stats / --backtest to send the result to Telegram instead of just printing it

Environment
  TWELVE_DATA_API_KEY  TELEGRAM_BOT_TOKEN  TELEGRAM_CHAT_ID
  SYMBOL=BTC/USD  INTERVAL_MIN=30  N=20  M=10  ATR_LEN=20  ATR_MULT=2.0
  DAY_TRADING=1  CLOSE=16:30  REOPEN=18:00  (New York time)
  SCAN_EVERY_MIN=60  DAILY_REPORT_HOUR_UTC=6 (-1 = off)  MAX_CATCHUP_HOURS=6  DISPLAY_TZ=Africa/Lagos
  DATA_DIR=<folder>/data  ACCOUNTS_FILE=<folder>/accounts.json
"""
import argparse
import json
import os
import sys
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone

import btc_bank
import btc_commands
import btc_history
import btc_report
import btc_stats
from btc_engine import Bar, Config, run_both
from btc_journal import read_jsonl
from btc_scan import build_scan

UTC = timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))
load_csv = btc_history.load_csv


# --------------------------------------------------------------------------- data
def twelve_base():
    return os.environ.get("TWELVE_DATA_BASE", "https://api.twelvedata.com").rstrip("/")


def telegram_base():
    return os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")


def _http_error_text(e):
    """The provider's own explanation (Telegram and Twelve Data both send one) instead of a bare 'HTTP Error 400'.
    Never includes the URL, which carries the token."""
    try:
        body = json.loads(e.read().decode("utf-8", "replace"))
        return f"{e.code} {body.get('description') or body.get('message') or e.reason}"
    except Exception:  # noqa: BLE001
        return f"{e.code} {e.reason}"


def fetch_bars(api_key, symbol, interval_min, outputsize=5000, now=None, end_date=None):
    """Candles from Twelve Data, oldest first, forming candle removed. One request per call."""
    params = {"symbol": symbol, "interval": f"{interval_min}min", "outputsize": outputsize,
              "timezone": "UTC", "format": "JSON", "apikey": api_key}
    if end_date:
        params["end_date"] = end_date
    q = urllib.parse.urlencode(params)
    data = None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(twelve_base() + "/time_series?" + q, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            data = {"status": "error", "code": e.code, "message": _http_error_text(e)}
        # a per-minute credit limit clears at the start of the next minute: wait once and retry
        if attempt == 1 and data.get("status") == "error" and data.get("code") == 429:
            _time.sleep(20)
            continue
        break
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data.get('message', data)}")
    bars = []
    for v in data["values"]:
        t = int(datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp())
        bars.append(Bar(t, float(v["open"]), float(v["high"]), float(v["low"]), float(v["close"])))
    bars.sort(key=lambda b: b.t)
    return only_closed(bars, interval_min, now)


def only_closed(bars, interval_min, now=None):
    now = int(now if now is not None else _time.time())
    return [b for b in bars if b.t + interval_min * 60 <= now]


def gap_count(bars, interval_min, lookback_bars=2000):
    step = interval_min * 60
    recent = bars[-lookback_bars:]
    return sum(1 for a, b in zip(recent, recent[1:]) if b.t - a.t > step)


# --------------------------------------------------------------------------- telegram, config, files
def anchored_bars(history_path, fetched, save):
    """Make the live signal identical from run to run.

    A fresh fetch is only the latest 5000 candles, and the ATR (Wilder smoothing) remembers where the replay began,
    so two runs on shifted windows would compute slightly different stops. Instead every closed candle is kept in the
    history CSV and the engine always replays from that same first candle. `fetched` wins on overlap. A missing or
    unreadable CSV just means the run uses what it fetched."""
    old = []
    if os.path.exists(history_path):
        try:
            old = btc_history.load_csv(history_path)
        except (OSError, ValueError, KeyError) as e:
            print(f"Warning: could not read {history_path} ({e}); using the fetched candles only.", file=sys.stderr)
    bars = btc_history.merge(old, fetched)
    if save and fetched and len(bars) != len(old):
        btc_history.save_csv(history_path, bars)
    return bars


def send_telegram(token, chat_id, text):
    """Telegram caps a message at 4096 characters; longer texts are split at paragraph breaks."""
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > 3800:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    chunks.append(cur)
    for chunk in chunks:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk[:4000]}).encode()
        req = urllib.request.Request(f"{telegram_base()}/bot{token}/sendMessage", data=data)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status != 200:
                    return False
        except urllib.error.HTTPError as e:
            print(f"Telegram refused the message: {_http_error_text(e)}", file=sys.stderr)
            return False
        except Exception as e:  # noqa: BLE001
            print(f"Telegram send failed: {type(e).__name__}", file=sys.stderr)  # not str(e): keep the token out of logs
            return False
    return True


def ping(key, symbol, interval_min, token, chat):
    """Check every connection and say plainly which one is broken. Returns (ok, lines)."""
    lines, ok = [], True
    if not key:
        lines.append("Twelve Data: TWELVE_DATA_API_KEY is not set.")
        ok = False
    else:
        try:
            bars = fetch_bars(key, symbol, interval_min, outputsize=5)
            if bars:
                lines.append(f"Twelve Data: OK, {symbol} {interval_min}min, last closed candle "
                             f"{datetime.fromtimestamp(bars[-1].t, UTC).strftime('%Y-%m-%d %H:%M')} UTC, "
                             f"close {bars[-1].c:g}.")
            else:
                lines.append(f"Twelve Data: answered but returned no closed candles for {symbol}.")
                ok = False
        except Exception as e:  # noqa: BLE001
            lines.append(f"Twelve Data: FAILED, {e}")
            ok = False
    if not (token and chat):
        lines.append("Telegram: TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID is not set.")
        ok = False
    else:
        if send_telegram(token, chat, "BTC bot ping: Telegram works. " + (lines[0] if lines else "")):
            lines.append("Telegram: OK, a test message was sent to your chat.")
        else:
            lines.append("Telegram: FAILED (see the line above in the log: wrong token, wrong chat id, or you have "
                         "not pressed Start on the bot).")
            ok = False
    return ok, lines


def env_bool(name, default):
    return os.environ.get(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


def hhmm(s):
    h, m = s.split(":")
    return int(h), int(m)


def config_from_env():
    ch, cm = hhmm(os.environ.get("CLOSE", "16:30"))
    oh, om = hhmm(os.environ.get("REOPEN", "18:00"))
    cfg = Config(
        n=int(os.environ.get("N", 20)), m=int(os.environ.get("M", 10)),
        atr_len=int(os.environ.get("ATR_LEN", 20)), atr_mult=float(os.environ.get("ATR_MULT", 2.0)),
        day_trading=env_bool("DAY_TRADING", True), close_h=ch, close_min=cm, open_h=oh, open_min=om,
        interval_min=int(os.environ.get("INTERVAL_MIN", 30)),
    )
    cfg.validate()
    return cfg


def paths():
    """Every file this runner touches, in one place. File names are fixed so btc_commands finds them."""
    data = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
    return {"data": data, "state": os.path.join(data, "state.json"), "trades": os.path.join(data, "trades.jsonl"),
            "scans": os.path.join(data, "scans.jsonl"), "ledger": os.path.join(data, "bank_ledger.jsonl"),
            "latest": os.path.join(data, "latest.json"), "history": os.path.join(data, "history_30m.csv"),
            "backtest": os.path.join(data, "backtest"),
            "accounts": os.environ.get("ACCOUNTS_FILE", os.path.join(HERE, "accounts.json"))}


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")


def stamp_text(asof, display_tz):
    return btc_report.when(asof, display_tz)


# --------------------------------------------------------------------------- scan plumbing
def fetch_extra_frames(key, symbol):
    """15M and 5M candles for the scan. A failure is NOT fatal: that frame is simply reported as not checked."""
    out = []
    for iv in (15, 5):
        try:
            out.append(fetch_bars(key, symbol, iv, outputsize=400))
        except Exception as e:  # noqa: BLE001
            print(f"Warning: could not fetch {iv}min candles ({e}); that timeframe will say 'not checked'.",
                  file=sys.stderr)
            out.append(None)
    return out[0], out[1]


def scan_at(bars30, b15, b5, snaps, close_time):
    """Scan as of one candle close, with the strategy state recorded at that candle."""
    sub = [b for b in bars30 if b.t + 1800 <= close_time]
    return build_scan(sub, b15, b5, close_time, strategy=snaps.get(close_time))


# --------------------------------------------------------------------------- backtest / replay
def run_backtest(bars, cfg, accounts, out_dir=None):
    """Replay candles through both sides and every demo account (in memory only: the live ledger is never touched).
    Returns the results text; writes flat CSVs into out_dir when given."""
    lg, sh, events, _ = run_both(bars, cfg, keep_last=0)
    segs = btc_stats.segments_from_events(events)
    strategy = btc_stats.compute_stats(segs)
    by_side = {k: btc_stats.compute_stats(v) for k, v in btc_stats.split_by(segs, "side").items()}
    pairs, closed = [], []
    if accounts:
        bank = btc_bank.Bank(accounts, path=None)
        bank.ensure_accounts()
        for ev in events:
            bank.commit(bank.plan(ev)[1])
        closed = bank.closed_trades()
        pairs = [(row, btc_stats.compute_stats(bank.closed_trades(row["name"]), row["start"]))
                 for row in bank.summary()]
    first, last = datetime.fromtimestamp(bars[0].t, UTC), datetime.fromtimestamp(bars[-1].t, UTC)
    stamp = (f"{(bars[-1].t - bars[0].t) / 86400:.0f} days of candles, {first:%Y-%m-%d} to {last:%Y-%m-%d} UTC, "
             f"{'day-trading' if cfg.day_trading else 'swing'} mode")
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        btc_stats.write_closed_csv(os.path.join(out_dir, "strategy_trades.csv"), segs)
        btc_stats.write_closed_csv(os.path.join(out_dir, "account_trades.csv"), closed)
    return btc_report.stats_report("BTC replay of historical candles", stamp, strategy, by_side, pairs)


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Turtle Breakout BTC bot")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending; write nothing")
    ap.add_argument("--status", action="store_true", help="show the current state and exit")
    ap.add_argument("--report", action="store_true", help="show the technical + fundamental reports and exit")
    ap.add_argument("--stats", action="store_true", help="show statistics from the stored live record and exit")
    ap.add_argument("--backtest", action="store_true", help="replay history with full statistics and exit")
    ap.add_argument("--download-history", type=int, metavar="DAYS", help="download DAYS of candles to the CSV")
    ap.add_argument("--ping", action="store_true", help="test the Twelve Data key and the Telegram link, then exit")
    ap.add_argument("--send", action="store_true", help="with --stats/--backtest: send to Telegram")
    ap.add_argument("--csv", help="use 30-minute candles from this CSV instead of Twelve Data / the history file")
    args = ap.parse_args(argv)

    cfg = config_from_env()
    P = paths()
    symbol = os.environ.get("SYMBOL", "BTC/USD")
    display_tz = os.environ.get("DISPLAY_TZ", "Africa/Lagos")
    catchup_s = float(os.environ.get("MAX_CATCHUP_HOURS", 6)) * 3600
    scan_every_s = int(os.environ.get("SCAN_EVERY_MIN", 60)) * 60
    report_hour = int(os.environ.get("DAILY_REPORT_HOUR_UTC", 6))
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    key = os.environ.get("TWELVE_DATA_API_KEY")

    accounts, accounts_problem = btc_bank.load_accounts(P["accounts"])
    if accounts_problem:
        print(f"Warning: {accounts_problem}", file=sys.stderr)

    def deliver(text):
        if args.dry_run or not (token and chat):
            print(text + "\n" + "-" * 40)
            return args.dry_run  # without credentials a real run counts as NOT delivered
        return send_telegram(token, chat, text)

    def output(text):
        """--stats / --backtest: print, and also send when asked."""
        print(text)
        return deliver(text) if (args.send and not args.dry_run and token and chat) else True

    # ---- modes that do not need the live loop
    if args.ping:
        ok, lines = ping(key, symbol, cfg.interval_min, token, chat)
        print("\n".join(lines))
        return 0 if ok else 1

    if args.download_history:
        if not key:
            sys.exit("TWELVE_DATA_API_KEY is not set.")
        bars, rep = btc_history.download(lambda end, n: fetch_bars(key, symbol, cfg.interval_min, n, end_date=end),
                                         args.download_history)
        if not bars:
            sys.exit("No candles came back, so nothing was saved.")
        if os.path.exists(P["history"]):
            bars = btc_history.merge(btc_history.load_csv(P["history"]), bars)
        btc_history.save_csv(P["history"], bars)
        print(f"Saved {len(bars)} candles ({rep['days_covered']:.0f} days covered, {rep['pages']} request(s); "
              f"stopped because {rep['why']}) to {P['history']}")
        return 0

    if args.stats:
        latest = load_state(P["latest"])
        stamp = f"Recorded up to {btc_report.when(latest['asof'], display_tz)}" if latest else "No run recorded yet"
        return 0 if output(btc_commands.results_text(P["data"], accounts, "BTC results", stamp)) else 1

    if args.backtest:
        src = args.csv or (P["history"] if os.path.exists(P["history"]) else None)
        if not src:
            sys.exit("No history to replay. Run --download-history 365 first, or pass --csv file.")
        bars = btc_history.load_csv(src)
        if len(bars) < 1500:
            sys.exit(f"Only {len(bars)} candles in {src}: need at least about 1500 (a month) to warm up and trade.")
        gaps = gap_count(bars, cfg.interval_min, lookback_bars=len(bars))
        if gaps:
            print(f"Warning: {gaps} gap(s) in the candles; bands/ATR can be slightly off around gaps.", file=sys.stderr)
        out_dir = None if args.dry_run else P["backtest"]
        return 0 if output(run_backtest(bars, cfg, accounts, out_dir)) else 1

    # ---- live data
    if args.csv:
        bars = only_closed(btc_history.load_csv(args.csv), cfg.interval_min, now=10**12)
    else:
        if not key:
            sys.exit("TWELVE_DATA_API_KEY is not set.")
        bars = fetch_bars(key, symbol, cfg.interval_min)
        bars = anchored_bars(P["history"], bars, save=not (args.dry_run or args.status or args.report))
    if not bars:
        sys.exit("No closed candles returned.")
    gaps = gap_count(bars, cfg.interval_min)
    if gaps:
        print(f"Warning: {gaps} gap(s) in the candle data; the engine handles missed closes, "
              f"but bands/ATR can be slightly off around gaps.", file=sys.stderr)

    lg, sh, events, snaps = run_both(bars, cfg)
    last_bar_close = bars[-1].t + cfg.interval_min * 60
    statuses = {"LONG": lg.status(), "SHORT": sh.status()}

    def frames():
        return (None, None) if (args.csv or not key) else fetch_extra_frames(key, symbol)

    # the Bank replays its ledger; in a dry run it never writes
    bank = btc_bank.Bank(accounts, path=P["ledger"], write=not args.dry_run)
    if accounts:
        bank.ensure_accounts()  # a dry run keeps this in memory only (write=False), so sizing still works

    def account_block():
        if accounts_problem:
            return [f"Accounts: {accounts_problem}. Position sizes are not being calculated."]
        if not accounts:
            return [f"Accounts: no {os.path.basename(P['accounts'])} found, so no position sizes are calculated."]
        st = statuses["LONG"]
        return bank.summary_lines() + bank.warnings(st.get("last_close"), st.get("atr"))

    def write_latest():
        if not args.dry_run:
            save_json(P["latest"], {"asof": last_bar_close, "cfg": asdict(cfg), "statuses": statuses,
                                    "updated_utc": datetime.now(UTC).isoformat(timespec="seconds")})

    if args.status:
        deliver(btc_report.format_status(statuses, cfg, account_block()))
        return 0

    if args.report:
        b15, b5 = frames()
        scan = scan_at(bars, b15, b5, snaps, last_bar_close)
        stamp = stamp_text(last_bar_close, display_tz)
        ok = deliver(btc_report.technical_report(scan, stamp))
        ok = deliver(btc_report.fundamental_report(scan, stamp)) and ok
        return 0 if ok else 1

    state = load_state(P["state"])
    now = int(_time.time())

    if state is None:
        hello = (f"BTC Turtle bot is online ({symbol}, {cfg.interval_min}m candles, long and short).\n\n"
                 + btc_report.format_status(statuses, cfg, account_block()))
        if deliver(hello) and not args.dry_run:
            b15, b5 = frames()
            append_jsonl(P["scans"], {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "tag": "first_run",
                                      **scan_at(bars, b15, b5, snaps, last_bar_close)})
            save_json(P["state"], {"last_close_time": last_bar_close, "last_scan_close_time": last_bar_close,
                                   "last_report_day": "", "updated_utc": datetime.now(UTC).isoformat(timespec="seconds")})
            write_latest()
        return 0

    last_seen = state["last_close_time"]
    recorded = {r["event_id"] for r in read_jsonl(P["trades"])}   # never record or alert the same signal twice
    pending = [e for e in events if e.close_time > last_seen and e.event_id not in recorded]
    stale = [e for e in pending if e.close_time < now - catchup_s]
    new_state = dict(state)
    b15 = b5 = None
    have_frames = False
    delivered = 0
    failed_close = None

    to_process = pending
    if stale and not deliver(btc_report.format_catchup(stale, display_tz)):
        failed_close, to_process = stale[0].close_time - 1, []

    for ev in to_process:
        is_stale = ev.close_time < now - catchup_s
        if not have_frames and not is_stale:
            b15, b5 = frames()
            have_frames = True
        scan = scan_at(bars, b15 if not is_stale else None, b5 if not is_stale else None, snaps, ev.close_time)
        results, entries = bank.plan(ev) if accounts else ([], [])
        if not is_stale:
            notes = [f"Accounts: {accounts_problem}. Position sizes were not calculated."] if accounts_problem else []
            text = btc_report.format_event(ev, cfg, display_tz, context=btc_report.context_lines(scan, ev.side),
                                           accounts=btc_report.account_lines(results), notes=notes)
            if not deliver(text):
                failed_close = ev.close_time - 1  # retry this one (and later ones) next run
                break
            delivered += 1
        bank.commit(entries)  # in memory only on a dry run, so later events in the run see earlier ones
        if not args.dry_run:
            ts = datetime.now(UTC).isoformat(timespec="seconds")
            append_jsonl(P["trades"], {"ts": ts, "event_id": ev.event_id, "side": ev.side, "kind": ev.kind,
                                       "bar_time": ev.t, "close_time": ev.close_time, "price": ev.price,
                                       "entry_level": ev.entry_level, "exit_level": ev.exit_level, "stop": ev.stop,
                                       "active_stop": ev.active_stop, "atr": ev.atr, "late": ev.late,
                                       "flat": ev.flat, "alerted": not is_stale, "accounts": results})
            append_jsonl(P["scans"], {"ts": ts, "tag": f"event:{ev.event_id}", **scan})
            new_state["last_scan_close_time"] = max(new_state.get("last_scan_close_time", 0), ev.close_time)
    new_state["last_close_time"] = failed_close if failed_close is not None else max(last_seen, last_bar_close)

    # scheduled scan (stored only) and the daily report (sent), once the events are out of the way
    if failed_close is None:
        due_scan = last_bar_close - new_state.get("last_scan_close_time", 0) >= scan_every_s
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        due_report = (report_hour >= 0 and datetime.now(UTC).hour >= report_hour
                      and new_state.get("last_report_day") != today)
        if due_scan or due_report:
            if not have_frames:
                b15, b5 = frames()
            scan = scan_at(bars, b15, b5, snaps, last_bar_close)
            if due_scan and not args.dry_run:
                append_jsonl(P["scans"], {"ts": datetime.now(UTC).isoformat(timespec="seconds"),
                                          "tag": "scheduled", **scan})
                new_state["last_scan_close_time"] = last_bar_close
            if due_report:
                stamp = stamp_text(last_bar_close, display_tz)
                ok1 = deliver(btc_report.technical_report(scan, stamp))
                ok2 = ok1 and deliver(btc_report.fundamental_report(scan, stamp))
                if ok2 and not args.dry_run:
                    new_state["last_report_day"] = today

    if not args.dry_run:
        new_state["updated_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
        save_json(P["state"], new_state)
        write_latest()
    print(f"{delivered} new event(s) alerted, {len(stale)} recorded without alert; last candle closed "
          f"{datetime.fromtimestamp(last_bar_close, UTC):%Y-%m-%d %H:%M} UTC", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
