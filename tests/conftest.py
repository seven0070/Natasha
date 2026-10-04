"""Test fixtures.

Isolation rule: every test run gets its own ``NATASHA_HOME`` under a temporary directory, set before
any ``natasha`` import, so no test can read or write the developer's real state. Singleton caches are
reset between tests by :func:`reset_everything`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# --- the home must exist before natasha is imported anywhere ------------------------------- #
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="natasha-tests-"))
os.environ.setdefault("NATASHA_HOME", str(_SESSION_HOME))
os.environ.setdefault("NATASHA_AUTH_REQUIRED", "true")

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The reset helper lives in its own module so tests can import it by a stable name instead of
# importing "conftest", which pytest also loads under that bare name from other directories.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from natasha_testkit import RESET_PROBLEMS, reset_everything  # noqa: E402


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A fresh NATASHA_HOME for one test."""
    target = tmp_path / "natasha-home"
    target.mkdir(parents=True, exist_ok=True)
    reset_everything(target)
    yield target
    reset_everything()


@pytest.fixture()
def settings(home: Path):
    from natasha.core import load_settings

    return load_settings(str(home))


@pytest.fixture()
def log(home: Path):
    from natasha.events import get_event_log

    return get_event_log()


@pytest.fixture()
def policy(settings):
    from natasha.security.policy import PolicyEngine

    return PolicyEngine(settings.security)


@pytest.fixture()
def runtime(home: Path):
    """A fully wired runtime with no model providers configured (offline echo is the last resort)."""
    from natasha.runtime import get_runtime

    instance = get_runtime()
    yield instance
    try:
        instance.shutdown()
    except Exception:
        pass
    reset_everything()


@pytest.fixture()
def fixtures_dir() -> Path:
    return FIXTURES
