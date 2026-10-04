#!/usr/bin/env python3
"""Export the API's OpenAPI schema to ``packages/api-contract/openapi.json``.

The committed file is the contract: `tests/unit/test_api_contract.py` re-exports the schema from a
locally built app and fails when the two disagree, so a route cannot be added, renamed or removed
without the contract being updated deliberately.

    python3 scripts/export_openapi.py            # write the file
    python3 scripts/export_openapi.py --check     # exit 1 when it is out of date
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

TARGET = REPO_ROOT / "packages" / "api-contract" / "openapi.json"


def build_schema() -> dict:
    from natasha.api.app import create_app

    # serve_ui=False: the contract is the API, and mounting the console would only add its routes.
    app = create_app(runtime=None, serve_ui=False)
    schema = app.openapi()
    schema["info"]["title"] = "Natasha API"
    return schema


def render(schema: dict) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="export or check the committed API contract")
    parser.add_argument("--check", action="store_true",
                        help="verify the committed file matches the app (no writes)")
    args = parser.parse_args()

    rendered = render(build_schema())
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
        if current != rendered:
            print(f"{TARGET.relative_to(REPO_ROOT)} is out of date; run scripts/export_openapi.py")
            return 1
        print(f"{TARGET.relative_to(REPO_ROOT)} is up to date")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8")
    paths = len(build_schema().get("paths", {}))
    print(f"wrote {TARGET.relative_to(REPO_ROOT)} ({paths} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
