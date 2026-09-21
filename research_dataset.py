"""
research_dataset.py
====================
Research & Backtest Lab — DATA LAYER. Added 2026-09-18, per chat.

ARCHITECTURE (read this before adding anything here)
------------------------------------------------------
This file is a LEAF, same tier as scanner_observation.py, and it is
DELIBERATELY read-only:

  - It only ever opens the permanent, append-only logs scanner_live.py
    already writes (ohlc_*_log.jsonl, zone_log.jsonl, scan_index_log.
    jsonl, structure_digest_log.jsonl). It never opens state.json,
    candle_cache.json, or any other file a live/shadow pass owns and
    mutates, and it never calls load_state()/save_state() or any
    other scanner_common persistence function that writes.
  - It imports scanner_common (for BASE_DIR/PIP_SIZE) only. It does
    NOT import scanner_observation — zone detection math (detect_fvg,
    ATR, etc.) is not reused here because it doesn't need to be: zone
    detection happens exactly ONCE, live, in scanner_live.py's
    _scan_once() (build_zone_observations(), scanner_observation.py),
    and this file only ever reads the already-decided result back out
    of zone_log.jsonl. That's the actual single-source-of-truth
    guarantee — not "this file also calls detect_fvg()," but "this
    file has no code path that could ever compute a zone independently
    and disagree with what LIVE logged." A later test that genuinely
    needs to re-run detection against a reconstructed historical window
    (rather than just reading what was already logged) would import
    scanner_observation's pure functions directly when that's built —
    check_layer_imports.py already allows it (research_dataset/
    research_lab are not forbidden from importing scanner_observation)
    — but nothing here does that today, so this comment doesn't claim
    it does. [Corrected 2026-09-18 — an earlier draft of this docstring
    said the opposite; flagged and fixed same-day, per chat.]
  - It never imports scanner_live or min_scanner — this is enforced by
    check_layer_imports.py, not just this comment.
  - scanner_live.py / min_scanner.py must NEVER import this file or
    research_lab.py. A research finding is not allowed to feed back
    into a live decision just because it's technically importable —
    per chat, "a finding should have to earn its way into MIN"
    through replication and an actual review, not a Python import.
    check_layer_imports.py's FORBIDDEN dict enforces this in both
    directions — see the entries added there in the same turn this
    file shipped.

This is what "the backtesting feature should go in blind to the
outcome" means structurally in this codebase, not just as a coding
habit: AsOfCandles below is the ONLY way any test/backtest built on
top of this file is allowed to look at candles, and it is physically
incapable of returning a candle timestamped after its cutoff — the
slice happens once, in __init__, and nothing on the object can widen
it afterward.
"""
import os
import json
import pandas as pd

from scanner_common import BASE_DIR, PIP_SIZE, SESSION_WINDOWS_UTC

OHLC_HISTORY_FILES = {
    "5min":  os.path.join(BASE_DIR, "ohlc_5m_log.jsonl"),
    "15min": os.path.join(BASE_DIR, "ohlc_15m_log.jsonl"),
    "1h":    os.path.join(BASE_DIR, "ohlc_1h_log.jsonl"),
}
ZONE_LOG_FILE             = os.path.join(BASE_DIR, "zone_log.jsonl")
SCAN_INDEX_LOG_FILE       = os.path.join(BASE_DIR, "scan_index_log.jsonl")
STRUCTURE_DIGEST_LOG_FILE = os.path.join(BASE_DIR, "structure_digest_log.jsonl")
# Added 2026-09-20, per chat (friend's "storing context" proposal, second
# pass) — see this same constant's comment in scanner_common.py for why
# it's a separate file joined by scan_id rather than a wider structure_
# digest row. load_full_context() below is the pre-joined read path;
# nothing downstream needs to know it's two files on disk.
THESIS_CONTEXT_LOG_FILE   = os.path.join(BASE_DIR, "thesis_context_log.jsonl")


# =========================================================================
# RAW LOADERS — each one just parses a jsonl file into a DataFrame/list.
# No filtering, no joins, no interpretation. If a file doesn't exist yet
# (fresh clone, or a log that hasn't started collecting), return an empty
# frame/list rather than raising — every caller downstream already has to
# handle "not enough data yet" as a normal case, not an error.
# =========================================================================
def _read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                # One bad line must not take down the whole dataset —
                # same "one bad trailing line shouldn't block the whole
                # file" discipline live-scan.yml's own integrity check
                # uses. Skip and note it; never silently drop it with
                # nothing printed.
                print(f"[RESEARCH DATASET] skipping malformed line "
                      f"{line_no} in {path}: {e}")
    return rows


def load_candles(timeframe):
    """Returns the FULL permanent candle history for one timeframe
    ("5min" / "15min" / "1h") as a DataFrame indexed by UTC datetime,
    sorted ascending, deduplicated on the index (append-only files are
    safe from in-place edits, but a crashed-and-retried write could in
    theory double-append one row — cheap to guard here once rather
    than trust every caller to remember).

    This is the immutable ground truth: every row is a CLOSED candle
    (fetch_ohlc() never returns the still-forming bar — see its own
    docstring), so nothing about a row here can change after the fact.
    """
    rows = _read_jsonl(OHLC_HISTORY_FILES[timeframe])
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.set_index("datetime").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df.columns = [c.capitalize() if c in ("open", "high", "low", "close") else c
                  for c in df.columns]
    return df[["Open", "High", "Low", "Close"]]


def load_zones(zone_type=None, direction=None):
    """Returns zone_log.jsonl rows as a DataFrame, oldest first.
    Optionally filtered to zone_type ("FVG" / "ORDER_BLOCK") and/or
    direction ("BULLISH" / "BEARISH"). One row per zone's FIRST
    detection only (that's what zone_log.jsonl already guarantees —
    see _log_new_zones()'s dedup in scanner_live.py), so this is
    already the right shape for a dataset: one row = one zone
    instance, not one row per scan it happened to still be open."""
    rows = _read_jsonl(ZONE_LOG_FILE)
    if not rows:
        return pd.DataFrame(columns=[
            "zone_type", "direction", "timeframe", "high", "low",
            "formation_time", "identity_key", "scan_id", "scanned_at",
        ])
    df = pd.DataFrame(rows)
    df["formation_time"] = pd.to_datetime(df["formation_time"], utc=True)
    df = df.sort_values("formation_time").reset_index(drop=True)
    if zone_type is not None:
        df = df[df["zone_type"] == zone_type]
    if direction is not None:
        df = df[df["direction"] == direction]
    return df.reset_index(drop=True)


def load_scan_index():
    """scan_id <-> scanned_at mapping, as a DataFrame indexed by scan_id."""
    rows = _read_jsonl(SCAN_INDEX_LOG_FILE)
    if not rows:
        return pd.DataFrame(columns=["scanned_at"]).rename_axis("scan_id")
    df = pd.DataFrame(rows)
    df["scanned_at"] = pd.to_datetime(df["scanned_at"], utc=True)
    return df.set_index("scan_id").sort_index()


def load_structure_digest_log():
    """One row per scan — the full flattened structure/location/
    liquidity snapshot build_structure_digest() computed that scan.
    Nested dicts (structure/location/liquidity) are flattened with a
    dot-joined column name so this is immediately usable as a flat
    analysis table without a second parsing pass."""
    rows = _read_jsonl(STRUCTURE_DIGEST_LOG_FILE)
    if not rows:
        return pd.DataFrame()
    flat_rows = []
    for row in rows:
        flat = {"scan_id": row.get("scan_id"), "scanned_at": row.get("scanned_at")}
        # "regime" added per chat (macro_bias/macro_bias_stale, additive,
        # 2026-09-20) — rows logged before this shipped simply won't have
        # a "regime" key, and .get(section, {}) already handles that as
        # missing columns rather than a crash; no backfill, same
        # discipline used elsewhere in this codebase for additive fields.
        for section in ("structure", "location", "liquidity", "regime"):
            for k, v in row.get(section, {}).items():
                flat[f"{section}.{k}"] = v
        flat_rows.append(flat)
    df = pd.DataFrame(flat_rows)
    df["scanned_at"] = pd.to_datetime(df["scanned_at"], utc=True)
    return df.sort_values("scan_id").reset_index(drop=True)


def load_thesis_context_log():
    """One row per scan that reached thesis computation — break_count,
    transition_cause, trend_health, failure_risk, confidence, and the
    mtf_15m/mtf_5m/campaign dicts, flattened the same dot-joined way as
    load_structure_digest_log(). A scan missing from this table (phase
    computation failed, or predates 2026-09-20) is a genuinely unknown
    row, not a zero/false one — callers must left-join against
    load_structure_digest_log() (see load_full_context()) rather than
    assume every scan_id appears here."""
    rows = _read_jsonl(THESIS_CONTEXT_LOG_FILE)
    if not rows:
        return pd.DataFrame()
    flat_rows = []
    for row in rows:
        flat = {
            "scan_id": row.get("scan_id"),
            "scanned_at": row.get("scanned_at"),
            "phase": row.get("phase"),
            "break_count": row.get("break_count"),
            "transition_cause": row.get("transition_cause"),
            "trend_health": row.get("trend_health"),
            "failure_risk": row.get("failure_risk"),
            "confidence": row.get("confidence"),
        }
        for section in ("mtf_15m", "mtf_5m", "campaign"):
            for k, v in (row.get(section) or {}).items():
                flat[f"{section}.{k}"] = v
        flat_rows.append(flat)
    df = pd.DataFrame(flat_rows)
    df["scanned_at"] = pd.to_datetime(df["scanned_at"], utc=True)
    return df.sort_values("scan_id").reset_index(drop=True)


def load_full_context():
    """The friend's "context snapshot" as ONE table, per chat — a left
    join of load_structure_digest_log() (base: every scan, unconditional
    coverage) with load_thesis_context_log() (enrichment: only scans
    that reached thesis computation) on scan_id. Columns from the
    thesis side are NaN, not a fabricated default, on scans it doesn't
    cover — a NaN break_count means "not computed this scan," never 0.

    This is the one function a future context-matching test (research_
    lab.py) should actually query — it exists specifically so nothing
    downstream has to know the context snapshot is physically two
    append-only files joined by scan_id, the same pattern zone_log.jsonl/
    scan_index_log.jsonl already use against structure_digest_log.jsonl.

    Coverage note (per chat, friend's coarse/detailed-era point): every
    row here predating 2026-09-18 simply doesn't exist (structure_digest_
    log.jsonl didn't exist yet) — there is no need for a separate
    "coarse era" flag or a fabricated UNKNOWN sentinel column, since an
    absent scan_id already IS the honest "no detailed context available"
    answer for that period. Older history (shadow_trade_log.jsonl etc.)
    stays queryable on its own terms via research_dataset's other
    loaders — it's a different, coarser table, not backfilled into this
    one.
    """
    structure = load_structure_digest_log()
    thesis = load_thesis_context_log()
    if structure.empty or thesis.empty:
        return structure
    thesis_cols = thesis.drop(columns=["scanned_at"])
    return structure.merge(thesis_cols, on="scan_id", how="left", suffixes=("", "_thesis"))


# =========================================================================
# ANTI-LOOKAHEAD PRIMITIVE
# =========================================================================
class AsOfCandles:
    """
    A read-only, timestamp-bounded view over candle history. Built ONCE
    from a cutoff timestamp; nothing on this object can ever reveal a
    candle after that cutoff — the slice happens in __init__ and there
    is no method that accepts a later cutoff or reaches back into the
    source DataFrame.

    Every zone-detection / condition-classification step in a test or
    backtest MUST receive one of these (built from the zone's own
    formation_time, or the scan_id's own scanned_at), never the raw
    output of load_candles(). Outcome measurement is a SEPARATE step
    with its own, independently-built window (see outcome_window()) —
    keeping "what did we know" and "what happened next" as two
    objects that can never accidentally be the same one is what makes
    "blind to the outcome" a property of the code, not a promise about
    how carefully someone used it.
    """
    __slots__ = ("_df", "cutoff")

    def __init__(self, full_df, cutoff):
        self.cutoff = pd.Timestamp(cutoff)
        self._df = full_df[full_df.index <= self.cutoff]

    def __len__(self):
        return len(self._df)

    def tail(self, n):
        return self._df.tail(n)

    @property
    def frame(self):
        """The bounded frame itself — still cutoff-limited, this does
        not hand back a wider view than the object was built with."""
        return self._df


def outcome_window(full_df, start, end=None, max_candles=None):
    """
    Builds the OUTCOME side of a test: candles strictly AFTER `start`
    (a zone's formation_time, or wherever the condition under test was
    established), up to `end` or `max_candles` bars, whichever is
    given. This is a plain DataFrame, not an AsOfCandles — deliberately
    a different type, so a test function that asks for "the candles"
    without specifying which kind can't accidentally receive outcome
    data where a blind AsOfCandles was required, or vice versa. Never
    pass the same object to both the condition step and the outcome
    step of a test.
    """
    after = full_df[full_df.index > pd.Timestamp(start)]
    if end is not None:
        after = after[after.index <= pd.Timestamp(end)]
    if max_candles is not None:
        after = after.head(max_candles)
    return after


# =========================================================================
# TOUCH EVENT MODEL (per chat, 2026-09-18 — friend's design review)
# =========================================================================
# Two documents converged independently on the same point: a zone that
# gets touched 5 times is NOT 5 independent zone observations (that's
# pseudo-replication — it would silently overweight whatever zones
# happen to sit in choppy price), and "touch count" collapsed into a
# strong/weak bucket throws away the one thing that's actually
# informative — the TRAJECTORY across touches (penetration/reaction per
# touch, in order). "Does more prior interaction correlate with weaker
# reaction?" needs to stay a research QUESTION the data answers, not a
# rule baked into how the data gets collected ("little market
# interference" — see chat).
#
# So this is built ONCE, here, as the canonical event model: the ZONE
# is the unit of origin, TOUCHES are ordered sub-events under it, each
# carrying its own penetration + timing, with NO reaction/outcome
# classification baked in — reaction thresholds are a TEST's modeling
# choice (research_lab.py), not a dataset concern. Every test that
# needs touch-level data (first-touch reaction, degradation vs
# persistence, structure-conditioning) calls this SAME function rather
# than each re-implementing its own "what counts as a touch" logic —
# that's exactly the kind of drift the FVG-detection reuse decision
# above already protects against, applied to touches instead of zones.
def touch_events(zone_low, zone_high, candles_after_formation, max_touches=5,
                  max_wait_candles=None):
    """
    Walks forward through `candles_after_formation` (an outcome_window()
    slice — see its own docstring on why this is deliberately not an
    AsOfCandles) and returns one dict per TOUCH: a maximal contiguous
    run of candles whose [Low, High] range overlaps [zone_low, zone_high].
    Up to `max_touches`, each with:

        touch_number             1-indexed
        touch_time                first candle of the run
        run_end_time               last candle of the run (reaction/
                                    outcome for this touch should be
                                    measured from HERE forward, via
                                    outcome_window(), by the caller —
                                    this function does not classify
                                    outcomes)
        entry_price                run's first candle's Close
        penetration_pips           v1 mechanical definition: how much
                                    of the run's total High/Low range
                                    overlapped the zone's own width, in
                                    pips (bounded by zone width). Not
                                    direction-aware (doesn't yet know
                                    which edge price entered from) —
                                    documented limitation, refine once
                                    checked against real charts, same
                                    "prove it before widening it"
                                    discipline as _classify_reaction()'s
                                    own v1 note in research_lab.py.
        fully_swept                 bool: did this run's range cover
                                    BOTH zone edges (price traded clean
                                    through, not just tagged it)
        candles_since_formation      touch_time minus zone formation,
                                    in candle COUNT (position-based,
                                    not wall-clock — safe across
                                    weekend gaps)
        candles_since_prev_touch    None for touch #1

    Search resumes from the candle immediately after each run ends, so
    the same in-zone candles are never counted into two touches.
    `max_wait_candles`, if given, caps how far forward EACH individual
    touch search looks (not the whole walk) — leave None for "search
    the entire outcome window."
    """
    touches = []
    n = len(candles_after_formation)
    if n == 0:
        return touches
    highs = candles_after_formation["High"].values
    lows = candles_after_formation["Low"].values
    closes = candles_after_formation["Close"].values
    idx = candles_after_formation.index

    search_from = 0
    prev_touch_time = None
    while len(touches) < max_touches and search_from < n:
        end_bound = n if max_wait_candles is None else min(n, search_from + max_wait_candles)
        overlap = (highs[search_from:end_bound] >= zone_low) & (lows[search_from:end_bound] <= zone_high)
        if not overlap.any():
            break
        first_rel = overlap.argmax()
        first_pos = search_from + first_rel
        # extend forward while still overlapping — one contiguous run
        run_end_pos = first_pos
        while run_end_pos + 1 < end_bound and overlap[run_end_pos + 1 - search_from]:
            run_end_pos += 1

        run_high = highs[first_pos:run_end_pos + 1].max()
        run_low  = lows[first_pos:run_end_pos + 1].min()
        penetration_pips = (min(run_high, zone_high) - max(run_low, zone_low)) / PIP_SIZE
        fully_swept = bool(run_low <= zone_low and run_high >= zone_high)
        touch_time = idx[first_pos]

        touches.append({
            "touch_number": len(touches) + 1,
            "touch_time": touch_time.isoformat(),
            "run_end_time": idx[run_end_pos].isoformat(),
            "entry_price": float(closes[first_pos]),
            "penetration_pips": round(float(penetration_pips), 2),
            "fully_swept": fully_swept,
            "candles_since_formation": first_pos,  # position 0 = first candle after formation
            "candles_since_prev_touch": (
                None if prev_touch_time is None
                else first_pos - candles_after_formation.index.get_loc(prev_touch_time)
            ),
        })
        prev_touch_time = touch_time
        search_from = run_end_pos + 1

    return touches


# =========================================================================
# MATCHED CONTROL SAMPLING (per chat — "first-class citizen of the Lab,
# not merely a calculation attached to each test")
# =========================================================================
def session_bucket(ts):
    """
    Which configured session (SESSION_WINDOWS_UTC, scanner_common.py) a
    timestamp falls in, or None. Public (not test-local) because it's
    used for two separate things that must stay consistent with each
    other: matching baseline candidates to the real population's
    session distribution (matched_session_timestamps() below), AND
    breaking a test's own results down BY session as an explicit
    dimension (per chat, friend's review — "if London FVGs react 62%
    of the time but random London locations react 61%, that's a
    different observation than a pooled 62%"). One definition, reused
    both places.
    """
    hour = pd.Timestamp(ts).hour
    for i, (start, end) in enumerate(SESSION_WINDOWS_UTC):
        if start <= hour < end:
            return i
    return None


def matched_session_timestamps(candles_df, reference_timestamps, seed=42):
    """
    For each timestamp in `reference_timestamps` (typically real touch
    events), returns one randomly-sampled candle timestamp from
    `candles_df` in the SAME session bucket (session_bucket() above). A
    plain list, same length and order as `reference_timestamps` — the
    caller applies whatever outcome measurement it wants to these
    points, using the exact same function it used on the real events
    (that symmetry is what makes it a fair control, not a strawman).

    Pulled out of any one test (previously private to research_lab.py's
    zone_reaction_test) so degradation/structure-conditioning/future
    tests all draw baselines the same documented way instead of each
    growing its own slightly-different sampling logic.
    """
    import random
    rng = random.Random(seed)

    by_session = {}
    for ts in candles_df.index:
        by_session.setdefault(session_bucket(ts), []).append(ts)

    sampled = []
    for ref_ts in reference_timestamps:
        bucket = session_bucket(ref_ts)
        candidates = by_session.get(bucket) or list(candles_df.index)
        sampled.append(rng.choice(candidates))
    return sampled


# =========================================================================
# HOLDOUT SPLIT (per chat — "the big one missing... otherwise you've
# optimized against your historical sample")
# =========================================================================
def development_holdout_boundary(candles_df, holdout_fraction=0.3):
    """
    Returns the single UTC timestamp splitting `candles_df` into a
    DEVELOPMENT period (everything before it — explore, tune
    reaction_pips/lookahead, compare zone types) and a HOLDOUT period
    (everything from it forward — touched only once, for final
    verification of a specific config already chosen on development
    data). Split is CHRONOLOGICAL, not random — this is a time series;
    a random split would leak future information into "development"
    via candles adjacent in time to holdout candles.

    This function only computes the boundary. It does not enforce
    anything by itself — see zone_reaction_test()'s `dataset_split`
    parameter (research_lab.py) for the part that actually refuses to
    touch holdout data without an explicit, loud opt-in.
    """
    if candles_df.empty:
        return None
    cutoff_pos = int(len(candles_df) * (1 - holdout_fraction))
    cutoff_pos = min(max(cutoff_pos, 0), len(candles_df) - 1)
    return candles_df.index[cutoff_pos]
