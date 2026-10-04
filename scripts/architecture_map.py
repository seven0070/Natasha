"""Print the repository's real import graph and check it against the declared layer rules.

The architecture itself lives in :mod:`natasha.architecture` - the layer table, the rules and the
scanner. This script is only the report: it is what produced the numbers in
``ARCHITECTURE_REFACTOR_REPORT.md`` and what ``scripts/check_architecture.py`` runs in CI.

Usage::

    python3 scripts/architecture_map.py                 # human-readable report
    python3 scripts/architecture_map.py --json          # machine-readable
    python3 scripts/architecture_map.py --edges         # every cross-layer import
    python3 scripts/architecture_map.py --check         # exit 1 on an undeclared violation
    python3 scripts/architecture_map.py --check --strict  # exit 1 on declared exceptions too
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from natasha.architecture import (  # noqa: E402 - path juggling is the point of a script
    LAYERS,
    MODULE_EXCEPTIONS,
    RULES,
    layer_of,
    rule_for,
    scan_tree,
)


def as_json(report, violations, exceptions) -> dict:
    return {
        "layers": LAYERS,
        "rules": {f"{source}->{target}": allowed
                  for (source, target), (allowed, _) in RULES.items()},
        "files": report.files,
        "matrix": report.layer_matrix(),
        "violations": [
            {"file": edge.file, "line": edge.line, "from": edge.source, "to": edge.target,
             "from_layer": edge.source_layer, "to_layer": edge.target_layer, "lazy": edge.lazy}
            for edge in violations
        ],
        "exceptions": [
            {"file": edge.file, "line": edge.line, "from": edge.source, "to": edge.target,
             "from_layer": edge.source_layer, "to_layer": edge.target_layer, "reason": edge.exception}
            for edge in exceptions
        ],
        "counts": {"files": len(report.files), "edges": len(report.edges),
                   "violations": len(violations), "exceptions": len(exceptions)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="map and check the Natasha layer boundaries")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--check", action="store_true", help="exit 1 when a rule is violated")
    parser.add_argument("--edges", action="store_true", help="list every cross-layer import")
    parser.add_argument("--strict", action="store_true",
                        help="treat declared exceptions as violations too")
    args = parser.parse_args()

    report = scan_tree(REPO_ROOT)
    violations = report.violations()
    exceptions = report.exceptions()

    if args.json:
        print(json.dumps(as_json(report, violations, exceptions), indent=2))
        if args.check and violations:
            return 1
        if args.check and args.strict and exceptions:
            return 1
        return 0

    print(f"Layers: {', '.join(sorted(LAYERS))} ({len(LAYERS)}) "
          f"over {len(report.files)} modules | {len(report.edges)} project-internal imports | "
          f"{len(exceptions)} declared exception(s)\n")

    matrix = report.layer_matrix()
    print(f"{'from':<16}{'to':<16}{'ok':>5}{'exc':>5}{'viol':>6}  rule")
    for source in sorted(matrix):
        for target in sorted(matrix[source]):
            counts = matrix[source][target]
            allowed, reason = rule_for(source, target)
            mark = "ok " if allowed else ("excepted" if not counts["violation"] else "VIOLATION")
            print(f"{source:<16}{target:<16}{counts['ok']:>5}{counts['exception']:>5}"
                  f"{counts['violation']:>6}  {mark} {reason}")

    if args.edges:
        print()
        for edge in sorted(report.edges, key=lambda item: (item.file, item.line)):
            kind = "lazy" if edge.lazy else "top "
            print(f"{edge.file}:{edge.line} [{kind}] {edge.source_layer}->{edge.target_layer} "
                  f"{edge.source} -> {edge.target}")

    print()
    if exceptions:
        print(f"{len(exceptions)} declared exception(s) - known, reasoned, ratcheted:")
        for edge in exceptions:
            print(f"  {edge.file}:{edge.line}  {edge.source_layer} -> {edge.target_layer}"
                  f"  ({edge.source} -> {edge.target})\n      reason: {edge.exception}")
        stale = report.stale_exceptions()
        if stale:
            print(f"  stale exception(s) that match no import (remove them): {stale}")
        print()
    if violations:
        print(f"{len(violations)} undeclared violation(s):")
        for edge in violations:
            print(f"  {edge.file}:{edge.line}  {edge.source_layer} imports {edge.target_layer}"
                  f"  ({edge.source} -> {edge.target})")
        return 1
    if args.strict and exceptions:
        print(f"strict mode: {len(exceptions)} declared exception(s) still violate the rules")
        return 1
    print(f"no undeclared layering violations ({len(report.files)} modules scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
