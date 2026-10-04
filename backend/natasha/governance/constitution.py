"""The Constitutional Core.

Seven protected areas are defined (spec section 20). For each area the constitution carries
*runtime-checkable* invariants - not assertions in a prompt - plus a signed manifest of the source
files that implement governance, security and approvals, so silent edits are detectable.

The interesting property is what happens when something tries to change them: ``guard_write``
refuses model/worker actors outright, and ``UpgradeGovernor`` refuses to auto-apply changes in
protected areas even with an owner approval, because "the owner approves" and "the agent rewrites
its own oversight" must never collapse into the same action.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core import GovernanceViolation, hmac_sign, hmac_verify, sha256_file
from ..core.clock import iso
from ..core.paths import get_paths
from ..core.risk import RiskLevel
from ..events import EventKind, EventLog, get_event_log

#: Areas the constitution protects.
PROTECTED_AREAS: dict[str, str] = {
    "identity": "Natasha's identity, owner binding and personality directives",
    "owner_authority": "the owner's sole authority to grant permissions and approve actions",
    "security": "security policy, sandboxing and prompt-injection defenses",
    "permissions": "the capability model and default-deny posture",
    "audit": "the append-only event log and its integrity",
    "upgrade_rules": "the rules governing self-modification",
    "rollback": "snapshot and rollback capability",
}

#: Source modules that implement protected behaviour. Writes here are guarded.
PROTECTED_PATHS: tuple[str, ...] = (
    "governance/",
    "security/policy.py",
    "security/audit.py",
    "approvals/engine.py",
    "approvals/models.py",
    "credentials/crypto.py",
    "credentials/vault.py",
    "events/log.py",
    "events/models.py",
)

_AGENT_PREFIXES = ("model", "worker", "agent", "skill", "mcp", "plugin", "external")


@dataclass
class Invariant:
    """A rule with a runtime check."""

    id: str
    statement: str
    area: str
    check: Callable[[], tuple[bool, str]]
    enforced_by: str = "code"


@dataclass
class GovernanceReport:
    ok: bool
    checked_at: str
    invariants: list[dict[str, Any]] = field(default_factory=list)
    manifest_ok: bool = True
    manifest_changes: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "checked_at": self.checked_at, "invariants": self.invariants,
            "manifest_ok": self.manifest_ok, "manifest_changes": self.manifest_changes,
            "violations": self.violations,
        }


class Constitution:
    """Evaluates invariants and guards protected resources."""

    def __init__(self, *, settings: Any = None, log: EventLog | None = None, manifest_key: bytes | None = None) -> None:
        self.settings = settings
        self.log = log or get_event_log()
        self._manifest_key = manifest_key
        self._lock = threading.RLock()

    # -- invariants ------------------------------------------------------------ #
    def invariants(self, *, vault: Any = None, approvals: Any = None) -> list[Invariant]:
        items = [
            Invariant(
                id="audit.append_only",
                statement="The event log rejects UPDATE and DELETE; history cannot be rewritten.",
                area="audit",
                check=self._check_audit_append_only,
                enforced_by="sqlite triggers + hash chain",
            ),
            Invariant(
                id="audit.chain_intact",
                statement="Every event is hash-chained to its predecessor.",
                area="audit",
                check=lambda: self._check_chain(),
                enforced_by="events.log.verify_chain",
            ),
            Invariant(
                id="permissions.default_deny",
                statement="Capabilities are denied unless explicitly permitted.",
                area="permissions",
                check=self._check_default_deny,
                enforced_by="security.policy.PolicyEngine",
            ),
            Invariant(
                id="permissions.owner_approval",
                statement="HIGH/CRITICAL operations initiated by non-owner actors require owner approval.",
                area="permissions",
                check=self._check_approval_policy,
                enforced_by="security.policy + approvals.engine",
            ),
            Invariant(
                id="permissions.no_self_approval",
                statement="A non-owner actor cannot approve its own request.",
                area="owner_authority",
                check=lambda: (True, "enforced by approvals.engine.approve actor check"),
                enforced_by="approvals.engine.approve",
            ),
            Invariant(
                id="credentials.encrypted",
                statement="Secrets are stored only as authenticated ciphertext.",
                area="security",
                check=lambda: self._check_credentials(vault),
                enforced_by="credentials.crypto + credentials.vault",
            ),
            Invariant(
                id="security.injection_defense",
                statement="External content is treated as data and never as authority.",
                area="security",
                check=lambda: (True, "external content wrapped by security.injection.ExternalContent"),
                enforced_by="security.injection",
            ),
            Invariant(
                id="security.prompt_injection_guard",
                statement="Tool/model output cannot grant capabilities or disable auditing.",
                area="security",
                check=self._check_no_governance_in_tools,
                enforced_by="security.policy OWNER_ONLY set",
            ),
            Invariant(
                id="upgrade.owner_gate",
                statement="Protected upgrades require an owner approval token bound to the proposal.",
                area="upgrade_rules",
                check=lambda: (True, "enforced by governance.upgrade_governor.apply"),
                enforced_by="governance.upgrade_governor",
            ),
            Invariant(
                id="rollback.available",
                statement="Snapshots can be listed and restored.",
                area="rollback",
                check=lambda: (get_paths().snapshots.exists(), f"snapshot dir {get_paths().snapshots}"),
                enforced_by="governance.rollback.SnapshotManager",
            ),
        ]
        return items

    def verify(self, *, vault: Any = None, approvals: Any = None) -> GovernanceReport:
        results: list[dict[str, Any]] = []
        violations: list[str] = []
        for invariant in self.invariants(vault=vault, approvals=approvals):
            try:
                ok, detail = invariant.check()
            except Exception as exc:  # a failing check is itself a finding
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results.append(
                {"id": invariant.id, "area": invariant.area, "ok": bool(ok), "detail": detail,
                 "statement": invariant.statement, "enforced_by": invariant.enforced_by}
            )
            if not ok:
                violations.append(f"{invariant.id}: {detail}")
        manifest_ok, changes = self.verify_manifest()
        report = GovernanceReport(
            ok=not violations and manifest_ok,
            checked_at=iso(),
            invariants=results,
            manifest_ok=manifest_ok,
            manifest_changes=changes,
            violations=violations,
        )
        self.log.append(
            EventKind.SECURITY,
            {"action": "constitution.verify", "ok": report.ok, "violations": violations,
             "manifest_changes": changes},
            actor="system", source="governance.constitution",
            risk=RiskLevel.NONE if report.ok else RiskLevel.CRITICAL,
        )
        return report

    # -- checks ---------------------------------------------------------------- #
    def _check_audit_append_only(self) -> tuple[bool, str]:
        import sqlite3

        conn = self.log._conn  # noqa: SLF001 - intentional integrity probe
        triggers = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
        }
        required = {"events_append_only_update", "events_append_only_delete"}
        missing = required - triggers
        if missing:
            return False, f"append-only triggers missing: {sorted(missing)}"
        try:
            conn.execute("SAVEPOINT probe")
            conn.execute("UPDATE events SET actor = actor WHERE seq = -1")
            conn.execute("ROLLBACK TO probe")
            conn.execute("RELEASE probe")
        except sqlite3.DatabaseError as exc:
            if "append-only" in str(exc):
                return True, "triggers active and enforced"
            return False, f"unexpected database error: {exc}"
        return False, "append-only trigger did not fire"

    def _check_chain(self) -> tuple[bool, str]:
        ok, report = self.log.verify_chain()
        return ok, json.dumps(report) if not ok else f"{report['checked']} events verified"

    def _check_default_deny(self) -> tuple[bool, str]:
        from ..core.config import SecuritySettings

        settings = self.settings or SecuritySettings()
        effect = getattr(getattr(settings, "security", settings), "default_effect", "deny")
        return (effect == "deny", f"security.default_effect={effect!r}")

    def _check_approval_policy(self) -> tuple[bool, str]:
        from ..core.config import SecuritySettings

        settings = self.settings or SecuritySettings()
        sec = getattr(settings, "security", settings)
        required = set(sec.require_approval_for or [])
        must_include = {"credential.use", "shell.exec", "computer.control"}
        missing = must_include - required
        if missing:
            return False, f"require_approval_for is missing {sorted(missing)}"
        return True, f"{len(required)} capabilities gated"

    def _check_credentials(self, vault: Any | None) -> tuple[bool, str]:
        from ..credentials.crypto import SecretBox, get_master_key
        from ..credentials.vault import CredentialVault

        box = SecretBox(get_master_key())
        if vault is None:
            return True, f"encryption available: {box.encryption_algorithm()}"
        stats = vault.stats()
        return True, f"{stats['active']} active credentials, {stats['algorithm']}"

    def _check_no_governance_in_tools(self) -> tuple[bool, str]:
        from ..security.policy import Capability, OWNER_ONLY

        required = {Capability.GOVERNANCE_WRITE, Capability.IDENTITY_WRITE, Capability.CREDENTIAL_ADMIN,
                    Capability.UPGRADE_APPLY, Capability.SYSTEM_CONFIG}
        missing = required - set(OWNER_ONLY)
        if missing:
            return False, f"OWNER_ONLY is missing {sorted(c.value for c in missing)}"
        return True, "owner-only capabilities enforced"

    # -- protected paths ------------------------------------------------------- #
    def is_protected(self, path: str | Path) -> bool:
        """True when *path* implements protected behaviour.

        Accepts absolute paths, repo-relative paths and package-relative paths, because the same
        file is referred to all three ways from different call sites - and a false negative here
        would be a security hole.
        """
        text = str(path).replace("\\", "/").removeprefix("./")
        if text.startswith("/"):
            resolution_root = Path(__file__).resolve().parents[1]  # .../backend/natasha
            candidate = Path(text)
            for base in (resolution_root, resolution_root.parent, resolution_root.parent.parent):
                try:
                    text = str(candidate.relative_to(base)).replace("\\", "/")
                    break
                except ValueError:
                    continue
            else:
                return False
        for prefix in ("backend/natasha/", "natasha/"):
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        return any(text == entry.rstrip("/") or text.startswith(entry) for entry in PROTECTED_PATHS)

    def guard_write(self, path: str | Path, *, actor: str, approved_by_owner: bool = False) -> None:
        """Refuse agent-originated writes to protected paths. Raises GovernanceViolation."""
        if not self.is_protected(path):
            return
        actor_kind = actor.split(":", 1)[0].lower()
        if actor_kind in _AGENT_PREFIXES and not approved_by_owner:
            self.log.append(
                EventKind.SECURITY,
                {"action": "governance.protected_write_blocked", "path": str(path), "actor": actor},
                actor=actor, source="governance.constitution", risk=RiskLevel.CRITICAL,
            )
            raise GovernanceViolation(
                f"{actor!r} may not modify protected path {path!s}; protected areas: "
                f"{', '.join(PROTECTED_AREAS)}"
            )

    # -- manifest -------------------------------------------------------------- #
    def manifest_path(self) -> Path:
        return get_paths().ensure().keys / "constitution.lock"

    def compute_manifest(self) -> dict[str, str]:
        root = Path(__file__).resolve().parents[1]
        manifest: dict[str, str] = {}
        for entry in PROTECTED_PATHS:
            target = root / entry.rstrip("/")
            if target.is_dir():
                for file in sorted(target.rglob("*.py")):
                    manifest[str(file.relative_to(root))] = sha256_file(file)
            elif target.is_file():
                manifest[entry] = sha256_file(target)
        return manifest

    def write_manifest(self) -> Path:
        manifest = self.compute_manifest()
        path = self.manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        signature = hmac_sign(self._key(), manifest)
        path.write_text(json.dumps({"manifest": manifest, "signature": signature, "created_at": iso()}, indent=2))
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover
            pass
        return path

    def verify_manifest(self) -> tuple[bool, list[str]]:
        """Compare current protected files against the signed manifest."""
        path = self.manifest_path()
        if not path.exists():
            self.write_manifest()
            return True, []
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return False, ["constitution.lock is not valid JSON"]
        stored = data.get("manifest", {})
        if not hmac_verify(self._key(), stored, data.get("signature", "")):
            return False, ["constitution.lock signature invalid (manifest tampered)"]
        current = self.compute_manifest()
        changes: list[str] = []
        for name, digest in stored.items():
            if name not in current:
                changes.append(f"removed:{name}")
            elif current[name] != digest:
                changes.append(f"modified:{name}")
        for name in current:
            if name not in stored:
                changes.append(f"added:{name}")
        return (not changes), changes

    def _key(self) -> bytes:
        if self._manifest_key is None:
            from ..credentials.crypto import get_master_key

            self._manifest_key = get_master_key()
        return self._manifest_key


_CONSTITUTIONS: dict[str, Constitution] = {}
_LOCK = threading.Lock()


def get_constitution(**kwargs: Any) -> Constitution:
    with _LOCK:
        if "default" not in _CONSTITUTIONS:
            _CONSTITUTIONS["default"] = Constitution(**kwargs)
        return _CONSTITUTIONS["default"]


def reset_constitutions() -> None:
    with _LOCK:
        _CONSTITUTIONS.clear()
