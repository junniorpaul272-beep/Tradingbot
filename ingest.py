"""
ingest.py (Postgres / Supabase version)
========================================
Reads the scanner's append-only jsonl files and loads them into a
Postgres database (Supabase or any other Postgres host). Safe to run
over and over -- it only reads lines it hasn't seen before (tracked
in ingest_cursor), and any single malformed/torn line (see: non-
atomic write bug) is skipped and logged rather than crashing the
whole run.

Run this on a schedule (GitHub Actions cron) -- the dashboard NEVER
reads the jsonl files directly; it only ever queries the database via
Supabase's REST API.

CHANGED FROM THE SQLITE VERSION (flagged, not silent):
  - sqlite3 -> psycopg2. Connection now comes from a DATABASE_URL
    environment variable instead of a --db file path, since Postgres
    is a network connection, not a local file.
  - Placeholders: "?" -> "%s" (psycopg2's paramstyle).
  - "INSERT OR IGNORE" -> "INSERT ... ON CONFLICT (<key_cols>) DO
    NOTHING". SQLite infers the conflict target from any violated
    unique constraint; Postgres requires it named explicitly, which
    is exactly what the existing `key_cols` tuple in SOURCES was
    already carrying (previously unused) -- now it's load-bearing.
  - conn.executescript(schema.sql) -> a single conn.execute() call.
    Postgres's simple query protocol accepts multiple ;-separated
    statements in one call, same effect, no library-level "script"
    method needed.
  - datetime('now') -> now() in the cursor upsert (both engines
    otherwise use the same ON CONFLICT ... DO UPDATE syntax; this
    was the only engine-specific token in that query).

Usage:
    export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
    python3 ingest.py --data-dir /path/to/scanner/base_dir
"""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2


# Each entry: (jsonl filename, sqlite table, column mapping function)
def map_shadow_trade(rec):
    return {
        "trade_id":             rec.get("trade_id"),
        "methodology_version":  rec.get("methodology_version"),
        "resolved_at":          rec.get("resolved_at"),
        "experiment":           rec.get("experiment"),
        "variant":              rec.get("variant"),
        "tier_number":          rec.get("tier_number"),
        "atr_pips":             rec.get("atr_pips"),
        "target_r":             rec.get("target_r"),
        "direction":            rec.get("direction"),
        "opened_at":            rec.get("opened_at"),
        "bars_open":            rec.get("bars_open"),
        "resolved_candle_time": rec.get("resolved_candle_time"),
        "outcome":              rec.get("outcome"),
        "r_achieved":           rec.get("r_achieved"),
        "tags_json":            json.dumps(rec.get("tags") or {}),
    }


def map_leg_obs(rec):
    return {
        "leg_id":                rec.get("leg_id"),
        "fate":                  rec.get("fate"),
        "resolved_at":           rec.get("resolved_at"),
        "bars_open":             rec.get("bars_open"),
        "macro_was_choch":       int(bool(rec.get("macro_was_choch"))) if rec.get("macro_was_choch") is not None else None,
        "macro_leg_direction":   rec.get("macro_leg_direction"),
        "macro_leg_length_pips": rec.get("macro_leg_length_pips"),
        "bos_15m_direction":     rec.get("bos_15m_direction"),
        "bos_15m_break_count":   rec.get("bos_15m_break_count"),
        "bos_15m_was_choch":     int(bool(rec.get("bos_15m_was_choch"))) if rec.get("bos_15m_was_choch") is not None else None,
        "atr_pips":              rec.get("atr_pips"),
        "atr_percentile_15m":    rec.get("atr_percentile_15m"),
        "tier1_touched_bar":     rec.get("tier1_touched_bar"),
        "tier2_touched_bar":     rec.get("tier2_touched_bar"),
        "tier3_touched_bar":     rec.get("tier3_touched_bar"),
        "regime_json":           json.dumps({k: v for k, v in rec.items()
                                              if k.startswith("regime_")}),
        "market_state_json":     json.dumps({k: v for k, v in rec.items()
                                              if k.startswith("market_")}),
    }


def map_failure_case(rec):
    return {
        "case_number":         rec.get("case_number"),
        "methodology_version": rec.get("methodology_version"),
        "trade_id":            rec.get("id"),
        "opened_at":           rec.get("opened_at"),
        "tier_label":          rec.get("tier_label"),
        "tier_number":         rec.get("tier_number"),
        "direction":           rec.get("direction"),
        "expected":            rec.get("expected"),
        "observed":            rec.get("observed"),
        "r_achieved":          rec.get("r_achieved"),
        "atr_pips":            rec.get("atr_pips"),
        "bars_open":           rec.get("bars_open"),
        "conviction_score":    rec.get("conviction_score"),
        "predicted_win_prob":  rec.get("predicted_win_prob"),
        "comparisons_json":    json.dumps(rec.get("comparisons") or []),
        "conclusion":          rec.get("conclusion"),
        "n_winners_compared":  rec.get("n_winners_compared"),
        "n_losers_compared":   rec.get("n_losers_compared"),
    }


def map_live_trade(rec):
    return {
        "trade_id":     rec.get("trade_id") or rec.get("id"),
        "pair":         rec.get("pair"),
        "direction":    rec.get("direction"),
        "entry_price":  rec.get("entry_price") or rec.get("entry"),
        "exit_price":   rec.get("exit_price") or rec.get("exit"),
        "opened_at":    rec.get("opened_at"),
        "closed_at":    rec.get("closed_at"),
        "target_r":     rec.get("target_r"),
        "realized_r":   rec.get("realized_r"),
        "result":       rec.get("result"),
        "tier_label":   rec.get("tier_label"),
        "note":         rec.get("note"),
    }


SOURCES = [
    # (jsonl filename,          sqlite table,        mapping fn,       natural key columns)
    ("shadow_trade_log.jsonl",  "shadow_trades",      map_shadow_trade, ("trade_id", "resolved_at", "variant")),
    ("leg_obs_log.jsonl",       "leg_observations",   map_leg_obs,      ("leg_id", "resolved_at")),
    ("failure_case_log.jsonl",  "failure_cases",      map_failure_case, ("case_number",)),
    ("live_trade_log.jsonl",    "live_trades",         map_live_trade,  ("trade_id",)),
]


def get_cursor(conn, source_file):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT lines_read FROM ingest_cursor WHERE source_file = %s", (source_file,)
        )
        row = cur.fetchone()
    return row[0] if row else 0


def set_cursor(conn, source_file, lines_read):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ingest_cursor (source_file, lines_read, last_run_at)
               VALUES (%s, %s, now())
               ON CONFLICT (source_file) DO UPDATE SET
                   lines_read = excluded.lines_read,
                   last_run_at = excluded.last_run_at""",
            (source_file, lines_read),
        )


def ingest_file(conn, data_dir, filename, table, mapper, key_cols):
    path = Path(data_dir) / filename
    if not path.exists():
        print(f"  [skip] {filename} not found in {data_dir}")
        return 0, 0

    already_read = get_cursor(conn, filename)
    inserted, skipped = 0, 0

    with open(path, "r") as f:
        lines = f.readlines()

    new_lines = lines[already_read:]
    if not new_lines:
        print(f"  [ok] {filename}: no new lines ({already_read} already ingested)")
        return 0, 0

    conflict_cols = ", ".join(key_cols)

    for line in new_lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            # This is exactly the torn-write scenario -- log it, move on.
            skipped += 1
            print(f"  [WARN] malformed line in {filename}, skipped: {e}")
            continue

        mapped = mapper(rec)
        cols = list(mapped.keys())
        placeholders = ", ".join("%s" for _ in cols)
        col_list = ", ".join(cols)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                    f"ON CONFLICT ({conflict_cols}) DO NOTHING",
                    [mapped[c] for c in cols],
                )
            inserted += 1
        except psycopg2.Error as e:
            skipped += 1
            conn.rollback()  # Postgres aborts the whole transaction on error;
                              # roll back so the next insert in this loop can proceed.
            print(f"  [WARN] insert failed for {table}, skipped: {e}")

    set_cursor(conn, filename, len(lines))
    print(f"  [ok] {filename}: +{inserted} rows, {skipped} skipped, "
          f"cursor now at {len(lines)} lines")
    return inserted, skipped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True,
                         help="Directory containing the scanner's jsonl files")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        sys.exit("DATABASE_URL environment variable is not set. "
                 "In GitHub Actions this comes from a repo Secret; "
                 "locally, export it before running this script.")

    schema_path = Path(__file__).parent / "schema_postgres.sql"
    conn = psycopg2.connect(database_url)
    if schema_path.exists():
        with conn.cursor() as cur:
            cur.execute(schema_path.read_text())
        conn.commit()
    else:
        print(f"  [skip] {schema_path.name} not found -- assuming tables "
              f"already exist in Supabase (fine if you ran the schema by "
              f"hand already; add this file back later if you ever change "
              f"the schema and want it auto-applied).")

    total_in, total_skip = 0, 0
    print(f"Ingesting from {args.data_dir}")
    for filename, table, mapper, key_cols in SOURCES:
        inserted, skipped = ingest_file(conn, args.data_dir, filename, table, mapper, key_cols)
        total_in += inserted
        total_skip += skipped

    conn.commit()
    conn.close()
    print(f"\nDone. {total_in} rows inserted total, {total_skip} lines skipped total.")
    if total_skip:
        print("Skipped lines were malformed JSON or failed insert -- "
              "check warnings above.", file=sys.stderr)


if __name__ == "__main__":
    main()
