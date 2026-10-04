"""Shared integration fixtures: a real runtime driven by a deterministic scripted provider.

Tests in this directory exercise the wiring between subsystems, so they use the *real* runtime,
database, event log, policy engine and tool registry - only the model provider is scripted, so a
turn is reproducible instead of depending on a network service.

The provider implementation lives in ``tests/natasha_testkit.py`` so the end-to-end suite can build
exactly the same runtime without importing this file.
"""

from __future__ import annotations

import pytest

from natasha_testkit import build_scripted_runtime, scripted_provider_class

ScriptedProvider = scripted_provider_class()


@pytest.fixture()
def scripted():
    """A standalone scripted provider (for tests that bring their own brain)."""
    return ScriptedProvider(credential_provider=None)


@pytest.fixture()
def runtime(home):
    """A real runtime whose only *viable* provider is the scripted one."""
    yield build_scripted_runtime(home)


@pytest.fixture()
def rt(runtime):
    """The runtime alone, when the script does not matter."""
    return runtime[0]


@pytest.fixture()
def client(runtime):
    """A TestClient over the real app with the scripted runtime injected (no UI mounted)."""
    from fastapi.testclient import TestClient

    from natasha.api.app import create_app

    runtime, adapter = runtime
    app = create_app(runtime=runtime, serve_ui=False)
    with TestClient(app) as test_client:
        test_client.app.state.runtime = runtime
        test_client.adapter = adapter
        yield test_client


@pytest.fixture()
def ui_client(runtime):
    """The same app, but serving the console from ``frontend/`` as well."""
    from fastapi.testclient import TestClient

    from natasha.api.app import create_app

    runtime, adapter = runtime
    app = create_app(runtime=runtime, serve_ui=True)
    with TestClient(app) as test_client:
        test_client.app.state.runtime = runtime
        test_client.adapter = adapter
        yield test_client


@pytest.fixture()
def owner(client):
    """Set up the owner, log in, and hand back an authenticated client."""
    passphrase = "correct-horse-battery-staple"
    status = client.get("/api/auth/status").json()
    if not status.get("initialised"):
        created = client.post("/api/auth/setup",
                              json={"passphrase": passphrase, "owner_id": "owner"})
        assert created.status_code in (200, 201), created.text
    login = client.post("/api/auth/login", json={"passphrase": passphrase, "client": "test"})
    assert login.status_code == 200, login.text
    client.headers.update({"X-Natasha-Token": login.json()["token"]})
    return client
