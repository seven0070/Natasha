"""The high-level memory façade must match the store it wraps.

``MemoryManager`` is the convenience layer tools, workers and the UI are meant to use, so a stale
signature there is a broken production path even when the store below it is fine. These tests call
every public method and check the invariants that matter: provenance is recorded (including the
turn that produced the memory), corrections supersede instead of overwriting, and the audit log
carries the trace id.
"""

from __future__ import annotations

import pytest

from natasha.events import EventKind
from natasha.memory import MemoryKind, MemoryManager, get_memory_manager, get_memory_store
from natasha.memory.manager import reset_memory_managers




@pytest.fixture()
def manager(home):
    """A manager over the store of this test's home (no cross-test leakage)."""
    reset_memory_managers()
    from natasha.memory.working import reset_working_memory

    reset_working_memory()
    store = get_memory_store()
    yield MemoryManager(store=store)
    store.close()
    reset_memory_managers()


def test_remember_records_provenance_and_trace(manager):
    record = manager.remember("The deploy window is Sunday.", kind=MemoryKind.SEMANTIC,
                              actor="owner", source="owner", trace_id="trn_abc", mission_id="msn_1")
    assert record.provenance.trace_id == "trn_abc"
    assert record.provenance.mission_id == "msn_1"
    assert record.provenance.actor == "owner"
    assert manager.store.get(record.id).content == "The deploy window is Sunday."


def test_the_memory_audit_event_carries_the_turn_that_caused_it(manager):
    manager.remember("Audited fact.", trace_id="trn_audit")
    events = manager.store.log.query(kinds=[EventKind.MEMORY], limit=10)
    assert events, "storing a memory must be audited"
    assert any(event.trace_id == "trn_audit" for event in events)
    assert events[0].payload["provenance"]["trace_id"] == "trn_audit"


def test_remember_turn_writes_both_sides_and_updates_working_memory(manager):
    records = manager.remember_turn("What is the plan?", "Ship on Sunday.", trace_id="trn_turn")
    assert len(records) == 2
    assert {record.kind for record in records} == {MemoryKind.EPISODIC}
    assert any("Ship on Sunday." in record.content for record in records)
    assert manager.working.items(), "the last turn belongs in working memory"
    assert "Ship on Sunday." in manager.working.items()[0].content


def test_learn_procedure_keeps_the_steps_in_order(manager):
    record = manager.learn_procedure("deploy", ["build", "migrate", "restart"])
    assert record.kind is MemoryKind.PROCEDURAL
    assert record.content.index("build") < record.content.index("migrate") < record.content.index("restart")
    assert "procedure" in record.tags


def test_note_artifact_indexes_the_real_path(manager, tmp_path):
    artifact = tmp_path / "report.md"
    artifact.write_text("# Report\n")
    record = manager.note_artifact(str(artifact), description="markdown report", mission_id="msn_9")
    assert record.kind is MemoryKind.ARTIFACT
    assert record.provenance.reference == str(artifact)
    assert record.metadata["path"] == str(artifact)


def test_recall_and_recall_context_agree(manager):
    manager.remember("Natasha stores memories in SQLite.", kind=MemoryKind.SEMANTIC)
    manager.remember("The weather in Bengaluru is warm.", kind=MemoryKind.SEMANTIC)
    hits = manager.recall("where are memories stored", k=3)
    assert hits and "SQLite" in hits[0].record.content
    context = manager.recall_context("where are memories stored", k=3)
    assert context["count"] == len(context["memories"])
    assert context["memories"][0]["id"] == hits[0].record.id
    assert 0.0 <= context["memories"][0]["score"] <= 1.0
    assert context["memories"][0]["breakdown"], "the UI needs the score breakdown"


def test_correction_supersedes_instead_of_overwriting(manager):
    original = manager.remember("The meeting is at 10:00.", kind=MemoryKind.SEMANTIC)
    result = manager.correct(original.id, "The meeting moved to 11:00.", reason="owner update")
    replacement_id = result["replacement"]["id"]

    assert result["replacement"]["metadata"]["corrects"] == original.id
    assert result["superseded"]["id"] == original.id
    assert manager.store.get(original.id).superseded_by == replacement_id
    assert "11:00" in manager.store.get(replacement_id).content
    assert "10:00" in manager.store.get(original.id).content, "history must survive a correction"


def test_forget_reports_what_it_removed(manager):
    record = manager.remember("Forget me.", kind=MemoryKind.SEMANTIC)
    result = manager.forget(memory_id=record.id, hard=True)
    assert result["forgotten"] == 1
    assert record.id in result["ids"]


def test_the_manager_singleton_is_reused(manager):
    assert get_memory_manager() is get_memory_manager()
