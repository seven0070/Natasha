"""Master-key handling and authenticated encryption.

Master key resolution order:

1. ``$NATASHA_MASTER_KEY`` (hex, base64 or passphrase - passphrases are stretched with scrypt)
2. ``$NATASHA_HOME/keys/master.key`` (32 random bytes, mode 0600, generated on first init)
3. failure - Natasha refuses to store credentials rather than storing them weakly.

Encryption is AES-256-GCM (via ``cryptography``) with a per-message random 96-bit nonce and
HKDF-SHA256 subkey derivation. A clearly-labelled stdlib fallback (SHAKE256 keystream +
HMAC-SHA256 encrypt-then-MAC) is used only when ``cryptography`` is unavailable, so the vault
degrades rather than breaking - and says so in the envelope.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
import os
import secrets
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core import ConfigurationError, CredentialError, derive_key
from ..core.paths import get_paths

MASTER_KEY_ENV = "NATASHA_MASTER_KEY"
KEY_BYTES = 32
ENVELOPE_VERSION = 1

try:  # pragma: no cover - exercised by whichever branch is available
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    HAVE_AESGCM = True
except Exception:  # pragma: no cover
    AESGCM = None  # type: ignore[assignment]
    HAVE_AESGCM = False


# --------------------------------------------------------------------------- #
# master key
# --------------------------------------------------------------------------- #
def _decode_material(raw: str) -> bytes | None:
    """Interpret an env-provided key as hex/base64 when it looks like one."""
    text = raw.strip()
    try:
        if len(text) in {64, 128} and all(c in "0123456789abcdefABCDEF" for c in text):
            return bytes.fromhex(text)
    except ValueError:
        pass
    try:
        if len(text) >= 40 and text.endswith("=") or (len(text) % 4 == 0 and len(text) >= 44):
            padded = text + "=" * (-len(text) % 4)
            decoded = base64.urlsafe_b64decode(padded)
            if len(decoded) >= 16:
                return decoded
    except Exception:
        pass
    return None


def derive_from_passphrase(passphrase: str, salt: bytes) -> bytes:
    """scrypt stretch for human-memorable master keys."""
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=KEY_BYTES)


def _key_file() -> Path:
    return get_paths().ensure().keys / "master.key"


def load_or_create_master_key(*, allow_create: bool = True) -> bytes:
    """Read the master key, generating one on first run."""
    env = os.environ.get(MASTER_KEY_ENV, "").strip()
    if env:
        decoded = _decode_material(env)
        if decoded and len(decoded) >= KEY_BYTES:
            return decoded[:KEY_BYTES]
        salt_file = get_paths().ensure().keys / "master.salt"
        if not salt_file.exists():
            salt_file.write_bytes(secrets.token_bytes(16))
            _chmod(salt_file)
        return derive_from_passphrase(env, salt_file.read_bytes())

    path = _key_file()
    if path.exists():
        raw = path.read_bytes()
        if len(raw) < KEY_BYTES:
            raise ConfigurationError(f"master key file {path} is truncated ({len(raw)} bytes)")
        return raw[:KEY_BYTES]
    if not allow_create:
        raise ConfigurationError(
            f"no master key found: set ${MASTER_KEY_ENV} or run `natasha init` to create {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(KEY_BYTES)
    path.write_bytes(key)
    _chmod(path)
    return key


def _chmod(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - windows
        pass


_MASTER: bytes | None = None
_LOCK = threading.Lock()


def get_master_key() -> bytes:
    """Process-wide master key."""
    global _MASTER
    if _MASTER is None:
        with _LOCK:
            if _MASTER is None:
                _MASTER = load_or_create_master_key()
    return _MASTER


def set_master_key(key: bytes, *, persist: bool = False) -> None:
    """Inject a master key (tests, or after rotation)."""
    global _MASTER
    if len(key) < 16:
        raise CredentialError("master key too short")
    with _LOCK:
        _MASTER = key
        if persist:
            path = _key_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(key)
            _chmod(path)


def clear_master_key_cache() -> None:
    global _MASTER
    with _LOCK:
        _MASTER = None


def rotate_master_key(*, new_key: bytes | None = None, rewrap: bool = True) -> bytes:
    """Rotate the master key, re-wrapping every stored credential envelope."""
    old = get_master_key()
    fresh = new_key or secrets.token_bytes(KEY_BYTES)
    if rewrap:
        from .vault import CredentialVault

        CredentialVault().rewrap(old_key=old, new_key=fresh)
    path = _key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fresh)
    _chmod(path)
    old_backup = path.with_suffix(".key.bak")
    old_backup.write_bytes(old)
    _chmod(old_backup)
    set_master_key(fresh)
    return fresh


# --------------------------------------------------------------------------- #
# authenticated encryption
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SealedSecret:
    """A versioned ciphertext envelope; safe to store and to log."""

    envelope: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(self.envelope, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "SealedSecret":
        return cls(json.loads(raw))


class SecretBox:
    """Encrypt/decrypt small secrets with a master key."""

    def __init__(self, master_key: bytes | None = None, *, key_id: str = "master") -> None:
        self._key = master_key or get_master_key()
        if len(self._key) < 16:
            raise CredentialError("master key must be at least 16 bytes")
        self.key_id = key_id

    # -- key derivation -------------------------------------------------------- #
    def _subkey(self, salt: bytes, info: bytes) -> bytes:
        return derive_key(self._key, salt=salt, length=32, info=info)

    # -- api ------------------------------------------------------------------- #
    def encrypt_bytes(self, plaintext: bytes, *, aad: bytes = b"") -> SealedSecret:
        salt = secrets.token_bytes(16)
        if HAVE_AESGCM:
            nonce = secrets.token_bytes(12)
            subkey = self._subkey(salt, b"natasha:aesgcm:v1")
            ciphertext = AESGCM(subkey).encrypt(nonce, plaintext, aad or None)  # type: ignore[misc]
            envelope = {
                "v": ENVELOPE_VERSION, "alg": "AES-256-GCM", "kid": self.key_id,
                "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(),
                "ct": base64.b64encode(ciphertext).decode(),
            }
        else:
            nonce = secrets.token_bytes(16)
            enc_key = self._subkey(salt, b"natasha:shake:v1:enc")
            mac_key = self._subkey(salt, b"natasha:shake:v1:mac")
            ciphertext = _shake_keystream(enc_key, nonce, len(plaintext))
            body = bytes(a ^ b for a, b in zip(plaintext, ciphertext, strict=True))
            tag = hmac_mod.new(mac_key, nonce + aad + body, hashlib.sha256).hexdigest()
            envelope = {
                "v": ENVELOPE_VERSION, "alg": "SHAKE256-keystream+HMAC-SHA256 (portable fallback)",
                "kid": self.key_id, "salt": base64.b64encode(salt).decode(),
                "nonce": base64.b64encode(nonce).decode(), "ct": base64.b64encode(body).decode(), "tag": tag,
            }
        return SealedSecret(envelope)

    def decrypt_bytes(self, sealed: SealedSecret | dict[str, Any] | str, *, aad: bytes = b"") -> bytes:
        envelope = _as_envelope(sealed)
        version = int(envelope.get("v", 0))
        if version != ENVELOPE_VERSION:
            raise CredentialError(f"unsupported envelope version {version}")
        salt = base64.b64decode(envelope["salt"])
        nonce = base64.b64decode(envelope["nonce"])
        body = base64.b64decode(envelope["ct"])
        alg = str(envelope.get("alg", ""))
        if alg.startswith("AES-256-GCM"):
            if not HAVE_AESGCM:  # pragma: no cover
                raise CredentialError("envelope requires the `cryptography` package to decrypt")
            subkey = self._subkey(salt, b"natasha:aesgcm:v1")
            try:
                return AESGCM(subkey).decrypt(nonce, body, aad or None)  # type: ignore[misc]
            except Exception as exc:
                raise CredentialError("decryption failed (wrong key or tampered ciphertext)") from exc
        enc_key = self._subkey(salt, b"natasha:shake:v1:enc")
        mac_key = self._subkey(salt, b"natasha:shake:v1:mac")
        expected = hmac_mod.new(mac_key, nonce + aad + body, hashlib.sha256).hexdigest()
        if not hmac_mod.compare_digest(expected, str(envelope.get("tag", ""))):
            raise CredentialError("authentication tag mismatch (wrong key or tampered ciphertext)")
        return bytes(a ^ b for a, b in zip(body, _shake_keystream(enc_key, nonce, len(body)), strict=True))

    def encrypt_json(self, payload: dict[str, Any], *, aad: bytes = b"") -> SealedSecret:
        return self.encrypt_bytes(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(), aad=aad)

    def decrypt_json(self, sealed: SealedSecret | dict[str, Any] | str, *, aad: bytes = b"") -> dict[str, Any]:
        return json.loads(self.decrypt_bytes(sealed, aad=aad).decode("utf-8"))

    def rewrap(self, sealed: SealedSecret | dict[str, Any] | str, *, old_key: bytes) -> SealedSecret:
        """Re-encrypt an envelope from an old master key to this box's key."""
        plaintext = SecretBox(old_key).decrypt_bytes(sealed)
        return self.encrypt_bytes(plaintext)

    def encryption_algorithm(self) -> str:
        return "AES-256-GCM" if HAVE_AESGCM else "SHAKE256-keystream+HMAC-SHA256"


def _as_envelope(sealed: SealedSecret | dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(sealed, SealedSecret):
        return sealed.envelope
    if isinstance(sealed, str):
        return json.loads(sealed)
    if isinstance(sealed, dict):
        return sealed
    raise CredentialError(f"cannot interpret envelope of type {type(sealed).__name__}")


def _shake_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """Portable keystream: SHAKE256 over key||nonce, expanded as needed."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        shake = hashlib.shake_256(key + nonce + counter.to_bytes(4, "big"))
        out += shake.digest(64)
        counter += 1
    return bytes(out[:length])
