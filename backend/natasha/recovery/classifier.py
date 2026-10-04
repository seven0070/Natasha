"""Failure classification: what kind of failure happened, and what may be done about it."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any

from ..core.clock import iso
from ..core.hashing import sha256_text


class FailureClass(str, enum.Enum):
    """The failure taxonomy drives the repair policy."""

    TRANSIENT = "transient"            # network blip, temporary lock - retry may help
    RATE_LIMITED = "rate_limited"      # back off, then retry or reroute
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    RESOURCE = "resource"              # disk/memory/CPU exhausted
    INVALID_INPUT = "invalid_input"    # bad arguments - retry cannot help
    PERMISSION = "permission"          # policy denied - escalate, never retry
    APPROVAL_REQUIRED = "approval_required"
    VERIFICATION = "verification"      # produced something that did not check out
    CONFLICT = "conflict"              # concurrent modification / conflicting state
    NOT_FOUND = "not_found"
    GOVERNANCE = "governance"          # constitutional stop - never repaired automatically
    SECURITY = "security"              # injection/credential incident - never auto-repaired
    UNKNOWN = "unknown"

    @property
    def retryable(self) -> bool:
        return self in (FailureClass.TRANSIENT, FailureClass.RATE_LIMITED,
                        FailureClass.PROVIDER_UNAVAILABLE, FailureClass.TIMEOUT,
                        FailureClass.RESOURCE, FailureClass.CONFLICT)

    @property
    def repairable(self) -> bool:
        """Whether an automated repair is allowed at all."""
        return self not in (FailureClass.GOVERNANCE, FailureClass.SECURITY, FailureClass.PERMISSION,
                            FailureClass.APPROVAL_REQUIRED)

    @property
    def needs_owner(self) -> bool:
        return self in (FailureClass.GOVERNANCE, FailureClass.SECURITY, FailureClass.PERMISSION,
                        FailureClass.APPROVAL_REQUIRED, FailureClass.INVALID_INPUT)


#: Exception type names map deterministically to a class - checked before message substrings so a
#: typed failure can never be mis-read because of wording.
_TYPE_PATTERNS: list[tuple[FailureClass, tuple[str, ...]]] = [
    (FailureClass.GOVERNANCE, ("governanceviolation", "constitutionviolation", "protectedareaerror")),
    (FailureClass.SECURITY, ("securityviolation", "injectiondetected", "promptinjectiondetected",
                             "credentialerror", "credentialleak", "sanitizererror")),
    (FailureClass.APPROVAL_REQUIRED, ("approvalrequired", "approvalpending", "awaitingapproval")),
    (FailureClass.PERMISSION, ("accessdenied", "policydenied", "policyviolation", "permissionerror",
                               "forbidden", "notpermitted", "unauthorised", "unauthorized")),
    (FailureClass.RATE_LIMITED, ("ratelimiterror", "ratelimitexceeded", "quotaexceeded")),
    (FailureClass.PROVIDER_UNAVAILABLE, ("providerunavailable", "providererror", "connectionerror",
                                         "connectionrefusederror", "servicenavailable", "servicenavailableerror",
                                         "apierror", "httperror")),
    (FailureClass.TIMEOUT, ("timeouterror", "timeoutexpired", "deadlineexceeded", "asyntimeouterror")),
    (FailureClass.RESOURCE, ("resourcerror", "resourceerror", "resourceexhausted", "memoryerror",
                             "diskfull")),
    (FailureClass.NOT_FOUND, ("notfounderror", "filenotfounderror", "nofileerror", "lookupmissing")),
    (FailureClass.CONFLICT, ("conflicterror", "integrityerror", "alreadyexists", "staleerror")),
    (FailureClass.VERIFICATION, ("verificationfailed", "verificationerror", "unproven", "checkfailed")),
    (FailureClass.INVALID_INPUT, ("validationerror", "invalidargument", "schemaerror", "valueerror",
                                  "typeerror", "keyerror", "attributeerror")),
    (FailureClass.TRANSIENT, ("transienterror", "flakyerror", "retryableerror")),
]

#: Substrings that identify each class in a message, checked in order.
_PATTERNS: list[tuple[FailureClass, tuple[str, ...]]] = [
    (FailureClass.GOVERNANCE, ("governance", "constitution", "protected area", "governance violation")),
    (FailureClass.SECURITY, ("injection", "exfiltration", "credential leak", "security violation",
                             "prompt injection", "dangerous pattern")),
    (FailureClass.APPROVAL_REQUIRED, ("approval required", "requires approval", "requires owner approval",
                                      "awaiting approval", "needs the owner", "owner approval")),
    (FailureClass.PERMISSION, ("policy denied", "denied", "not permitted", "forbidden", "owner-only",
                               "401", "403")),
    (FailureClass.RATE_LIMITED, ("rate limit", "429", "too many requests", "quota exceeded")),
    (FailureClass.PROVIDER_UNAVAILABLE, ("unavailable", "connection refused", "no healthy provider",
                                         "502", "503", "provider error")),
    (FailureClass.TIMEOUT, ("timed out", "timeout", "deadline exceeded")),
    (FailureClass.RESOURCE, ("no space", "out of memory", "too many open files", "disk full")),
    (FailureClass.NOT_FOUND, ("not found", "no such file", "404")),
    (FailureClass.CONFLICT, ("conflict", "already exists", "checksum mismatch", "stale", "concurrent")),
    (FailureClass.VERIFICATION, ("verification failed", "did not pass", "check failed", "unproven")),
    (FailureClass.INVALID_INPUT, ("invalid argument", "validation", "schema", "malformed", "unexpected field")),
    (FailureClass.TRANSIENT, ("temporarily", "try again", "transient", "flaky")),
]


@dataclass
class FailureRecord:
    """A classified failure, ready for the repair policy."""

    message: str
    class_: FailureClass = FailureClass.UNKNOWN
    operation: str = ""
    attempt: int = 1
    max_attempts: int = 3
    reversible: bool = True
    details: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=iso)

    @property
    def fingerprint(self) -> str:
        """Stable id for "the same failure again", used to stop pointless repair loops.

        Volatile fragments (attempt numbers, ids, ports, timings) are stripped so a *deterministic*
        error compares equal across retries while a genuinely different error does not.
        """
        volatile = re.sub(r"[0-9a-f]{8,}|\d+", "#", self.message.lower())
        collapsed = " ".join(volatile.split())[:300]
        return sha256_text(f"{self.class_.value}|{self.operation}|{collapsed}")[:16]

    @property
    def retryable(self) -> bool:
        return self.class_.retryable and self.attempt < self.max_attempts

    @property
    def repairable(self) -> bool:
        return self.class_.repairable and self.attempt <= self.max_attempts

    @property
    def exhausted(self) -> bool:
        return self.attempt >= self.max_attempts

    @property
    def needs_owner(self) -> bool:
        return self.class_.needs_owner or self.exhausted

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message[:500], "class": self.class_.value, "operation": self.operation,
                "attempt": self.attempt, "max_attempts": self.max_attempts, "reversible": self.reversible,
                "retryable": self.retryable, "repairable": self.repairable, "needs_owner": self.needs_owner,
                "details": self.details, "at": self.at}


def classify(error: BaseException | str, *, operation: str = "", attempt: int = 1,
             max_attempts: int = 3, reversible: bool = True,
             details: dict[str, Any] | None = None) -> FailureRecord:
    """Classify an exception or message into the failure taxonomy."""
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
        extra = {"type": type(error).__name__}
        code = getattr(error, "code", "")
        if code:
            extra["code"] = code
    else:
        text = str(error)
        extra = {}
    lowered = text.lower()
    failure_class = FailureClass.UNKNOWN
    if isinstance(error, BaseException):
        type_name = type(error).__name__.lower()
        for candidate, needles in _TYPE_PATTERNS:
            if any(needle in type_name for needle in needles):
                failure_class = candidate
                break
    if failure_class is FailureClass.UNKNOWN:
        for candidate, needles in _PATTERNS:
            if any(needle in lowered for needle in needles):
                failure_class = candidate
                break
    return FailureRecord(message=text, class_=failure_class, operation=operation, attempt=attempt,
                         max_attempts=max_attempts, reversible=reversible,
                         details={**extra, **(details or {})})
