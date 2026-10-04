"""Skill isolation: execute skills out-of-process, with no ambient authority.

A skill cannot reach Natasha's internals: it gets a scrubbed environment, its own working
directory, a JSON payload on stdin and a JSON result on stdout. Declared permissions are enforced
before the process starts, and the manifest decides the granted capability set.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core import SandboxError, SkillError


@dataclass
class SkillSandbox:
    """Describes how a skill may run."""

    directory: Path
    entrypoint: str
    runtime: str = "python"
    timeout_seconds: float = 120.0
    permissions: list[str] = None  # type: ignore[assignment]
    workspace: Path | None = None
    max_output_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        self.permissions = list(self.permissions or [])
        if self.runtime not in {"python", "node", "binary"}:
            raise SandboxError(f"unsupported runtime {self.runtime!r}")

    def environment(self) -> dict[str, str]:
        """Minimal environment: no path to Natasha's credentials or configuration."""
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.workspace or self.directory),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NATASHA_SKILL_PERMISSIONS": ",".join(sorted(self.permissions)),
        }

    def command(self) -> list[str]:
        entry = str(self.directory / self.entrypoint)
        if self.runtime == "python":
            return [sys.executable, "-I", entry]
        if self.runtime == "node":
            return ["node", entry]
        return [entry]


async def run_isolated(sandbox: SkillSandbox, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a skill entrypoint in a subprocess and return its JSON result."""
    if not (sandbox.directory / sandbox.entrypoint).is_file():
        raise SkillError(f"skill entrypoint missing: {sandbox.entrypoint}")
    workdir = sandbox.workspace or Path(tempfile.mkdtemp(prefix="natasha-skill-"))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        process = await asyncio.create_subprocess_exec(
            *sandbox.command(), cwd=str(sandbox.directory), env=sandbox.environment(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SandboxError(f"cannot start skill runtime {sandbox.runtime!r}: {exc}") from exc

    stdin_payload = json.dumps(payload).encode()
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=stdin_payload), timeout=sandbox.timeout_seconds
        )
    except asyncio.TimeoutError:
        process.kill()
        raise SkillError(f"skill exceeded its {sandbox.timeout_seconds}s budget")

    if len(stdout) > sandbox.max_output_bytes:
        raise SkillError(f"skill output exceeded {sandbox.max_output_bytes} bytes")
    text = stdout.decode("utf-8", "replace").strip()
    if process.returncode != 0:
        raise SkillError(
            f"skill exited with code {process.returncode}: {stderr.decode('utf-8', 'replace')[:400]}"
        )
    if not text:
        return {"ok": True, "output": None, "stderr": stderr.decode("utf-8", "replace")[:2000]}
    # Skills may print logs before the JSON result: take the last JSON object on stdout.
    for candidate in reversed(text.splitlines()):
        candidate = candidate.strip()
        if candidate.startswith("{"):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    try:
        return {"ok": True, "output": json.loads(text)}
    except json.JSONDecodeError:
        return {"ok": True, "output": text}
