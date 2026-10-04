"""Encrypted credential vault.

The database stores **metadata in the clear** (so the UI can list credentials, scopes and rotation
dates) and **secrets only as ciphertext**. There is no plaintext credential path: ``get_secret``
is the single decrypting entry point and it registers the value with the secret sanitiser so it
can never be logged afterwards.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import CredentialError, NotFoundError, new_id, sha256_text
from ..core.clock import iso
from ..core.paths import get_paths
from ..events.sanitizer import get_sanitizer
from .crypto import SecretBox, SealedSecret, get_master_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    ref           TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    provider      TEXT NOT NULL DEFAULT '',
    label         TEXT NOT NULL DEFAULT '',
    scopes        TEXT NOT NULL DEFAULT '[]',
    metadata      TEXT NOT NULL DEFAULT '{}',
    fingerprint   TEXT NOT NULL DEFAULT '',
    version       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    rotated_at    TEXT NOT NULL DEFAULT '',
    last_used_at  TEXT NOT NULL DEFAULT '',
    expires_at    TEXT NOT NULL DEFAULT '',
    revoked       INTEGER NOT NULL DEFAULT 0,
    envelope      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS credential_versions (
    id          TEXT PRIMARY KEY,
    ref         TEXT NOT NULL,
    version     INTEGER NOT NULL,
    envelope    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    rotated_out INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cred_versions ON credential_versions(ref);
"""


@dataclass
class CredentialMetadata:
    """Everything about a credential that is safe to display."""

    ref: str
    kind: str
    provider: str = ""
    label: str = ""
    scopes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    rotated_at: str = ""
    last_used_at: str = ""
    expires_at: str = ""
    revoked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref, "kind": self.kind, "provider": self.provider, "label": self.label,
            "scopes": self.scopes, "metadata": self.metadata, "fingerprint": self.fingerprint,
            "version": self.version, "created_at": self.created_at, "updated_at": self.updated_at,
            "rotated_at": self.rotated_at, "last_used_at": self.last_used_at,
            "expires_at": self.expires_at, "revoked": self.revoked,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "CredentialMetadata":
        return cls(
            ref=row["ref"], kind=row["kind"], provider=row["provider"], label=row["label"],
            scopes=json.loads(row["scopes"] or "[]"), metadata=json.loads(row["metadata"] or "{}"),
            fingerprint=row["fingerprint"], version=int(row["version"]), created_at=row["created_at"],
            updated_at=row["updated_at"], rotated_at=row["rotated_at"], last_used_at=row["last_used_at"],
            expires_at=row["expires_at"], revoked=bool(row["revoked"]),
        )


class CredentialVault:
    """Encrypted-at-rest store for secrets and OAuth token sets."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        box: SecretBox | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.box = box or SecretBox()
        self._own_connection = connection is None
        if connection is not None:
            self._conn = connection
            self.path = Path(":memory:")
        else:
            target = Path(path) if path else get_paths().ensure().db_path("vault.db")
            target.parent.mkdir(parents=True, exist_ok=True)
            self.path = target
            self._conn = sqlite3.connect(target, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()

    # -- helpers --------------------------------------------------------------- #
    @staticmethod
    def normalize_ref(reference: str) -> str:
        text = (reference or "").strip()
        if not text:
            raise CredentialError("credential reference is required")
        if text.startswith("credential://"):
            return text
        if text.startswith(("vault://", "env://", "keyring://")):
            return "credential://" + text.split("://", 1)[1]
        return "credential://" + text

    # -- writes ---------------------------------------------------------------- #
    def put(
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
    ) -> CredentialMetadata:
        ref = self.normalize_ref(reference)
        if not secret:
            raise CredentialError("refusing to store an empty secret")
        payload = secret if isinstance(secret, dict) else {"value": secret}
        envelope = self.box.encrypt_json(payload).to_json()
        fingerprint = sha256_text(json.dumps(payload, sort_keys=True))[:16]
        now = iso()
        with self._lock:
            existing = self._conn.execute("SELECT version, envelope FROM credentials WHERE ref = ?", (ref,)).fetchone()
            version = 1
            if existing:
                version = int(existing["version"]) + 1
                self._conn.execute(
                    "INSERT INTO credential_versions (id, ref, version, envelope, created_at, rotated_out)"
                    " VALUES (?,?,?,?,?,1)",
                    (new_id("cv"), ref, int(existing["version"]), existing["envelope"], now),
                )
            self._conn.execute(
                "INSERT INTO credentials (ref, kind, provider, label, scopes, metadata, fingerprint, version,"
                " created_at, updated_at, rotated_at, expires_at, revoked, envelope)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)"
                " ON CONFLICT(ref) DO UPDATE SET kind=excluded.kind, provider=excluded.provider,"
                " label=excluded.label, scopes=excluded.scopes, metadata=excluded.metadata,"
                " fingerprint=excluded.fingerprint, version=excluded.version, updated_at=excluded.updated_at,"
                " rotated_at=excluded.rotated_at, expires_at=excluded.expires_at, revoked=0,"
                " envelope=excluded.envelope",
                (
                    ref, kind, provider, label, json.dumps(scopes or []), json.dumps(metadata or {}),
                    fingerprint, version, now, now, now if existing else "", expires_at, envelope,
                ),
            )
            self._conn.commit()
        return self.metadata(ref)

    def rotate(self, reference: str, new_secret: str | dict[str, Any]) -> CredentialMetadata:
        ref = self.normalize_ref(reference)
        meta = self.metadata(ref)
        return self.put(
            ref, new_secret, kind=meta.kind, provider=meta.provider, label=meta.label,
            scopes=meta.scopes, metadata=meta.metadata, expires_at=meta.expires_at,
        )

    def revoke(self, reference: str) -> None:
        ref = self.normalize_ref(reference)
        with self._lock:
            cursor = self._conn.execute("UPDATE credentials SET revoked = 1, updated_at = ? WHERE ref = ?", (iso(), ref))
            self._conn.commit()
        if cursor.rowcount == 0:
            raise NotFoundError(f"credential {ref} not found")

    def delete(self, reference: str, *, purge_versions: bool = False) -> None:
        ref = self.normalize_ref(reference)
        with self._lock:
            self._conn.execute("DELETE FROM credentials WHERE ref = ?", (ref,))
            if purge_versions:
                self._conn.execute("DELETE FROM credential_versions WHERE ref = ?", (ref,))
            self._conn.commit()

    def rewrap(self, *, old_key: bytes, new_key: bytes) -> int:
        """Re-encrypt every secret from *old_key* to *new_key* (master key rotation)."""
        old_box, new_box = SecretBox(old_key), SecretBox(new_key)
        count = 0
        with self._lock:
            for row in self._conn.execute("SELECT ref, envelope FROM credentials").fetchall():
                sealed = SealedSecret.from_json(row["envelope"])
                rotated = new_box.encrypt_bytes(old_box.decrypt_bytes(sealed))
                self._conn.execute("UPDATE credentials SET envelope = ? WHERE ref = ?", (rotated.to_json(), row["ref"]))
                count += 1
            self._conn.commit()
        return count

    # -- reads ----------------------------------------------------------------- #
    def exists(self, reference: str) -> bool:
        ref = self.normalize_ref(reference)
        row = self._conn.execute("SELECT revoked FROM credentials WHERE ref = ?", (ref,)).fetchone()
        return row is not None and not row["revoked"]

    def metadata(self, reference: str) -> CredentialMetadata:
        ref = self.normalize_ref(reference)
        row = self._conn.execute("SELECT * FROM credentials WHERE ref = ?", (ref,)).fetchone()
        if row is None:
            raise NotFoundError(f"credential {ref} not found")
        return CredentialMetadata.from_row(row)

    def list_metadata(self, *, provider: str = "", include_revoked: bool = False) -> list[CredentialMetadata]:
        clauses, params = [], []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if not include_revoked:
            clauses.append("revoked = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(f"SELECT * FROM credentials {where} ORDER BY ref", params).fetchall()
        return [CredentialMetadata.from_row(row) for row in rows]

    def get_secret(self, reference: str, *, mark_used: bool = True) -> dict[str, Any]:
        """Decrypt a credential. The only path that yields plaintext."""
        ref = self.normalize_ref(reference)
        row = self._conn.execute("SELECT envelope, revoked FROM credentials WHERE ref = ?", (ref,)).fetchone()
        if row is None:
            raise NotFoundError(f"credential {ref} not found")
        if row["revoked"]:
            raise CredentialError(f"credential {ref} has been revoked")
        payload = self.box.decrypt_json(SealedSecret.from_json(row["envelope"]))
        secret_value = payload.get("value", "")
        if isinstance(secret_value, str) and secret_value:
            get_sanitizer().register(secret_value, reference=ref)
        for key, value in payload.items():
            if isinstance(value, str) and key in {"access_token", "refresh_token", "client_secret", "private_key"}:
                get_sanitizer().register(value, reference=ref)
        if mark_used:
            with self._lock:
                self._conn.execute("UPDATE credentials SET last_used_at = ? WHERE ref = ?", (iso(), ref))
                self._conn.commit()
        return payload

    def version_history(self, reference: str) -> list[dict[str, Any]]:
        ref = self.normalize_ref(reference)
        rows = self._conn.execute(
            "SELECT id, version, created_at, rotated_out FROM credential_versions WHERE ref = ? ORDER BY version DESC",
            (ref,),
        ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        total = int(self._conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"])
        revoked = int(self._conn.execute("SELECT COUNT(*) AS n FROM credentials WHERE revoked = 1").fetchone()["n"])
        return {
            "total": total, "active": total - revoked, "revoked": revoked,
            "algorithm": self.box.encryption_algorithm(), "path": str(self.path),
        }

    def close(self) -> None:
        if self._own_connection:
            self._conn.close()


_VAULTS: dict[str, CredentialVault] = {}
_LOCK = threading.Lock()


def get_vault() -> CredentialVault:
    key = str(get_paths().ensure().db_path("vault.db"))
    with _LOCK:
        if key not in _VAULTS:
            _VAULTS[key] = CredentialVault(box=SecretBox(get_master_key()))
        return _VAULTS[key]


def reset_vaults() -> None:
    with _LOCK:
        for vault in _VAULTS.values():
            vault.close()
        _VAULTS.clear()
