"""chat_records.py — the only place the conversation basics read files: the candle logs, the scan/signal/shadow/case logs, the BUS ledger rows and
the account profile. It reads and reduces; it decides nothing. chat.py (pure) turns what is read here into answers."""
import json
import os
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
TF_FILE = {"5m": "ohlc_5m_log.jsonl", "15m": "ohlc_15m_log.jsonl", "1h": "ohlc_1h_log.jsonl"}


def _ts(x):
    try:
        t = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except Exception:
        return None


_CACHE = {}


class Candles(list):
    """A list of candles that also knows how many malformed rows were left out of it."""
    dropped = 0


def _sane(row):
    o, h, l, c = row["o"], row["h"], row["l"], row["c"]
    vals = (o, h, l, c)
    if any(not isinstance(v, float) or v != v or v in (float("inf"), float("-inf")) or v <= 0 for v in vals):
        return False
    return h >= max(o, c) - 1e-9 and l <= min(o, c) + 1e-9


def load_candles(root, tf):
    """Recorded candles for one timeframe: sorted, one per timestamp (the logs repeat rows; a conflicting repeat keeps the later one).
    Rows that cannot be real prices (not a number, zero or negative, high below low, high or low that does not contain the open and close, or more than 10% away from the file's
    typical price) are LEFT OUT and counted in `.dropped`, so a damaged log cannot feed an invented number into an answer."""
    path = os.path.join(root, TF_FILE[tf])
    out = Candles()
    if not os.path.exists(path):
        return out
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)               # unchanged file -> the parsed result from last time (the log is re-read only when the scanner appends to it)
    hit = _CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    by, bad = {}, 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                t = _ts(r["datetime"])
                row = {"t": t, "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"]}
                row = {k: (float(v) if k != "t" and isinstance(v, (int, float)) and not isinstance(v, bool) else v) for k, v in row.items()}
                if t is None or not _sane(row):
                    bad += 1
                    continue
                by[t] = row
            except Exception:
                bad += 1
    if by:
        closes = sorted(v["c"] for v in by.values())
        med = closes[len(closes) // 2]
        for k in list(by):
            v = by[k]
            if any(abs(v[x] - med) / med > 0.10 for x in ("o", "h", "l", "c")):
                del by[k]
                bad += 1
    out.extend(by[k] for k in sorted(by))
    out.dropped = bad
    _CACHE[path] = (key, out)
    return out


def _read(path, key):
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


def load_counts_source(root, ledger_records):
    """The recorded events, each already reduced to times. `ledger_records` = the BUS ledger rows (the runner already has them)."""
    g = lambda name: _read(os.path.join(root, name), None)
    sigs = []
    for r in g("signal_event_log.jsonl"):
        t = _ts(r.get("fired_at"))
        if t:
            sigs.append({"t": t, "risk_pips": ((r.get("risk_result") or {}).get("risk_pips")), "direction": r.get("direction")})
    by = lambda typ: [_ts(r.get("recorded_at")) for r in ledger_records if r.get("event_type") == typ]
    return {
        "scans": [_ts(r.get("scanned_at")) for r in g("scan_log.jsonl")],
        "signals": sigs,
        "shadow": [_ts(r.get("opened_at")) for r in g("shadow_trade_log.jsonl")],
        "cases": [_ts(r.get("opened_at")) for r in g("failure_case_log.jsonl")],
        "rejected": [_ts(r.get("opened_at")) for r in g("shadow_trade_log.jsonl") if r.get("experiment") == "EXPE_REJECTED_LIVE"],
        "announced": by("BUS_SIGNAL_ANNOUNCED"),
        "my_trades": by("TRADE_LOGGED"),
        "received": by("MESSAGE_RECEIVED"),
        "replied": by("BUS_REPLY"),
    }


def load_profile(root):
    try:
        with open(os.path.join(root, "account_profiles.json"), encoding="utf-8") as f:
            d = json.load(f)
        p = d["profiles"][d["active"]]
        return {"name": p.get("name", d["active"]), "balance": float(p["balance"]), "risk_pct": float(p["risk_pct"])}
    except Exception:
        return None


def load_state_times(root):
    """The engine's own leg/phase timing from state.json (read-only): {origin, direction, ts, phase, age_bars} or None."""
    try:
        with open(os.path.join(root, "state.json"), encoding="utf-8") as f:
            s = json.load(f)
        o = _ts(s.get("macro_leg_origin_time"))
        return {"origin": o, "direction": s.get("macro_leg_direction"), "ts": _ts(s.get("timestamp")), "phase": s.get("market_phase"), "age_bars": s.get("market_phase_age_bars")}
    except Exception:
        return None


def last_signal_record(root, now=None):
    """-> the newest signal_event_log record exactly as the engine wrote it (a dict), or None. Unreadable lines are skipped."""
    import json, os
    best, best_t = None, None
    try:
        with open(os.path.join(root, "signal_event_log.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    t = _ts(r.get("fired_at"))
                except Exception:
                    continue
                if isinstance(r, dict) and t is not None and (now is None or t <= now + timedelta(minutes=5)) and (best_t is None or t >= best_t):
                    best, best_t = r, t
    except OSError:
        return None
    return best
