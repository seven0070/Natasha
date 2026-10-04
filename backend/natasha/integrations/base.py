"""Connector contract and registry."""

from __future__ import annotations

import abc
import threading
from dataclasses import dataclass, field
from typing import Any

from ..core import NotFoundError
from ..core.risk import RiskLevel
from ..security.injection import ContentTrust, ExternalContent
from ..security.policy import Capability


@dataclass
class ConnectorAction:
    """One operation a connector exposes."""

    name: str
    description: str
    method: str = "GET"
    path: str = ""
    capability: Capability = Capability.NET_HTTP
    risk: RiskLevel = RiskLevel.MEDIUM
    required_arguments: list[str] = field(default_factory=list)
    #: Which *argument* identifies the thing being acted on ("repo", "channel", "to"). The policy
    #: engine and the approval fingerprint use it, so an approval is bound to a target.
    resource_field: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "method": self.method,
                "capability": self.capability.value, "risk": self.risk.name,
                "required_arguments": self.required_arguments,
                "resource_field": self.resource_field}


@dataclass
class ConnectorResult:
    ok: bool
    data: Any = None
    error: str = ""
    status: int = 0
    untrusted_text: str = ""
    suspicious: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "data": self.data, "error": self.error, "status": self.status,
                "suspicious": self.suspicious}


class Connector(abc.ABC):
    """Base class for integrations."""

    name: str = "connector"
    description: str = ""
    credential_ref: str = ""
    actions: list[ConnectorAction] = []

    def __init__(self, *, credential_ref: str = "", broker: Any = None, settings: dict[str, Any] | None = None) -> None:
        self.credential_ref = credential_ref or self.credential_ref
        self.broker = broker
        self.settings = settings or {}

    @abc.abstractmethod
    async def perform(self, action: str, arguments: dict[str, Any], *, approval_id: str = "",
                      actor: str = "model:main") -> ConnectorResult:
        """Execute an action.

        ``approval_id`` carries the owner's approval for credential use when the calling actor is not
        the owner. Connectors must never silently proceed without the credentials they need.
        """

    def network_host(self, action: str, arguments: dict[str, Any]) -> str:
        """The host this connector will actually contact for *action*.

        Argument-driven connectors (REST, webhooks) return ``""`` and let the target URL speak for
        itself. Connectors that talk to a *configured* server (GitHub, Slack, SMTP) must return that
        server's host: the policy engine checks network access against the host, and a call with no
        host is denied outright - a connector that stays silent here can never run.
        """
        return self.settings.get("base_url", "") or ""

    def headers(self, purpose: str, *, approval_id: str = "", actor: str = "model:main") -> dict[str, str]:
        """Auth headers for this connector, obtained from the broker (never stored here).

        Raises :class:`CredentialError` when a credential is configured but cannot be used - silence
        here would mean an unauthenticated request pretending to be authenticated.
        """
        if self.broker is None or not self.credential_ref:
            return {}
        from ..core import CredentialError

        try:
            with self.broker.issue(self.credential_ref, purpose=purpose, actor=actor, scope=purpose,
                                   approval_id=approval_id) as handle:
                return handle.headers()
        except Exception as exc:
            raise CredentialError(
                f"connector {self.name!r} needs credential {self.credential_ref!r} for {purpose!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def approval_from(self, arguments: dict[str, Any]) -> str:
        """Approval id passed by the caller (namespaced so it can never collide with action fields)."""
        return str(arguments.pop("_approval_id", "") or "")

    def mark_untrusted(self, text: str, *, source: str) -> tuple[str, bool]:
        content = ExternalContent(text=text, source=source, trust=ContentTrust.EXTERNAL)
        return content.text, content.suspicious

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "description": self.description, "credential_ref": self.credential_ref,
            "actions": [action.to_dict() for action in self.actions],
        }


class IntegrationRegistry:
    """Registered connectors, exposed as tools."""

    def __init__(self, *, broker: Any = None) -> None:
        self.broker = broker
        self._connectors: dict[str, Connector] = {}
        self._lock = threading.RLock()

    def register(self, connector: Connector) -> Connector:
        with self._lock:
            self._connectors[connector.name] = connector
        return connector

    def get(self, name: str) -> Connector:
        with self._lock:
            connector = self._connectors.get(name)
        if connector is None:
            raise NotFoundError(f"integration {name!r} is not registered")
        return connector

    def all(self) -> list[Connector]:
        with self._lock:
            return list(self._connectors.values())

    def describe(self) -> list[dict[str, Any]]:
        return [connector.describe() for connector in self.all()]

    def register_tools(self, tools: Any) -> list[str]:
        """Expose each connector action as a policy-checked tool."""
        from ..security.validation import Schema
        from ..tools.base import FunctionTool, ToolResult

        registered: list[str] = []
        for connector in self.all():
            for action in connector.actions:
                tool_name = f"integration__{connector.name}__{action.name}"

                async def handler(arguments: dict[str, Any], context: Any, *, _connector: str = connector.name,
                                  _action: str = action.name) -> ToolResult:
                    approval_id = str(context.extra.get("approval_id", "") if context is not None else "")
                    result = await self.get(_connector).perform(_action, arguments,
                                                                approval_id=approval_id,
                                                                actor=getattr(context, "actor", "model:main"))
                    if result.ok:
                        return ToolResult.success(result.data, status=result.status,
                                                  suspicious=result.suspicious)
                    return ToolResult.failure(result.error or "integration call failed", status=result.status)

                def resolver(arguments: dict[str, Any], _connector: str = connector.name,
                             _action: str = action.name, _field: str = action.resource_field) -> str:
                    """What this call acts on: the host it will contact, else the named argument.

                    Without this the policy engine sees an empty resource and denies every
                    argument-driven connector with "cannot determine host" - i.e. the tool is
                    registered but can never run.
                    """
                    target = self.get(_connector)
                    host = target.network_host(_action, arguments)
                    if host:
                        return host
                    if _field:
                        value = arguments.get(_field)
                        if isinstance(value, str):
                            return value[:400]
                    return ""

                tools.register(
                    FunctionTool(
                        tool_name, handler, description=f"[{connector.name}] {action.description}",
                        capability=action.capability, risk=action.risk,
                        schema=Schema.object({}, additional=True),
                        # Risk decides this: a HIGH-risk action always needs explicit owner approval,
                        # and the spec is what the UI, the CLI and the mission planner read.
                        requires_approval=action.risk.requires_owner_approval(),
                        resource_field=action.resource_field,
                        resource_resolver=resolver,
                        tags=("integration", connector.name),
                    )
                )
                registered.append(tool_name)
        return registered


_REGISTRY: IntegrationRegistry | None = None
_LOCK = threading.Lock()


def get_integration_registry(**kwargs: Any) -> IntegrationRegistry:
    global _REGISTRY
    with _LOCK:
        if _REGISTRY is None:
            _REGISTRY = IntegrationRegistry(**kwargs)
        return _REGISTRY


def reset_integration_registry() -> None:
    global _REGISTRY
    with _LOCK:
        _REGISTRY = None
