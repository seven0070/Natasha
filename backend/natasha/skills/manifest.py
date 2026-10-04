"""Skill manifests: parsing, validation and integrity."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core import SkillError, sha256_file

MANIFEST_NAME = "skill.json"
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_\-]{2,48}$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")

#: Permissions a skill may declare. Anything outside this set fails validation.
ALLOWED_PERMISSIONS = {
    "fs.read", "fs.write", "net.http", "memory.read", "memory.write", "artifact.write",
    "model.call", "shell.exec", "code.exec", "credential.use", "browser.control",
}


class SkillValidationError(SkillError):
    """Manifest failed validation."""


@dataclass
class SkillManifest:
    """Declared contract of a skill."""

    id: str
    name: str
    version: str
    description: str = ""
    entrypoint: str = "main.py"
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    permissions: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    risk: str = "MEDIUM"
    publisher: str = ""
    checksum: str = ""
    signature: str = ""
    runtime: str = "python"
    timeout_seconds: float = 120.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"


def load_manifest(path: str | Path) -> SkillManifest:
    """Load and validate a manifest from a file or directory."""
    target = Path(path)
    if target.is_dir():
        target = target / MANIFEST_NAME
    if not target.is_file():
        raise SkillValidationError(f"manifest not found: {target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillValidationError(f"manifest is not valid JSON: {exc}") from exc
    return validate_manifest(data, base_dir=target.parent)


def validate_manifest(data: dict[str, Any], *, base_dir: Path | None = None) -> SkillManifest:
    """Validate manifest structure, permissions and declared entrypoint."""
    problems: list[str] = []
    for required in ("id", "name", "version"):
        if not data.get(required):
            problems.append(f"missing required field {required!r}")
    skill_id = str(data.get("id", ""))
    if skill_id and not NAME_PATTERN.match(skill_id):
        problems.append(f"id {skill_id!r} must be lowercase alphanumeric with _ or -")
    version = str(data.get("version", ""))
    if version and not SEMVER.match(version):
        problems.append(f"version {version!r} is not semver")

    permissions = list(data.get("permissions", []) or [])
    unknown = set(permissions) - ALLOWED_PERMISSIONS
    if unknown:
        problems.append(f"unknown permissions: {sorted(unknown)}")

    dependencies = list(data.get("dependencies", []) or [])
    for dependency in dependencies:
        if not isinstance(dependency, str) or not dependency.strip():
            problems.append("dependencies must be non-empty strings")
            break

    runtime = str(data.get("runtime", "python")).lower()
    if runtime not in {"python", "node", "binary"}:
        problems.append(f"unsupported runtime {runtime!r}")

    entrypoint = str(data.get("entrypoint", "main.py"))
    if Path(entrypoint).is_absolute() or ".." in Path(entrypoint).parts:
        problems.append("entrypoint must be a relative path inside the skill directory")

    if base_dir is not None and not problems:
        entry_path = base_dir / entrypoint
        if not entry_path.is_file():
            problems.append(f"entrypoint {entrypoint!r} does not exist in the skill package")

    if problems:
        raise SkillValidationError("; ".join(problems))

    manifest = SkillManifest(
        id=skill_id, name=str(data.get("name", skill_id)), version=version,
        description=str(data.get("description", "")), entrypoint=entrypoint,
        inputs=dict(data.get("inputs", {}) or {}), outputs=dict(data.get("outputs", {}) or {}),
        permissions=permissions, dependencies=dependencies,
        risk=str(data.get("risk", "MEDIUM")).upper(), publisher=str(data.get("publisher", "")),
        checksum=str(data.get("checksum", "")), signature=str(data.get("signature", "")),
        runtime=runtime, timeout_seconds=float(data.get("timeout_seconds", 120.0) or 120.0),
        metadata=dict(data.get("metadata", {}) or {}),
    )
    if base_dir is not None and not manifest.checksum:
        manifest.checksum = compute_skill_checksum(base_dir, manifest)
    return manifest


def compute_skill_checksum(directory: Path, manifest: SkillManifest | None = None) -> str:
    """Deterministic checksum over the skill's files (entrypoint + manifest + assets)."""
    import hashlib

    digest = hashlib.sha256()
    files = sorted(
        path for path in directory.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith(".pyc")
    )
    for path in files:
        digest.update(str(path.relative_to(directory)).encode())
        digest.update(sha256_file(path).encode())
    return digest.hexdigest()


def verify_checksum(directory: Path, expected: str) -> tuple[bool, str]:
    """Verify a package checksum captured at install time."""
    actual = compute_skill_checksum(directory)
    return (actual == expected, actual)


def write_manifest(manifest: SkillManifest, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / MANIFEST_NAME
    target.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
    return target
