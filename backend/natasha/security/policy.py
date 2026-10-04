"""Capability policy engine - the enforcement point for least privilege.

Design rules
------------
* **Default deny.** Anything not explicitly permitted is refused.
* **Deny beats allow.** Explicit denies win over broad allows.
* **Filesystem decisions resolve symlinks** and normalise ``..`` *before* matching, so
  ``workspace/../../etc/passwd`` cannot escape an allow-list.
* **Every decision is explainable** - ``Decision.reason`` is shown to the user in approval cards
  and audit events.
* **Approval is a distinct outcome from allow**, carrying the risk level that triggered it.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
import shlex
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from ..core import PolicyDenied
from ..core.risk import RiskLevel, elevate
from ..core.config import SecuritySettings


class Capability(str, Enum):
    """Every privileged thing Natasha can do."""

    FS_READ = "fs.read"
    FS_WRITE = "fs.write"
    FS_DELETE = "fs.delete"
    FS_LIST = "fs.list"
    SHELL_EXEC = "shell.exec"
    CODE_EXEC = "code.exec"
    NET_HTTP = "net.http"
    NET_SOCKET = "net.socket"
    SCREEN_CAPTURE = "screen.capture"
    INPUT_CONTROL = "input.control"
    CLIPBOARD_READ = "clipboard.read"
    CLIPBOARD_WRITE = "clipboard.write"
    MICROPHONE = "microphone.use"
    CAMERA = "camera.use"
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    MEMORY_DELETE = "memory.delete"
    CREDENTIAL_USE = "credential.use"
    CREDENTIAL_ADMIN = "credential.admin"
    MCP_EXECUTE = "mcp.execute"
    MCP_INSTALL = "mcp.install"
    SKILL_EXECUTE = "skill.execute"
    SKILL_INSTALL = "skill.install"
    MARKETPLACE_DOWNLOAD = "marketplace.download"
    BROWSER_CONTROL = "browser.control"
    APP_CONTROL = "app.control"
    WINDOW_CONTROL = "window.control"
    MODEL_CALL = "model.call"
    SYSTEM_INFO = "system.info"      # reading the clock, environment summaries - never a resource
    DATA_QUERY = "data.query"        # local data manipulation that touches no external resource
    ARTIFACT_WRITE = "artifact.write"
    AUDIT_READ = "audit.read"
    GOVERNANCE_READ = "governance.read"
    GOVERNANCE_WRITE = "governance.write"
    IDENTITY_WRITE = "identity.write"
    SYSTEM_CONFIG = "system.config"
    UPGRADE_PROPOSE = "upgrade.propose"
    UPGRADE_APPLY = "upgrade.apply"

    @classmethod
    def parse(cls, value: object) -> "Capability":
        if isinstance(value, Capability):
            return value
        text = str(value or "").strip().lower()
        for member in cls:
            if member.value == text or member.name.lower() == text:
                return member
        raise PolicyDenied(f"unknown capability: {value!r}")


class Effect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL = "approval"


#: Baseline risk of a capability when used on a benign resource.
BASE_RISK: dict[Capability, RiskLevel] = {
    Capability.FS_READ: RiskLevel.LOW,
    Capability.FS_LIST: RiskLevel.LOW,
    Capability.FS_WRITE: RiskLevel.MEDIUM,
    Capability.FS_DELETE: RiskLevel.HIGH,
    Capability.SHELL_EXEC: RiskLevel.HIGH,
    Capability.CODE_EXEC: RiskLevel.HIGH,
    Capability.NET_HTTP: RiskLevel.LOW,
    Capability.NET_SOCKET: RiskLevel.MEDIUM,
    Capability.SCREEN_CAPTURE: RiskLevel.MEDIUM,
    Capability.INPUT_CONTROL: RiskLevel.HIGH,
    Capability.CLIPBOARD_READ: RiskLevel.MEDIUM,
    Capability.CLIPBOARD_WRITE: RiskLevel.LOW,
    Capability.MICROPHONE: RiskLevel.MEDIUM,
    Capability.CAMERA: RiskLevel.MEDIUM,
    Capability.MEMORY_READ: RiskLevel.LOW,
    Capability.MEMORY_WRITE: RiskLevel.LOW,
    Capability.MEMORY_DELETE: RiskLevel.HIGH,
    Capability.CREDENTIAL_USE: RiskLevel.HIGH,
    Capability.CREDENTIAL_ADMIN: RiskLevel.CRITICAL,
    Capability.MCP_EXECUTE: RiskLevel.MEDIUM,
    Capability.MCP_INSTALL: RiskLevel.HIGH,
    Capability.SKILL_EXECUTE: RiskLevel.MEDIUM,
    Capability.SKILL_INSTALL: RiskLevel.HIGH,
    Capability.MARKETPLACE_DOWNLOAD: RiskLevel.MEDIUM,
    Capability.BROWSER_CONTROL: RiskLevel.HIGH,
    Capability.APP_CONTROL: RiskLevel.HIGH,
    Capability.WINDOW_CONTROL: RiskLevel.MEDIUM,
    Capability.MODEL_CALL: RiskLevel.LOW,
    Capability.SYSTEM_INFO: RiskLevel.NONE,
    Capability.DATA_QUERY: RiskLevel.NONE,
    Capability.ARTIFACT_WRITE: RiskLevel.LOW,
    Capability.AUDIT_READ: RiskLevel.LOW,
    Capability.GOVERNANCE_READ: RiskLevel.LOW,
    Capability.GOVERNANCE_WRITE: RiskLevel.CRITICAL,
    Capability.IDENTITY_WRITE: RiskLevel.CRITICAL,
    Capability.SYSTEM_CONFIG: RiskLevel.HIGH,
    Capability.UPGRADE_PROPOSE: RiskLevel.LOW,
    Capability.UPGRADE_APPLY: RiskLevel.CRITICAL,
}

#: Capabilities that may never be self-approved by a model or worker actor.
OWNER_ONLY: frozenset[Capability] = frozenset(
    {
        Capability.GOVERNANCE_WRITE,
        Capability.IDENTITY_WRITE,
        Capability.CREDENTIAL_ADMIN,
        Capability.UPGRADE_APPLY,
        Capability.SYSTEM_CONFIG,
    }
)

#: Capabilities a worker must have explicitly scoped - a worker with no mission scope is inert.
WORKER_SCOPED: frozenset[Capability] = frozenset(
    {
        Capability.FS_WRITE, Capability.FS_DELETE, Capability.SHELL_EXEC, Capability.CODE_EXEC,
        Capability.NET_SOCKET, Capability.CREDENTIAL_USE, Capability.BROWSER_CONTROL,
        Capability.INPUT_CONTROL, Capability.SCREEN_CAPTURE, Capability.APP_CONTROL,
        Capability.WINDOW_CONTROL, Capability.MEMORY_WRITE, Capability.MEMORY_DELETE,
        Capability.SKILL_EXECUTE,
        Capability.MCP_EXECUTE, Capability.MODEL_CALL, Capability.ARTIFACT_WRITE,
    }
)

#: Capabilities that are never available to a worker, whatever the scope says.
WORKER_FORBIDDEN: frozenset[Capability] = frozenset(
    {
        Capability.CREDENTIAL_ADMIN, Capability.GOVERNANCE_WRITE, Capability.IDENTITY_WRITE,
        Capability.SYSTEM_CONFIG, Capability.UPGRADE_APPLY, Capability.MCP_INSTALL,
        Capability.SKILL_INSTALL,
    }
)

#: Actors considered to be the human owner.
OWNER_ACTORS: frozenset[str] = frozenset({"owner", "user", "human", "local-owner"})


@dataclass
class PolicyRequest:
    """A single question to the policy engine."""

    capability: Capability
    resource: str = ""
    actor: str = "system"
    context: dict[str, Any] = field(default_factory=dict)
    risk_hint: RiskLevel | None = None
    mission_scope: Iterable[Capability] | None = None
    reversible: bool = True


@dataclass
class Decision:
    """The engine's answer, with enough detail to explain and audit it."""

    effect: Effect
    capability: Capability
    risk: RiskLevel
    reason: str
    resource: str = ""
    obligations: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW

    @property
    def needs_approval(self) -> bool:
        return self.effect is Effect.APPROVAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect": self.effect.value,
            "capability": self.capability.value,
            "risk": self.risk.name,
            "reason": self.reason,
            "resource": self.resource,
            "obligations": self.obligations,
        }

    def raise_if_denied(self) -> None:
        if self.effect is Effect.DENY:
            raise PolicyDenied(self.reason, capability=self.capability.value, resource=self.resource)


_DANGEROUS_SHELL = re.compile(r"[;&|`$><\n]|\$\(|\.\./")
_SAFE_COMMAND = re.compile(r"^[A-Za-z0-9_./@:+=-]+$")


def _expand_placeholders(text: str) -> str:
    """Expand the runtime directory placeholders used in settings defaults."""
    from ..core import get_paths

    paths = get_paths()
    table = {
        "$NATASHA_HOME": str(paths.home),
        "$HOME": str(Path.home()),
        "$WORKSPACE": str(paths.workspace),
        "$ARTIFACTS": str(paths.artifacts),
        "$UPLOADS": str(paths.uploads),
        "$DATA": str(paths.home),
    }
    out = str(text)
    for key in sorted(table, key=len, reverse=True):
        out = out.replace(key, table[key])
    return out


class PolicyEngine:
    """Evaluates :class:`PolicyRequest` objects against configured policy."""

    def __init__(self, settings: SecuritySettings | None = None) -> None:
        self.settings = settings or SecuritySettings()
        # Roots may contain $HOME/$WORKSPACE/$ARTIFACTS/$UPLOADS placeholders. Expand them here as
        # well as in the settings loader: a directly-constructed SecuritySettings (tests, embedded
        # use, the desktop app) must not end up with literal "$ARTIFACTS" as a filesystem root.
        self._read_roots = [str(Path(_expand_placeholders(p)).expanduser().resolve())
                            for p in self._expand(self.settings.fs_read_allow)]
        self._write_roots = [str(Path(_expand_placeholders(p)).expanduser().resolve())
                             for p in self._expand(self.settings.fs_write_allow)]
        self._deny_globs = [_expand_placeholders(glob) for glob in self._expand(self.settings.fs_deny_globs)]
        self._net_allow = [domain.lower().lstrip(".") for domain in self.settings.network_allow_domains]
        self._net_deny = [domain.lower().lstrip(".") for domain in self.settings.network_deny_domains]

    @staticmethod
    def _expand(values: list[str]) -> list[str]:
        """Expand ``$WORKSPACE``/``$ARTIFACTS``/``$HOME`` style placeholders to real directories.

        ``SecuritySettings`` may be constructed directly (tests, embedded use), so the expansion
        cannot rely on the settings loader having already done it - a literal ``$ARTIFACTS`` root
        would silently make every artifact write "outside permitted roots".
        """
        from ..core import get_paths

        paths = get_paths()
        table = {
            "$NATASHA_HOME": str(paths.home), "$WORKSPACE": str(paths.workspace),
            "$ARTIFACTS": str(paths.artifacts), "$UPLOADS": str(paths.uploads),
            "$DATA": str(paths.data), "$HOME": str(Path.home()),
        }
        expanded: list[str] = []
        for value in values:
            text = str(value)
            for needle, replacement in table.items():
                text = text.replace(needle, replacement)
            expanded.append(text)
        return expanded

    # -- public API ------------------------------------------------------------ #
    def check(self, request: PolicyRequest) -> Decision:
        """Evaluate *request*. Never raises for policy outcomes; returns a Decision.

        The capability is parsed and normalised first: a caller that passes the *string*
        ``"shell.exec"`` (API payload, tool spec, plugin) must be subject to exactly the same rules
        as one that passes the enum - an unparsed string would otherwise fall through every table.
        """
        capability = Capability.parse(request.capability)
        if capability is not request.capability:
            request = replace(request, capability=capability)
        base_risk = BASE_RISK.get(capability, RiskLevel.MEDIUM)
        risk = request.risk_hint or base_risk

        if request.mission_scope is not None:
            scope = {Capability.parse(c) for c in request.mission_scope}
            if scope and capability not in scope:
                return Decision(
                    Effect.DENY, capability, risk,
                    f"capability {capability.value} is outside the mission scope "
                    f"({', '.join(sorted(c.value for c in scope)) or 'empty'})",
                    resource=request.resource,
                )

        handler = _DISPATCH.get(capability)
        if handler is not None:
            decision = handler(self, request, risk)
        else:
            decision = Decision(Effect.ALLOW, capability, risk, "no resource policy applies", request.resource)

        return self._apply_global_rules(request, decision)

    def allowed(self, capability: Capability | str, resource: str = "", **ctx: Any) -> bool:
        return self.check(PolicyRequest(Capability.parse(capability), resource, context=ctx)).allowed

    def require(self, capability: Capability | str, resource: str = "", **ctx: Any) -> Decision:
        """Check and raise :class:`PolicyDenied` when refused. Approvals pass through as APPROVAL."""
        decision = self.check(PolicyRequest(Capability.parse(capability), resource, context=ctx))
        decision.raise_if_denied()
        return decision

    # -- global rules ---------------------------------------------------------- #
    def _apply_global_rules(self, request: PolicyRequest, decision: Decision) -> Decision:
        """Layer identity, approval and owner-authority rules over a resource decision."""
        capability, risk = request.capability, decision.risk
        actor_is_owner = request.actor.split(":", 1)[0].lower() in OWNER_ACTORS

        if decision.effect is Effect.DENY:
            # Explicit refusal always wins, whatever the actor.
            return decision

        is_worker = request.actor.split(":", 1)[0].lower() == "worker"
        if is_worker:
            if capability in WORKER_FORBIDDEN:
                return Decision(Effect.DENY, capability, RiskLevel.CRITICAL,
                                f"{capability.value} is never available to a worker", resource=request.resource)
            if capability in WORKER_SCOPED and request.mission_scope is None:
                return Decision(Effect.DENY, capability, max(risk, RiskLevel.MEDIUM),
                                f"worker {request.actor!r} may not use {capability.value} without an "
                                "explicit mission scope", resource=request.resource)
            if request.mission_scope is not None and capability not in request.mission_scope:
                return Decision(Effect.DENY, capability, max(risk, RiskLevel.MEDIUM),
                                f"worker {request.actor!r} has no grant for {capability.value} in this "
                                "mission", resource=request.resource)

        if capability in OWNER_ONLY and not actor_is_owner:
            # Owner-only capabilities are refused to workers/models outright - not merely
            # "approval gated" - because the acting party cannot legitimately hold them.
            return Decision(
                Effect.DENY, capability, RiskLevel.CRITICAL,
                f"{capability.value} is owner-only; actor {request.actor!r} cannot hold it",
                resource=request.resource,
            )

        if actor_is_owner:
            # The owner acting directly *is* the authority. Approval gates exist to constrain
            # autonomous/model-initiated action, so an owner-actor request is allowed through -
            # but CRITICAL work still carries a confirmation obligation for the UI.
            obligations = list(decision.obligations)
            if risk is RiskLevel.CRITICAL:
                obligations.append("owner_confirmation_required")
            return Decision(Effect.ALLOW, capability, risk, f"owner authority: {decision.reason}",
                            resource=request.resource, obligations=obligations)

        if self.settings.default_effect == "deny" and capability.value in self.settings.require_approval_for:
            return Decision(
                Effect.APPROVAL, capability, max(risk, RiskLevel.HIGH),
                f"{capability.value} is listed in security.require_approval_for",
                resource=request.resource, obligations=decision.obligations,
            )

        if risk.requires_owner_approval():
            return Decision(
                Effect.APPROVAL, capability, risk,
                f"{risk.name} risk operation initiated by {request.actor!r} requires explicit owner approval",
                resource=request.resource, obligations=decision.obligations,
            )

        return decision

    # -- filesystem ------------------------------------------------------------ #
    def resolve_path(self, raw: str, *, must_exist: bool = False) -> Path:
        """Resolve *raw* to an absolute, symlink-free path (``..`` collapsed)."""
        if not raw or "\x00" in raw:
            raise PolicyDenied("empty or malformed path")
        # ``normpath`` collapses ``..``/``.`` *lexically* before anything else looks at the string.
        # Without it, "<root>/sub/../../etc/passwd" still starts with the root prefix and the
        # confinement check below would pass while the real target is outside the sandbox.
        candidate = Path(os.path.normpath(os.path.expanduser(raw)))
        if not candidate.is_absolute():
            candidate = Path(os.path.normpath(str(Path.cwd() / candidate)))
        if must_exist:
            return candidate.resolve()
        # Resolve the deepest existing ancestor so symlinked parents cannot be used to escape.
        probe = candidate
        missing: list[str] = []
        while not probe.exists() and probe != probe.parent:
            missing.append(probe.name)
            probe = probe.parent
        resolved = probe.resolve()
        for part in reversed(missing):
            resolved = resolved / part
        return resolved

    def _check_fs(self, request: PolicyRequest, risk: RiskLevel, *, writing: bool, deleting: bool = False) -> Decision:
        raw = request.resource
        try:
            path = self.resolve_path(raw)
        except PolicyDenied as exc:
            return Decision(Effect.DENY, request.capability, risk, str(exc), resource=raw)
        text = str(path)

        for pattern in self._deny_globs:
            if _matches_deny_glob(pattern, text, path.name):
                return Decision(
                    Effect.DENY, request.capability, RiskLevel.HIGH,
                    f"path matches protected pattern {pattern!r}", resource=text,
                )

        roots = self._write_roots if (writing or deleting) else self._read_roots
        inside = any(text == root or text.startswith(root.rstrip("/") + "/") for root in roots)
        if not inside:
            # Confinement is a *root* rule, not a risk rule: an approval must never be able to
            # widen it, or "the model may only write inside these roots" would be a lie. Widening
            # is an owner configuration change (security.fs_write_allow / security.fs_read_allow).
            verb = "delete" if deleting else ("write" if writing else "read")
            key = "security.fs_write_allow" if (writing or deleting) else "security.fs_read_allow"
            return Decision(
                Effect.DENY, request.capability, max(risk, RiskLevel.HIGH),
                f"refusing to {verb} outside permitted roots: {text} "
                f"(add the path to {key} to permit it)",
                resource=text,
            )

        if deleting:
            return Decision(Effect.APPROVAL, request.capability, RiskLevel.HIGH,
                            f"deletion is irreversible-by-default: {text}", resource=text,
                            obligations=["create_backup", "confirm_after_delete"])
        return Decision(Effect.ALLOW, request.capability, risk, "inside permitted roots", resource=text)

    # -- shell ----------------------------------------------------------------- #
    def _check_shell(self, request: PolicyRequest, risk: RiskLevel) -> Decision:
        command = (request.resource or "").strip()
        if not command:
            return Decision(Effect.DENY, request.capability, risk, "empty command", resource="")
        lowered = command.lower()
        for banned in self.settings.command_denylist:
            if banned.lower() in lowered:
                return Decision(Effect.DENY, request.capability, RiskLevel.CRITICAL,
                                f"command matches denylist entry {banned!r}", resource=command)
        if _DANGEROUS_SHELL.search(command):
            return Decision(
                Effect.APPROVAL, request.capability, RiskLevel.HIGH,
                "command uses shell metacharacters (pipes, redirects, substitution) and needs approval",
                resource=command,
                obligations=["no_shell_interpretation", "audit_full_command"],
            )
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return Decision(Effect.DENY, request.capability, risk, f"unparsable command: {exc}", resource=command)
        if not argv:
            return Decision(Effect.DENY, request.capability, risk, "empty argv", resource=command)
        program = Path(argv[0]).name
        allowlist = set(self.settings.command_allowlist)
        if program not in allowlist:
            return Decision(
                Effect.APPROVAL, request.capability, RiskLevel.HIGH,
                f"program {program!r} is not in the command allowlist", resource=command,
            )
        return Decision(Effect.ALLOW, request.capability, RiskLevel.MEDIUM, "allowlisted program, no shell metacharacters",
                        resource=command, obligations=["exec_without_shell", "capture_exit_code"])

    # -- network --------------------------------------------------------------- #
    def _check_network(self, request: PolicyRequest, risk: RiskLevel) -> Decision:
        url = request.resource or ""
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.hostname or "").lower()
        if not host:
            return Decision(
                Effect.DENY, request.capability, risk,
                (f"no host to check for {url!r}: a network operation must name the host it will "
                 "contact (configure the connector's host, or pass a URL)") if url
                else "no host to check for a network operation: configure the connector's host "
                     "or pass a URL",
                resource=url,
            )
        if parsed.scheme and parsed.scheme not in {"http", "https", "ws", "wss"}:
            return Decision(Effect.DENY, request.capability, RiskLevel.HIGH,
                            f"scheme {parsed.scheme!r} is not permitted", resource=url)

        if host in self._net_deny or any(fnmatch.fnmatch(host, pattern) for pattern in self._net_deny):
            return Decision(Effect.DENY, request.capability, RiskLevel.HIGH,
                            f"host {host!r} is explicitly denied", resource=url)
        override = request.context.get("allow_host_override")
        if host in self._net_allow or any(
            host == pattern or fnmatch.fnmatch(host, pattern) for pattern in self._net_allow
        ) or (override and host == str(override).lower()):
            return Decision(Effect.ALLOW, request.capability, RiskLevel.LOW,
                            f"host {host!r} is allowlisted", resource=url,
                            obligations=["no_credential_leakage_to_redirects"])
        if _is_private_host(host):
            return Decision(Effect.APPROVAL, request.capability, RiskLevel.HIGH,
                            f"private/link-local host {host!r} requires approval (SSRF guard)", resource=url)
        return Decision(Effect.APPROVAL, request.capability, RiskLevel.MEDIUM,
                        f"host {host!r} is not allowlisted; outbound request needs approval", resource=url)

    # -- credentials ----------------------------------------------------------- #
    def _check_credential(self, request: PolicyRequest, risk: RiskLevel) -> Decision:
        reference = request.resource or ""
        allowed_scopes = request.context.get("allowed_scopes")
        scope = request.context.get("scope", "")
        if not reference:
            return Decision(Effect.DENY, request.capability, risk, "no credential reference supplied", resource="")
        if allowed_scopes is not None and scope and scope not in set(allowed_scopes):
            return Decision(Effect.DENY, request.capability, risk,
                            f"scope {scope!r} is not permitted for actor {request.actor!r}", resource=reference)
        return Decision(Effect.APPROVAL, request.capability, RiskLevel.HIGH,
                        f"credential use ({scope or 'unspecified scope'}) requires approval", resource=reference,
                        obligations=["broker_only", "never_log_secret", "single_use_handle"])

    # -- computer use ---------------------------------------------------------- #
    def _check_computer(self, request: PolicyRequest, risk: RiskLevel) -> Decision:
        if not request.context.get("user_present", True):
            return Decision(Effect.DENY, request.capability, RiskLevel.HIGH,
                            "computer control requires an attended session", resource=request.resource)
        if request.capability is Capability.INPUT_CONTROL and not request.reversible:
            return Decision(Effect.APPROVAL, request.capability, RiskLevel.CRITICAL,
                            "irreversible input control (clicks/typing) requires approval", resource=request.resource)
        return Decision(Effect.ALLOW, request.capability, risk, "attended computer-use request", resource=request.resource,
                        obligations=["visible_indicator", "kill_switch_armed"])


def _matches_deny_glob(pattern: str, path_text: str, basename: str) -> bool:
    """Match absolute globs, directory globs and bare basename patterns.

    ``**/.ssh/**``, ``/etc/**``, ``*.pem`` and ``.env`` must all work, because these patterns
    come from user configuration and a mismatch would silently open a hole.
    """
    expanded = os.path.expanduser(pattern)
    if expanded.startswith("/"):
        return (
            path_text == expanded
            or fnmatch.fnmatch(path_text, expanded)
            or fnmatch.fnmatch(path_text, expanded.rstrip("/") + "/*")
        )
    bare = expanded.lstrip("*/")
    return (
        fnmatch.fnmatch(path_text, "*/" + bare)
        or fnmatch.fnmatch(path_text, bare)
        or fnmatch.fnmatch(basename, bare)
    )


def _is_private_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host in {"localhost"} or host.endswith(".local") or host.endswith(".internal")
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved


_DISPATCH = {
    Capability.FS_READ: lambda engine, request, risk: engine._check_fs(request, risk, writing=False),
    Capability.FS_LIST: lambda engine, request, risk: engine._check_fs(request, risk, writing=False),
    Capability.FS_WRITE: lambda engine, request, risk: engine._check_fs(request, risk, writing=True),
    Capability.FS_DELETE: lambda engine, request, risk: engine._check_fs(request, risk, writing=True, deleting=True),
    Capability.ARTIFACT_WRITE: lambda engine, request, risk: engine._check_fs(request, risk, writing=True),
    Capability.SHELL_EXEC: lambda engine, request, risk: engine._check_shell(request, risk),
    Capability.NET_HTTP: lambda engine, request, risk: engine._check_network(request, risk),
    Capability.NET_SOCKET: lambda engine, request, risk: engine._check_network(request, risk),
    Capability.CREDENTIAL_USE: lambda engine, request, risk: engine._check_credential(request, risk),
    Capability.CREDENTIAL_ADMIN: lambda engine, request, risk: engine._check_credential(request, risk),
    Capability.SCREEN_CAPTURE: lambda engine, request, risk: engine._check_computer(request, risk),
    Capability.INPUT_CONTROL: lambda engine, request, risk: engine._check_computer(request, risk),
    Capability.BROWSER_CONTROL: lambda engine, request, risk: engine._check_computer(request, risk),
    Capability.APP_CONTROL: lambda engine, request, risk: engine._check_computer(request, risk),
    Capability.WINDOW_CONTROL: lambda engine, request, risk: engine._check_computer(request, risk),
}
