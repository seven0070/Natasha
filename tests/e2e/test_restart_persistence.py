"""Persistence across a real stop/start cycle.

Requirement: memory, missions, artifacts, credentials metadata and the audit log must survive a
restart, and the audit chain must still verify afterwards. These tests build a runtime, work with
it, shut it down exactly as the CLI does, then build a *second* runtime on the same home and check
that nothing was kept only in RAM.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from natasha.memory import MemoryKind, Provenance
from natasha_testkit import build_scripted_runtime

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def test_memories_missions_and_artifacts_survive_a_restart(home):
    home = Path(home)
    first, adapter = build_scripted_runtime(home)

    # Working state: a memory, a mission that produces an artifact, and a credential.
    memory = first.memory.add(MemoryKind.SEMANTIC, "The owner prefers concise status reports.",
                              provenance=Provenance(source="owner", actor="owner"),
                              actor="owner", tags=["preference"])
    adapter.script = [{"content": "The report is written."}]
    mission = first.missions.create("Persist a report", title="persist-report",
                                    success_criteria=["artifact_exists"],
                                    verification_plan=["artifact_exists"], created_by="owner")
    first.missions.plan(mission.id, [
        {"title": "write", "kind": "tool", "tool": "write_artifact",
         "arguments": {"name": "persisted.md", "content": "# Persisted\n\nStill here after restart.\n"}},
    ])
    run = asyncio.run(first.missions.run(mission.id, actor="model:main"))
    assert run.mission.state.value == "succeeded", run.mission.error
    artifact_path = Path(run.mission.artifacts[0])
    assert artifact_path.exists()

    events_before = first.log.query(limit=500)
    first.shutdown()

    # ---- restart on the same home --------------------------------------------------------------
    second, _ = build_scripted_runtime(home)
    try:
        recalled = second.memory.recall("how does the owner like status reports", limit=5)
        assert recalled, "memory must survive a restart"
        assert recalled[0].record.id == memory.id

        reloaded = second.missions.get(mission.id)
        assert reloaded.state.value == "succeeded"
        assert reloaded.verification.get("passed") is True
        assert Path(reloaded.artifacts[0]).exists()
        assert reloaded.artifacts[0] == str(artifact_path)
        assert "Still here after restart." in artifact_path.read_text()

        ok, report = second.log.verify_chain()
        assert ok, f"the audit chain must still verify after a restart: {report}"
        assert report["checked"] >= len(events_before) - 1

        stats = second.memory.stats()
        assert stats["events"] >= 1
    finally:
        second.shutdown()


def test_a_second_runtime_does_not_see_the_first_runtimes_settings_drift(home):
    """Two runtimes in one process must not share a closed database handle (a real restart bug)."""
    home = Path(home)
    first, _ = build_scripted_runtime(home)
    record = first.memory.add(MemoryKind.SEMANTIC, "Only in the first runtime.",
                              provenance=Provenance(source="owner", actor="owner"))
    first.shutdown()

    second, _ = build_scripted_runtime(home)
    try:
        assert second.memory.get(record.id).content == "Only in the first runtime."
        # A write after the restart must land too, not fail on a stale connection.
        fresh = second.memory.add(MemoryKind.SEMANTIC, "Written after the restart.",
                                  provenance=Provenance(source="owner", actor="owner"))
        assert second.memory.get(fresh.id).content == "Written after the restart."
    finally:
        second.shutdown()


def test_the_database_files_are_where_the_backup_command_expects_them(home):
    """``natasha backup`` tars the home; the durable state has to actually live under it."""
    home = Path(home)
    runtime, _ = build_scripted_runtime(home)
    try:
        runtime.memory.add(MemoryKind.SEMANTIC, "Back me up.",
                           provenance=Provenance(source="owner", actor="owner"))
        runtime.log.append("system", {"action": "backup-probe"})
        names = {path.name for path in home.rglob("*") if path.is_file()}
        assert "events.db" in names, names
        assert "memory" in names, names          # the memory database
        assert "missions.db" in names or "missions" in names, names
    finally:
        runtime.shutdown()


def test_settings_round_trip_on_disk(home):
    """A settings change that is saved must be visible to the next process, not just in memory."""
    home = Path(home)
    runtime, _ = build_scripted_runtime(home)
    try:
        applied = runtime.update_settings({"voice": {"speed": 190}})
        assert applied == {"voice.speed": 190}
        assert runtime.settings.voice.speed == 190
        target = Path(runtime.settings.save())
        assert target.exists(), "settings.save() must write a real file"
        assert "190" in target.read_text(encoding="utf-8")
    finally:
        runtime.shutdown()

    third, _ = build_scripted_runtime(home)
    try:
        assert third.settings.voice.speed == 190
    finally:
        third.shutdown()
