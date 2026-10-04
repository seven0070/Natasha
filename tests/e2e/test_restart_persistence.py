"""Restart durability: everything the owner made must still be there after the process stops.

The master prompt asks for memory that survives a restart, a database that can be stopped and started,
and a clean-install path that ends with a restart. This file drives the real runtime through
``start → write → shutdown → start again`` and asserts that the *durable* state came back: memories and
their provenance, world beliefs, missions, the artifact on disk, and an unbroken audit chain that still
contains the pre-restart events.

Nothing is mocked except the model provider - the same substitution the rest of the e2e suite makes -
and the second runtime is built from scratch on the same ``NATASHA_HOME``, which is exactly what
``natasha serve`` does after a crash or a reboot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from natasha.memory import Provenance
from natasha_testkit import build_scripted_runtime

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def test_state_written_before_a_restart_is_still_there_afterwards(home):
    # -- first process: write durable state ---------------------------------- #
    runtime, _adapter = build_scripted_runtime(home)
    artifacts = Path(runtime.paths.artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    artifact = artifacts / "restart-proof.txt"
    artifact.write_text("written before the restart\n", encoding="utf-8")

    memory_record = runtime.memory.add(
        "semantic",
        "The restart proof artifact records that durable state survives a shutdown.",
        provenance=Provenance(source="e2e-restart", actor="owner", trace_id="restart-proof"),
        entities=["restart proof"],
    )
    artifact_memory = runtime.memory.note_artifact(str(artifact), description="restart proof",
                                                   actor="owner")
    entity = runtime.world.upsert_entity("restart-proof-run", kind="other", actor="owner")
    runtime.world.set_belief("restart-proof-run", "status", "written", actor="owner")
    mission = runtime.missions.create("Prove that durable state survives a restart",
                                      title="restart proof", created_by="owner")

    ok, report = runtime.log.verify_chain()
    assert ok, f"the event chain must be intact before the restart: {report}"
    events_before = runtime.log.query(trace_id="restart-proof", limit=100)
    ids_before = {event.id for event in events_before}
    assert memory_record.id and artifact_memory.id and mission.id

    runtime.shutdown()

    # -- second process: the same home, everything rebuilt from disk ---------- #
    restarted, _adapter2 = build_scripted_runtime(home)
    try:
        # MEMORY: same record, same provenance, still retrievable by content.
        found = restarted.memory.recall("does durable state survive a restart", limit=5)
        ids = {hit.record.id for hit in found}
        assert memory_record.id in ids, f"memory lost across restart: {ids}"
        reloaded = restarted.memory.get(memory_record.id)
        assert reloaded is not None
        assert reloaded.provenance.trace_id == "restart-proof"
        assert "durable state" in reloaded.content

        # The artifact memory survived too, and still points at a file that is really there.
        assert restarted.memory.get(artifact_memory.id) is not None
        assert artifact.is_file()
        assert artifact.read_text(encoding="utf-8") == "written before the restart\n"

        # WORLD: entities and beliefs are durable.
        assert restarted.world.get_entity(entity.id) is not None
        beliefs = restarted.world.beliefs_of("restart-proof-run")
        assert any(str(belief.get("value")) == "written" for belief in beliefs), beliefs

        # MISSIONS: the mission is still known, with its title and criteria.
        reloaded_mission = restarted.missions.get(mission.id)
        assert reloaded_mission.id == mission.id
        assert reloaded_mission.title == "restart proof"

        # AUDIT: the chain still verifies and still contains the events from the first process.
        ok, report = restarted.log.verify_chain()
        assert ok, f"the event chain must survive a restart: {report}"
        still_there = {event.id for event in restarted.log.query(trace_id="restart-proof", limit=100)}
        assert ids_before and ids_before <= still_there, (
            f"events from before the restart are missing: {sorted(ids_before - still_there)}")
    finally:
        restarted.shutdown()


def test_a_second_runtime_does_not_inherit_the_first_ones_objects(home):
    """Two runtimes in one process must not share a live connection or a cache.

    This is the bug class the per-runtime engines exist to prevent: a cached store whose connection
    was closed by a shutdown makes the *next* command fail somewhere unrelated.
    """
    first, _adapter = build_scripted_runtime(home)
    first.memory.add("semantic", "first runtime only", actor="owner")
    first.shutdown()

    second, _adapter2 = build_scripted_runtime(home)
    try:
        assert second.memory is not first.memory
        assert second.log is not first.log
        # ... and the second runtime can still write, which it could not if it held closed handles.
        record = second.memory.add("semantic", "second runtime can still write", actor="owner")
        assert second.memory.get(record.id) is not None
        ok, _report = second.log.verify_chain()
        assert ok
    finally:
        second.shutdown()
