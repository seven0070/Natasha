"""The committed API contract must match the application.

`packages/api-contract/openapi.json` pins the REST surface for clients. This test regenerates the
schema from a freshly built app and compares: adding, renaming or deleting a route without
re-exporting fails here, which is what makes the file a contract instead of a snapshot somebody took
once.

The generation is deliberately run in-process with a real ``create_app`` call (no runtime injected,
so no database is touched) - the same call a client would make against a live server.
"""

from __future__ import annotations

import json
from pathlib import Path

CONTRACT = Path(__file__).resolve().parents[2] / "packages" / "api-contract" / "openapi.json"

#: Routes that must exist whatever else changes: losing one of these is a breaking API change, not a
#: refactor, and the client's core flows depend on them.
REQUIRED_PATHS = (
    "/api/info",
    "/api/health",
    "/api/auth/status",
    "/api/auth/login",
    "/api/chat",
    "/api/chat/stream",
    "/api/memory",
    "/api/missions",
    "/api/approvals",
    "/api/tools",
    "/api/providers",
    "/api/skills",
    "/api/mcp",
    "/api/activity",
    "/api/voice/capabilities",
    "/api/vision/capabilities",
    "/api/computer/capabilities",
    "/api/creation/capabilities",
    "/api/settings",
    "/api/doctor",
)


def _live_schema() -> dict:
    from natasha.api.app import create_app

    schemas = []
    app = create_app(runtime=None, serve_ui=False)
    for route in app.routes:
        schema = getattr(route, "openapi", None)
        if schema:  # only real APIRoutes carry an openapi() method
            schema()
    schemas.append(app.openapi())
    return schemas[0]


def test_the_committed_contract_parses_and_has_paths():
    assert CONTRACT.is_file(), f"{CONTRACT} is missing; run scripts/export_openapi.py"
    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert document["openapi"].startswith("3."), document["openapi"]
    assert document["info"]["title"] == "Natasha API"
    assert len(document["paths"]) > 100, f"only {len(document['paths'])} paths exported"


def test_the_contract_matches_the_application():
    committed = json.loads(CONTRACT.read_text(encoding="utf-8"))
    live = _live_schema()
    # Compare paths and methods, not prose: docstrings legitimately change without breaking clients.
    def shape(document: dict) -> dict[str, list[str]]:
        return {path: sorted(method for method in operations if method != "parameters")
                for path, operations in document["paths"].items()}

    committed_shape = shape(committed)
    live_shape = shape(live)
    missing = {path: live_shape[path] for path in live_shape if path not in committed_shape}
    extra = {path: committed_shape[path] for path in committed_shape if path not in live_shape}
    assert not missing, f"the app serves routes missing from the contract: {sorted(missing)}"
    assert not extra, f"the contract promises routes the app no longer serves: {sorted(extra)}"
    changed = {path: (committed_shape[path], live_shape[path]) for path in live_shape
               if path in committed_shape and committed_shape[path] != live_shape[path]}
    assert not changed, (
        "these routes changed methods without the contract being regenerated "
        f"(run scripts/export_openapi.py): {changed}")


def test_the_contract_covers_the_endpoints_the_console_needs():
    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    paths = set(document["paths"])
    missing = [path for path in REQUIRED_PATHS if path not in paths]
    assert not missing, f"the API no longer exposes: {missing}"


def test_the_contract_contains_no_credentials_and_no_local_paths():
    """A schema is generated from signatures; it must never leak configuration or secrets."""
    text = CONTRACT.read_text(encoding="utf-8")
    # `api_key` (a credential *kind*) and `credential://` (a reference, not a value) legitimately
    # appear in descriptions and defaults. What must never appear is a secret, a key or a machine path.
    for marker in ("NATASHA_HOME=", "/home/", "/Users/", "passphrase=", "BEGIN PRIVATE KEY", "sk-"):
        assert marker not in text, f"the exported contract contains {marker!r}"
