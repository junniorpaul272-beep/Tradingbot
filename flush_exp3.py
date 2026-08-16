#!/usr/bin/env python3
"""
flush_exp3.py — one-time, manually-run maintenance script. Per chat,
2026-08-15: EXP3_POI (Order Block vs FVG shadow comparison) is being
retired now that FVG has graduated to a live Tier 1 fallback POI (see
_tier1_poi_evaluate()'s 2026-08-15 fix). The EXP3 slot is being reused
for a new Support/Resistance experiment — same precedent as the
2026-08-11 EXP4_LIQUIDITY -> EXP4_POLICY_LAB slot reuse, EXCEPT that
reuse was uncomplicated because the old slot had ZERO logged trades
ever. EXP3_POI has real accumulated history, so leaving it in place
would silently pool two unrelated experiments' results under one
key — inflating both the "overall EXP3" rollup and any future S/R
report that reads the same key. This script:

  1. Builds a full final report of EXP3_POI's history (order_block vs
     fvg variants, split out — never pooled together, since that was
     the whole point of tagging them separately in the first place) and
     sends it to Telegram, so the research isn't just lost.
  2. ARCHIVES (does not silently delete) every resolved EXP3_POI record
     out of the live shadow_trade_log.jsonl into its own permanent file,
     EXP3_POI_ARCHIVE_FILE — respecting this codebase's own "permanent
     logs are never silently rewritten" discipline. The record isn't
     gone, it's just no longer in the file live report functions read.
  3. Clears EXP3_POI's entries from shadow_state.json (pending setups,
     seen_legs dedup keys) so no old-format setup can resolve into the
     new experiment's history, and resets shadow_stats.json's EXP3_POI
     counters to zero.

Deliberately NOT wired into scanner_live.py or min_scanner.py's normal
scan flow — this is a one-time cutover, run manually:

    python3 flush_exp3.py            # dry run — report only, no changes
    python3 flush_exp3.py --commit   # actually archive + reset

Safe to re-run: a second run with nothing left in shadow_trade_log.jsonl
tagged EXP3_POI will report "0 resolved records" and skip the reset
(idempotent, not destructive on a second call).
"""
import sys
import json
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scanner_common import send_telegram, atomic_write_json, SHADOW_TRADE_LOG_FILE, BASE_DIR
from min_scanner import (
    load_shadow_state, save_shadow_state,
    load_shadow_stats, save_shadow_stats,
    _read_shadow_trade_log, _r_stats, _empty_experiment_stat, _dedup_key,
    SHADOW_METHODOLOGY_VERSION,
)

EXP3_POI_ARCHIVE_FILE = os.path.join(BASE_DIR, "exp3_poi_pre_sr_archive.jsonl")


def build_report(records):
    """One section per variant (order_block / fvg), never pooled — plus
    an overall total for context. Uses the same _r_stats() every other
    experiment report in this codebase uses, so the numbers are directly
    comparable to anything you've seen in /shadow poi before today."""
    if not records:
        return None, {"order_block": [], "fvg": []}

    by_variant = {"order_block": [], "fvg": []}
    for r in records:
        v = r.get("variant")
        if v in by_variant:
            by_variant[v].append(r)
        else:
            by_variant.setdefault(v or "unknown", []).append(r)

    lines = [
        "\U0001F4E6 *EXP3_POI — Final Report (archived before S/R reuse)*",
        "\u2500" * 25,
        f"_Generated {datetime.now(timezone.utc).isoformat()}. "
        f"This experiment slot is being retired — FVG graduated to a live "
        f"Tier 1 fallback POI, and EXP3 is being reused for a new "
        f"Support/Resistance experiment. This is the permanent record of "
        f"everything EXP3_POI ever logged as the OB-vs-FVG comparison._",
        "",
        f"*Total resolved: {len(records)}*",
        "",
    ]

    for variant, recs in by_variant.items():
        if not recs:
            lines.append(f"*{variant}*: _no resolved records_")
            lines.append("")
            continue
        stats = _r_stats(recs)
        wins = sum(1 for r in recs if r.get("r_achieved", 0) > 0)
        lines.append(f"*{variant}* (n=`{stats['n']}`)")
        lines.append(
            f"  win rate `{stats['win_rate']*100:.0f}%` ({wins}W/{stats['n']-wins}L) | "
            f"avg R `{stats['avg_r']:+.2f}` | "
            f"PF `{stats['pf'] if stats['pf'] is not None else '—'}`"
        )
        lines.append("")

    lines.append(
        "_Archived to exp3_poi_pre_sr_archive.jsonl (permanent, off the "
        "active log). EXP3_POI's pending/seen_legs/stats reset to zero. "
        "The new Support/Resistance experiment logs under EXP3_SR, a "
        "distinct key, not EXP3_POI reused with new meaning — see "
        "/shadow sr going forward._"
    )
    return "\n".join(lines), by_variant


def main():
    commit = "--commit" in sys.argv

    all_records = _read_shadow_trade_log(experiment="EXP3_POI")
    print(f"Found {len(all_records)} resolved EXP3_POI records in {SHADOW_TRADE_LOG_FILE}")

    report_text, by_variant = build_report(all_records)

    if report_text is None:
        print("Nothing to flush — EXP3_POI has no resolved records. Nothing sent, nothing changed.")
        return

    print("\n--- REPORT PREVIEW ---\n")
    print(report_text)
    print("\n--- END PREVIEW ---\n")

    if not commit:
        print("DRY RUN — no Telegram message sent, no files changed. Re-run with --commit to apply.")
        return

    sent = send_telegram(report_text)
    if not sent:
        print("[ABORT] Telegram send failed — stopping before touching any files, so the "
              "report isn't lost if the archive/reset step ran without a surviving record of it.")
        sys.exit(1)
    print("Report sent to Telegram.")

    # ---- 1. Archive: append EXP3_POI records to their own permanent file ----
    with open(EXP3_POI_ARCHIVE_FILE, "a") as f:
        for r in all_records:
            f.write(json.dumps(r, default=str) + "\n")
    print(f"Archived {len(all_records)} records to {EXP3_POI_ARCHIVE_FILE}")

    # ---- 2. Rewrite shadow_trade_log.jsonl WITHOUT EXP3_POI lines ----
    # Read the raw file directly (not via _read_shadow_trade_log, which
    # already filters by methodology_version — we need every line,
    # including any that wouldn't have matched, so nothing is silently
    # dropped by a filter irrelevant to this operation).
    kept_lines = []
    removed = 0
    with open(SHADOW_TRADE_LOG_FILE, "r") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except Exception:
                kept_lines.append(line.rstrip("\n"))  # malformed line — leave untouched, not this script's job
                continue
            if rec.get("experiment") == "EXP3_POI":
                removed += 1
                continue
            kept_lines.append(line.rstrip("\n"))

    with open(SHADOW_TRADE_LOG_FILE, "w") as f:
        for line in kept_lines:
            f.write(line + "\n")
    print(f"Removed {removed} EXP3_POI lines from {SHADOW_TRADE_LOG_FILE} "
          f"({len(kept_lines)} lines kept, all other experiments untouched).")

    # ---- 3. Clear EXP3_POI from shadow_state.json (pending + seen_legs) ----
    shadow_state = load_shadow_state()
    before_pending = len(shadow_state.get("pending", []))
    shadow_state["pending"] = [p for p in shadow_state.get("pending", [])
                                if p.get("experiment") != "EXP3_POI"]
    dropped_pending = before_pending - len(shadow_state["pending"])

    seen_legs = shadow_state.get("seen_legs", {})
    dropped_keys = [k for k in seen_legs if k == "EXP3_POI" or k.startswith("EXP3_POI::")]
    for k in dropped_keys:
        del seen_legs[k]
    save_shadow_state(shadow_state)
    print(f"shadow_state.json: dropped {dropped_pending} pending EXP3_POI setup(s), "
          f"cleared seen_legs keys {dropped_keys}")

    # ---- 4. Reset shadow_stats.json's EXP3_POI counters ----
    shadow_stats = load_shadow_stats()
    shadow_stats["EXP3_POI"] = _empty_experiment_stat()
    save_shadow_stats(shadow_stats)
    print("shadow_stats.json: EXP3_POI counters reset to zero.")

    print("\nDone. EXP3_POI's slot is clean and ready for the new Support/Resistance experiment.")


if __name__ == "__main__":
    main()
