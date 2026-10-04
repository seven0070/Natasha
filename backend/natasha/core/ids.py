"""Identifier generation.

Ids are sortable-ish (time prefix) and namespaced by kind, e.g. ``evt_1f0c...``.
They are opaque: never parse an id, always look it up.
"""

from __future__ import annotations

import secrets
import time
from uuid import uuid4

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(value: int, width: int = 0) -> str:
    if value == 0:
        out = "0"
    else:
        digits = []
        while value:
            value, rem = divmod(value, 36)
            digits.append(_ALPHABET[rem])
        out = "".join(reversed(digits))
    return out.rjust(width, "0")


def new_id(prefix: str = "") -> str:
    """Return a new unique id, optionally namespaced with ``prefix``."""
    stamp = _b36(int(time.time() * 1000), 8)
    rand = _b36(secrets.randbits(64), 13)
    body = f"{stamp}{rand}"
    return f"{prefix}_{body}" if prefix else body


def short_id(value: str, length: int = 8) -> str:
    """Human-facing abbreviation of an id."""
    core = value.split("_", 1)[1] if "_" in value else value
    return core[:length]


def uuid7_like() -> str:
    """UUID4 string; kept as a named helper so call sites are greppable."""
    return str(uuid4())
