"""Security: capability policy, argument validation, injection defense, audit, secret isolation.

External content is *data*, never authority. Nothing a model produces can bypass these checks,
because the policy engine is consulted by the executor, not by the model.
"""

from .audit import AuditTrail
from .injection import (
    ContentTrust,
    ExternalContent,
    InjectionGuard,
    InjectionReport,
    InjectionVerdict,
    TrustedContext,
    detect_injection,
    get_injection_guard,
    mark_external,
    neutralise,
    wrap_untrusted,
)
from .policy import Capability, Decision, Effect, PolicyEngine, PolicyRequest
from .validation import Schema, ValidationIssue, validate_arguments

__all__ = [
    "AuditTrail", "Capability", "Decision", "Effect", "PolicyEngine", "PolicyRequest",
    "ContentTrust", "ExternalContent", "InjectionReport", "InjectionGuard", "InjectionVerdict",
    "TrustedContext", "detect_injection", "get_injection_guard", "mark_external", "neutralise", "wrap_untrusted",
    "Schema", "ValidationIssue", "validate_arguments",
]
