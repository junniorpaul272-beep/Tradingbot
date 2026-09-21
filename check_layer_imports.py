#!/usr/bin/env python3
"""
check_layer_imports.py — enforces the scanner's one-way dependency rule
at the repo level, per chat 2026-08-14.

Rule (stated in scanner_observation.py's own module docstring and
min_scanner.py's "never imported FROM the other direction" comment):

    scanner_common.py       <- imported by everything
    scanner_observation.py  <- imports only scanner_common
    scanner_live.py         <- imports scanner_common, scanner_observation, min_scanner
    min_scanner.py          <- imports scanner_common, scanner_observation
                                (NEVER scanner_live)

This has held by hand so far (verified clean as of 2026-08-14) but had
no automated check. Run this in CI on every push/PR; nonzero exit code
on any violation.

Usage:
    python3 check_layer_imports.py [path-to-repo-root]
"""
import ast
import sys
from pathlib import Path

# Lower layer -> set of modules it is FORBIDDEN to import (higher layers).
# system_ledger.py added 2026-08-23, per chat — sits at the same layer as
# scanner_observation.py (a leaf that only imports scanner_common), so it
# gets the same forbidden set. min_scanner.py importing it is expected
# and fine (min_scanner's own forbidden set already only excludes
# scanner_live, unchanged).
#
# mil.py / worldstate.py / hfis.py added per chat (physiology work) —
# previously this dict had no entries for them at all, meaning the
# checker never opened these three files as targets and could not have
# failed on anything they import, regardless of what that was. Passing
# before this edit reflected a blind spot, not a validated architecture.
FORBIDDEN = {
    "scanner_common":      {"scanner_observation", "scanner_live", "min_scanner", "system_ledger", "mil", "worldstate", "hfis", "market_story", "narration_library", "research_dataset", "research_lab"},
    "scanner_observation": {"scanner_live", "min_scanner", "mil", "worldstate", "hfis", "market_story", "narration_library", "research_dataset", "research_lab"},
    "system_ledger":       {"scanner_live", "min_scanner", "scanner_observation", "mil", "worldstate", "hfis", "market_story", "narration_library", "research_dataset", "research_lab"},
    "min_scanner":         {"scanner_live", "mil", "worldstate", "hfis", "market_story", "narration_library", "research_dataset", "research_lab"},
    # mil.py and hfis.py are organism-agnostic by design — per chat, MIL
    # must never open state.json/WorldState itself (it returns payloads
    # for LIVE to commit) and HFIS is a pure local narrator — so BOTH are
    # forbidden from importing anything else in this codebase at all.
    "mil":                 {"scanner_common", "scanner_observation", "scanner_live", "min_scanner", "system_ledger", "worldstate", "hfis", "market_story", "narration_library", "research_dataset", "research_lab"},
    "hfis":                {"scanner_common", "scanner_observation", "scanner_live", "min_scanner", "system_ledger", "mil", "worldstate", "market_story", "narration_library", "research_dataset", "research_lab"},
    # worldstate.py legitimately reads scanner_common + min_scanner
    # (min_scanner owns the loader functions it aggregates) — forbidden
    # only from the top of the stack and its new siblings.
    "worldstate":          {"scanner_live", "system_ledger", "mil", "hfis", "market_story", "narration_library", "research_dataset", "research_lab"},
    # market_story.py ADDED (2026-09-15, per chat + CI catch — this
    # module previously had no entry at all, meaning the checker never
    # opened it as a target, the same blind spot the mil/worldstate/hfis
    # comment above already flagged once for those three. Same
    # organism-agnostic treatment as hfis: a pure classification layer,
    # legitimately reachable only from scanner_live.py (the only current
    # caller), forbidden from importing anything else in this codebase.
    # It DOES import brain.py (for _state_family()) — deliberately not
    # forbidden here since brain.py itself has no entry in this dict at
    # all (pre-existing gap, not introduced by this module — worth a
    # separate decision on whether brain.py belongs in this graph, not
    # bundled into this fix).
    "market_story":        {"scanner_common", "scanner_observation", "scanner_live", "min_scanner", "system_ledger", "mil", "worldstate", "hfis", "narration_library", "research_dataset", "research_lab"},
    # narration_library.py ADDED (2026-09-16, per chat — the architecture
    # redesign: a Library layer of pure, deterministic text-rendering
    # functions over the story dict, sitting between market_story.py's
    # fact-gathering and scanner_live.py's message assembly. Same
    # organism-agnostic treatment as hfis/market_story: reachable only
    # from scanner_live.py, forbidden from importing anything else in
    # this codebase (including hfis.py and market_story.py themselves —
    # its whole design point is that it never reaches into raw machine
    # state on its own, only ever receives an already-built dict).
    "narration_library":   {"scanner_common", "scanner_observation", "scanner_live", "min_scanner", "system_ledger", "mil", "worldstate", "hfis", "market_story", "research_dataset", "research_lab"},
    # research_dataset.py / research_lab.py ADDED 2026-09-21, per chat —
    # research_dataset.py's OWN module docstring already claimed (since
    # 2026-09-18, when it shipped) that "check_layer_imports.py's
    # FORBIDDEN dict enforces this in both directions — see the entries
    # added there in the same turn this file shipped." That claim was
    # false: neither module had ever been added to this dict at all,
    # the exact same blind-spot bug already caught three times before in
    # this file's own history (mil/worldstate/hfis, then market_story,
    # then narration_library) — a module absent from FORBIDDEN is never
    # opened as a target and can violate the one-way rule silently,
    # forever, with a green CI run the whole time. Caught while actually
    # fixing it, not assumed clean because the docstring said so.
    #
    # research_dataset.py is a LEAF (same tier as scanner_observation.py,
    # per its own docstring) — legitimately imports scanner_common
    # ONLY today, with scanner_observation explicitly allowed for later
    # (its docstring: "research_dataset/research_lab are not forbidden
    # from importing scanner_observation" — so scanner_observation is
    # deliberately left OUT of research_dataset's forbidden set below,
    # unlike every other entry in this dict). Forbidden from every
    # live-side module, exactly like scanner_observation, PLUS
    # research_lab (the layer built on top of it — a leaf must never
    # import back up into what depends on it).
    "research_dataset":    {"scanner_live", "min_scanner", "system_ledger", "mil", "worldstate", "hfis", "market_story", "narration_library", "research_lab"},
    # research_lab.py sits one layer above research_dataset.py (imports
    # PIP_SIZE from scanner_common + everything it needs from
    # research_dataset — confirmed via its actual import lines, not
    # assumed) — same live-side isolation as research_dataset, but
    # WITHOUT research_dataset itself in the forbidden set, since that
    # import is the whole point of the file.
    "research_lab":        {"scanner_live", "min_scanner", "system_ledger", "mil", "worldstate", "hfis", "market_story", "narration_library"},
    # scanner_live.py is the top of the stack — nothing is forbidden to it.
}


def imported_modules(filepath):
    """Returns the set of module names this file imports (both
    `import X` and `from X import ...` forms), via AST — never executes
    the file, so this is safe to run against code with side effects at
    import time (e.g. reading TELEGRAM_TOKEN from the environment)."""
    tree = ast.parse(filepath.read_text(), filename=str(filepath))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module.split(".")[0])
    return modules


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    violations = []

    for module_name, forbidden_set in FORBIDDEN.items():
        filepath = root / f"{module_name}.py"
        if not filepath.exists():
            print(f"  [skip] {filepath} not found")
            continue

        found = imported_modules(filepath)
        bad = found & forbidden_set
        if bad:
            for b in sorted(bad):
                violations.append(f"{module_name}.py imports {b} — violates one-way dependency rule")
        else:
            print(f"  [ok] {module_name}.py — no forbidden imports")

    if violations:
        print("\nLAYER VIOLATIONS FOUND:")
        for v in violations:
            print(f"  \u2717 {v}")
        sys.exit(1)

    print("\nAll layer boundaries clean.")
    sys.exit(0)


if __name__ == "__main__":
    main()
