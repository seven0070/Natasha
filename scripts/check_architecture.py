"""CI gate: fail the build when a module import breaks a declared layer rule.

Equivalent to ``python3 scripts/architecture_map.py --check`` but with CI-shaped output (one line
per violation, no tables) and an optional ``--report`` file. Declared exceptions - the known debt
listed in :data:`natasha.architecture.MODULE_EXCEPTIONS` - do not fail the build; new, undeclared
cross-layer imports do. ``--strict`` also fails on the declared debt, which is the ratchet used once
the ports refactor lands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from natasha.architecture import scan_tree  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="fail when the layering rules are broken")
    parser.add_argument("--strict", action="store_true",
                        help="also fail on declared, reasoned exceptions")
    parser.add_argument("--root", default=str(REPO_ROOT), help="repository root to scan")
    parser.add_argument("--report", default="", help="write the violation list to this file")
    args = parser.parse_args()

    report = scan_tree(args.root)
    violations = report.violations()
    exceptions = report.exceptions()

    lines = [f"architecture: {len(report.files)} modules, {len(report.edges)} internal imports, "
             f"{len(violations)} violation(s), {len(exceptions)} declared exception(s)"]
    for edge in violations:
        lines.append(f"VIOLATION {edge.file}:{edge.line} "
                     f"{edge.source_layer}->{edge.target_layer} {edge.source} -> {edge.target}")
    for prefix, layer in report.stale_exceptions():
        lines.append(f"STALE-EXCEPTION {prefix} -> {layer} (no import matches; remove it)")

    failed = bool(violations)
    if args.strict and exceptions:
        lines.append(f"strict: {len(exceptions)} declared exception(s) still violate the rules")
        failed = True

    output = "\n".join(lines)
    print(output)
    if args.report:
        Path(args.report).write_text(output + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
