"""The named verification checks a mission may reference from its verification plan.

A mission plan is data - often written by a model - so it can only name checks that are *registered
here*; a name that is not registered is reported as missing rather than treated as passed. Each
factory receives ``(mission, payload)`` and returns a real :class:`VerificationCheck`.
"""

from __future__ import annotations

from typing import Any

from ..core import ConflictError
from ..verification import (
    CommandCheck,
    FileExistsCheck,
    ImportCheck,
    OutputSchemaCheck,
    PythonSyntaxCheck,
    SideEffectCheck,
    VerificationCheck,
)


def _path(payload: dict[str, Any]) -> str:
    """The path a file check applies to: explicit argument, per-check plan arguments, or the first
    artifact the mission actually produced."""
    scopes = [payload, payload.get("check_arguments") if isinstance(payload.get("check_arguments"), dict) else {}]
    for scope in scopes:
        for key in ("path", "artifact", "file", "target"):
            value = (scope or {}).get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    artifacts = payload.get("artifacts") or []
    if artifacts:
        return str(artifacts[0])
    raise ConflictError("this check needs a 'path' (or 'artifact'/'file'/'target') argument, "
                        "or a mission that produced at least one artifact")


def artifact_exists(mission: Any, payload: dict[str, Any]) -> VerificationCheck:
    """The artifact the step claims to have produced is really on disk and non-empty."""
    return FileExistsCheck(f"artifact_exists:{_path(payload).split('/')[-1]}", _path(payload),
                           min_bytes=int(payload.get("min_bytes", 1)),
                           sha256=str(payload.get("sha256", "") or ""))


def artifact_absent(mission: Any, payload: dict[str, Any]) -> VerificationCheck:
    """The artifact must *not* exist - used after a cleanup or a rollback."""

    class _Absent(VerificationCheck):
        name = "artifact_absent"

        def __init__(self, path: str) -> None:
            self.name = f"artifact_absent:{path.split('/')[-1]}"
            self.path = path

        async def check(self, context: dict[str, Any]):
            from pathlib import Path

            from ..verification import CheckResult, CheckStatus

            exists = Path(self.path).exists()
            return CheckResult(self.name, CheckStatus.FAILED if exists else CheckStatus.PASSED,
                               "file is gone" if not exists else f"file still exists: {self.path}",
                               {"path": self.path})

    return _Absent(_path(payload))


def command_succeeds(mission: Any, payload: dict[str, Any]) -> VerificationCheck:
    """A real command exits zero."""
    command = payload.get("command") or payload.get("cmd")
    if not command:
        raise ConflictError("the 'command' check needs a 'command' argument")
    argv = command if isinstance(command, list) else [str(command)]
    return CommandCheck(str(payload.get("name") or "command_succeeds"), argv,
                        cwd=str(payload.get("cwd", "") or ""),
                        timeout=float(payload.get("timeout_seconds", 300)))


def module_imports(mission: Any, payload: dict[str, Any]) -> VerificationCheck:
    """A module really imports (proves the package wiring, not just the syntax)."""
    module = payload.get("module")
    if not module:
        raise ConflictError("the 'import' check needs a 'module' argument")
    return ImportCheck(str(payload.get("name") or f"imports:{module}"), str(module),
                       pythonpath=str(payload.get("pythonpath", "") or ""))


def python_compiles(mission: Any, payload: dict[str, Any]) -> VerificationCheck:
    """Every Python file under a root compiles."""
    return PythonSyntaxCheck(str(payload.get("name") or "python_syntax"),
                             root=str(payload.get("root", "") or ""),
                             patterns=list(payload.get("patterns") or ["**/*.py"]))


def output_shape(mission: Any, payload: dict[str, Any]) -> VerificationCheck:
    """Structured step output matches the declared schema."""
    schema = payload.get("schema")
    if not isinstance(schema, dict):
        raise ConflictError("the 'output_schema' check needs a 'schema' object")
    return OutputSchemaCheck(str(payload.get("name") or "output_schema"), schema,
                             value=payload.get("value"))


def side_effect_happened(mission: Any, payload: dict[str, Any]) -> VerificationCheck:
    """Assert an action really happened - the anti-fabrication check for external effects."""
    return SideEffectCheck(str(payload.get("name") or "side_effect"),
                           expectation=str(payload.get("expectation", "the action happened")),
                           actual=payload.get("actual"))


def steps_completed(mission: Any, payload: dict[str, Any]) -> VerificationCheck:
    """Every step of the mission actually finished (registered by the engine as well)."""
    from .engine import _StepsCompletedCheck

    return _StepsCompletedCheck(mission)


#: name -> factory. A mission plan may only reference these names.
DEFAULT_CHECKS = {
    "artifact_exists": artifact_exists,
    "artifact_absent": artifact_absent,
    "command_succeeds": command_succeeds,
    "module_imports": module_imports,
    "python_compiles": python_compiles,
    "output_schema": output_shape,
    "side_effect": side_effect_happened,
    "steps_completed": steps_completed,
}


def register_default_checks(engine: Any) -> list[str]:
    """Register every standard check on a mission engine; returns the names."""
    for name, factory in DEFAULT_CHECKS.items():
        engine.register_check(name, factory)
    return sorted(DEFAULT_CHECKS)
