"""UniversalCredentialManager - one place to store, scope, test, rotate and revoke credentials.

Supported kinds: api keys, bearer tokens, OAuth2 (authorization code, PKCE, device), refresh
tokens, service accounts, SSH keys, local secrets, MCP credentials and custom shapes.

Access control
--------------
Administration (``store``/``rotate``/``revoke``) is owner-only. *Use* goes through the
:class:`~natasha.credentials.broker.CredentialBroker` so the model never holds a secret; the
manager's ``resolve`` performs the policy check and returns plaintext only to trusted callers.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..core import ApprovalDenied, ApprovalRequired, CredentialError, NotFoundError
from ..core.clock import parse_iso, utcnow
from ..core.risk import RiskLevel
from ..events import EventKind, EventLog, get_event_log
from ..security.policy import Capability, Effect, PolicyEngine, PolicyRequest
from .crypto import SecretBox
from .vault import CredentialMetadata, CredentialVault


@dataclass(frozen=True)
class CredentialKind:
    """How a credential kind is presented to a provider and validated."""

    name: str
    header: str = "Authorization"
    scheme: str = "Bearer"
    fields: tuple[str, ...] = ("value",)
    description: str = ""
    requires_refresh: bool = False

    def headers(self, secret: dict[str, Any]) -> dict[str, str]:
        value = secret.get("value") or secret.get("access_token") or secret.get("token") or ""
        if not value:
            raise CredentialError(f"credential of kind {self.name} has no usable value")
        return {self.header: f"{self.scheme} {value}".strip()}


KINDS: dict[str, CredentialKind] = {
    "api_key": CredentialKind("api_key", description="Provider API key sent as a bearer token"),
    "bearer": CredentialKind("bearer", description="Raw bearer token"),
    "header_key": CredentialKind("header_key", header="x-api-key", scheme="", description="API key in a custom header"),
    "oauth2": CredentialKind("oauth2", header="Authorization", scheme="Bearer", fields=("access_token", "refresh_token"), requires_refresh=True, description="OAuth 2.0 authorization-code credential"),
    "oauth_pkce": CredentialKind("oauth_pkce", fields=("access_token", "refresh_token"), requires_refresh=True, description="OAuth 2.0 + PKCE (public clients)"),
    "device_code": CredentialKind("device_code", fields=("access_token", "refresh_token"), requires_refresh=True, description="OAuth 2.0 device-authorization credential"),
    "refresh_token": CredentialKind("refresh_token", fields=("refresh_token",), requires_refresh=True, description="Standalone refresh token"),
    "service_account": CredentialKind("service_account", fields=("client_email", "private_key"), header="Authorization", scheme="Bearer", description="Service-account JSON (JWT-bearer flow)"),
    "ssh_key": CredentialKind("ssh_key", fields=("private_key", "public_key"), description="SSH private key for agent auth"),
    "local_secret": CredentialKind("local_secret", description="Local-only secret (never sent to providers)"),
    "mcp": CredentialKind("mcp", description="Credential scoped to one MCP server"),
    "custom": CredentialKind("custom", description="Custom shape declared by a skill or integration"),
}


class UniversalCredentialManager:
    """Owner-facing credential administration with audit and policy enforcement."""

    def __init__(
        self,
        vault: CredentialVault | None = None,
        *,
        policy: PolicyEngine | None = None,
        log: EventLog | None = None,
        box: SecretBox | None = None,
    ) -> None:
        self.vault = vault or CredentialVault(box=box)
        self.policy = policy or PolicyEngine()
        self.log = log or get_event_log()
        self._testers: dict[str, Callable[[str, dict[str, Any]], tuple[bool, str]]] = {}
        self._lock = threading.RLock()

    # -- registration ---------------------------------------------------------- #
    def register_tester(self, provider: str, tester: Callable[[str, dict[str, Any]], tuple[bool, str]]) -> None:
        """Register a connection test (e.g. a provider health probe)."""
        with self._lock:
            self._testers[provider] = tester

    # -- administration -------------------------------------------------------- #
    def store(
        self,
        reference: str,
        secret: str | dict[str, Any],
        *,
        kind: str = "api_key",
        provider: str = "",
        label: str = "",
        scopes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        expires_at: str = "",
        actor: str = "owner",
    ) -> CredentialMetadata:
        """Store or replace a credential (owner-only)."""
        if kind not in KINDS:
            raise CredentialError(f"unknown credential kind {kind!r}; known: {', '.join(sorted(KINDS))}")
        self._require_admin(actor, reference)
        meta = self.vault.put(
            reference, secret, kind=kind, provider=provider, label=label or reference,
            scopes=scopes or [], metadata=metadata or {}, expires_at=expires_at,
        )
        self._event("stored", reference=meta.ref, actor=actor, provider=provider, kind=kind,
                    scopes=meta.scopes, details={"fingerprint": meta.fingerprint})
        return meta

    def rotate(self, reference: str, new_secret: str | dict[str, Any], *, actor: str = "owner") -> CredentialMetadata:
        self._require_admin(actor, reference)
        meta = self.vault.rotate(reference, new_secret)
        self._event("rotated", reference=meta.ref, actor=actor, provider=meta.provider, kind=meta.kind,
                    details={"version": meta.version})
        return meta

    def revoke(self, reference: str, *, actor: str = "owner") -> None:
        self._require_admin(actor, reference)
        self.vault.revoke(reference)
        self._event("revoked", reference=reference, actor=actor, risk=RiskLevel.HIGH)

    def delete(self, reference: str, *, actor: str = "owner", purge_history: bool = True) -> None:
        self._require_admin(actor, reference)
        self.vault.delete(reference, purge_versions=purge_history)
        self._event("deleted", reference=reference, actor=actor, risk=RiskLevel.HIGH)

    # -- reads ----------------------------------------------------------------- #
    def exists(self, reference: str) -> bool:
        try:
            return self.vault.exists(reference)
        except CredentialError:
            return False

    def list(self, *, provider: str = "") -> list[dict[str, Any]]:
        """Metadata only - never returns secret material."""
        return [meta.to_dict() for meta in self.vault.list_metadata(provider=provider)]

    def health(self, reference: str) -> dict[str, Any]:
        meta = self.vault.metadata(reference)
        expired = bool(meta.expires_at and parse_iso(meta.expires_at) <= utcnow())
        return {
            "ref": meta.ref, "kind": meta.kind, "provider": meta.provider, "revoked": meta.revoked,
            "expired": expired, "version": meta.version, "last_used_at": meta.last_used_at,
            "rotated_at": meta.rotated_at, "scopes": meta.scopes,
        }

    def test_connection(self, reference: str, *, actor: str = "owner") -> dict[str, Any]:
        """Probe the provider with the stored credential (never echoes the secret)."""
        meta = self.vault.metadata(reference)
        tester = self._testers.get(meta.provider)
        if tester is None:
            return {"ok": False, "reason": f"no connection test registered for provider {meta.provider!r}"}
        decision = self.policy.check(
            PolicyRequest(Capability.CREDENTIAL_USE, reference, actor=actor, context={"scope": "test_connection"})
        )
        decision.raise_if_denied()
        secret = self.vault.get_secret(reference, mark_used=False)
        try:
            ok, message = tester(meta.provider, secret)
        except Exception as exc:  # pragma: no cover - provider specific
            ok, message = False, f"{type(exc).__name__}: {exc}"
        self._event("tested", reference=reference, actor=actor, provider=meta.provider,
                    details={"ok": ok, "message": message[:200]})
        return {"ok": ok, "message": message}

    def resolve(self, reference: str, *, purpose: str = "", actor: str = "owner",
                approval_id: str = "") -> dict[str, Any]:
        """Decrypt for an authorised caller.

        Only an ALLOW decision returns the secret. An APPROVAL decision means the *owner* must grant
        it first (with a real approval token); a non-owner actor can therefore never walk away with
        plaintext just because the request was "close enough" to allowed. Tool- and model-facing calls
        should use the broker, which issues short-lived handles instead.
        """
        decision = self.policy.check(
            PolicyRequest(Capability.CREDENTIAL_USE, reference, actor=actor, context={"scope": purpose})
        )
        decision.raise_if_denied()
        if decision.effect is not Effect.ALLOW:
            if not approval_id:
                self._event("approval_required", reference=reference, actor=actor,
                            details={"purpose": purpose, "reason": decision.reason})
                raise ApprovalRequired(
                    f"reading {reference!r} needs explicit owner approval ({decision.reason}); "
                    "pass a consumed approval id or use the CLI as the owner",
                    reference=reference,
                )
            if self.approvals is None:
                raise ApprovalDenied("an approval id was supplied but no approval engine is attached")
            self.approvals.consume(approval_id, Capability.CREDENTIAL_USE.value, actor=actor)
        secret = self.vault.get_secret(reference)
        self._event("used", reference=reference, actor=actor,
                    details={"purpose": purpose, "approved": bool(approval_id)})
        return secret

    def headers_for(self, reference: str, *, actor: str = "owner", purpose: str = "",
                    approval_id: str = "") -> dict[str, str]:
        """Build auth headers for an outbound provider call (same ALLOW/approval rule as resolve)."""
        secret = self.resolve(reference, purpose=purpose, actor=actor, approval_id=approval_id)
        meta = self.vault.metadata(reference)
        kind = KINDS.get(meta.kind, KINDS["custom"])
        payload = secret if isinstance(secret, dict) else {"value": secret}
        return kind.headers(payload)

    def metadata(self, reference: str) -> CredentialMetadata:
        return self.vault.metadata(reference)

    def stats(self) -> dict[str, Any]:
        return self.vault.stats()

    # -- internals ------------------------------------------------------------- #
    def _require_admin(self, actor: str, reference: str) -> None:
        decision = self.policy.check(
            PolicyRequest(Capability.CREDENTIAL_ADMIN, reference, actor=actor, context={"scope": "admin"})
        )
        if not decision.allowed:
            self._event("admin_denied", reference=reference, actor=actor, risk=RiskLevel.CRITICAL,
                        details={"reason": decision.reason})
            raise CredentialError(f"credential administration refused for {actor!r}: {decision.reason}")

    def _event(
        self,
        action: str,
        *,
        reference: str,
        actor: str,
        provider: str = "",
        kind: str = "",
        scopes: list[str] | None = None,
        risk: RiskLevel = RiskLevel.MEDIUM,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.log.append(
            EventKind.CREDENTIAL,
            {
                "action": action, "ref": reference, "provider": provider, "kind": kind,
                "scopes": scopes or [], "details": details or {},
            },
            actor=actor, source="credentials.manager", risk=risk,
        )


_MANAGERS: dict[str, UniversalCredentialManager] = {}
_LOCK = threading.Lock()


def get_credential_manager() -> UniversalCredentialManager:
    with _LOCK:
        if "default" not in _MANAGERS:
            _MANAGERS["default"] = UniversalCredentialManager()
        return _MANAGERS["default"]


def reset_credential_managers() -> None:
    with _LOCK:
        for manager in _MANAGERS.values():
            manager.vault.close()
        _MANAGERS.clear()
