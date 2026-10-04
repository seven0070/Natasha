"""The vocabulary of proof: individual check types."""

from __future__ import annotations

import abc
import asyncio
import enum
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class CheckStatus(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class CheckResult:
    """One check's outcome, with the evidence that justifies it."""

    name: str
    status: CheckStatus = CheckStatus.PASSED
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    required: bool = True

    @property
    def ok(self) -> bool:
        return self.status is CheckStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status.value, "ok": self.ok, "detail": self.detail,
                "evidence": self.evidence, "duration_ms": round(self.duration_ms, 2),
                "required": self.required}


class VerificationCheck(abc.ABC):
    """A single, named way to prove something."""

    name: str = "check"
    required: bool = True

    async def run(self, context: dict[str, Any]) -> CheckResult:
        import time

        started = time.perf_counter()
        try:
            result = await self.check(context)
        except Exception as exc:
            result = CheckResult(self.name, CheckStatus.ERROR, f"{type(exc).__name__}: {exc}",
                                 required=self.required)
        result.duration_ms = (time.perf_counter() - started) * 1000
        result.required = self.required
        return result

    @abc.abstractmethod
    async def check(self, context: dict[str, Any]) -> CheckResult:  # pragma: no cover - interface
        raise NotImplementedError


class CommandCheck(VerificationCheck):
    """Run a command (tests, linters, builds) and require a zero exit code."""

    def __init__(self, name: str, command: list[str] | str, *, cwd: str = "", timeout: float = 300.0,
                 env: dict[str, str] | None = None, shell: bool = False, required: bool = True) -> None:
        self.name = name
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.env = env or {}
        self.shell = shell
        self.required = required

    async def check(self, context: dict[str, Any]) -> CheckResult:
        cwd = self.cwd or context.get("cwd") or str(Path.cwd())
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
               "PYTHONPATH": str(Path(cwd) / "backend"), **self.env}
        if self.shell:
            process = await asyncio.create_subprocess_shell(
                self.command if isinstance(self.command, str) else " ".join(self.command),
                cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        else:
            argv = self.command if isinstance(self.command, list) else [self.command]
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            process.kill()
            return CheckResult(self.name, CheckStatus.FAILED, f"timed out after {self.timeout}s")
        out, err = stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
        if process.returncode == 0:
            return CheckResult(self.name, CheckStatus.PASSED, "command succeeded",
                              {"returncode": 0, "stdout_tail": out[-1500:]})
        return CheckResult(self.name, CheckStatus.FAILED, f"exit code {process.returncode}",
                          {"returncode": process.returncode, "stderr_tail": err[-2000:],
                           "stdout_tail": out[-800:]})


class PythonSyntaxCheck(VerificationCheck):
    """Every Python file under a root must at least compile."""

    def __init__(self, name: str = "python_syntax", *, root: str = "", patterns: list[str] | None = None,
                 required: bool = True) -> None:
        self.name = name
        self.root = root
        self.patterns = patterns or ["**/*.py"]
        self.required = required

    async def check(self, context: dict[str, Any]) -> CheckResult:
        import py_compile

        base = Path(self.root or context.get("cwd") or Path.cwd())
        failures: list[str] = []
        checked = 0
        for pattern in self.patterns:
            for path in base.glob(pattern):
                if "__pycache__" in path.parts or not path.is_file():
                    continue
                checked += 1
                try:
                    py_compile.compile(str(path), doraise=True)
                except py_compile.PyCompileError as exc:
                    failures.append(f"{path}: {str(exc).splitlines()[-1][:160]}")
        if failures:
            names = ", ".join(item.split(":", 1)[0] for item in failures[:3])
            detail = f"{len(failures)} file(s) failed to compile: {names}"
            return CheckResult(self.name, CheckStatus.FAILED, detail[:300],
                               {"failures": failures[:10], "checked": checked})
        return CheckResult(self.name, CheckStatus.PASSED, f"{checked} file(s) compiled", {"checked": checked})


class ImportCheck(VerificationCheck):
    """A module must import (proves the package wiring works, not just the syntax)."""

    def __init__(self, name: str, module: str, *, pythonpath: str = "", required: bool = True) -> None:
        self.name = name
        self.module = module
        self.pythonpath = pythonpath
        self.required = required

    async def check(self, context: dict[str, Any]) -> CheckResult:
        import asyncio
        import os
        import sys

        pythonpath = self.pythonpath or context.get("pythonpath", str(Path.cwd() / "backend"))
        snippet = (f"import sys; sys.path.insert(0, {pythonpath!r}); "
                   f"import {self.module}; print(getattr({self.module!r}, '__name__', 'ok'))")
        env = {**os.environ, "PYTHONPATH": pythonpath, "PYTHONDONTWRITEBYTECODE": "1"}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", snippet, cwd=str(Path(pythonpath).parent),
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            process.kill()
            return CheckResult(self.name, CheckStatus.FAILED, "import timed out after 60s")
        out = stdout.decode("utf-8", "replace")
        err = stderr.decode("utf-8", "replace")
        if process.returncode == 0:
            return CheckResult(self.name, CheckStatus.PASSED, f"imported {self.module}",
                               {"module": self.module, "stdout_tail": out[-500:]})
        return CheckResult(self.name, CheckStatus.FAILED, f"{self.module} is not importable",
                           {"module": self.module, "returncode": process.returncode,
                            "stderr_tail": err[-1200:]})


class FileExistsCheck(VerificationCheck):
    """An artifact must exist (and optionally be non-trivial and match a hash)."""

    def __init__(self, name: str, path: str, *, min_bytes: int = 1, sha256: str = "",
                 required: bool = True) -> None:
        self.name = name
        self.path = path
        self.min_bytes = min_bytes
        self.sha256 = sha256
        self.required = required

    async def check(self, context: dict[str, Any]) -> CheckResult:
        from ..core import sha256_file

        target = Path(self.path) if Path(self.path).is_absolute() else Path(
            context.get("cwd", ".")) / self.path
        if not target.exists():
            return CheckResult(self.name, CheckStatus.FAILED, f"missing: {target}")
        if target.is_file() and target.stat().st_size < self.min_bytes:
            return CheckResult(self.name, CheckStatus.FAILED,
                               f"only {target.stat().st_size} bytes", {"path": str(target)})
        evidence = {"path": str(target)}
        if target.is_file() and self.sha256:
            actual = sha256_file(target)
            evidence["sha256"] = actual
            if actual != self.sha256:
                return CheckResult(self.name, CheckStatus.FAILED, "checksum mismatch", evidence)
        return CheckResult(self.name, CheckStatus.PASSED, "artifact present", evidence)


class OutputSchemaCheck(VerificationCheck):
    """Structured output must satisfy a declared schema."""

    def __init__(self, name: str, schema: Any, *, value: Any = None, required: bool = True) -> None:
        self.name = name
        self.schema = schema
        self.value = value
        self.required = required

    async def check(self, context: dict[str, Any]) -> CheckResult:
        from ..security.validation import Schema, ValidationError, validate_arguments

        try:
            schema = Schema.from_dict(self.schema)
        except ValidationError as exc:
            return CheckResult(self.name, CheckStatus.ERROR, f"invalid schema: {exc}")
        payload = self.value if self.value is not None else context.get("output", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                return CheckResult(self.name, CheckStatus.FAILED, f"output is not JSON: {exc}")
        try:
            validate_arguments(schema, payload if isinstance(payload, dict) else {"value": payload})
        except ValidationError as exc:
            return CheckResult(self.name, CheckStatus.FAILED, f"schema violation: {exc}",
                              {"issues": exc.details.get("issues", [])})
        return CheckResult(self.name, CheckStatus.PASSED, "output matches the declared schema")


class SideEffectCheck(VerificationCheck):
    """Assert that an action actually happened - the anti-fabrication check."""

    def __init__(self, name: str, *, expectation: str, actual: Any = None,
                 predicate: Any = None, required: bool = True) -> None:
        self.name = name
        self.expectation = expectation
        self.actual = actual
        self.predicate = predicate
        self.required = required

    async def check(self, context: dict[str, Any]) -> CheckResult:
        actual = self.actual if self.actual is not None else context.get("actual")
        if self.predicate is not None:
            try:
                ok = bool(self.predicate(actual))
            except Exception as exc:
                return CheckResult(self.name, CheckStatus.ERROR, f"predicate raised: {exc}")
            return CheckResult(self.name, CheckStatus.PASSED if ok else CheckStatus.FAILED,
                              f"expected {self.expectation}; got {actual!r}", {"actual": repr(actual)[:400]})
        ok = actual == self.expectation
        return CheckResult(self.name, CheckStatus.PASSED if ok else CheckStatus.FAILED,
                          f"expected {self.expectation!r}; got {actual!r}", {"actual": repr(actual)[:400]})


class CompositeCheck(VerificationCheck):
    """All sub-checks must pass (used for multi-part acceptance criteria)."""

    def __init__(self, name: str, checks: list[VerificationCheck], *, require_all: bool = True,
                 required: bool = True) -> None:
        self.name = name
        self.checks = checks
        self.require_all = require_all
        self.required = required

    async def check(self, context: dict[str, Any]) -> CheckResult:
        results = [await check.run(context) for check in self.checks]
        oks = [result.ok for result in results]
        passed = all(oks) if self.require_all else any(oks)
        return CheckResult(
            self.name, CheckStatus.PASSED if passed else CheckStatus.FAILED,
            f"{sum(oks)}/{len(oks)} sub-checks passed",
            {"sub": [result.to_dict() for result in results]},
        )

class BehaviorCheck(VerificationCheck):
    """A caller-supplied predicate - used when acceptance is domain-specific.

    The predicate receives the check context and returns ``(ok, detail)``. Because a predicate is
    code written by Natasha's developers (or the tests), it is a legitimate proof - unlike asking the
    model whether it did a good job.
    """

    def __init__(self, name: str, predicate: Any, *, required: bool = True, description: str = "") -> None:
        self.name = name
        self.predicate = predicate
        self.required = required
        self.description = description

    async def check(self, context: dict[str, Any]) -> CheckResult:
        try:
            outcome = self.predicate(context)
            if asyncio.iscoroutine(outcome):
                outcome = await outcome
        except Exception as exc:
            return CheckResult(self.name, CheckStatus.ERROR, f"predicate raised: {type(exc).__name__}: {exc}")
        if isinstance(outcome, tuple):
            ok, detail = bool(outcome[0]), str(outcome[1] if len(outcome) > 1 else "")
        else:
            ok, detail = bool(outcome), self.description
        return CheckResult(self.name, CheckStatus.PASSED if ok else CheckStatus.FAILED,
                           detail or ("predicate satisfied" if ok else "predicate not satisfied"))
