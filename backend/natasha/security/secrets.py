"""Secret isolation helpers: references, redaction and process hygiene."""

from __future__ import annotations

import os
import re
from typing import Any

from ..core import CredentialError
from ..core.hashing import sha256_text
from .injection import ContentTrust, ExternalContent

REFERENCE_SCHEMES = ("credential://", "env://", "vault://", "keyring://", "file://")
_REFERENCE = re.compile(r"^(credential|vault|keyring|env|file)://[A-Za-z0-9._:@/\-]{1,200}$")


def is_reference(value: str) -> bool:
    """True when *value* points at a secret instead of containing one."""
    return bool(_REFERENCE.match(value or ""))


def require_reference(value: str, *, field_name: str = "value") -> str:
    if not is_reference(value):
        raise CredentialError(
            f"{field_name} must be a secret reference "
            f"({'/'.join(s + '//' for s in REFERENCE_SCHEMES)}); inline secrets are refused"
        )
    return value


def fingerprint(secret: str) -> str:
    """Stable, non-reversible identifier for a secret (rotation bookkeeping)."""
    return sha256_text(secret)[:16]


def resolve_env_reference(reference: str) -> str | None:
    """``env://NAME`` -> ``os.environ['NAME']`` (used only at execution time)."""
    if reference.startswith("env://"):
        name = reference[len("env://"):]
        return os.environ.get(name)
    return None


def scrub_env(names: list[str] | None = None) -> None:
    """Remove secret-bearing variables from the process environment (sandbox hygiene)."""
    targets = names or [
        name for name in os.environ
        if re.search(r"(?i)(api[_-]?key|secret|token|password|credential)", name)
    ]
    for name in targets:
        os.environ.pop(name, None)


def as_untrusted(text: str, source: str) -> ExternalContent:
    """Tool output carrying secrets is still untrusted content."""
    return ExternalContent(text=text, source=source, trust=ContentTrust.TOOL_OUTPUT)


def safe_metadata(data: dict[str, Any]) -> dict[str, Any]:
    """Strip anything resembling a secret from metadata destined for storage/UI."""
    from ..events.sanitizer import get_sanitizer

    return get_sanitizer().sanitize(data)
