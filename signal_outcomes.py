"""
signal_outcomes.py — resolve undelivered-signal records into outcomes,
using ONLY candles that came after the signal. Read-only. Stdlib only.

WHY THIS EXISTS (2026-09-30, communication rebuild)
---------------------------------------------------
While scanner_live.py runs in engine-only mode, every signal that fires and
passes the risk gate is written to signal_event_log.jsonl as
FIRED_RISK_GATE_PASSED_UNDELIVERED — but no trade opens, so nothing ever
resolves it. Without this module those records are a list of entries with no
results, and the whole silent period is a hole in the evidence base. This
closes it, deterministically, from the permanent OHLC archive.

THE NO-HINDSIGHT RULE (the one primitive everything later depends on)
---------------------------------------------------------------------
An outcome for a signal may be computed ONLY from candles strictly AFTER the
signal's `entry_candle_ts`, and the signal's own fields (entry/sl/tp) are the
only thing taken from the record. Nothing about the outcome is ever written
back into, or inferred into, the original record. `resolve_signal()` takes the
candle list as an argument and filters it itself, so a caller cannot
accidentally hand it the future and have it leak into the decision side.

WHAT THIS IS *NOT*
------------------
This is a RAW first-touch outcome: stop vs. target on 5-minute bars. It does
NOT model the live trade-management rules (partial at partial_r, break-even
at breakeven_r, position sizing by band) because those live in scanner_live.py
and this module is layer-isolated from it on purpose. Treat the R numbers as
"what the bare stop/target pair did", an upper-level sanity check, not a
replacement for a faithful replay of manage_active_trade().

KNOWN BIASES (printed by summarize(), not hidden)
-------------------------------------------------
- Same-bar tie (a bar whose range touches BOTH stop and target): counted as a
  STOP. Conservative; real order of touches inside a 5m bar is unknowable.
- No spread/slippage/commission is applied.
- Signals are NOT independent: with no trade freeze in engine-only mode,
  neighbouring legs overlap in time and one macro move can resolve several
  signals the same way. Any per-signal Sharpe is therefore optimistic.
- Unresolved signals (neither level hit within `max_bars`) are reported as
  OPEN, never silently treated as wins or losses.
"""

import json
import math
import os

STATUS_UNDELIVERED = "FIRED_RISK_GATE_PASSED_UNDELIVERED"

DEFAULT_MAX_BARS = 288   # 24h of 5-minute bars


# ---------------------------------------------------------------- loading
def _read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue          # never let one corrupt line sink the run
    return rows


def load_signal_events(path):
    """Undelivered signal records only, oldest first."""
    ev = [r for r in _read_jsonl(path) if r.get("status") == STATUS_UNDELIVERED]
    ev.sort(key=lambda r: r.get("fired_at") or "")
    return ev


def load_candles(path):
    """5m archive rows -> list of dicts sorted by datetime (ISO strings
    compare correctly because the archive writes ts.isoformat() with a
    consistent offset)."""
    rows = [r for r in _read_jsonl(path) if "datetime" in r]
    rows.sort(key=lambda r: r["datetime"])
    return rows


# ---------------------------------------------------------------- core
def resolve_signal(event, candles, max_bars=DEFAULT_MAX_BARS):
    """Return an outcome dict for one signal event.

    Strictly causal: only candles with datetime > event['entry_candle_ts']
    are examined. `candles` may contain the whole archive; filtering happens
    here.
    """
    rr = event.get("risk_result") or {}
    entry, sl, tp = rr.get("entry"), rr.get("sl"), rr.get("tp")
    direction = (event.get("direction") or "").upper()
    base = {
        "event_id": event.get("event_id"),
        "fired_at": event.get("fired_at"),
        "tier_label": event.get("tier_label"),
        "direction": direction,
        "leg_id": event.get("leg_id"),
    }
    if None in (entry, sl, tp) or direction not in ("BUY", "SELL"):
        return {**base, "outcome": "INVALID", "reason": "missing entry/sl/tp/direction"}

    risk = (entry - sl) if direction == "BUY" else (sl - entry)
    reward = (tp - entry) if direction == "BUY" else (entry - tp)
    if risk <= 0 or reward <= 0:
        return {**base, "outcome": "INVALID",
                "reason": f"non-positive risk/reward (risk={risk}, reward={reward})"}
    target_r = reward / risk

    t0 = event.get("entry_candle_ts")
    if not t0:
        return {**base, "outcome": "INVALID", "reason": "no entry_candle_ts"}
    after = [c for c in candles if c["datetime"] > t0][:max_bars]

    mfe = 0.0     # best excursion in R
    mae = 0.0     # worst excursion in R (negative)
    for i, c in enumerate(after, start=1):
        hi, lo = c["high"], c["low"]
        if direction == "BUY":
            hit_sl, hit_tp = lo <= sl, hi >= tp
            fav, adv = (hi - entry) / risk, (lo - entry) / risk
        else:
            hit_sl, hit_tp = hi >= sl, lo <= tp
            fav, adv = (entry - lo) / risk, (entry - hi) / risk
        mfe, mae = max(mfe, fav), min(mae, adv)

        if hit_sl and hit_tp:
            return {**base, "outcome": "LOSS", "r": -1.0, "bars": i,
                    "resolved_at": c["datetime"], "tie_bar": True,
                    "target_r": round(target_r, 3),
                    "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}
        if hit_sl:
            return {**base, "outcome": "LOSS", "r": -1.0, "bars": i,
                    "resolved_at": c["datetime"], "tie_bar": False,
                    "target_r": round(target_r, 3),
                    "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}
        if hit_tp:
            return {**base, "outcome": "WIN", "r": round(target_r, 3), "bars": i,
                    "resolved_at": c["datetime"], "tie_bar": False,
                    "target_r": round(target_r, 3),
                    "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}

    return {**base, "outcome": "OPEN", "r": None, "bars": len(after),
            "bars_available": len(after), "target_r": round(target_r, 3),
            "mfe_r": round(mfe, 3), "mae_r": round(mae, 3),
            "reason": ("no stop/target touch in the candles available so far"
                       if len(after) < max_bars else "horizon reached without a touch")}


def resolve_all(events, candles, max_bars=DEFAULT_MAX_BARS):
    return [resolve_signal(e, candles, max_bars=max_bars) for e in events]


# ---------------------------------------------------------------- stats
def _wilson(wins, n, z=1.96):
    if n == 0:
        return (None, None)
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(c - h, 3), round(c + h, 3))


def summarize(outcomes):
    """Per-tier and overall: n, win rate + Wilson CI, mean R, stdev R, and a
    naive per-signal Sharpe (mean/stdev of R, NOT annualised). Always
    carries the caveat list — the numbers are not meaningful without it."""
    def block(rows):
        res = [o for o in rows if o["outcome"] in ("WIN", "LOSS")]
        rs = [o["r"] for o in res]
        n = len(res)
        wins = sum(1 for o in res if o["outcome"] == "WIN")
        mean = sum(rs) / n if n else None
        sd = None
        if n > 1:
            sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1))
        return {
            "resolved": n, "open": sum(1 for o in rows if o["outcome"] == "OPEN"),
            "invalid": sum(1 for o in rows if o["outcome"] == "INVALID"),
            "wins": wins, "win_rate": round(wins / n, 3) if n else None,
            "win_rate_ci95": _wilson(wins, n),
            "mean_r": round(mean, 3) if mean is not None else None,
            "stdev_r": round(sd, 3) if sd else None,
            "per_signal_sharpe_naive": round(mean / sd, 3) if (sd and mean is not None) else None,
            "tie_bars_counted_as_loss": sum(1 for o in res if o.get("tie_bar")),
        }
    tiers = sorted({o.get("tier_label") for o in outcomes if o.get("tier_label")})
    return {
        "overall": block(outcomes),
        "by_tier": {t: block([o for o in outcomes if o.get("tier_label") == t]) for t in tiers},
        "caveats": [
            "Raw first-touch stop/target on 5m bars; live partials/break-even are NOT modelled.",
            "Same-bar stop+target touches are counted as losses (conservative).",
            "No spread, slippage or commission applied.",
            "Signals overlap in time and are not independent; per-signal Sharpe is optimistic.",
            "Small n: read win_rate_ci95, not win_rate.",
        ],
    }


def run(signal_log="signal_event_log.jsonl", candle_log="ohlc_5m_log.jsonl",
        max_bars=DEFAULT_MAX_BARS):
    events = load_signal_events(signal_log)
    candles = load_candles(candle_log)
    outcomes = resolve_all(events, candles, max_bars=max_bars)
    return outcomes, summarize(outcomes)


if __name__ == "__main__":
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    outs, summ = run(os.path.join(root, "signal_event_log.jsonl"),
                     os.path.join(root, "ohlc_5m_log.jsonl"))
    print(json.dumps({"summary": summ, "outcomes": outs}, indent=2, default=str))
