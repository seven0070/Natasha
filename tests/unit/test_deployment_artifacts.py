"""The deployment artifacts must describe a runnable system.

There is no Docker daemon in CI, so instead of building the image this test checks the things that
break a deployment silently: a `COPY` of a path that does not exist, a compose file that is not valid
YAML, an entry point that is not executable or references a command that was renamed, a settings file
that does not parse, and documentation that points at files the reader cannot open.

Everything asserted here was verified by hand against the working tree first; the test exists so it
cannot rot.
"""

from __future__ import annotations

import re
import shlex
import sys
import tomllib
from pathlib import Path

import pytest

from natasha.core.config import load_settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    path = REPO_ROOT / relative
    assert path.is_file(), f"{relative} is missing"
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ Dockerfile


def test_dockerfile_copies_only_paths_that_exist():
    text = _read("Dockerfile")
    copied = re.findall(r"^COPY\s+(?!--from)(.+)$", text, flags=re.MULTILINE)
    assert copied, "the Dockerfile copies nothing; the image would be empty"
    for line in copied:
        parts = shlex.split(line)
        sources = parts[:-1]
        assert sources, f"COPY with no source: {line}"
        for source in sources:
            assert not source.startswith("--"), f"unexpected flag in COPY: {line}"
            assert (REPO_ROOT / source).exists(), f"Dockerfile COPY references a missing path: {source}"


def test_dockerfile_uses_a_non_root_user_and_handles_signals():
    text = _read("Dockerfile")
    assert re.search(r"^USER\s+(?!root)\S+", text, flags=re.MULTILINE), "the image must not run as root"
    assert "tini" in text, "Python must not be PID 1: without an init, SIGTERM is not delivered"
    assert "HEALTHCHECK" in text, "a container without a healthcheck cannot be supervised"
    assert 'VOLUME' in text or "volumes:" in _read("docker-compose.yml")


def test_the_dockerfile_healthcheck_targets_a_real_endpoint_shape():
    text = _read("Dockerfile")
    assert "/api/health" in text
    # runtime.ok is what /api/health actually returns; checking a bare 200 would pass on a half-dead app.
    assert "runtime" in text and "get('ok')" in text


def test_the_container_entrypoint_runs_migrations_before_serving():
    entrypoint = REPO_ROOT / "infra" / "docker" / "entrypoint.sh"
    assert entrypoint.is_file()
    if sys.platform != "win32":
        assert entrypoint.stat().st_mode & 0o111, "the entrypoint must be executable"
    text = entrypoint.read_text(encoding="utf-8")
    assert "migrate up" in text, "the container must migrate the volume it is starting against"
    assert "apps.server" in text, "the entrypoint must start the documented entry point"
    assert text.index("migrate up") < text.index("apps.server"), "migrations must run before serving"
    # A shell that fails fast: a broken migration must stop the container, not serve half a schema.
    assert "set -eu" in text


# ------------------------------------------------------------------ compose


def test_compose_is_valid_yaml_and_declares_the_expected_services():
    yaml = pytest.importorskip("yaml")  # the dev extra ships PyYAML; skip cleanly if it does not
    document = yaml.safe_load(_read("docker-compose.yml"))
    services = document.get("services", {})
    assert "natasha" in services
    natasha = services["natasha"]
    assert natasha["build"]["context"] == "."
    assert any(volume.endswith(":/data") for volume in natasha["volumes"]), (
        "the agent's home must be a volume, or every restart loses the vault and the database")
    assert natasha.get("security_opt") == ["no-new-privileges:true"]
    assert "ALL" in natasha.get("cap_drop", []), "the agent needs no Linux capabilities"
    assert "healthcheck" in natasha
    # Optional services must be behind profiles so `docker compose up` stays minimal.
    for optional in ("ollama", "postgres"):
        if optional in services:
            assert services[optional].get("profiles"), f"{optional} must be behind a profile"


def test_compose_never_contains_a_secret():
    text = _read("docker-compose.yml")
    for pattern in (r"sk-[A-Za-z0-9]{10,}", r"BEGIN [A-Z ]*PRIVATE KEY", r"password\s*[:=]\s*['\"]?[^\s${\"']"):
        assert not re.search(pattern, text), f"a credential looks hard-coded: {pattern}"


def test_env_example_lists_no_secrets_and_documents_the_nested_syntax():
    text = _read(".env.example")
    assert "NATASHA_" in text
    assert "__" in text, "nested settings use a double underscore; the example must show it"
    active = [line.strip() for line in text.splitlines()
              if line.strip() and not line.strip().startswith("#")]
    secrets = [line for line in active if re.search(r"(API_KEY|SECRET|TOKEN)\s*=", line, re.IGNORECASE)]
    assert not secrets, f"credentials must never come from the environment: {secrets}"


# ------------------------------------------------------------------ systemd + config


def test_the_systemd_unit_is_hardened_and_starts_the_documented_command():
    text = _read("infra/systemd/natasha.service")
    assert "ExecStart=/opt/natasha/.venv/bin/natasha serve" in text
    for directive in ("NoNewPrivileges=true", "ProtectSystem=strict", "PrivateTmp=true",
                      "ReadWritePaths=/var/lib/natasha"):
        assert directive in text, f"the unit is missing {directive}"
    assert "Restart=" in text
    assert "NATASHA_HOME=" in text


def test_the_repository_configuration_parses_and_loads():
    data = tomllib.loads(_read("config/natasha.toml"))
    assert data["security"]["default_effect"] == "deny", "the repository default must be deny"
    assert data["executive"]["require_verification"] is True
    assert "data_dir" not in data, "a machine-specific path must not live in the repository config"
    assert "paths" not in data

    settings = load_settings()
    assert settings.security.default_effect == "deny"
    # The layering must actually pick this file up: deployment=server comes from config/natasha.toml.
    assert settings.deployment == "server", "config/natasha.toml is not being read"
    assert settings.auth_required is True


def test_a_database_backend_the_stores_cannot_use_is_refused_loudly():
    """`memory.db_backend = "postgres"` must fail at load, not silently run SQLite.

    The runtime stores are SQLite-only; the PostgreSQL schema exists for the migration runner. A
    setting that says otherwise and is ignored is worse than a setting that does not exist.
    """
    from natasha.core import ConfigurationError
    from natasha.core.config import load_settings

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(overrides={"memory": {"db_backend": "postgres"}})
    message = str(excinfo.value)
    assert "postgres" in message and "SQLite" in message
    assert "docs/deployment.md" in message, "the error must say where the full story is"


# ------------------------------------------------------------------ documentation


def test_every_document_the_index_promises_exists():
    index = _read("docs/README.md")
    links = re.findall(r"\]\(([^)]+\.md)\)", index)
    assert links, "docs/README.md links to nothing"
    for link in links:
        assert (REPO_ROOT / "docs" / link).is_file(), f"docs/README.md links to a missing file: {link}"


def test_readme_links_resolve():
    readme = _read("README.md")
    for link in re.findall(r"\]\(([^)#][^)]*)\)", readme):
        if link.startswith(("http://", "https://", "mailto:")):
            continue
        target = (REPO_ROOT / link).resolve()
        assert target.exists(), f"README.md links to a missing path: {link}"


def test_documented_commands_exist_in_the_cli():
    """The docs are executable: every `natasha <verb> <action>` inside a fenced block is a real command.

    Only fenced code blocks are inspected - prose mentions the agent by name too often for a naive
    scan, and the blocks are exactly what a reader will copy and paste.
    """
    import argparse

    from natasha.cli.main import build_parser

    parser = build_parser()
    subparsers = next(action for action in parser._actions
                      if isinstance(action, argparse._SubParsersAction))
    commands = set(subparsers.choices)
    action_map: dict[str, set[str]] = {}
    for name, sub in subparsers.choices.items():
        actions: set[str] = set()
        for action in sub._actions:
            if isinstance(action, argparse._SubParsersAction):
                actions.update(action.choices)
            elif action.dest == "action" and action.choices:
                actions.update(action.choices)
        action_map[name] = actions

    failures: list[str] = []
    for path in sorted((REPO_ROOT / "docs").rglob("*.md")) + [REPO_ROOT / "README.md"]:
        text = path.read_text(encoding="utf-8")
        for block in re.findall(r"```[a-z]*\n(.*?)```", text, flags=re.DOTALL):
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("#") or not line.startswith("natasha "):
                    continue
                words = re.findall(r"[a-z][a-z-]*", line.split()[1] if len(line.split()) > 1 else "")
                command = line.split()[1]
                if command.startswith("-"):
                    continue
                if command not in commands:
                    failures.append(f"{path.name}: unknown command `natasha {command}`")
                    continue
                actions = action_map.get(command, set())
                rest = line.split()[2:3]
                if rest and actions and rest[0] in re.findall(r"[a-z][a-z-]*", rest[0]):
                    action = rest[0]
                    if action not in actions:
                        failures.append(
                            f"{path.name}: `natasha {command} {action}` - {command} accepts "
                            f"{sorted(actions)}")
                del words
    assert not failures, "; ".join(failures)
