-- ============================================================
-- Trading Journal / Research Database — SQLite schema
-- Generated from the actual record shapes written by
-- scanner__10_.py (SHADOW_TRADE_LOG_FILE, LEG_OBS_LOG_FILE,
-- FAILURE_CASE_LOG_FILE). One database file, e.g. research.db.
--
-- Design rules followed throughout:
--   1. trade_id / leg_id / case_number can be NULL for records
--      written before those fields existed in the scanner —
--      every column that can be missing is nullable, on purpose.
--   2. ingest.py upserts on a natural key (never re-inserts the
--      same jsonl line twice, safe to re-run from scratch).
--   3. No column here invents data the scanner doesn't produce.
--      If a stat isn't in these tables, it isn't being collected
--      yet — that's a scanner change, not a dashboard workaround.
-- ============================================================

PRAGMA journal_mode = WAL;   -- readers (dashboard) don't block the ingest writer

-- ------------------------------------------------------------
-- 1. SHADOW TRADES  (from shadow_trade_log.jsonl)
--    One row per resolved shadow/research trade.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shadow_trades (
    row_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                TEXT,              -- nullable: pre-fix records have none
    methodology_version     TEXT,
    resolved_at             TEXT NOT NULL,     -- ISO8601 UTC
    experiment              TEXT NOT NULL,     -- e.g. EXP7_TIER_ATR, EXPE_REJECTED_LIVE
    variant                 TEXT NOT NULL,     -- e.g. TIER_3_STRUCT, TIER_2_FIB
    tier_number             INTEGER,
    atr_pips                REAL,
    target_r                REAL,
    direction               TEXT,              -- BUY / SELL
    opened_at               TEXT,
    bars_open               INTEGER,
    resolved_candle_time    TEXT,
    outcome                 TEXT,              -- WIN / LOSS / TIMEOUT_WIN / TIMEOUT_LOSS
    r_achieved              REAL,
    tags_json               TEXT,              -- raw tags dict, kept as JSON (schema drifts often)
    ingested_at             TEXT NOT NULL DEFAULT (datetime('now')),
    -- de-dup key: prefer trade_id, fall back to a composite when it's NULL
    UNIQUE(trade_id, resolved_at, variant)
);

CREATE INDEX IF NOT EXISTS idx_shadow_experiment ON shadow_trades(experiment);
CREATE INDEX IF NOT EXISTS idx_shadow_variant    ON shadow_trades(variant);
CREATE INDEX IF NOT EXISTS idx_shadow_resolved   ON shadow_trades(resolved_at);
CREATE INDEX IF NOT EXISTS idx_shadow_atr        ON shadow_trades(atr_pips);

-- ------------------------------------------------------------
-- 2. LEG OBSERVATIONS  (from leg_obs_log.jsonl)
--    One row per resolved H1 leg — the formation-time snapshot
--    plus which tier's zone got touched, and when.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leg_observations (
    row_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    leg_id                  TEXT NOT NULL,
    fate                    TEXT,              -- CONTINUED / REVERSED / INVALIDATED
    resolved_at             TEXT,
    bars_open               INTEGER,
    macro_was_choch         INTEGER,           -- 0/1 (SQLite has no native bool)
    macro_leg_direction     TEXT,
    macro_leg_length_pips   REAL,
    bos_15m_direction       TEXT,
    bos_15m_break_count     INTEGER,
    bos_15m_was_choch       INTEGER,
    atr_pips                REAL,
    atr_percentile_15m      REAL,
    tier1_touched_bar       INTEGER,           -- NULL = zone never touched before resolution
    tier2_touched_bar       INTEGER,
    tier3_touched_bar       INTEGER,
    regime_json             TEXT,              -- classify_regime() output, kept flexible
    market_state_json       TEXT,              -- compute_market_state() output, kept flexible
    ingested_at             TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(leg_id, resolved_at)
);

CREATE INDEX IF NOT EXISTS idx_legobs_fate ON leg_observations(fate);

-- ------------------------------------------------------------
-- 3. FAILURE CASES  (from failure_case_log.jsonl)
--    One row per auto-opened "expected WIN, got LOSS" investigation.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS failure_cases (
    row_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    case_number             INTEGER,
    methodology_version     TEXT,
    trade_id                TEXT,              -- 'id' field in the source record
    opened_at               TEXT,
    tier_label              TEXT,
    tier_number             INTEGER,
    direction               TEXT,
    expected                TEXT,
    observed                TEXT,
    r_achieved              REAL,
    atr_pips                REAL,
    bars_open               INTEGER,
    conviction_score        REAL,
    predicted_win_prob      REAL,
    comparisons_json        TEXT,              -- top-5 comparisons, kept as JSON
    conclusion              TEXT,
    n_winners_compared      INTEGER,
    n_losers_compared       INTEGER,
    ingested_at             TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(case_number)
);

-- ------------------------------------------------------------
-- 4. LIVE TRADES  (does NOT exist as a source file yet —
--    see the note in chat: stats["journal"] is a capped,
--    overwritten list, not a permanent log.
--    This table is ready for the moment you add
--    live_trade_log.jsonl to the scanner. Built to match the
--    shape of the existing journal entries so the migration
--    is a straight column mapping, nothing to redesign later.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS live_trades (
    row_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                TEXT,
    pair                    TEXT,              -- GBPUSD / EURUSD / BTCUSD
    direction               TEXT,
    entry_price             REAL,
    exit_price              REAL,
    opened_at               TEXT,
    closed_at               TEXT,
    target_r                REAL,
    realized_r              REAL,
    result                  TEXT,              -- WIN / LOSS
    tier_label              TEXT,
    note                    TEXT,
    ingested_at             TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(trade_id)
);

CREATE INDEX IF NOT EXISTS idx_live_closed_at ON live_trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_live_pair      ON live_trades(pair);

-- ------------------------------------------------------------
-- 5. INGESTION CURSOR
--    Tracks how many lines of each jsonl file have already been
--    read, so ingest.py only parses NEW lines on every run
--    instead of re-reading the whole file from scratch.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_cursor (
    source_file             TEXT PRIMARY KEY,
    lines_read              INTEGER NOT NULL DEFAULT 0,
    last_run_at             TEXT
);
