#!/usr/bin/env python3
"""
dedupe_shadow_log.py

Removes duplicate re-appends from shadow_trade_log.jsonl caused by the
crash-window bug (setups with a trade_id logged more than once because
shadow_state wasn't persisted before a restart re-resolved the same
candle batch).

Rule: keep the FIRST occurrence of each trade_id. All duplicate copies
of a trade_id are byte-identical in every field that matters (outcome,
r_achieved, experiment, variant) because they're re-derivations of the
same historical candle data -- so "first" vs "last" makes no difference
to correctness, only to count.

Rows with trade_id == null are LEFT ALONE. Those predate the trade_id
field being added to the log at all (see the audit note in
_append_shadow_trade_log in scanner.py) -- there's no way to tell if
they're duplicates or not, and they can't be created by this specific
bug going forward since every setup now carries an id.

Usage:
    python3 dedupe_shadow_log.py <input.jsonl> <output.jsonl>
"""
import json
import sys
from collections import Counter


def dedupe(input_path, output_path):
    seen = set()
    kept = []
    dropped = 0
    null_id_count = 0
    total = 0

    with open(input_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            record = json.loads(line)
            tid = record.get("trade_id")

            if tid is None:
                null_id_count += 1
                kept.append(record)
                continue

            if tid in seen:
                dropped += 1
                continue

            seen.add(tid)
            kept.append(record)

    with open(output_path, "w") as f:
        for record in kept:
            f.write(json.dumps(record) + "\n")

    print(f"Input rows:            {total}")
    print(f"Rows with no trade_id: {null_id_count} (kept as-is, can't dedupe)")
    print(f"Duplicate rows dropped: {dropped}")
    print(f"Output rows:           {len(kept)}")
    return kept


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 dedupe_shadow_log.py <input.jsonl> <output.jsonl>")
        sys.exit(1)
    dedupe(sys.argv[1], sys.argv[2])
