"""Natasha exception hierarchy.

Every error carries a stable ``code`` so the API, CLI, audit trail and frontend can react to a
machine-readable value instead of matching on prose.
"""

from __future__ import annotations

from typing import Any


class NatashaError(Exception):
    """Base class for all Natasha errors."""

    code = "natasha_error"
    http_status = 500

    def __init__(self, message: str = "", **details: Any) -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class ConfigurationError(NatashaError):
    code = "configuration_error"


class ValidationError(NatashaError):
    code = "validation_error"
    http_status = 422


class NotFoundError(NatashaError):
    code = "not_found"
    http_status = 404


class ConflictError(NatashaError):
    code = "conflict"
    http_status = 409


class PolicyDenied(NatashaError):
    """A capability was refused by the policy engine. Not retryable."""

    code = "policy_denied"
    http_status = 403



#: Alias kept for readability in subsystems that talk about access rather than policy.
AccessDenied = PolicyDenied

class ApprovalRequired(NatashaError):
    """The operation is permitted only with an explicit owner approval."""

    code = "approval_required"
    http_status = 428

    def __init__(self, message: str = "approval required", request_id: str = "", **details: Any) -> None:
        super().__init__(message, request_id=request_id, **details)
        self.request_id = request_id


class ApprovalDenied(NatashaError):
    code = "approval_denied"
    http_status = 403


class GovernanceViolation(NatashaError):
    """An action would breach the constitutional core. Hard stop, never auto-repaired."""

    code = "governance_violation"
    http_status = 403


class CredentialError(NatashaError):
    code = "credential_error"
    http_status = 400


class ProviderError(NatashaError):
    code = "provider_error"
    http_status = 502


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"
    http_status = 503


class RateLimited(ProviderError):
    code = "rate_limited"
    http_status = 429


class VerificationFailed(NatashaError):
    code = "verification_failed"
    http_status = 422


class RecoveryExhausted(NatashaError):
    code = "recovery_exhausted"
    http_status = 409


class ToolError(NatashaError):
    code = "tool_error"


class MCPError(NatashaError):
    code = "mcp_error"
    http_status = 502


class SkillError(NatashaError):
    code = "skill_error"


class SandboxError(NatashaError):
    code = "sandbox_error"
