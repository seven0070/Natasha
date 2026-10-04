"""End-to-end fixtures.

The e2e suite drives the runtime the way production does: a real runtime on a real ``NATASHA_HOME``,
a real database, the real policy engine and the real API app. The only substitution is the model
provider, which is scripted so a whole pipeline can be asserted deterministically. Nothing else is
stubbed, and the console smoke test starts the actual ``natasha serve`` process.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from natasha_testkit import build_scripted_runtime, scripted_provider_class

ScriptedProvider = scripted_provider_class()

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def scripted():
    return ScriptedProvider(credential_provider=None)


@pytest.fixture()
def runtime(home):
    """The real runtime, scripted model, on a fresh home."""
    yield build_scripted_runtime(home)


@pytest.fixture()
def rt(runtime):
    return runtime[0]


@pytest.fixture()
def client(runtime):
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
    passphrase = "correct-horse-battery-staple"
    if not client.get("/api/auth/status").json().get("initialised"):
        created = client.post("/api/auth/setup",
                              json={"passphrase": passphrase, "owner_id": "owner"})
        assert created.status_code in (200, 201), created.text
    login = client.post("/api/auth/login", json={"passphrase": passphrase, "client": "e2e"})
    assert login.status_code == 200, login.text
    client.headers.update({"X-Natasha-Token": login.json()["token"]})
    return client


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def live_server(tmp_path):
    """Start the real ``natasha serve`` process on a private home and port, then stop it.

    This is the startup path a user takes: the CLI, uvicorn, the mounted console, the auth gate.
    """
    home = tmp_path / "live-home"
    home.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "natasha.cli.main", "serve", "--host", "127.0.0.1",
         "--port", str(port)],
        cwd=str(REPO_ROOT), env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                                 "PYTHONPATH": str(REPO_ROOT / "backend"),
                                 "NATASHA_HOME": str(home)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 40
    import urllib.error
    import urllib.request

    while time.time() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"natasha serve exited early ({process.returncode}):\n{output[-2000:]}")
        try:
            with urllib.request.urlopen(f"{base}/api/info", timeout=2) as response:
                if response.status == 200:
                    break
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.3)
    else:  # pragma: no cover - only on a broken environment
        process.terminate()
        raise RuntimeError("natasha serve did not become ready in time")
    yield {"base": base, "home": home, "port": port, "process": process}
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover
        process.kill()
        process.wait(timeout=10)
