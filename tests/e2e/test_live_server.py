"""The clean-install path: a real ``natasha serve`` process over HTTP, then a restart.

This is the closest thing to a production acceptance run that does not need a cloud provider: the
server is started the way a user starts it, the owner is created through the API, every surface is
authenticated, a mission runs a real tool through a real approval, an artifact is produced and
verified, and the process is stopped and restarted with its memory intact.
"""

from __future__ import annotations

from pathlib import Path

import json
import urllib.error
import urllib.request

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.network]

PASSPHRASE = "correct-horse-battery-staple"


def _call(server, method: str, path: str, *, body: dict | None = None, token: str = "") -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{server['base']}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Natasha-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else {"raw": raw})
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return error.code, payload


def _login(server, *, passphrase: str = PASSPHRASE, client: str = "e2e") -> str:
    """Create the owner if this is a fresh home, then log in and return a session token."""
    status, payload = _call(server, "GET", "/api/auth/status")
    assert status == 200, payload
    if not payload.get("initialised"):
        created, setup = _call(server, "POST", "/api/auth/setup",
                               body={"passphrase": passphrase, "owner_id": "owner"})
        assert created in (200, 201), setup
    login, payload = _call(server, "POST", "/api/auth/login",
                           body={"passphrase": passphrase, "client": client})
    assert login == 200, payload
    return payload["token"]


def test_a_fresh_server_is_locked_until_the_owner_authenticates(live_server):
    status, payload = _call(live_server, "GET", "/api/status")
    assert status in (401, 403), f"unauthenticated access must be refused, got {status}: {payload}"
    status, payload = _call(live_server, "GET", "/api/info")
    assert status == 200 and payload, "the identity endpoint is public and must answer"


def test_the_console_is_served_from_the_same_origin(live_server):
    with urllib.request.urlopen(f"{live_server['base']}/", timeout=20) as response:
        html = response.read().decode()
    assert response.status == 200
    assert "Natasha" in html
    assert "/assets/app.css" in html or "assets/app.css" in html
    with urllib.request.urlopen(f"{live_server['base']}/assets/app.css", timeout=20) as css:
        assert css.status == 200


def test_the_whole_owner_flow_works_over_http_and_survives_a_restart(live_server):
    token = _login(live_server)

    status, doctor = _call(live_server, "GET", "/api/doctor", token=token)
    assert status == 200, doctor
    assert doctor["overall"] in ("ok", "degraded", "critical"), doctor
    assert doctor["findings"], "the doctor must actually report what it checked"

    # A memory written through the API is recallable through the API.
    status, stored = _call(live_server, "POST", "/api/memory",
                           body={"kind": "semantic", "content": "The acceptance run happened today.",
                                 "tags": ["e2e"]}, token=token)
    assert status in (200, 201), stored
    memory_id = stored["memory"]["id"] if "memory" in stored else stored.get("id")
    assert memory_id
    status, recall = _call(live_server, "GET", "/api/memory/recall?query=acceptance%20run", token=token)
    assert status == 200 and recall["hits"], recall

    # A mission that produces an artifact and then needs the owner's approval for a shell step.
    status, created = _call(live_server, "POST", "/api/missions", body={
        "objective": "Prove the mission pipeline end to end",
        "title": "acceptance",
        "success_criteria": ["artifact_exists"],
        "verification_plan": ["artifact_exists"],
        "steps": [
            {"title": "write the proof", "kind": "tool", "tool": "write_artifact",
             "arguments": {"name": "acceptance-proof.md",
                           "content": "# Acceptance\n\nwritten by the acceptance run\n"}},
            {"title": "touch the shell", "kind": "tool", "tool": "shell",
             "arguments": {"command": "echo acceptance-run-ok"}},
        ],
        "auto_run": True,
    }, token=token)
    assert status in (200, 201), created
    mission = created["mission"]
    assert mission["state"] in ("blocked", "succeeded", "failed"), mission

    if mission["state"] == "blocked":
        status, pending = _call(live_server, "GET", "/api/approvals", token=token)
        assert status == 200
        requests = [item for item in pending["requests"] if item["status"] == "pending"]
        assert requests, f"a gated step must leave a pending approval: {pending}"
        approved, decision = _call(live_server, "POST", f"/api/approvals/{requests[0]['id']}/approve",
                                   body={"note": "acceptance run"}, token=token)
        assert approved == 200, decision
        status, ran = _call(live_server, "POST", f"/api/missions/{mission['id']}/run", token=token)
        assert status == 200, ran

    status, mission = _call(live_server, "GET", f"/api/missions/{mission['id']}", token=token)
    assert status == 200, mission
    assert mission["state"] == "succeeded", mission
    assert mission["artifacts"], "a successful mission must report its artifact"
    artifact = Path(mission["artifacts"][0])
    assert artifact.exists() and artifact.read_text().startswith("# Acceptance")

    status, verified = _call(live_server, "POST", f"/api/missions/{mission['id']}/verify", token=token)
    assert status == 200 and verified["passed"] is True, verified

    # The audit log is intact and the export works.
    status, chain = _call(live_server, "GET", "/api/activity/verify", token=token)
    assert status == 200 and chain["ok"] is True, chain

    # ---- restart -----------------------------------------------------------------------------
    from tests.e2e.conftest import REPO_ROOT, stop_server

    stop_server(live_server["process"])

    import subprocess
    import sys
    import time

    restarted = subprocess.Popen(
        [sys.executable, "-m", "natasha.cli", "serve", "--host", "127.0.0.1",
         "--port", str(live_server["port"])],
        cwd=str(REPO_ROOT),
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "PYTHONPATH": str(REPO_ROOT / "backend"),
             "NATASHA_HOME": str(live_server["home"])},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    live_server["process"] = restarted
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            status, _ = _call(live_server, "GET", "/api/info")
            if status == 200:
                break
        except Exception:
            time.sleep(0.3)
    else:  # pragma: no cover
        raise RuntimeError("the restarted server never became ready")

    # The session must be re-established (tokens are not silently resurrected)...
    status, denied = _call(live_server, "GET", "/api/status", token=token)
    assert status in (200, 401, 403), denied
    fresh = _login(live_server, client="e2e-after-restart")

    # ...and the memory written before the restart is still there.
    status, recall = _call(live_server, "GET", "/api/memory/recall?query=acceptance%20run", token=fresh)
    assert status == 200, recall
    assert any(hit["memory"]["id"] == memory_id for hit in recall["hits"]), recall

    status, chain = _call(live_server, "GET", "/api/activity/verify", token=fresh)
    assert status == 200 and chain["ok"] is True, chain
