"""Natasha Agent - a persistent, multimodal, local-first personal AI agent.

Importing this package is deliberately cheap: submodules are loaded lazily so that ``import natasha``
does not open databases, start providers or touch the filesystem. Use :func:`get_runtime` for the
wired-up system, or import a subsystem directly, e.g. ``from natasha.memory import get_memory_store``.
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "0.9.0"
CODENAME = "cognitive-os"

#: The public subsystem map. Names resolve on first attribute access (PEP 562).
_SUBMODULES = {
    "core", "events", "security", "approvals", "credentials", "governance", "memory", "world",
    "brain", "tools", "perception", "voice", "computer", "creation", "affect", "missions",
    "agents", "mcp", "skills", "marketplace", "integrations", "verification", "recovery",
    "executive", "observability", "db", "config", "runtime",
}

#: The high-level entry points most callers want.
_LAZY_EXPORTS = {
    "get_runtime": ("natasha.runtime", "get_runtime"),
    "NatashaRuntime": ("natasha.runtime", "NatashaRuntime"),
    "get_executive": ("natasha.executive", "get_executive"),
    "Executive": ("natasha.executive", "Executive"),
    "get_brain": ("natasha.brain", "get_brain"),
    "get_memory_store": ("natasha.memory", "get_memory_store"),
    "get_tool_registry": ("natasha.tools", "get_tool_registry"),
    "get_approval_engine": ("natasha.approvals", "get_approval_engine"),
    "get_credential_vault": ("natasha.credentials", "get_credential_vault"),
    "get_mission_engine": ("natasha.missions", "get_mission_engine"),
    "get_paths": ("natasha.core", "get_paths"),
    "load_settings": ("natasha.core", "load_settings"),
}

__all__ = ["__version__", "CODENAME", "get_runtime", "NatashaRuntime", "Executive", "get_executive",
           "get_brain", "get_memory_store", "get_tool_registry", "get_approval_engine",
           "get_credential_vault", "get_mission_engine", "get_paths", "load_settings"]


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        module_name, attribute = _LAZY_EXPORTS[name]
        value = getattr(importlib.import_module(module_name), attribute)
        globals()[name] = value
        return value
    if name in _SUBMODULES:
        module = importlib.import_module(f"natasha.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module 'natasha' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + list(_SUBMODULES) + list(_LAZY_EXPORTS)))
