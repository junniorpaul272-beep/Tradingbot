# Integrity report (25 Oct) — the inspector and the integrity answers

**What it detects (tested by planting faults, attack_inspector.py):** 25 of 25 faults inside its declared scope (21 originally; 4 impossible-value checks added 26 Oct: see REPORT_VERIFICATION_ROUND.md): direction conflicts between engine parts, numbers stuck at placeholder, frozen/aged values (dwell), time going backwards or stale, repeated identity, unbounded growth, field layout changing (schema), version changes, plus a fuzz exercise on the engine's own reader.

**What it misses by design (said to you when you ask):** a price error close to the other levels, a scan id going backward, a stale day-old timestamp, absurd counts, a LOW-risk reading with no reasons. Of 5 faults still outside its scope it catches 1. "Clear" therefore means "none of the things it checks were found", never "the engine is right".

**Persistent faults stay visible:** a standing alarm does not clear itself; the schema alarm used to clear one run later (fixed, held open for 3 runs). **Masking:** one conflict used to absorb another (fixed with a per-finding identity). Two hidings remain: a LOW divergence is dropped when its own reference is already in conflict.

**Asking it:** "why does the integrity alarm say clear / what did it check / what could be wrong / what did it find before" read the real inspector files. If something is open it says "It isn't saying clear". If the inspector hasn't run recently, or its state file is missing, it says so instead of reassuring.

**Unknown:** why the MIN scan failed (run #6033) — needs the Actions log.
