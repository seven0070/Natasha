"""Test isolation: every process-wide singleton must be resettable.

If a reset hook silently goes missing, state leaks between tests and failures become order-dependent
- which is exactly how real regressions hide. This file exists so that can never happen quietly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_every_reset_hook_resolves():
    import natasha_testkit

    natasha_testkit.reset_everything()
    assert natasha_testkit.RESET_PROBLEMS == [], (
        f"unresolved reset hooks: {natasha_testkit.RESET_PROBLEMS}"
    )


def test_reset_everything_really_rebuilds_singletons(home):
    from natasha.events import get_event_log
    from natasha.memory import get_memory_store
    from natasha.security.policy import PolicyEngine
    from natasha.tools import get_tool_registry

    import natasha_testkit

    first_log = get_event_log()
    first_registry = get_tool_registry()
    natasha_testkit.reset_everything(home)
    assert get_event_log() is not first_log
    assert get_tool_registry() is not first_registry
    # A freshly built policy engine must point at *this* home, not the previous one.
    assert str(home / "workspace") in PolicyEngine()._write_roots
    assert get_memory_store().stats()["total"] == 0


def test_two_homes_do_not_share_state(tmp_path):
    import natasha_testkit

    natasha_testkit.reset_everything(tmp_path / "one")
    from natasha.memory import get_memory_store

    get_memory_store().add("semantic", "only in home one", actor="owner")

    natasha_testkit.reset_everything(tmp_path / "two")
    from natasha.memory import get_memory_store as second_store

    assert second_store().stats()["total"] == 0
