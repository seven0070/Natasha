"""Marketplace installer: permission review, owner approval, version pinning and rollback."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core import ApprovalRequired, VerificationFailed
from ..core.clock import iso
from ..core.risk import RiskLevel
from ..events import EventKind, EventLog, get_event_log
from ..security.policy import Capability, PolicyEngine, PolicyRequest
from .package import MarketplacePackage, SecurityReport, inspect_package
from .registry import InstalledItem, MarketplaceRegistry, get_marketplace_registry


@dataclass
class InstallPlan:
    """What the owner is being asked to approve."""

    package: MarketplacePackage
    report: SecurityReport
    requires_approval: bool
    permission_review: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package.to_dict(),
            "report": self.report.to_dict(),
            "requires_approval": self.requires_approval,
            "permission_review": self.permission_review,
        }


class MarketplaceInstaller:
    """Installs packages after the security pipeline, with owner approval where required."""

    def __init__(
        self,
        *,
        registry: MarketplaceRegistry | None = None,
        policy: PolicyEngine | None = None,
        log: EventLog | None = None,
        lifecycle: Any = None,
        tools: Any = None,
        approvals: Any = None,
        trusted_publishers: list[str] | None = None,
        publisher_secret: bytes | None = None,
    ) -> None:
        self.registry = registry or get_marketplace_registry()
        self.policy = policy or PolicyEngine()
        self.log = log or get_event_log()
        self.lifecycle = lifecycle
        self.tools = tools
        self.approvals = approvals
        self.trusted_publishers = list(trusted_publishers or [])
        self.publisher_secret = publisher_secret

    # -- inspection ------------------------------------------------------------ #
    def review(self, source: str, *, kind: str = "skill", expected_checksum: str = "") -> InstallPlan:
        """Run the pipeline and produce the review the owner sees before installing."""
        package, report, _ = inspect_package(
            source, kind=kind, expected_checksum=expected_checksum,
            trusted_publishers=self.trusted_publishers, publisher_secret=self.publisher_secret,
        )
        permission_review = [
            {
                "permission": permission,
                "impact": _permission_impact(permission),
                "risk": "HIGH" if permission in {"shell.exec", "code.exec", "credential.use", "browser.control"} else "MEDIUM",
            }
            for permission in package.permissions
        ]
        requires_approval = report.requires_owner_approval or bool(permission_review)
        plan = InstallPlan(package=package, report=report, requires_approval=requires_approval,
                           permission_review=permission_review)
        self._event("reviewed", package, risk=RiskLevel.MEDIUM,
                    details={"passed": report.passed, "findings": len(report.findings)})
        return plan

    # -- installation ---------------------------------------------------------- #
    def install(
        self,
        source: str,
        *,
        kind: str = "skill",
        expected_checksum: str = "",
        actor: str = "owner",
        approval_id: str = "",
        approve_permissions: bool = False,
        pin: bool = True,
        activate: bool = True,
    ) -> dict[str, Any]:
        """Install a package. Refuses without owner approval when the plan requires it."""
        plan = self.review(source, kind=kind, expected_checksum=expected_checksum)
        package = plan.package

        installed_check = self.registry.get(kind, package.name, version=package.version) if self._installed(kind, package.name, package.version) else None
        if installed_check is not None and installed_check.checksum != package.checksum:
            raise VerificationFailed(
                f"{package.key} is already installed with a different checksum; "
                "uninstall it first or install a different version"
            )

        if not plan.report.passed:
            self._event("install_blocked", package, risk=RiskLevel.HIGH,
                        details={"failed_steps": plan.report.failed_steps})
            raise VerificationFailed(
                f"package {package.key} failed the security pipeline: {', '.join(plan.report.failed_steps)}"
            )

        owner_actor = actor.split(":", 1)[0].lower() in {"owner", "user", "human", "local-owner"}
        if plan.requires_approval and not owner_actor:
            if not approval_id:
                request = None
                if self.approvals is not None:
                    request = self.approvals.request(
                        "marketplace.install",
                        reason=f"install {package.key} with permissions {package.permissions}",
                        risk=RiskLevel.HIGH, actor=actor, permissions=package.permissions,
                        resources=[package.name], arguments={"source": source, "checksum": package.checksum},
                        mission_id="", trace_id="",
                    )
                self._event("install_blocked", package, risk=RiskLevel.HIGH,
                            details={"reason": "owner approval required", "request_id": getattr(request, "id", "")})
                raise ApprovalRequired(
                    f"installing {package.key} requires owner approval "
                    f"(permissions: {', '.join(package.permissions) or 'none'})",
                    request_id=getattr(request, "id", ""),
                )
            self.approvals.consume(approval_id, "marketplace.install",
                                   {"source": source, "checksum": package.checksum}, actor=actor)
        elif plan.requires_approval and not approve_permissions:
            # Even the owner must acknowledge the permission list explicitly in the API/CLI.
            raise ApprovalRequired(
                f"{package.key} requests {package.permissions}; set approve_permissions=True to confirm",
            )

        # Installing third-party code is itself an owner-authority action: an autonomous actor can
        # only proceed with a consumed owner approval, never on its own initiative.
        if not owner_actor and not approval_id:
            request = None
            if self.approvals is not None:
                request = self.approvals.request(
                    "marketplace.install",
                    reason=f"install {package.key}",
                    risk=RiskLevel.HIGH, actor=actor, permissions=package.permissions,
                    resources=[package.name],
                    arguments={"source": source, "checksum": package.checksum},
                )
            self._event("install_blocked", package, risk=RiskLevel.HIGH,
                        details={"reason": "installation is owner-authority",
                                 "request_id": getattr(request, "id", "")})
            raise ApprovalRequired(
                f"{actor!r} may not install packages; this requires the owner",
                request_id=getattr(request, "id", ""),
            )

        # Permission gate: market installs are themselves a capability.
        decision = self.policy.check(
            PolicyRequest(Capability.SKILL_INSTALL if kind == "skill" else Capability.MCP_INSTALL,
                          package.name, actor=actor, context={"kind": kind, "permissions": package.permissions})
        )
        if decision.effect.value == "deny":
            raise VerificationFailed(f"policy denied installation: {decision.reason}")

        destination = None
        if kind == "skill" and self.lifecycle is not None:
            record = self.lifecycle.install(package.path, activate=False, approved_by=actor)
            destination = record.path
            if activate:
                self.lifecycle.activate(package.name, version=package.version)
            result_kind = "skill"
        else:
            destination = str(package.path)
            result_kind = kind

        item = InstalledItem(
            kind=kind, name=package.name, version=package.version, checksum=package.checksum,
            publisher=package.publisher, permissions=package.permissions, source=package.origin,
            path=str(destination), pinned=pin, active=True, security_report=plan.report.to_dict(),
            metadata={"key": package.key, "installed_by": actor},
        )
        self.registry.record(item)
        self._event("installed", package, risk=RiskLevel.MEDIUM,
                    details={"checksum": package.checksum[:12], "permissions": package.permissions,
                             "publisher": package.publisher})
        return {"installed": item.to_dict(), "report": plan.report.to_dict(), "kind": result_kind}

    def _installed(self, kind: str, name: str, version: str) -> bool:
        try:
            self.registry.get(kind, name, version=version)
            return True
        except Exception:
            return False

    def rollback(self, kind: str, name: str, *, to_version: str, actor: str = "owner") -> dict[str, Any]:
        """Re-activate a previously installed version and deactivate the current one."""
        target = self.registry.get(kind, name, version=to_version)
        current = None
        try:
            current = self.registry.get(kind, name)
        except Exception:
            pass
        if current and current.version != to_version:
            self.registry.deactivate(kind, name, version=current.version)
        target.active = True
        self.registry.record(target)
        if kind == "skill" and self.lifecycle is not None:
            self.lifecycle.rollback(name, to_version=to_version)
        self.log.append(
            EventKind.MARKETPLACE,
            {"action": "rolled_back", "kind": kind, "name": name, "from_version": current.version if current else "",
             "to_version": to_version},
            actor=actor, source="marketplace.installer", risk=RiskLevel.MEDIUM,
        )
        return {"rolled_back": target.to_dict(), "from_version": current.version if current else ""}

    def uninstall(self, kind: str, name: str, *, version: str = "", actor: str = "owner", purge: bool = False) -> dict[str, Any]:
        try:
            item = self.registry.get(kind, name, version=version)
        except Exception:
            raise
        if item.pinned:
            raise VerificationFailed(f"{item.key} is pinned; unpin it before uninstalling")
        self.registry.deactivate(kind, name, version=item.version)
        if kind == "skill" and self.lifecycle is not None:
            self.lifecycle.uninstall(name, version=item.version, purge=purge)
        self.log.append(
            EventKind.MARKETPLACE, {"action": "uninstalled", "kind": kind, "name": name, "version": item.version},
            actor=actor, source="marketplace.installer", risk=RiskLevel.MEDIUM,
        )
        return {"uninstalled": item.to_dict()}

    def _event(self, action: str, package: MarketplacePackage, *, risk: RiskLevel,
               details: dict[str, Any]) -> None:
        self.log.append(
            EventKind.MARKETPLACE,
            {"action": action, "package": package.key, "kind": package.kind, "publisher": package.publisher,
             **details},
            actor="owner", source="marketplace.installer", risk=risk,
        )


def _permission_impact(permission: str) -> str:
    return {
        "fs.read": "read files within permitted roots",
        "fs.write": "create or modify files (reversible via backups)",
        "net.http": "make outbound network requests",
        "memory.read": "read Natasha's memory",
        "memory.write": "store memories",
        "artifact.write": "write artifacts",
        "model.call": "call configured model providers",
        "shell.exec": "run commands on this machine (high impact)",
        "code.exec": "execute code on this machine (high impact)",
        "credential.use": "request credentials through the broker (scoped, audited)",
        "browser.control": "drive a browser",
    }.get(permission, "unknown impact")


_INSTALLER: MarketplaceInstaller | None = None
_LOCK = threading.Lock()


def get_marketplace_installer(**kwargs: Any) -> MarketplaceInstaller:
    global _INSTALLER
    with _LOCK:
        if _INSTALLER is None:
            _INSTALLER = MarketplaceInstaller(**kwargs)
        return _INSTALLER


def reset_marketplace_installer() -> None:
    global _INSTALLER
    with _LOCK:
        _INSTALLER = None
