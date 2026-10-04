"""Filesystem layout for Natasha state.

Everything mutable lives under ``NATASHA_HOME`` (default ``~/.natasha``) so the agent can run
read-only elsewhere and be wiped by deleting one directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_HOME = "~/.natasha"
_PRIVATE_DIRS = ("vault", "keys")


@dataclass(frozen=True)
class NatashaPaths:
    """Resolved directory layout."""

    home: Path
    config: Path
    data: Path
    db: Path
    logs: Path
    artifacts: Path
    vault: Path
    keys: Path
    skills: Path
    plugins: Path
    marketplace: Path
    mcp: Path
    sandboxes: Path
    missions: Path
    workspace: Path
    snapshots: Path
    reports: Path
    uploads: Path

    def ensure(self) -> "NatashaPaths":
        """Create every directory with tight permissions on private ones."""
        for field_name in self.__dataclass_fields__:  # type: ignore[attr-defined]
            path = getattr(self, field_name)
            if isinstance(path, Path) and path != self.home:
                path.mkdir(parents=True, exist_ok=True)
        for name in _PRIVATE_DIRS:
            _chmod_private(getattr(self, name))
        _chmod_private(self.home)
        return self

    def db_path(self, name: str = "natasha.db") -> Path:
        return self.db / name

    def as_dict(self) -> dict[str, str]:
        return {name: str(getattr(self, name)) for name in self.__dataclass_fields__}  # type: ignore[attr-defined]


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover - windows / restricted mounts
        pass


def build_paths(home: str | os.PathLike[str] | None = None) -> NatashaPaths:
    """Build the layout for *home* (or ``$NATASHA_HOME`` / ``~/.natasha``)."""
    root = Path(os.path.expanduser(str(home or os.environ.get("NATASHA_HOME") or DEFAULT_HOME))).resolve()
    return NatashaPaths(
        home=root,
        config=root / "config",
        data=root / "data",
        db=root / "db",
        logs=root / "logs",
        artifacts=root / "artifacts",
        vault=root / "vault",
        keys=root / "keys",
        skills=root / "skills",
        plugins=root / "plugins",
        marketplace=root / "marketplace",
        mcp=root / "mcp",
        sandboxes=root / "sandboxes",
        missions=root / "missions",
        workspace=root / "workspace",
        snapshots=root / "snapshots",
        reports=root / "reports",
        uploads=root / "uploads",
    )


@lru_cache(maxsize=1)
def get_paths() -> NatashaPaths:
    """Process-wide paths (honours ``NATASHA_HOME``)."""
    return build_paths()


def reset_paths_cache() -> None:
    """Test hook: forget the cached layout after changing ``NATASHA_HOME``."""
    get_paths.cache_clear()
