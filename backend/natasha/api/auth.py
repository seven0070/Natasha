"""Owner authentication for the local API.

One owner, one passphrase, no accounts, no cloud identity. The passphrase is stored as a scrypt hash;
sessions are random opaque tokens kept in SQLite with an expiry. Nothing here is a substitute for the
policy engine - it decides *who* is talking, the policy engine decides what they may do.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import ConflictError, ValidationError, get_paths
from ..core.clock import iso
from ..events import EventKind, get_event_log

#: scrypt parameters: deliberately slow enough to hurt guessing, fast enough for a desktop login.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
MIN_PASSPHRASE = 8
#: Sessions are invalidated after this long without use.
DEFAULT_SESSION_TTL_MINUTES = 720

SCHEMA = """
CREATE TABLE IF NOT EXISTS owner (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner_id TEXT NOT NULL,
    salt BLOB NOT NULL,
    verifier BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    expires_at REAL NOT NULL,
    client TEXT NOT NULL DEFAULT ''
);
"""


@dataclass
class Session:
    token: str
    owner_id: str
    created_at: str
    last_seen: str
    expires_at: float
    client: str = ""

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {"owner_id": self.owner_id, "created_at": self.created_at, "last_seen": self.last_seen,
                "expires_at": self.expires_at, "client": self.client,
                "token_prefix": self.token[:6]}


class AuthError(Exception):
    """Authentication failed (wrong passphrase, missing session, expired session)."""


def hash_passphrase(passphrase: str, *, salt: bytes | None = None) -> tuple[bytes, bytes]:
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise ValidationError(f"the owner passphrase must be at least {MIN_PASSPHRASE} characters")
    salt = salt or os.urandom(16)
    verifier = hashlib.scrypt(passphrase.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return salt, verifier


class AuthManager:
    """Owner credentials and sessions."""

    def __init__(self, *, db_path: str | Path | None = None, log: Any = None,
                 session_ttl_minutes: int = DEFAULT_SESSION_TTL_MINUTES, owner_id: str = "owner") -> None:
        target = Path(db_path) if db_path else get_paths().ensure().db_path("auth.db")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path = target
        self.log = log or get_event_log()
        self.session_ttl_minutes = max(5, int(session_ttl_minutes))
        self.owner_id = owner_id
        self._conn = sqlite3.connect(target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._conn:
            self._conn.executescript(SCHEMA)
        self._failed_attempts: dict[str, list[float]] = {}

    # ------------------------------------------------------------------ owner
    def initialised(self) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM owner WHERE id = 1").fetchone() is not None

    def setup(self, passphrase: str, *, owner_id: str = "") -> str:
        """Create the owner identity. Refuses to overwrite an existing one."""
        with self._lock:
            if self.initialised():
                raise ConflictError("the owner is already initialised; use login, or reset deliberately")
            salt, verifier = hash_passphrase(passphrase)
            self.owner_id = owner_id or self.owner_id
            with self._conn:
                self._conn.execute("INSERT INTO owner (id, owner_id, salt, verifier, created_at) VALUES (1, ?, ?, ?, ?)",
                                   (self.owner_id, salt, verifier, iso()))
        self.log.append(EventKind.SECURITY, {"action": "owner_initialised", "owner_id": self.owner_id},
                        actor="owner", source="api.auth", risk="MEDIUM")  # type: ignore[arg-type]
        return self.login(passphrase, client="setup")

    def change_passphrase(self, current: str, new: str) -> None:
        self.authenticate(current)
        salt, verifier = hash_passphrase(new)
        with self._lock, self._conn:
            self._conn.execute("UPDATE owner SET salt = ?, verifier = ? WHERE id = 1", (salt, verifier))
            self._conn.execute("DELETE FROM sessions")  # every other session dies with the old secret
        self.log.append(EventKind.SECURITY, {"action": "passphrase_changed"}, actor="owner",
                        source="api.auth", risk="HIGH")  # type: ignore[arg-type]

    def authenticate(self, passphrase: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT owner_id, salt, verifier FROM owner WHERE id = 1").fetchone()
        if row is None:
            raise AuthError("Natasha has no owner yet; complete setup first")
        _, verifier = hash_passphrase(passphrase, salt=bytes(row["salt"]))
        if not hmac.compare_digest(verifier, bytes(row["verifier"])):
            self.log.append(EventKind.SECURITY, {"action": "login_failed"}, actor="unknown",
                            source="api.auth", risk="MEDIUM")  # type: ignore[arg-type]
            raise AuthError("incorrect passphrase")
        return row["owner_id"]

    # ------------------------------------------------------------------ sessions
    def login(self, passphrase: str, *, client: str = "") -> str:
        owner_id = self.authenticate(passphrase)
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO sessions (token, owner_id, created_at, last_seen, expires_at, client)"
                               " VALUES (?, ?, ?, ?, ?, ?)",
                               (token, owner_id, iso(), iso(), now + self.session_ttl_minutes * 60, client[:80]))
        self.log.append(EventKind.SECURITY, {"action": "login", "client": client[:80]}, actor="owner",
                        source="api.auth", risk="LOW")  # type: ignore[arg-type]
        return token

    def session(self, token: str) -> Session | None:
        if not token:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        if row is None:
            return None
        session = Session(token=row["token"], owner_id=row["owner_id"], created_at=row["created_at"],
                          last_seen=row["last_seen"], expires_at=row["expires_at"], client=row["client"])
        if session.expired:
            self.logout(token)
            return None
        with self._lock, self._conn:
            self._conn.execute("UPDATE sessions SET last_seen = ? WHERE token = ?", (iso(), token))
        return session

    def logout(self, token: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
        return [Session(token=row["token"], owner_id=row["owner_id"], created_at=row["created_at"],
                        last_seen=row["last_seen"], expires_at=row["expires_at"],
                        client=row["client"]).to_dict() for row in rows]

    def revoke_all(self, *, keep: str = "") -> int:
        with self._lock, self._conn:
            if keep:
                cursor = self._conn.execute("DELETE FROM sessions WHERE token != ?", (keep,))
            else:
                cursor = self._conn.execute("DELETE FROM sessions")
            removed = cursor.rowcount or 0
        self.log.append(EventKind.SECURITY, {"action": "sessions_revoked", "count": removed},
                        actor="owner", source="api.auth", risk="HIGH")  # type: ignore[arg-type]
        return removed

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_MANAGER: AuthManager | None = None
_LOCK = threading.Lock()


def get_auth_manager(**kwargs: Any) -> AuthManager:
    global _MANAGER
    with _LOCK:
        if _MANAGER is None:
            _MANAGER = AuthManager(**kwargs)
        return _MANAGER


def reset_auth_manager() -> None:
    global _MANAGER
    with _LOCK:
        if _MANAGER is not None:
            _MANAGER.close()
        _MANAGER = None
