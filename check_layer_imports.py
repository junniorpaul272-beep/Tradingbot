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
FORBIDDEN = {
    "scanner_common":      {"scanner_observation", "scanner_live", "min_scanner"},
    "scanner_observation": {"scanner_live", "min_scanner"},
    "min_scanner":         {"scanner_live"},
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
