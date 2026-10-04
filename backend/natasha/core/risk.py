"""Risk model shared by policy, approvals, missions and the event log."""

from __future__ import annotations

from enum import IntEnum


class RiskLevel(IntEnum):
    """Ordered risk classification. Higher value == more dangerous."""

    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def parse(cls, value: object) -> "RiskLevel":
        if isinstance(value, RiskLevel):
            return value
        if isinstance(value, int):
            return cls(value)
        text = str(value or "").strip().upper()
        for member in cls:
            if member.name == text:
                return member
        aliases = {"INFO": cls.NONE, "NORMAL": cls.LOW, "WARN": cls.MEDIUM, "DANGER": cls.HIGH, "EXTREME": cls.CRITICAL}
        if text in aliases:
            return aliases[text]
        return cls.MEDIUM

    @property
    def label(self) -> str:
        return self.name.lower()

    def requires_owner_approval(self) -> bool:
        """HIGH and CRITICAL always require an explicit, scoped owner approval."""
        return self >= RiskLevel.HIGH

    def is_reversible_by_default(self) -> bool:
        return self <= RiskLevel.MEDIUM


def max_risk(*levels: RiskLevel) -> RiskLevel:
    return max(levels) if levels else RiskLevel.NONE


def elevate(level: RiskLevel, steps: int = 1) -> RiskLevel:
    """Raise *level* by *steps*, saturating at CRITICAL."""
    return RiskLevel(min(RiskLevel.CRITICAL, int(level) + steps))
