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

# DIMENSION-SPECIFIC RELABELING (2026-08-29, per chat — Vally: "audit
# the semantics and source path of measured_move_bucket = unknown
# before turning this into a conclusion"). Traced it: compute_measured_
# move_extension() (scanner_observation.py) returns None specifically
# when prior_macro_leg_origin/extreme are None — i.e. no valid PRIOR
# leg snapshot existed when this leg formed. That's a DATA-LINEAGE
# state (a fact about what was and wasn't recorded), not a market
# reading, and showing it as generic "unknown" right alongside genuine
# market values (expansion/exhaustion) invited exactly the
# misinterpretation Vally caught. Every other dimension's missing
# value genuinely has no more specific story than "wasn't populated",
# so those stay "unknown".
_NONE_LABELS = {
    "measured_move_bucket": "no_prior_leg",
}


def _display_value(dim, val):
    if val:
        return val
    return _NONE_LABELS.get(dim, "unknown")


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
    """
    Counts the condition mix across a set of enriched trade rows — used
    to compare cohorts (e.g. rise vs decay window, or winners vs
    losers).

    FIXED (2026-08-29, per chat — real report output showed rise/decay
    coverage as low as 4%/45% unmatched for some experiments, meaning
    the ORIGINAL version of this function — which folded "no_matching_
    leg" in as just another value within each dimension and percented
    against the FULL row count — was diluting every real condition
    percentage by however many trades had zero condition data. A 47%
    expansion / 45% exhaustion split reads very differently once you
    know it's actually computed over the 55% of trades that had a
    match, not all of them.

    Now returns match coverage explicitly (total/matched) and computes
    every dimension's percentages against the MATCHED count only —
    rows with no context are excluded from the dimension counters
    entirely rather than being counted as a fake "no_matching_leg"
    category value. See format_counter_block() below for how coverage
    is surfaced instead of hidden.
    """
    matched_rows = [r for r in rows if r.get("_context")]
    total = len(rows)
    matched = len(matched_rows)
    counters = {dim: defaultdict(int) for dim in CONDITION_DIMENSIONS}
    for r in matched_rows:
        ctx = r["_context"]
        for dim in CONDITION_DIMENSIONS:
            val = _display_value(dim, ctx.get(dim))
            counters[dim][val] += 1
    return {"total": total, "matched": matched, "counters": counters}


def format_counter_block(title, summary):
    total = summary["total"]
    matched = summary["matched"]
    if total == 0:
        return f"  {title}: (no trades)"
    coverage_pct = matched / total * 100 if total else 0.0
    lines = [f"  {title} — {matched}/{total} trades matched to a leg ({coverage_pct:.0f}% coverage):"]
    if matched == 0:
        lines.append("    (no matched trades — condition mix unavailable, don't infer anything from zero data)")
        return "\n".join(lines)
    for dim, counts in summary["counters"].items():
        parts = sorted(counts.items(), key=lambda kv: -kv[1])
        parts_str = ", ".join(f"{k}={v} ({v/matched*100:.0f}%)" for k, v in parts)
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


# ---------------------------------------------------------------------
# OUTCOME-CONDITIONED ANALYSIS (2026-08-29, per chat — Vally's review of
# the first report: cohort composition percentages alone can't tell you
# WHY performance changed. Three levels, matching his ordering exactly:
#   A. outcome_by_dimension()      — per-value win rate/expectancy/PF
#   B. compare_windows_by_dimension() — same value, rise vs decay: is it
#      composition shift (more of a weak bucket) or real decay (the SAME
#      bucket got worse)?
#   C. interaction_performance()   — 2D crosstab (e.g. phase x measured_
#      move), where a single-dimension view can hide the real story.
#
# All three use r_achieved's SIGN for win/loss math, not the outcome
# string — the real data has WIN/LOSS/TIMEOUT_WIN/TIMEOUT_LOSS, and
# string-matching against an assumed fixed set is exactly the kind of
# thing that silently breaks when a new outcome label gets added later.
# The raw label distribution is still reported separately (see
# _outcome_label_counts) since a rising TIMEOUT rate is itself a
# potentially real signal, not just noise to collapse away.
# ---------------------------------------------------------------------

def _outcome_label_counts(rows):
    counts = defaultdict(int)
    for r in rows:
        counts[r.get("outcome") or "unknown"] += 1
    return dict(counts)


def _r_stats(rows, r_key="r_achieved"):
    """Core win rate / expectancy / profit factor math, shared by all
    three analysis levels below. R-sign based, not outcome-label based
    — see module note above for why."""
    n = len(rows)
    if n == 0:
        return {"n": 0, "win_rate": None, "expectancy": None, "profit_factor": None, "outcomes": {}}
    r_values = [r.get(r_key) or 0.0 for r in rows]
    wins = [v for v in r_values if v > 0]
    losses = [v for v in r_values if v < 0]
    win_r = sum(wins)
    loss_r = abs(sum(losses))
    if loss_r > 0:
        pf = win_r / loss_r
    elif win_r > 0:
        pf = None  # no losses at all — "infinite" PF is not a meaningful number to print
    else:
        pf = 0.0
    return {
        "n": n,
        "win_rate": len(wins) / n * 100,
        "expectancy": sum(r_values) / n,
        "profit_factor": pf,
        "outcomes": _outcome_label_counts(rows),
    }


def outcome_by_dimension(rows, dimensions=None):
    """
    LEVEL A — per-value performance for each condition dimension.
    Only computed over rows with a matched leg (see summarize_
    conditions()'s docstring for why unmatched rows can't contribute a
    condition value at all — same principle applies here, not repeated).
    Returns {dimension: {value: stats_dict}}.
    """
    dimensions = dimensions or CONDITION_DIMENSIONS
    matched = [r for r in rows if r.get("_context")]
    result = {}
    for dim in dimensions:
        by_value = defaultdict(list)
        for r in matched:
            val = _display_value(dim, r["_context"].get(dim))
            by_value[val].append(r)
        result[dim] = {val: _r_stats(group) for val, group in by_value.items()}
    return result


def format_outcome_by_dimension(title, breakdown, min_n=5):
    """
    min_n (2026-08-29, per chat — small-sample experiments in the first
    report showed 100% figures built on 1-2 trades, which is noise, not
    a finding). Buckets below min_n are still counted but their stats
    are shown as "n too small" rather than a misleadingly precise
    percentage.
    """
    lines = [f"  {title}:"]
    for dim, values in breakdown.items():
        lines.append(f"    {dim}:")
        for val, stats in sorted(values.items(), key=lambda kv: -kv[1]["n"]):
            if stats["n"] < min_n:
                lines.append(f"      {val}: n={stats['n']} (too small to trust)")
                continue
            pf_str = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] is not None else "inf (no losses)"
            lines.append(
                f"      {val}: n={stats['n']}, win_rate={stats['win_rate']:.0f}%, "
                f"expectancy={stats['expectancy']:+.2f}R, PF={pf_str}"
            )
    return "\n".join(lines)


def compare_windows_by_dimension(rise_rows, decay_rows, dimensions=None, min_n=5):
    """
    LEVEL B — the crucial one (per chat, directly quoting the ask): for
    each value within each dimension, show expectancy in the rise
    window AND the decay window side by side, plus each window's share
    of trades (composition). Two different failure modes look
    different here:
      - COMPOSITION SHIFT: a value's expectancy is similar in both
        windows, but its SHARE of trades dropped (or rose) — the edge
        didn't change, the market just produced less/more of the
        condition that already worked/didn't.
      - REAL DECAY: the SAME value's expectancy itself got worse
        between windows — something changed within that condition
        specifically, not just how often it occurred.
    Both can be true for different dimensions at once — this returns
    everything and leaves the read to whoever's looking at it, per
    Vally's own framing: this tool explains, it doesn't conclude.
    """
    dimensions = dimensions or CONDITION_DIMENSIONS
    rise_matched = [r for r in rise_rows if r.get("_context")]
    decay_matched = [r for r in decay_rows if r.get("_context")]
    rise_total = len(rise_matched)
    decay_total = len(decay_matched)

    result = {}
    for dim in dimensions:
        rise_by_value = defaultdict(list)
        decay_by_value = defaultdict(list)
        for r in rise_matched:
            rise_by_value[_display_value(dim, r["_context"].get(dim))].append(r)
        for r in decay_matched:
            decay_by_value[_display_value(dim, r["_context"].get(dim))].append(r)

        all_values = set(rise_by_value) | set(decay_by_value)
        dim_result = {}
        for val in all_values:
            r_rows = rise_by_value.get(val, [])
            d_rows = decay_by_value.get(val, [])
            dim_result[val] = {
                "rise": _r_stats(r_rows),
                "decay": _r_stats(d_rows),
                "rise_share_pct": (len(r_rows) / rise_total * 100) if rise_total else 0.0,
                "decay_share_pct": (len(d_rows) / decay_total * 100) if decay_total else 0.0,
            }
        result[dim] = dim_result
    return result


def format_window_comparison(title, breakdown, min_n=5):
    lines = [f"  {title}:"]
    for dim, values in breakdown.items():
        lines.append(f"    {dim}:")
        for val, d in sorted(values.items(), key=lambda kv: -(kv[1]["rise"]["n"] + kv[1]["decay"]["n"])):
            rise, decay = d["rise"], d["decay"]
            rise_exp = f"{rise['expectancy']:+.2f}R" if rise["n"] >= min_n else f"n={rise['n']} (too small)"
            decay_exp = f"{decay['expectancy']:+.2f}R" if decay["n"] >= min_n else f"n={decay['n']} (too small)"
            lines.append(
                f"      {val}: rise {rise_exp} ({d['rise_share_pct']:.0f}% of rise trades, n={rise['n']}) "
                f"vs decay {decay_exp} ({d['decay_share_pct']:.0f}% of decay trades, n={decay['n']})"
            )
    return "\n".join(lines)


def interaction_performance(rows, dim1, dim2, min_n=5):
    """
    LEVEL C — 2D crosstab. Only matched rows contribute (same reasoning
    as outcome_by_dimension()). Returns {(val1, val2): stats_dict}.
    """
    matched = [r for r in rows if r.get("_context")]
    by_pair = defaultdict(list)
    for r in matched:
        v1 = _display_value(dim1, r["_context"].get(dim1))
        v2 = _display_value(dim2, r["_context"].get(dim2))
        by_pair[(v1, v2)].append(r)
    return {pair: _r_stats(group) for pair, group in by_pair.items()}


def format_interaction(title, dim1, dim2, breakdown, min_n=5):
    lines = [f"  {title} ({dim1} x {dim2}):"]
    for (v1, v2), stats in sorted(breakdown.items(), key=lambda kv: -kv[1]["n"]):
        if stats["n"] < min_n:
            lines.append(f"    {v1} x {v2}: n={stats['n']} (too small to trust)")
            continue
        pf_str = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] is not None else "inf (no losses)"
        lines.append(
            f"    {v1} x {v2}: n={stats['n']}, win_rate={stats['win_rate']:.0f}%, "
            f"expectancy={stats['expectancy']:+.2f}R, PF={pf_str}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------
# INTEGRITY / COVERAGE LAYER (2026-08-29, per chat — Vally's review of
# the real report: "no_matching_leg isn't a market phase. unknown
# measured move isn't necessarily a market phase either. They're data
# lineage states... right now the investigation output mixes them in
# the same condition distribution." His proposed fix, built here
# directly: report coverage and comparability FIRST, as its own
# section, before any market-condition percentage — so missing context
# can never masquerade as a market finding again.
# ---------------------------------------------------------------------

def compute_integrity_report(all_rows, rise_rows=None, decay_rows=None):
    """
    Coverage summary for a set of enriched trades, optionally split into
    rise/decay windows. `comparability` is a plain-language bound on how
    much the rise-vs-decay condition comparison can be trusted:
      GOOD    — both windows have solid coverage and a small gap between
                them (rise/decay are being compared on roughly equal
                footing).
      PARTIAL — coverage exists in both windows but is uneven enough
                that composition-shift conclusions should be treated as
                provisional, not settled (this is where EXP2 landed).
      LOW     — one window's coverage is so thin that any condition-mix
                comparison for it is close to meaningless (this is where
                Rejected Live's 96%-unmatched rise window landed).
    Thresholds are deliberately simple and stated here, not hidden
    inside a score — anyone reading this function can see exactly what
    GOOD/PARTIAL/LOW means and adjust it.
    """
    total = len(all_rows)
    matched = len([r for r in all_rows if r.get("_context")])
    coverage_pct = (matched / total * 100) if total else 0.0

    report = {
        "total": total, "matched": matched, "coverage_pct": coverage_pct,
        "rise_coverage_pct": None, "decay_coverage_pct": None,
        "comparability": None,
    }

    if rise_rows is not None and decay_rows is not None:
        rise_total, decay_total = len(rise_rows), len(decay_rows)
        rise_matched = len([r for r in rise_rows if r.get("_context")])
        decay_matched = len([r for r in decay_rows if r.get("_context")])
        rise_cov = (rise_matched / rise_total * 100) if rise_total else 0.0
        decay_cov = (decay_matched / decay_total * 100) if decay_total else 0.0
        report["rise_coverage_pct"] = rise_cov
        report["decay_coverage_pct"] = decay_cov

        gap = abs(rise_cov - decay_cov)
        low_side = min(rise_cov, decay_cov)
        if low_side >= 70 and gap < 20:
            comparability = "GOOD"
        elif low_side >= 40 and gap < 40:
            comparability = "PARTIAL"
        else:
            comparability = "LOW"
        report["comparability"] = comparability

    return report


def format_integrity_report(report, title="Investigation integrity"):
    lines = [
        f"  {title}:",
        f"    Outcome records: {report['total']}",
        f"    Context matched: {report['matched']} ({report['coverage_pct']:.0f}%)",
    ]
    if report.get("rise_coverage_pct") is not None:
        lines.append(f"    Rise coverage: {report['rise_coverage_pct']:.0f}%")
        lines.append(f"    Decay coverage: {report['decay_coverage_pct']:.0f}%")
        lines.append(f"    Comparability: {report['comparability']}")
        if report["comparability"] == "LOW":
            lines.append(
                "    ⚠️ Coverage is too low/uneven to attribute this window's "
                "conditions reliably — treat any market-condition comparison "
                "below as illustrative at best, not a finding."
            )
        elif report["comparability"] == "PARTIAL":
            lines.append(
                "    ⚠️ Coverage gap between windows — condition-mix "
                "comparisons below are provisional, not settled. Check the "
                "no_matching_leg performance table before trusting a "
                "composition-shift conclusion."
            )
    return "\n".join(lines)


def unmatched_performance(rows):
    """
    Win rate/expectancy/PF for trades with NO enclosing leg record — the
    population every condition-based stats function in this file
    excludes entirely. Per chat (Vally): "What was the outcome
    performance of no_matching_leg trades? That's now one of the most
    important missing tables" — if this population's own expectancy is
    very different from the matched population's, any condition-mix
    conclusion is only describing part of the story, and possibly not
    the important part.
    """
    unmatched = [r for r in rows if not r.get("_context")]
    return _r_stats(unmatched)


def format_unmatched_performance(title, stats, min_n=5):
    if stats["n"] < min_n:
        return f"  {title}: n={stats['n']} (too small to trust)"
    pf_str = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] is not None else "inf (no losses)"
    return (f"  {title}: n={stats['n']}, win_rate={stats['win_rate']:.0f}%, "
            f"expectancy={stats['expectancy']:+.2f}R, PF={pf_str}")


def should_investigate_deterioration(curve, peak_idx, trough_idx, rise_rows, decay_rows,
                                      min_trades=20, min_decline_r=3.0, min_coverage_pct=40):
    """
    Vally's four-question gate ("Should investigate deterioration?" —
    sufficient trades? meaningful decline? enough contextual coverage?
    enough observations in both periods?), made explicit and code-
    enforced instead of left to judgment each time a report is read.
    His EXP7 example is exactly what this exists to catch: a 1.95R
    decline across 29/21 trades is noise, not a finding, and running
    the full A/B/C machinery on it produces confident-looking output
    about nothing.

    Returns (should_investigate: bool, reasons: [str]). `reasons` lists
    EVERY check that failed, not just the first one, so a report can
    show the complete picture rather than stopping at the first red
    flag. Thresholds are plain keyword args, not buried constants —
    tune per how much noise you're willing to tolerate.
    """
    if peak_idx is None or trough_idx is None or trough_idx <= peak_idx:
        return False, ["No peak-then-trough pattern in the equity curve — nothing to investigate."]

    reasons = []
    n_rise, n_decay = len(rise_rows), len(decay_rows)
    if n_rise < min_trades or n_decay < min_trades:
        reasons.append(f"Sample too small (rise n={n_rise}, decay n={n_decay}, need {min_trades}+ each).")

    decline_r = curve[peak_idx][2] - curve[trough_idx][2]
    if decline_r < min_decline_r:
        reasons.append(f"Decline too small to be meaningful ({decline_r:.2f}R, need {min_decline_r:.1f}R+).")

    rise_matched = len([r for r in rise_rows if r.get("_context")])
    decay_matched = len([r for r in decay_rows if r.get("_context")])
    rise_cov = (rise_matched / n_rise * 100) if n_rise else 0.0
    decay_cov = (decay_matched / n_decay * 100) if n_decay else 0.0
    if rise_cov < min_coverage_pct or decay_cov < min_coverage_pct:
        reasons.append(
            f"Context coverage too low to attribute conditions "
            f"(rise {rise_cov:.0f}%, decay {decay_cov:.0f}%, need {min_coverage_pct}%+ each)."
        )

    return (len(reasons) == 0), reasons

