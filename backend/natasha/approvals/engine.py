"""Approval engine implementation."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import hmac

from ..core import ApprovalDenied, ApprovalRequired, NotFoundError, hmac_sign, hmac_verify, new_id
from ..core.clock import iso, parse_iso, utcnow
from ..core.paths import get_paths
from ..core.risk import RiskLevel
from ..events import EventKind, EventLog, get_event_log
from .models import (
    Approval,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalToken,
    operation_fingerprint,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests (
    id          TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    mission_id  TEXT NOT NULL DEFAULT '',
    trace_id    TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_status ON approval_requests(status);
CREATE INDEX IF NOT EXISTS idx_approval_mission ON approval_requests(mission_id);

CREATE TABLE IF NOT EXISTS approval_tokens (
    id          TEXT PRIMARY KEY,
    request_id  TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT '[]',
    token       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT NOT NULL DEFAULT '',
    revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_token_request ON approval_tokens(request_id);
"""


class ApprovalEngine:
    """Create, decide and consume approvals.

    The signing key is supplied by the caller (normally the credential vault's master key), which
    is what makes grants unforgeable: without the key you cannot mint a token that verifies.
    """

    def __init__(
        self,
        *,
        signing_key: bytes,
        path: str | Path | None = None,
        log: EventLog | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if not signing_key or len(signing_key) < 16:
            raise ValueError("approval signing key must be at least 16 bytes")
        self._key = signing_key
        self._own_connection = connection is None
        if connection is not None:
            self._conn = connection
            self.path = Path(":memory:")
        else:
            target = Path(path) if path else get_paths().ensure().db_path("approvals.db")
            target.parent.mkdir(parents=True, exist_ok=True)
            self.path = target
            self._conn = sqlite3.connect(target, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()
        self.log = log or get_event_log()

    # -- requests -------------------------------------------------------------- #
    def request(
        self,
        operation: str,
        *,
        reason: str,
        risk: RiskLevel | str = RiskLevel.HIGH,
        actor: str = "model:main",
        permissions: list[str] | None = None,
        resources: list[str] | None = None,
        arguments: dict[str, Any] | None = None,
        reversibility: str = "reversible",
        ttl_seconds: int = 900,
        mission_id: str = "",
        step_id: str = "",
        trace_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        """Register a PENDING approval request and emit an approval event."""
        request = ApprovalRequest(
            operation=operation, reason=reason, risk=RiskLevel.parse(risk), actor=actor,
            permissions=permissions or [], resources=resources or [], arguments=arguments or {},
            reversibility=reversibility, ttl_seconds=ttl_seconds, mission_id=mission_id, step_id=step_id,
            trace_id=trace_id, metadata=metadata or {},
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO approval_requests (id, payload, status, created_at, expires_at, mission_id, trace_id, fingerprint)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    request.id, json.dumps(request.to_dict(), default=str), ApprovalStatus.PENDING.value,
                    request.created_at, request.expires_at, request.mission_id, request.trace_id, request.fingerprint,
                ),
            )
            self._conn.commit()
        self.log.append(
            EventKind.APPROVAL,
            {
                "action": "requested", "request_id": request.id, "operation": operation, "reason": reason,
                "permissions": request.permissions, "resources": request.resources, "reversibility": reversibility,
                "expires_at": request.expires_at, "fingerprint": request.fingerprint,
            },
            actor=actor, source="approvals", trace_id=trace_id, mission_id=mission_id, risk=request.risk,
        )
        return request

    def require(
        self,
        operation: str,
        *,
        reason: str,
        risk: RiskLevel | str = RiskLevel.HIGH,
        actor: str = "model:main",
        **kwargs: Any,
    ) -> ApprovalRequest:
        """Register a request and raise :class:`ApprovalRequired` carrying its id."""
        request = self.request(operation, reason=reason, risk=risk, actor=actor, **kwargs)
        raise ApprovalRequired(
            f"{operation} requires owner approval: {reason}",
            request_id=request.id,
            risk=request.risk.name,
            expires_at=request.expires_at,
        )

    def get_request(self, request_id: str) -> ApprovalRequest:
        row = self._conn.execute("SELECT payload FROM approval_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"approval request {request_id} not found")
        return ApprovalRequest.from_dict(json.loads(row["payload"]))

    def status(self, request_id: str) -> ApprovalStatus:
        row = self._conn.execute("SELECT status FROM approval_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"approval request {request_id} not found")
        status = ApprovalStatus(row["status"])
        if status is ApprovalStatus.PENDING and self.get_request(request_id).is_expired():
            self._set_status(request_id, ApprovalStatus.EXPIRED, actor="system", note="ttl elapsed")
            return ApprovalStatus.EXPIRED
        return status

    def pending(self, *, mission_id: str = "", limit: int = 100) -> list[ApprovalRequest]:
        clause = "WHERE status = ?"
        params: list[Any] = [ApprovalStatus.PENDING.value]
        if mission_id:
            clause += " AND mission_id = ?"
            params.append(mission_id)
        rows = self._conn.execute(
            f"SELECT payload FROM approval_requests {clause} ORDER BY created_at DESC LIMIT ?", (*params, limit)
        ).fetchall()
        out = []
        for row in rows:
            request = ApprovalRequest.from_dict(json.loads(row["payload"]))
            if request.is_expired():
                self._set_status(request.id, ApprovalStatus.EXPIRED, actor="system", note="ttl elapsed")
                continue
            out.append(request)
        return out

    def approved_requests(self, *, operation: str = "", actor: str = "",
                          limit: int = 50) -> list[ApprovalRequest]:
        """Grants the owner has already given that nothing has consumed yet.

        Used to *reuse* an approval for exactly the action it was granted for. The fingerprint is
        re-checked by :meth:`ApprovalRequest.matches` at the call site, so an approval can never be
        widened to a different tool, argument set or resource.
        """
        clause = ("WHERE r.status = ? AND EXISTS (SELECT 1 FROM approval_tokens t WHERE t.request_id = r.id "
                  "AND t.used_at = '' AND t.revoked = 0)")
        params: list[Any] = [ApprovalStatus.APPROVED.value]
        if operation:
            clause += " AND json_extract(r.payload, '$.operation') = ?"
            params.append(operation)
        rows = self._conn.execute(
            f"SELECT r.payload FROM approval_requests r {clause} ORDER BY r.created_at DESC LIMIT ?",
            (*params, max(1, min(limit, 500))),
        ).fetchall()
        out: list[ApprovalRequest] = []
        for row in rows:
            request = ApprovalRequest.from_dict(json.loads(row["payload"]))
            if request.is_expired():
                self._set_status(request.id, ApprovalStatus.EXPIRED, actor="system", note="ttl elapsed")
                continue
            if actor and request.actor != actor:
                continue
            out.append(request)
        return out

    def history(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload, status FROM approval_requests ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for row in rows:
            data = json.loads(row["payload"])
            data["status"] = row["status"]
            out.append(data)
        return out

    # -- decisions ------------------------------------------------------------- #
    def approve(
        self,
        request_id: str,
        *,
        decided_by: str = "owner",
        note: str = "",
        scope: list[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> ApprovalToken:
        """Approve a pending request. Only the owner actor may approve."""
        if decided_by.split(":", 1)[0].lower() not in {"owner", "user", "human", "local-owner"}:
            self.log.append(
                EventKind.SECURITY,
                {"action": "approval.self_approval_attempt", "request_id": request_id, "actor": decided_by},
                actor=decided_by, source="approvals", risk=RiskLevel.CRITICAL,
            )
            raise ApprovalDenied(f"actor {decided_by!r} may not approve its own request")
        request = self.get_request(request_id)
        with self._lock:
            current = self.status(request_id)
            if current is ApprovalStatus.EXPIRED:
                raise ApprovalDenied(f"approval request {request_id} expired at {request.expires_at}")
            if current is not ApprovalStatus.PENDING:
                raise ApprovalDenied(f"approval request {request_id} is {current.value}")
            ttl = ttl_seconds or request.ttl_seconds
            expires_at = iso(utcnow() + timedelta(seconds=max(30, min(ttl, 86_400))))
            token = ApprovalToken(
                request_id=request_id,
                fingerprint=request.fingerprint,
                granted_by=decided_by,
                granted_at=iso(),
                expires_at=expires_at,
                scope=sorted(scope or request.permissions),
            )
            token.signature = hmac_sign(self._key, token.payload)
            self._conn.execute(
                "INSERT INTO approval_tokens (id, request_id, fingerprint, scope, token, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    token.id, token.request_id, token.fingerprint, json.dumps(token.scope),
                    json.dumps(token.to_dict(), default=str), token.granted_at, token.expires_at,
                ),
            )
            self._conn.execute(
                "UPDATE approval_requests SET status = ? WHERE id = ?", (ApprovalStatus.APPROVED.value, request_id)
            )
            self._conn.commit()
        self.log.append(
            EventKind.APPROVAL,
            {"action": "approved", "request_id": request_id, "operation": request.operation, "scope": token.scope,
             "token_id": token.id, "expires_at": token.expires_at, "note": note},
            actor=decided_by, source="approvals", trace_id=request.trace_id, mission_id=request.mission_id,
            risk=request.risk,
        )
        return token

    def deny(self, request_id: str, *, decided_by: str = "owner", note: str = "") -> Approval:
        request = self.get_request(request_id)
        self._set_status(request_id, ApprovalStatus.DENIED, actor=decided_by, note=note)
        return Approval(request_id=request_id, status=ApprovalStatus.DENIED, decided_by=decided_by,
                        decided_at=iso(), note=note)

    def cancel(self, request_id: str, *, actor: str = "system", note: str = "") -> Approval:
        self.get_request(request_id)
        self._set_status(request_id, ApprovalStatus.CANCELLED, actor=actor, note=note)
        return Approval(request_id=request_id, status=ApprovalStatus.CANCELLED, decided_by=actor,
                        decided_at=iso(), note=note)

    def _set_status(self, request_id: str, status: ApprovalStatus, *, actor: str, note: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE approval_requests SET status = ? WHERE id = ?", (status.value, request_id))
            self._conn.commit()
        self.log.append(
            EventKind.APPROVAL,
            {"action": status.value.lower(), "request_id": request_id, "note": note},
            actor=actor, source="approvals", risk=RiskLevel.LOW,
        )

    def expire_stale(self) -> int:
        """Mark elapsed pending requests EXPIRED. Safe to call periodically."""
        count = 0
        for row in self._conn.execute(
            "SELECT id, payload FROM approval_requests WHERE status = ?", (ApprovalStatus.PENDING.value,)
        ).fetchall():
            request = ApprovalRequest.from_dict(json.loads(row["payload"]))
            if request.is_expired():
                self._set_status(request.id, ApprovalStatus.EXPIRED, actor="system", note="ttl elapsed")
                count += 1
        return count

    # -- consumption ----------------------------------------------------------- #
    def consume(
        self,
        request_id: str,
        operation: str = "",
        arguments: dict[str, Any] | None = None,
        *,
        resource: str | None = None,
        actor: str = "",
    ) -> ApprovalToken:
        """Validate a grant and mark it used.

        The fingerprint is recomputed from the **stored request** unless the caller supplies
        explicit ``operation``/``arguments``/``resource`` - in which case they must match, which is
        how a caller proves it is performing the approved operation and not a wider one.

        Raises :class:`ApprovalDenied` for missing/expired/replayed/forged tokens or a fingerprint
        mismatch - the four ways an approval could otherwise be abused.
        """
        stored = self.get_request(request_id)
        operation = operation or stored.operation
        arguments = stored.arguments if arguments is None else arguments
        if resource is None:
            resource = stored.resources[0] if stored.resources else ""
        expected = operation_fingerprint(operation, arguments, resource=resource)
        rows = self._conn.execute(
            "SELECT token, used_at, revoked, expires_at FROM approval_tokens WHERE request_id = ? ORDER BY created_at DESC",
            (request_id,),
        ).fetchall()
        if not rows:
            raise ApprovalDenied(f"no approval token for request {request_id}")
        last_error = "no usable token"
        for row in rows:
            if row["used_at"]:
                last_error = "token already used (replay rejected)"
                continue
            if row["revoked"]:
                last_error = "token revoked"
                continue
            token = _token_from_json(row["token"])
            if token is None:
                last_error = "token payload is malformed (refused)"
                continue
            if not hmac_verify(self._key, token.payload, token.signature):
                last_error = "token signature invalid (forged or corrupted)"
                continue
            if token.is_expired():
                last_error = f"token expired at {token.expires_at}"
                continue
            if not hmac.compare_digest(token.fingerprint, expected):
                last_error = (
                    "token fingerprint does not match this operation "
                    "(approval is bound to the exact request it was granted for)"
                )
                continue
            with self._lock:
                updated = self._conn.execute(
                    "UPDATE approval_tokens SET used_at = ? WHERE id = ? AND used_at = ''",
                    (iso(), token.id),
                )
                self._conn.commit()
            if updated.rowcount != 1:
                last_error = "token consumed concurrently"
                continue
            self.log.append(
                EventKind.APPROVAL,
                {"action": "consumed", "request_id": request_id, "token_id": token.id, "operation": operation,
                 "resource": resource, "actor": actor or token.granted_by},
                actor=actor or token.granted_by, source="approvals", risk=RiskLevel.MEDIUM,
            )
            return token
        raise ApprovalDenied(f"approval {request_id} rejected: {last_error}")

    def revoke(self, request_id: str, *, actor: str = "owner") -> int:
        with self._lock:
            cursor = self._conn.execute("UPDATE approval_tokens SET revoked = 1 WHERE request_id = ?", (request_id,))
            self._conn.commit()
        self.log.append(
            EventKind.APPROVAL, {"action": "revoked", "request_id": request_id, "tokens": cursor.rowcount},
            actor=actor, source="approvals", risk=RiskLevel.MEDIUM,
        )
        return int(cursor.rowcount)

    def verify_token(self, token: ApprovalToken) -> bool:
        return hmac_verify(self._key, token.payload, token.signature)

    def close(self) -> None:
        if self._own_connection:
            self._conn.close()


def _token_from_json(raw: str) -> ApprovalToken | None:
    """Parse a stored token, returning ``None`` for anything malformed.

    A corrupted or hand-edited row must look like an *invalid* token (and be refused), never like a
    crash: raising from here would turn a tampered database into a denial-of-service on approvals.
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    required = ("id", "request_id", "fingerprint", "granted_by", "granted_at", "expires_at")
    if any(not isinstance(data.get(field), str) or not data[field] for field in required):
        return None
    scope = data.get("scope", [])
    return ApprovalToken(
        id=data["id"], request_id=data["request_id"], fingerprint=data["fingerprint"],
        granted_by=data["granted_by"], granted_at=data["granted_at"], expires_at=data["expires_at"],
        scope=[str(item) for item in scope] if isinstance(scope, list) else [],
        signature=data.get("signature", ""), used_at=data.get("used_at", ""),
    )


_ENGINES: dict[str, ApprovalEngine] = {}
_LOCK = threading.Lock()


def get_approval_engine(signing_key: bytes | None = None) -> ApprovalEngine:
    """Process-wide engine. Requires a signing key on first use."""
    with _LOCK:
        if "default" not in _ENGINES:
            if signing_key is None:
                from ..credentials.crypto import get_master_key

                signing_key = get_master_key()
            _ENGINES["default"] = ApprovalEngine(signing_key=signing_key)
        return _ENGINES["default"]


def reset_approval_engines() -> None:
    with _LOCK:
        for engine in _ENGINES.values():
            engine.close()
        _ENGINES.clear()
