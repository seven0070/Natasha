"""Uniform result envelope used by tools, stages, workers and the API."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class Status(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    NEEDS_APPROVAL = "needs_approval"
    DENIED = "denied"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Result(Generic[T]):
    """Outcome of a unit of work.

    ``ok`` is deliberately strict: a result is only ``OK`` when the work actually
    completed and was verified. Partial outcomes must say so.
    """

    status: Status
    value: T | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is Status.OK

    def unwrap(self) -> T:
        if not self.ok or self.value is None:
            raise RuntimeError(self.error or f"result is {self.status.value}")
        return self.value

    @classmethod
    def success(cls, value: T | None = None, **details: Any) -> "Result[T]":
        return cls(Status.OK, value=value, details=details)

    @classmethod
    def partial(cls, value: T | None = None, error: str = "", **details: Any) -> "Result[T]":
        return cls(Status.PARTIAL, value=value, error=error, details=details)

    @classmethod
    def failure(cls, error: str, **details: Any) -> "Result[T]":
        return cls(Status.FAILED, error=error, details=details)

    @classmethod
    def denied(cls, reason: str, **details: Any) -> "Result[T]":
        return cls(Status.DENIED, error=reason, details=details)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "value": self.value,
            "error": self.error,
            "details": self.details,
            "warnings": self.warnings,
        }
