"""Skill lifecycle: validate -> scan -> test -> approve -> install -> activate -> ... -> uninstall."""

from __future__ import annotations

import re
import shutil
import threading
from pathlib import Path
from typing import Any

from ..core import SkillError
from ..core.clock import iso
from ..core.paths import get_paths
from ..core.risk import RiskLevel
from ..events import EventKind, EventLog, get_event_log
from .manifest import SkillManifest, compute_skill_checksum, load_manifest, validate_manifest, verify_checksum
from .models import SkillRecord, SkillState
from .store import SkillStore

#: Which capability an undeclared dangerous pattern implies the skill actually needs.
PATTERN_CAPABILITIES: dict[str, str] = {
    "subprocess_shell": "shell.exec",
    "raw_socket": "net.http",
    "env_exfil": "net.http",
    "credential_paths": "credential.use",
    "eval_exec": "code.exec",
    "obfuscated_payload": "code.exec",
}


class SkillLifecycle:
    """Registry and state machine for skills."""

    def __init__(self, *, log: EventLog | None = None, db_path: str | Path | None = None,
                 store: SkillStore | None = None) -> None:
        self.store = store or SkillStore(db_path)
        self._lock = threading.RLock()
        self.log = log or get_event_log()
        self._observers: list[Any] = []

    # -- observers ------------------------------------------------------------- #
    def add_observer(self, callback: Any) -> None:
        """Register a callback invoked as ``callback(skill_id, version, event)`` on state changes.

        The skill runtime uses this to withdraw a tool the moment its skill is disabled or removed -
        an uninstalled skill must not stay callable.
        """
        with self._lock:
            self._observers.append(callback)

    def _notify(self, skill_id: str, version: str, event: str) -> None:
        for callback in list(self._observers):
            try:
                callback(skill_id, version, event)
            except Exception:
                pass

    # -- storage --------------------------------------------------------------- #
    def _save(self, record: SkillRecord) -> SkillRecord:
        """Persist a record. The store stamps ``updated_at``."""
        with self._lock:
            return self.store.save(record)

    def get(self, skill_id: str, *, version: str = "") -> SkillRecord:
        with self._lock:
            return self.store.get(skill_id, version=version)

    def list(self, *, state: str = "") -> list[SkillRecord]:
        with self._lock:
            return self.store.list(state=state)

    def close(self) -> None:
        """Close the underlying store (used by the runtime shutdown path and by tests)."""
        self.store.close()

    # -- lifecycle steps ------------------------------------------------------- #
    def validate(self, package_dir: str | Path) -> SkillManifest:
        """Parse and validate the manifest (state VALIDATED)."""
        manifest = load_manifest(package_dir)
        record = SkillRecord(
            id=manifest.id, version=manifest.version, name=manifest.name, state=SkillState.VALIDATED.value,
            path=str(Path(package_dir)), manifest=manifest.to_dict(),
            checksum=manifest.checksum or compute_skill_checksum(Path(package_dir), manifest),
        )
        self._save(record)
        self._event("validated", manifest, details={"permissions": manifest.permissions})
        return manifest

    def scan(self, skill_id: str, *, version: str = "") -> dict[str, Any]:
        """Static scan of the package for dangerous patterns (state SCANNED)."""
        record = self.get(skill_id, version=version)
        directory = Path(record.path)
        # One scanner for the whole system: the same code decides for a local skill and for a
        # marketplace package, so the two can never disagree.
        from ..marketplace.package import static_scan

        _clean, findings = static_scan(directory)
        manifest = SkillManifest(**record.manifest)
        declared = set(manifest.permissions)
        needs = {PATTERN_CAPABILITIES[item["pattern"]] for item in findings
                 if item["pattern"] in PATTERN_CAPABILITIES}
        missing = needs - declared
        record.scan = {
            "findings": findings,
            "undeclared_capabilities": sorted(missing),
            "clean": not findings,
            "scanned_at": iso(),
        }
        record.state = SkillState.SCANNED.value if not findings else SkillState.FAILED.value
        self._save(record)
        self._event("scanned", manifest, risk=RiskLevel.HIGH if findings else RiskLevel.LOW,
                    details={"findings": len(findings), "undeclared": sorted(missing)})
        return record.scan

    def test(self, skill_id: str, *, version: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run the skill once in the sandbox with a probe payload (state TESTED).

        Works from synchronous callers *and* from inside a running event loop.
        """
        from ..core import run_coroutine_sync

        return run_coroutine_sync(self.test_async(skill_id, version=version, payload=payload))

    async def test_async(self, skill_id: str, *, version: str = "",
                         payload: dict[str, Any] | None = None) -> dict[str, Any]:
        from .isolation import SkillSandbox, run_isolated

        record = self.get(skill_id, version=version)
        manifest = SkillManifest(**record.manifest)
        sandbox = SkillSandbox(
            directory=Path(record.path), entrypoint=manifest.entrypoint, runtime=manifest.runtime,
            timeout_seconds=min(manifest.timeout_seconds, 60.0), permissions=manifest.permissions,
        )
        try:
            result = await run_isolated(sandbox, payload or {})
            outcome = {"ok": True, "result": result, "tested_at": iso()}
            record.state = SkillState.TESTED.value
        except Exception as exc:
            outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "tested_at": iso()}
            record.state = SkillState.FAILED.value
        record.test = outcome
        self._save(record)
        self._event("tested", manifest, risk=RiskLevel.MEDIUM, details={"ok": outcome["ok"]})
        return outcome

    def approve(self, skill_id: str, *, version: str = "", approved_by: str = "owner") -> SkillRecord:
        if approved_by.split(":", 1)[0].lower() not in {"owner", "user", "human", "local-owner"}:
            raise SkillError(f"{approved_by!r} may not approve skills")
        record = self.get(skill_id, version=version)
        if record.state not in {SkillState.SCANNED.value, SkillState.TESTED.value}:
            raise SkillError(f"skill {skill_id!r} must pass scan/test before approval (state={record.state})")
        record.state = SkillState.APPROVED.value
        self._save(record)
        self._event("approved", SkillManifest(**record.manifest), details={"approved_by": approved_by})
        return record

    #: Capabilities a skill may never hold, whatever its manifest asks for. A skill that wants one
    #: of these is not "risky", it is out of bounds - the code that enforces governance cannot be
    #: supplied by a downloaded package.
    FORBIDDEN_PERMISSIONS: tuple[str, ...] = (
        "governance.read", "governance.write", "identity.write", "credential.admin",
        "upgrade.apply", "system.config", "mcp.install", "skill.install",
    )
    #: Permissions that require an explicit, per-run owner approval rather than a silent grant.
    ALWAYS_APPROVAL: tuple[str, ...] = (
        "shell.exec", "code.exec", "credential.use", "browser.control", "fs.write",
        "input.control", "app.control",
    )

    def install(self, package_dir: str | Path, *, activate: bool = True, approved_by: str = "owner") -> SkillRecord:
        """Copy an approved package into the skills directory and optionally activate it.

        Installation is a privilege boundary: a skill is third-party code, so the manifest is
        validated, forbidden capabilities are refused, the source is statically scanned, and only the
        owner may approve the install.
        """
        if approved_by.split(":", 1)[0].lower() not in {"owner", "user", "human", "local-owner"}:
            raise SkillError(
                f"{approved_by!r} may not approve a skill install; only the owner can"
            )
        manifest = load_manifest(package_dir)
        source = Path(package_dir)

        forbidden = sorted(set(manifest.permissions) & set(self.FORBIDDEN_PERMISSIONS))
        if forbidden:
            self._event("install_refused", manifest, risk=RiskLevel.CRITICAL,
                        details={"reason": "forbidden permissions", "permissions": forbidden})
            raise SkillError(
                f"skill {manifest.id!r} requests permissions that are never granted to skills: {forbidden}"
            )

        findings = self._static_scan(source)
        blocking = list(findings)
        if blocking:
            self._event("install_refused", manifest, risk=RiskLevel.HIGH,
                        details={"reason": "static scan", "findings": blocking[:5]})
            raise SkillError(
                f"skill {manifest.id!r} failed the static scan: "
                + "; ".join(f"{item['pattern']} in {item['file']}" for item in blocking[:3])
            )
        checksum = compute_skill_checksum(source, manifest)
        if manifest.checksum and manifest.checksum != checksum:
            raise SkillError(
                f"checksum mismatch for {manifest.key}: manifest={manifest.checksum[:12]}… package={checksum[:12]}…"
            )
        destination = get_paths().ensure().skills / manifest.id / manifest.version
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        record = SkillRecord(
            id=manifest.id, version=manifest.version, name=manifest.name,
            state=SkillState.INSTALLED.value, path=str(destination), manifest=manifest.to_dict(),
            checksum=checksum, installed_at=iso(),
        )
        self._save(record)
        self._event("installed", manifest, details={"checksum": checksum[:12], "path": str(destination)})
        if activate:
            return self.activate(manifest.id, version=manifest.version)
        return record

    def activate(self, skill_id: str, *, version: str = "") -> SkillRecord:
        record = self.get(skill_id, version=version)
        ok, actual = verify_checksum(Path(record.path), record.checksum)
        if not ok:
            record.state = SkillState.FAILED.value
            self._save(record)
            self._event("activation_blocked", SkillManifest(**record.manifest), risk=RiskLevel.HIGH,
                        details={"reason": "checksum changed since install", "actual": actual[:12]})
            raise SkillError(f"skill {skill_id!r} was modified after install; reinstall before activating")
        record.state = SkillState.ACTIVE.value
        self._save(record)
        self._event("activated", SkillManifest(**record.manifest))
        return record

    def rollback(self, skill_id: str, *, to_version: str) -> SkillRecord:
        target = self.get(skill_id, version=to_version)
        if target.state in {SkillState.UNINSTALLED.value}:
            raise SkillError(f"version {to_version} of {skill_id!r} is uninstalled")
        target.state = SkillState.ACTIVE.value
        self._save(target)
        for record in self.list():
            if record.id == skill_id and record.version != to_version and record.state == SkillState.ACTIVE.value:
                record.state = SkillState.DISABLED.value
                self._save(record)
        self._notify(skill_id, to_version, "rollback")
        self._event("rolled_back", SkillManifest(**target.manifest), risk=RiskLevel.MEDIUM,
                    details={"to_version": to_version})
        return target

    def disable(self, skill_id: str, *, version: str = "") -> SkillRecord:
        record = self.get(skill_id, version=version)
        record.state = SkillState.DISABLED.value
        self._save(record)
        self._notify(skill_id, record.version, "disabled")
        self._event("disabled", SkillManifest(**record.manifest))
        return record

    def uninstall(self, skill_id: str, *, version: str = "", purge: bool = False) -> SkillRecord:
        record = self.get(skill_id, version=version)
        record.state = SkillState.UNINSTALLED.value
        self._notify(skill_id, record.version, "uninstalled")
        self._save(record)
        if purge:
            shutil.rmtree(record.path, ignore_errors=True)
        self._event("uninstalled", SkillManifest(**record.manifest), risk=RiskLevel.MEDIUM)
        return record

    def active_skills(self) -> list[SkillRecord]:
        return self.list(state=SkillState.ACTIVE.value)

    def _static_scan(self, package_dir: Path) -> list[dict[str, str]]:
        """Static scan shared with the marketplace installer (one scanner, one verdict)."""
        try:
            from ..marketplace.package import static_scan

            ok, findings = static_scan(package_dir)
            if ok:
                return []
            return list(findings)
        except Exception:
            # The scanner must never be the reason a skill slips through: if it cannot run, the
            # install is refused rather than silently approved.
            raise SkillError(
                "skill static scan is unavailable; refusing to install unvetted third-party code"
            )

    def _event(self, action: str, manifest: SkillManifest, *, risk: RiskLevel = RiskLevel.LOW,
               details: dict[str, Any] | None = None) -> None:
        self.log.append(
            EventKind.SKILL,
            {"action": action, "skill": manifest.id, "version": manifest.version, "name": manifest.name,
             "permissions": manifest.permissions, **(details or {})},
            actor="owner", source="skills.lifecycle", risk=risk,
        )


_LIFECYCLE: SkillLifecycle | None = None
_LOCK = threading.Lock()


def get_skill_lifecycle(**kwargs: Any) -> SkillLifecycle:
    global _LIFECYCLE
    with _LOCK:
        if _LIFECYCLE is None:
            _LIFECYCLE = SkillLifecycle(**kwargs)
        return _LIFECYCLE


def reset_skill_lifecycle() -> None:
    global _LIFECYCLE
    with _LOCK:
        _LIFECYCLE = None
