"""Hashing, canonical serialisation and signing primitives.

The event log chains events with :func:`chain_hash`; approvals are signed with
:func:`hmac_sign`; artifacts are addressed by :func:`sha256_file`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

_CHUNK = 1 << 20


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace, UTF-8 safe.

    Used for hashing and signing so that logically-equal payloads always produce
    identical bytes regardless of dict insertion order.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def hmac_sign(secret: bytes, payload: Any, *, algorithm: str = "sha256") -> str:
    """Keyed signature over the canonical form of *payload*."""
    return hmac.new(secret, canonical_json(payload).encode("utf-8"), getattr(hashlib, algorithm)).hexdigest()


def hmac_verify(secret: bytes, payload: Any, signature: str) -> bool:
    """Constant-time verification of :func:`hmac_sign` output."""
    try:
        expected = hmac_sign(secret, payload)
    except Exception:
        return False
    return constant_time_eq(expected, signature or "")


def chain_hash(previous_hash: str, payload: Any) -> str:
    """Hash linking an event to its predecessor (Merkle-ish append-only chain)."""
    return sha256_text(f"{previous_hash}|{canonical_json(payload)}")


def constant_time_eq(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def derive_key(material: bytes, *, salt: bytes, length: int = 32, info: bytes = b"natasha") -> bytes:
    """HKDF-SHA256 key derivation (stdlib only)."""
    prk = hmac.new(salt, material, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]
