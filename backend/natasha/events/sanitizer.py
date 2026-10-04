"""Secret sanitisation for anything that reaches the event log, logs or model context.

Defence in depth: three independent passes.
1. **Known secrets** - literal values registered by the credential vault are replaced by reference.
2. **Key-name policy** - fields named like secrets are redacted wholesale.
3. **Pattern + entropy** - provider key shapes, JWTs, bearer tokens, private keys and
   high-entropy blobs are redacted even when we never saw the secret before.
"""

from __future__ import annotations

import math
import re
import threading
from typing import Any, Iterable

REDACTED = "[REDACTED]"

_SECRET_KEY_PATTERN = re.compile(
    r"(?i)^(.*[._-])?(api[_-]?key|apikey|secret|token|password|passwd|pwd|credential|credentials|"
    r"authorization|auth|bearer|private[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|"
    r"refresh[_-]?token|session[_-]?key|signing[_-]?key|cookie|set-cookie)([._-].*)?$"
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b")),
    ("groq_key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")),
    ("hf_token", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws_key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,8000}?-----END [A-Z ]*PRIVATE KEY-----")),
    ("dsn", re.compile(r"(?i)\b[a-z][a-z0-9+.\-]{2,20}://[^\s:/@]{1,64}:[^\s:/@]{4,}@")),
)

_URL_CREDENTIALS = re.compile(r"(?i)(https?://)([^/\s:@]+):([^/\s:@]+)@")
_KEYED_QUERY = re.compile(r"(?i)([?&](?:api[_-]?key|key|token|access_token|signature|sig)=)([^&\s]+)")

_SECRET_FIELD_MIN_LEN = 16


def shannon_entropy(text: str) -> float:
    """Entropy in bits per character."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = len(text)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


#: Natasha generates ids like ``mem_mutkfz3z0dicjckhyh98u`` - they are not secrets and must not
#: be redacted, or every tool result referencing an id becomes unusable.
_ID_LIKE = re.compile(r"^[a-z]{2,8}_[0-9a-z]{6,40}$")
_HEX_LIKE = re.compile(r"^(0x)?[0-9a-fA-F]{32,}$")


def looks_like_secret(value: str) -> bool:
    """Heuristic: long, dense, mixed-charset string with no spaces.

    Deliberately conservative about false positives: our own ids, hex digests, URLs and paths are
    excluded, because over-redaction destroys usability and trains people to ignore warnings.
    """
    if len(value) < 32 or " " in value or "\n" in value:
        return False
    if value.startswith(("http://", "https://", "file://", "credential://", "env://")):
        return False
    if _ID_LIKE.match(value) or _HEX_LIKE.match(value):
        return False
    if "/" in value or ":" in value or "@" in value:
        return False
    if value.islower() or value.isupper():
        # Real tokens nearly always mix case or carry a prefix pattern already matched elsewhere.
        return False
    entropy = shannon_entropy(value)
    if entropy < 3.6:
        return False
    classes = sum(
        1
        for test in (str.islower, str.isupper, str.isdigit)
        if any(test(char) for char in value)
    ) + (1 if any(not char.isalnum() for char in value) else 0)
    return classes >= 3 and len({*value}) >= 12


class SecretSanitizer:
    """Thread-safe redactor. Register live secrets so they can never be logged."""

    def __init__(self, *, entropy_check: bool = True) -> None:
        self._known: dict[str, str] = {}
        self._lock = threading.RLock()
        self._entropy_check = entropy_check
        self._allow: list[str] = []

    # -- registry -------------------------------------------------------------- #
    def register(self, secret: str, *, reference: str = REDACTED) -> None:
        """Remember a live secret so it is redacted everywhere it appears."""
        if not secret or len(secret) < 6:
            return
        with self._lock:
            self._known[secret] = reference

    def unregister(self, secret: str) -> None:
        with self._lock:
            self._known.pop(secret, None)

    def clear(self) -> None:
        with self._lock:
            self._known.clear()

    def known_count(self) -> int:
        return len(self._known)

    # -- scanning -------------------------------------------------------------- #
    def contains_secret(self, text: str) -> bool:
        return self._scan(text) != text

    def _scan(self, text: str) -> str:
        if not text:
            return text
        out = text
        with self._lock:
            for secret, reference in sorted(self._known.items(), key=lambda kv: -len(kv[0])):
                if secret in out:
                    out = out.replace(secret, reference)
        for name, pattern in _PATTERNS:
            out = pattern.sub(f"[REDACTED:{name}]", out)
        out = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", out)
        out = _KEYED_QUERY.sub(lambda m: m.group(1) + REDACTED, out)
        return out

    def sanitize_text(self, text: str) -> str:
        """Redact secrets in a plain string."""
        return self._scan(text)

    # -- structured ------------------------------------------------------------ #
    def sanitize(self, value: Any, *, key: str | None = None, depth: int = 0) -> Any:
        """Return a deep, redacted copy of *value*."""
        if depth > 24:
            return "[TRUNCATED:DEPTH]"
        if key is not None and self._is_secret_key(key):
            if isinstance(value, (dict, list)):
                return REDACTED
            if value in (None, "", REDACTED):
                return value
            return REDACTED
        if isinstance(value, dict):
            return {k: self.sanitize(v, key=str(k), depth=depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            items = [self.sanitize(item, key=key, depth=depth + 1) for item in value]
            return items if isinstance(value, list) else type(value)(items)
        if isinstance(value, bytes):
            return f"[{len(value)} bytes]"
        if isinstance(value, str):
            if self._entropy_check and looks_like_secret(value) and not self._is_reference(value):
                return "[REDACTED:high-entropy]"
            return self._scan(value)
        return value

    @staticmethod
    def _is_reference(value: str) -> bool:
        return value.startswith(("credential://", "env://", "vault://", "file://", "keyring://"))

    @staticmethod
    def _is_secret_key(key: str) -> bool:
        if not key:
            return False
        if key.startswith("credential_ref") or key.endswith("_ref"):
            return False  # references are safe to keep
        return bool(_SECRET_KEY_PATTERN.match(key))


_SANITIZER: SecretSanitizer | None = None
_LOCK = threading.Lock()


def get_sanitizer() -> SecretSanitizer:
    """Process-wide sanitizer shared by the vault, log, API and model context builder."""
    global _SANITIZER
    if _SANITIZER is None:
        with _LOCK:
            if _SANITIZER is None:
                _SANITIZER = SecretSanitizer()
    return _SANITIZER


def redact_iter(values: Iterable[str], sanitizer: SecretSanitizer | None = None) -> list[str]:
    san = sanitizer or get_sanitizer()
    return [san.sanitize_text(value) for value in values]
