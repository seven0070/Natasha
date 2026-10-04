"""The skills CLI, driven as a real process.

`tests/integration/test_skills_chain.py` covers the lifecycle objects; this file covers the commands
the owner actually types, because the two are wired separately and a stale attribute in an output
formatter is invisible to a class-level test. Each test starts the CLI in a subprocess with its own
``NATASHA_HOME``, so nothing leaks between tests and the process behaves exactly as it does for a user.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_skill"
SKILL_ID = json.loads((SAMPLE / "skill.json").read_text(encoding="utf-8"))["id"]


def _cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["NATASHA_HOME"] = str(home)
    environment["PYTHONPATH"] = str(BACKEND) + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "natasha.cli", *args],
        capture_output=True, text=True, env=environment, cwd=str(REPO_ROOT), timeout=180,
    )


@pytest.fixture()
def cli_home(tmp_path: Path) -> Path:
    home = tmp_path / "natasha-home"
    home.mkdir()
    return home


def test_the_cli_installs_a_skill_and_lists_it(cli_home):
    """`skills list` used to raise AttributeError once a skill existed: the record shape had moved."""
    installed = _cli(cli_home, "skills", "install", str(SAMPLE))
    assert installed.returncode == 0, installed.stderr
    assert SKILL_ID in installed.stdout

    listing = _cli(cli_home, "skills", "list")
    assert listing.returncode == 0, listing.stderr
    assert SKILL_ID in listing.stdout
    assert "ACTIVE" in listing.stdout


def test_skills_list_is_json_when_asked(cli_home):
    _cli(cli_home, "skills", "install", str(SAMPLE))
    listing = _cli(cli_home, "--json", "skills", "list")
    assert listing.returncode == 0, listing.stderr
    records = json.loads(listing.stdout)
    assert isinstance(records, list) and records
    assert records[0]["id"] == SKILL_ID


def test_an_empty_vault_is_not_an_error(cli_home):
    listing = _cli(cli_home, "skills", "list")
    assert listing.returncode == 0, listing.stderr
    assert listing.stdout.strip() == ""


def test_the_cli_runs_an_installed_skill_with_a_payload(cli_home):
    _cli(cli_home, "skills", "install", str(SAMPLE))
    run = _cli(cli_home, "--json", "skills", "run", SKILL_ID, "--payload", '{"text": "one two three"}')
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["ok"] is True, result
    assert result["output"]["words"] == 3
