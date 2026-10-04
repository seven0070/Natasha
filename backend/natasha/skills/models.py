"""The skill vocabulary: states and the installed-skill record.

These types are the contract between the lifecycle rules (``natasha.skills.lifecycle``) and the store
that persists them (``natasha.skills.store``), so they live in the interface core and both sides may
import them freely.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from ..core.clock import iso


class SkillState(str, Enum):
    """The lifecycle states a skill moves through, in order (with FAILED reachable from any step)."""

    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    SCANNED = "SCANNED"
    TESTED = "TESTED"
    APPROVED = "APPROVED"
    INSTALLED = "INSTALLED"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    UNINSTALLED = "UNINSTALLED"
    FAILED = "FAILED"


@dataclass
class SkillRecord:
    """One installed version of one skill."""

    id: str
    version: str
    name: str
    state: str
    path: str
    manifest: dict[str, Any] = field(default_factory=dict)
    scan: dict[str, Any] = field(default_factory=dict)
    test: dict[str, Any] = field(default_factory=dict)
    checksum: str = ""
    installed_at: str = ""
    updated_at: str = field(default_factory=iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["SkillRecord", "SkillState"]
