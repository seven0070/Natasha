"""The HTTP and WebSocket surface: authentication, authorisation, validation and the chat stream.

The API is the boundary the UI and the SDKs go through, so these tests check the boundary properties:
nothing sensitive is reachable without the owner's token, a weak passphrase cannot be set, request
bodies are validated, the websocket refuses an unauthenticated client, and a streamed turn produces
the same final result as the non-streamed one.
"""

from __future__ import annotations

import json

import pytest

from natasha.api.app import create_app

pytestmark = pytest.mark.integration

PASSPHRASE = "correct-horse-battery-staple"


@pytest.fixture()
def client(runtime):
    """A TestClient over the real app, with the scripted runtime injected."""
    from fastapi.testclient import TestClient

    runtime, adapter = runtime
    app = create_app(runtime=runtime, serve_ui=False)
    with TestClient(app) as test_client:
        test_client.app.state.runtime = runtime
        test_client.adapter = adapter
        yield test_client


@pytest.fixture()
def owner(client):
    """An authenticated client: set up the owner, log in, return the token."""
    status = client.get("/api/auth/status").json()
    if not status.get("initialised"):
        created = client.post("/api/auth/setup", json={"passphrase": PASSPHRASE, "owner_id": "owner"})
        assert created.status_code in (200, 201), created.text
    login = client.post("/api/auth/login", json={"passphrase": PASSPHRASE, "client": "test"})
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    client.headers.update({"X-Natasha-Token": token})
    return token


def test_the_api_is_not_reachable_without_the_owners_token(client):
    for path in ("/api/memory/stats", "/api/missions", "/api/security/audit", "/api/approvals/pending"):
        response = client.get(path)
        assert response.status_code in (401, 403), f"{path} answered {response.status_code}"


def test_setup_requires_a_real_passphrase(client):
    client.get("/api/auth/status")
    weak = client.post("/api/auth/setup", json={"passphrase": "short", "owner_id": "owner"})
    assert weak.status_code == 422                       # schema validation, not a silent default


def test_the_owner_can_authenticate_and_read_protected_state(client, owner):
    me = client.get("/api/auth/sessions")
    assert me.status_code == 200
    stats = client.get("/api/memory/stats")
    assert stats.status_code == 200
    assert "by_kind" in stats.json()


def test_a_wrong_passphrase_is_rejected(client, owner):
    response = client.post("/api/auth/login", json={"passphrase": "not-the-passphrase", "client": "test"})
    assert response.status_code in (401, 403)


def test_a_chat_turn_over_http_returns_the_runtime_answer(client, owner):
    client.adapter.script = [{"content": "The API is wired to the real executive."}]
    response = client.post("/api/chat", json={"message": "Are you wired up?"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "real executive" in body["reply"]
    assert body["provider"] == "scripted"
    assert body["turn_id"]


def test_the_streaming_endpoint_emits_events_and_a_final_result(client, owner):
    client.adapter.script = [{"content": "Streaming over SSE."}]
    with client.stream("POST", "/api/chat/stream", json={"message": "Stream this"}) as response:
        assert response.status_code == 200
        payload = "".join(chunk for chunk in response.iter_text())
    events = [json.loads(line[6:]) for line in payload.splitlines() if line.startswith("data: ")]
    kinds = [event.get("type") for event in events]
    assert "token" in kinds
    assert kinds[-1] == "turn_finished"
    assert events[-1]["result"]["reply"] == "Streaming over SSE."


def test_the_websocket_refuses_an_unauthenticated_client(client):
    client.get("/api/auth/status")
    with client.websocket_connect("/api/ws/chat") as socket:
        socket.send_json({"type": "auth", "token": "not-a-real-token"})
        first = socket.receive_json()
    assert first["type"] == "error"
    assert "authentication" in first["error"].lower()


def test_the_websocket_runs_a_turn_for_the_owner(client, owner):
    client.adapter.script = [{"content": "Hello over the socket."}]
    with client.websocket_connect("/api/ws/chat") as socket:
        socket.send_json({"type": "auth", "token": owner})
        socket.send_json({"type": "chat", "message": "Hi", "conversation_id": "conv-ws"})
        seen = []
        while True:
            event = socket.receive_json()
            seen.append(event)
            if event.get("type") in ("turn_finished", "error"):
                break
    kinds = [event["type"] for event in seen]
    assert "turn_finished" in kinds, seen
    final = next(event for event in seen if event["type"] == "turn_finished")
    assert "socket" in final["result"]["reply"]


def test_a_bad_request_body_is_rejected_with_a_schema_error(client, owner):
    response = client.post("/api/chat", json={"message": ""})
    assert response.status_code == 422


def test_an_unknown_route_is_a_clean_404(client, owner):
    assert client.get("/api/does-not-exist").status_code == 404


def test_memory_writes_over_the_api_are_owner_actions(client, owner):
    created = client.post("/api/memory", json={
        "kind": "semantic", "content": "The owner's project codename is Falcon.",
        "tags": ["project"], "importance": 0.8,
    })
    assert created.status_code in (200, 201), created.text
    recall = client.get("/api/memory/recall", params={"query": "project codename", "limit": 5})
    assert recall.status_code == 200
    hits = recall.json()["hits"]
    assert hits and "Falcon" in hits[0]["memory"]["content"]


def test_a_mission_can_be_created_run_and_inspected_over_the_api(client, owner):
    client.adapter.script = [{"content": "ok"}] * 3
    created = client.post("/api/missions", json={
        "objective": "Note something down", "success_criteria": ["steps_completed"],
        "steps": [{"title": "note", "kind": "note", "payload": {"note": "api-created"}}],
        "auto_run": True,
    })
    assert created.status_code in (200, 201), created.text
    mission_id = created.json()["mission"]["id"]
    detail = client.get(f"/api/missions/{mission_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == mission_id
    assert body["steps"]


def test_the_audit_endpoint_reports_an_intact_chain(client, owner):
    response = client.get("/api/security/audit")
    assert response.status_code == 200
    events = response.json()["events"]
    assert events
    from natasha.events import get_event_log

    ok, report = get_event_log().verify_chain()
    assert ok is True, report


def test_secrets_never_come_back_out_of_the_api(client, owner):
    """A credential written through the API is referenced, never echoed."""
    stored = client.post("/api/security/credentials", json={
        "name": "demo-key", "kind": "api_key", "secret": "sk-live-supersecret-value-123456",
        "metadata": {"provider": "demo"},
    })
    if stored.status_code in (200, 201):
        listed = client.get("/api/security/credentials")
        assert listed.status_code == 200
        body = listed.text
        assert "sk-live-supersecret-value-123456" not in body


def test_a_dangerous_tool_over_the_chat_api_still_needs_approval(client, owner, home):
    """Being logged in as the owner must not silently authorise the *model's* tool calls."""
    from pathlib import Path

    proof = Path(home) / "workspace" / "api-approval.txt"
    client.adapter.script = [
        {"content": "Running it.", "tool_calls": [{"name": "shell", "arguments": {"command": f"touch {proof}"}}]},
        {"content": "It did not run."},
    ]
    response = client.post("/api/chat", json={"message": "Please create that file"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tool_calls"] and body["tool_calls"][0]["ok"] is False
    assert body["approvals_requested"], "the owner must be asked before the action runs"
    assert not proof.exists()
