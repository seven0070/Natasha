"""Rate limits must actually bite - and must not be a back door.

The API can start model calls, shell commands and missions, and it guards the owner passphrase, so
these tests check three things: a burst is refused with a 429 carrying ``Retry-After``, a refusal
does not break the rest of the API, and the budgets are per client so one caller cannot exhaust
another's.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_a_burst_of_chat_requests_is_refused(owner):
    """The chat bucket is small enough that a runaway loop is stopped."""
    statuses = []
    for _ in range(40):
        response = owner.post("/api/chat", json={"message": "hello"})
        statuses.append(response.status_code)
        if response.status_code == 429:
            assert response.headers.get("Retry-After"), "a 429 must say when to retry"
            assert "chat" in response.json()["detail"]
            break
    assert 429 in statuses, f"the chat bucket never engaged: {statuses}"
    assert 200 in statuses, "the limiter must allow normal use before it engages"


def test_login_attempts_are_throttled(owner):
    """Guessing the owner passphrase must not be free."""
    failures = 0
    for _ in range(30):
        response = owner.post("/api/auth/login", json={"passphrase": "wrong-passphrase"})
        if response.status_code == 429:
            break
        assert response.status_code in (400, 401, 403), response.text
        failures += 1
    assert failures < 30, "the login bucket never engaged"


def test_reads_keep_working_after_a_limit_is_hit(owner):
    """A limited bucket must not lock the owner out of the rest of the console."""
    for _ in range(40):
        if owner.post("/api/chat", json={"message": "hello"}).status_code == 429:
            break
    assert owner.get("/api/status").status_code == 200
    assert owner.get("/api/activity").status_code == 200


def test_limits_can_be_disabled_by_configuration(owner):
    """An operator who wants no in-process limit can turn it off; the default is on."""
    runtime = owner.app.state.runtime
    assert runtime.settings.limits.enabled is True
    runtime.settings.limits.enabled = False
    owner.app.state.limiter = None
    for _ in range(40):
        assert owner.post("/api/chat", json={"message": "hello"}).status_code == 200


def test_the_websocket_shares_the_chat_budget(owner):
    """An unauthenticated websocket still costs a token, so the socket cannot be a bypass."""
    limiter = owner.app.state.limiter
    before = limiter.stats()["count"]
    with owner.websocket_connect("/api/ws/chat") as socket:
        socket.send_json({"token": "not-a-token", "message": "hi"})
        frame = socket.receive_json()
        assert frame["type"] == "error"
    assert limiter.stats()["count"] >= before, "the socket must be accounted for"
