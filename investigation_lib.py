"""
Shared library for shadow_investigation.py and live_investigation.py.

Both scripts do the same fundamental thing: take a log of resolved
trades (thin — outcome, R, timing, a few experiment/tier tags) and join
it against leg_obs_log.jsonl (rich — market_phase, regime, volatility,
campaign extension) by timestamp containment, so every trade can be
examined with the market conditions that were actually present when it
happened. This file holds the join and the reporting helpers; the two
driver scripts only differ in which log they read and which fields they
group by (experiment for shadow, tier for live).

See each driver script's own module docstring for usage and the
caveats on join granularity — repeated here only where it matters for
a specific function.
"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone


def load_jsonl(path):
    records = []
    try:
        with open(path, "r") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"  [WARN] {path}:{line_num} malformed JSON, skipping: {e}", file=sys.stderr)
    except FileNotFoundError:
        print(f"[ERROR] File not found: {path}", file=sys.stderr)
        sys.exit(1)
    return records


def parse_ts(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return None


def build_leg_intervals(leg_records):
    """Sorted list of (opened_at, resolved_at, formation_state, fate).
    leg_obs_log.jsonl only contains RESOLVED legs (_append_leg_obs_log),
    so every entry has both timestamps — no open-ended intervals here."""
    intervals = []
    for rec in leg_records:
        opened = parse_ts(rec.get("opened_at"))
        resolved = parse_ts(rec.get("resolved_at"))
        if opened is None or resolved is None:
            continue
        intervals.append((opened, resolved, rec.get("formation_state") or {}, rec.get("fate")))
    intervals.sort(key=lambda t: t[0])
    return intervals


def find_enclosing_leg(trade_opened_at, leg_intervals):
    """Linear scan — fine for an offline script over weeks of legs, not
    a live-path concern."""
    if trade_opened_at is None:
        return None
    for opened, resolved, formation_state, fate in leg_intervals:
        if opened <= trade_opened_at < resolved:
            return formation_state, fate
    return None


def iso_week_key(dt):
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


CONDITION_DIMENSIONS = [
    "market_phase", "atr_bucket", "session", "bias_state",
    "spike_state", "measured_move_bucket",
]


def enrich_trades(trades, leg_intervals, opened_key="opened_at", resolved_key="resolved_at"):
    """
    Attaches market-condition context to each trade via the leg_obs join.
    `opened_key`/`resolved_key` let callers handle field-name differences
    between logs (shadow trades use resolved_at, live trades use
    closed_at — see each driver script).

    Returns a list of enriched dicts, each the original trade record
    plus: _opened_dt, _resolved_dt, _context (formation_state or None),
    _enclosing_leg_fate. Trades with no resolvable timestamp are dropped
    (can't be placed on a timeline); trades with no enclosing leg keep
    _context=None rather than being dropped — see driver scripts for how
    that's surfaced (never silently discarded).
    """
    enriched = []
    unmatched = 0
    for t in trades:
        opened = parse_ts(t.get(opened_key))
        resolved = parse_ts(t.get(resolved_key))
        if resolved is None:
            continue
        match = find_enclosing_leg(opened, leg_intervals)
        context, leg_fate = (None, None) if match is None else match
        if match is None:
            unmatched += 1
        row = dict(t)
        row["_opened_dt"] = opened
        row["_resolved_dt"] = resolved
        row["_context"] = context
        row["_enclosing_leg_fate"] = leg_fate
        enriched.append(row)
    enriched.sort(key=lambda r: r["_resolved_dt"])
    return enriched, unmatched


def summarize_conditions(rows):
    """Counts the condition mix across a set of enriched trade rows —
    used to compare cohorts (e.g. rise vs decay window, or winners vs
    losers)."""
    counters = {dim: defaultdict(int) for dim in CONDITION_DIMENSIONS}
    for r in rows:
        ctx = r.get("_context")
        for dim in CONDITION_DIMENSIONS:
            val = (ctx or {}).get(dim, "unknown") if ctx else "no_matching_leg"
            counters[dim][val or "unknown"] += 1
    return counters


def format_counter_block(title, counters, total):
    if total == 0:
        return f"  {title}: (no trades)"
    lines = [f"  {title}:"]
    for dim, counts in counters.items():
        parts = sorted(counts.items(), key=lambda kv: -kv[1])
        parts_str = ", ".join(f"{k}={v} ({v/total*100:.0f}%)" for k, v in parts)
        lines.append(f"    {dim}: {parts_str}")
    return "\n".join(lines)


def build_weekly_curve(rows, r_key="r_achieved"):
    """Weekly cumulative R, keyed on _resolved_dt. Returns
    (curve, weekly_dict) where curve is a list of
    (week, weekly_r, cumulative_r, n_trades) sorted by week."""
    weekly = defaultdict(lambda: {"rows": [], "r_sum": 0.0})
    for r in rows:
        wk = iso_week_key(r["_resolved_dt"])
        weekly[wk]["rows"].append(r)
        weekly[wk]["r_sum"] += (r.get(r_key) or 0.0)

    weeks_sorted = sorted(weekly.keys())
    cumulative = 0.0
    curve = []
    for wk in weeks_sorted:
        cumulative += weekly[wk]["r_sum"]
        curve.append((wk, weekly[wk]["r_sum"], cumulative, len(weekly[wk]["rows"])))
    return curve, weekly


def find_peak_then_trough(curve):
    """Auto-locates the global cumulative-R peak, then the lowest point
    AFTER that peak. Returns (peak_idx, trough_idx) — trough_idx is None
    if the peak is the last point (nothing after it to decay into)."""
    if not curve:
        return None, None
    peak_idx = max(range(len(curve)), key=lambda i: curve[i][2])
    trough_idx = None
    if peak_idx < len(curve) - 1:
        trough_idx = min(range(peak_idx, len(curve)), key=lambda i: curve[i][2])
    return peak_idx, trough_idx


def context_flat(ctx):
    """Flattens a formation_state dict into the fixed column set used
    by per-trade CSV/table output — missing keys show as '' rather than
    raising, since older leg_obs records may predate a given field."""
    ctx = ctx or {}
    return {dim: ctx.get(dim, "") for dim in CONDITION_DIMENSIONS}
