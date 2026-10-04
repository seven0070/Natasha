"""Approval engine - explicit, scoped, auditable, expiring, unforgeable.

A model or worker can *ask*; only the owner can *grant*. Grants are HMAC-signed over the exact
operation fingerprint, expire, and are single-use, so an approval for "read file X" can never be
replayed for "delete file X".
"""

from .engine import ApprovalEngine, get_approval_engine
from .models import Approval, ApprovalRequest, ApprovalStatus, ApprovalToken

__all__ = [
    "ApprovalEngine", "get_approval_engine", "Approval", "ApprovalRequest", "ApprovalStatus", "ApprovalToken",
]
