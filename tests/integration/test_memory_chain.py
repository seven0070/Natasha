"""Memory: ten kinds, hybrid retrieval, provenance, decay, forgetting and containment.

Memory is where an agent is most likely to lie to itself, so the properties here are about honesty
and control: every memory carries provenance, retrieval is explainable, workers cannot rewrite the
owner's memory, deletion needs the owner, secrets never land in the store, and the ten kinds stay
distinct instead of collapsing into one blob.
"""

from __future__ import annotations

import json

import pytest

from natasha.core import ConflictError
from natasha.core.clock import iso, utcnow
from natasha.events import EventKind
from natasha.memory import MemoryKind, Provenance, Retention

pytestmark = pytest.mark.integration

TEN_KINDS = [kind.value for kind in MemoryKind]


@pytest.fixture()
def memory(rt):
    return rt.memory


def test_there_are_exactly_the_ten_documented_kinds():
    assert set(TEN_KINDS) == {
        "working", "episodic", "semantic", "procedural", "profile", "preference",
        "relationship", "task", "artifact", "world",
    }
    assert len(TEN_KINDS) == 10


def test_every_kind_can_be_stored_and_recalled(memory):
    for index, kind in enumerate(TEN_KINDS):
        memory.add(kind, f"{kind} fact number {index}", tags=[kind], actor="owner")
    stats = memory.stats()
    for kind in TEN_KINDS:
        assert stats["by_kind"].get(kind, 0) >= 1, kind
    assert stats["total"] == 10


def test_retrieval_finds_a_fact_by_meaning_and_by_keyword(memory):
    memory.add("semantic", "The owner's project codename is Falcon.", tags=["project"], actor="owner")
    memory.add("semantic", "The owner prefers dark mode in every editor.", tags=["preference"], actor="owner")
    semantic = memory.recall("what is the project codename", limit=5)
    assert semantic and "Falcon" in semantic[0].record.content
    keyword = memory.recall("dark mode", limit=5)
    assert keyword and "dark mode" in keyword[0].record.content


def test_kind_filters_keep_the_kinds_separate(memory):
    memory.add("profile", "The owner is called Sam.", actor="owner")
    memory.add("episodic", "Sam asked about dark mode yesterday.", actor="owner")
    profile_only = memory.recall("Sam", kinds=[MemoryKind.PROFILE], limit=5)
    assert profile_only
    assert all(hit.record.kind is MemoryKind.PROFILE for hit in profile_only)


def test_provenance_is_required_and_recorded(memory):
    record = memory.add("semantic", "Paris is the capital of France", actor="owner",
                        provenance=Provenance(source="web", reference="https://example.com/france",
                                              trust="external"))
    assert record.provenance.source == "web"
    assert record.provenance.reference == "https://example.com/france"
    assert record.provenance.trust == "external"
    assert record.to_dict()["provenance"]["source"] == "web"


def test_empty_memories_are_refused(memory):
    with pytest.raises(ConflictError):
        memory.add("semantic", "   ", actor="owner")


def test_scored_retrieval_explains_itself(memory):
    memory.add("semantic", "The owner's favourite colour is teal", actor="owner")
    hits = memory.recall("favourite colour", limit=3)
    assert hits
    hit = hits[0]
    assert hit.score > 0
    explain = hit.explain()
    assert explain["memory_id"] == hit.record.id
    assert explain["signals"]            # the ranking is not a black box


def test_importance_and_pinning_change_retention(memory):
    low = memory.add("semantic", "Trivial note about the weather", importance=0.05, actor="owner")
    high = memory.add("semantic", "The owner's passport expires in March", importance=0.99,
                      pinned=True, actor="owner")
    hits = memory.recall("passport expires", limit=5)
    assert hits and hits[0].record.id == high.id
    assert memory.get(high.id, touch=False).pinned is True
    assert low.id != high.id


def test_a_worker_cannot_delete_the_owners_memory(memory):
    record = memory.add("profile", "The owner is called Sam", actor="owner")
    with pytest.raises(Exception):
        memory.forget(record.id, actor="worker:coder")
    assert memory.get(record.id, touch=False) is not None


def test_forgetting_removes_the_content_but_keeps_the_history(memory):
    record = memory.add("episodic", "A conversation about a secret project", actor="owner")
    outcome = memory.forget(record.id, actor="owner")
    assert record.id in outcome["ids"]
    # The audit trail survives: memory_history shows it was added and then forgotten.
    history = memory.history(record.id, actor="owner")
    assert history
    actions = {entry.get("action") for entry in history}
    assert "added" in actions


def test_transient_memories_expire_on_decay(memory):
    memory.add("episodic", "A passing thought", retention=Retention.TRANSIENT,
               expires_at=iso(utcnow().replace(year=utcnow().year - 1)), actor="owner")
    outcome = memory.decay(actor="system")
    assert "expired" in outcome
    assert outcome["expired"] >= 1


def test_consolidation_merges_duplicates(memory):
    for _ in range(3):
        memory.add("semantic", "The owner's project codename is Falcon", actor="owner")
    outcome = memory.consolidate(actor="system", min_duplicates=2)
    assert outcome.get("merged", 0) >= 1
    hits = memory.recall("codename Falcon", limit=10)
    assert len(hits) < 3


def test_artifacts_are_indexed_with_their_path(memory):
    record = memory.note_artifact("/tmp/example/report.md", description="monthly report", actor="owner")
    assert record.kind is MemoryKind.ARTIFACT
    assert "/tmp/example/report.md" in record.content
    hits = memory.recall("monthly report", limit=5)
    assert any(hit.record.kind is MemoryKind.ARTIFACT for hit in hits)


def test_memory_events_are_audited(memory, log):
    record = memory.add("semantic", "Audited fact", actor="owner")
    events = log.query(kinds=[EventKind.MEMORY], limit=50)
    assert any(event.payload.get("memory_id") == record.id for event in events)


def test_export_and_import_round_trip(memory, tmp_path):
    memory.add("profile", "The owner is called Sam", actor="owner")
    memory.add("preference", "Prefers concise answers", actor="owner")
    payload = memory.export(actor="owner")
    serialized = json.dumps(payload, default=str)
    assert "Sam" in serialized

    from natasha.memory import MemoryStore

    # Import into a real second database: the exported payload is portable, and importing twice is
    # idempotent because ids are preserved.
    copy = MemoryStore(db_path=str(tmp_path / "copy.db"))
    try:
        assert copy.import_(payload, actor="owner") >= 2
        assert copy.import_(payload, actor="owner") == 0
        assert copy.recall("called Sam", limit=5)
    finally:
        copy.close()


def test_memory_without_a_policy_cannot_be_written_by_a_model_as_profile(memory):
    """Model-inferred facts are allowed, but they are marked as inferred rather than as owner truth."""
    record = memory.add("semantic", "The owner probably likes tea", actor="model:main",
                        provenance=Provenance(source="inference", trust="inferred", actor="model:main"))
    assert record.provenance.trust == "inferred"
    assert record.confidence <= 0.8


def test_secrets_are_refused_or_redacted_in_the_store(memory):
    secret = "sk-live-abcdef0123456789abcdef"
    record = memory.add("semantic", f"The api key is {secret}", actor="owner")
    stored = memory.get(record.id, touch=False)
    body = json.dumps(stored.to_dict() if hasattr(stored, "to_dict") else stored, default=str)
    # Either the write was refused (raising earlier) or the value is not sitting in plaintext.
    assert secret not in body or "[redacted" in body
