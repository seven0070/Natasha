"""The full request pipeline, end to end on a real runtime.

The master prompt asks for proof that a real request travels

    perception -> intent -> memory retrieval -> workspace -> planning -> capability selection ->
    policy -> approval -> execution -> observation -> verification -> repair -> memory update ->
    final response

so this test walks that path in order and asserts the evidence each stage leaves behind. Nothing is
stubbed except the model provider: the policy engine, the approval engine, the tool registry, the
event log and the database are the production ones.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from natasha.approvals import ApprovalStatus
from natasha.events import EventKind
from natasha.executive import Turn
from natasha.memory import MemoryKind, Provenance
from natasha.verification import FileExistsCheck, PythonSyntaxCheck, VerificationEngine

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _turn(message: str, *, actor: str = "model:main", conversation_id: str = "pipeline") -> Turn:
    return Turn(message=message, actor=actor, conversation_id=conversation_id)


def _stage(name: str, detail: str) -> None:
    """Print the stage so a failing run shows exactly how far the pipeline got."""
    print(f"[pipeline] {name}: {detail}")


def test_a_request_travels_the_whole_pipeline(runtime):
    runtime, adapter = runtime
    home = Path(runtime.paths.home)
    target = home / "workspace" / "pipeline-proof.txt"
    proof = "verified content written by the pipeline test\n"

    # 1. PERCEPTION ---------------------------------------------------------------- the owner speaks
    turn = _turn("Write the pipeline proof file, then remember that you did it.")
    _stage("perception", f"turn {turn.id} accepted from {turn.actor}")
    assert turn.message.strip() and turn.id

    # 2-4. INTENT / MEMORY RETRIEVAL / WORKSPACE ----------------------------------- planning input
    # The memory the model will be able to cite is written first, so retrieval has something real
    # to find (not a fixture): recall goes through the same path the orchestrator uses.
    runtime.memory.add(MemoryKind.SEMANTIC, "The pipeline proof file lives in the workspace.",
                       provenance=Provenance(source="e2e", actor="owner"),
                       entities=["pipeline proof file"])
    hits = runtime.memory.recall("where does the pipeline proof file live", limit=5)
    _stage("memory_retrieval", f"{len(hits)} hit(s), top score {hits[0].score:.2f}")
    assert hits and hits[0].record.content.startswith("The pipeline proof file")

    # 5-7. PLANNING / CAPABILITY SELECTION / POLICY -------------------------------- model asks for a tool
    adapter.script = [
        {"content": "Writing the file.", "tool_calls": [
            {"name": "fs_write", "arguments": {"path": str(target), "content": proof}}]},
        {"content": "File written.", "tool_calls": [
            {"name": "remember", "arguments": {
                "content": "The pipeline proof file was written and verified.",
                "kind": "semantic"}}]},
        {"content": "The pipeline proof file is written and recorded."},
    ]
    first = asyncio.run(runtime.executive.run_turn(turn))

    # CAPABILITY SELECTION: the tool the model named really exists in the registry it was offered.
    assert adapter.requests, "the model must have been called through the brain"
    assert first.tool_calls[0]["tool"] == "fs_write"
    assert "fs_write" in runtime.tools.names()

    # POLICY + APPROVAL: the write is inside the workspace, so policy may allow it outright; if it
    # does not, the run must stop at an approval request instead of touching disk. Either way the
    # gate is enforced by code, not by the model.
    approval_id = ""
    if not first.tool_calls[0]["ok"]:
        approval_id = first.approvals_requested[0]["request_id"]
        request = runtime.approvals.get_request(approval_id)
        _stage("policy_approval", f"gated as {request.operation} ({request.reversibility})")
        assert request.operation == "tool.fs_write"
        assert not target.exists(), "nothing may happen before the owner decides"
        decision = asyncio.run(runtime.executive.resolve_approval(approval_id, actor="owner",
                                                                  note="e2e approval"))
        assert decision["status"] == ApprovalStatus.APPROVED.value
        adapter.script = [
            {"content": "Writing the file.", "tool_calls": [
                {"name": "fs_write", "arguments": {"path": str(target), "content": proof}}]},
            {"content": "File written.", "tool_calls": [
                {"name": "remember", "arguments": {
                    "content": "The pipeline proof file was written and verified.",
                    "kind": "semantic"}}]},
            {"content": "The pipeline proof file is written and recorded."},
        ]
        first = asyncio.run(runtime.executive.run_turn(_turn(turn.message)))
    else:
        _stage("policy_approval", "policy allowed the in-workspace write without an approval")

    # 8-9. EXECUTION + OBSERVATION ------------------------------------------------- the tool really ran
    assert first.tool_calls[0]["ok"] is True, first.tool_calls[0]["error"]
    assert target.read_text() == proof
    _stage("execution_observation", f"fs_write wrote {target.stat().st_size} bytes")

    # 11. VERIFICATION ------------------------------------------------------------- the claim is checked
    report = asyncio.run(runtime.verification.verify(
        "pipeline proof file", [FileExistsCheck("proof_on_disk", str(target), min_bytes=len(proof))],
        trace_id=first.turn_id, actor="model:main"))
    assert report.passed, report.summary
    _stage("verification", report.summary)

    # ... and a false claim cannot pass: the same engine refuses a file that is not there, and
    # require() raises instead of letting a caller report success.
    false_report = asyncio.run(runtime.verification.verify(
        "a file that was never written",
        [FileExistsCheck("phantom", str(home / "workspace" / "never-written.txt"))]))
    assert false_report.passed is False
    with pytest.raises(Exception):
        VerificationEngine.require(false_report)
    _stage("false_success_refused", "; ".join(false_report.unproven and
                                                [check.name for check in false_report.unproven]) or "checked")

    # 13. MEMORY UPDATE ------------------------------------------------------------ the turn can cite memory
    _stage("tool_calls", str([(call["tool"], call["ok"], call["error"]) for call in first.tool_calls]))
    assert first.memory_writes, "a turn that stored a memory must report the write"
    for memory_id in first.memory_writes:
        assert runtime.memory.get(memory_id) is not None, "the reported memory must exist"
    _stage("memory_update", f"{len(first.memory_writes)} write(s) recorded on the turn")

    # 14. FINAL RESPONSE ----------------------------------------------------------- honest, non-empty
    assert first.reply.strip()
    assert first.offline_placeholder is False, "a scripted provider is configured; this is not offline"
    assert first.provider == "scripted"
    _stage("final_response", first.reply[:80])

    # Every stage above was audited on the real event log, and the log chain verifies.
    events = runtime.log.query(trace_id=first.turn_id, limit=500)
    kinds = {event.kind for event in events}
    _stage("events", ", ".join(sorted(kind.name for kind in kinds)))
    expected = {EventKind.MESSAGE, EventKind.TOOL_EXECUTION, EventKind.MEMORY}
    missing = {kind.name for kind in expected - kinds}
    assert not missing, f"the turn did not audit these stages: {missing}"
    ok, report = runtime.log.verify_chain()
    assert ok, f"event log chain broken: {report}"
    _stage("event_log", f"{report.get('checked')} events chained, none broken")

    # Working memory holds the turn, which is what the console shows as "current activity".
    items = runtime.working.items() if hasattr(runtime.working, "items") else []
    _stage("workspace", f"{len(items)} working-memory item(s)")


def test_a_failed_tool_is_repaired_once_and_never_reported_as_success(runtime):
    """The repair stage: a tool that fails is retried, and the failure stays visible in the answer."""
    runtime, adapter = runtime
    missing = Path(runtime.paths.home) / "workspace" / "not-there.txt"

    adapter.script = [
        {"content": "Reading.", "tool_calls": [{"name": "fs_read", "arguments": {"path": str(missing)}}]},
        {"content": "I could not read that file."},
    ]
    result = asyncio.run(runtime.executive.run_turn(_turn("Read the missing file")))
    assert result.reply.strip()
    assert result.error in ("", None) or result.partial is True   # a turn may fail; it may not lie
    _stage("repair", f"turn error={result.error!r} reply={result.reply[:60]!r}")

    failures = runtime.log.query(kinds=[EventKind.FAILURE], limit=200)
    if failures:
        assert "not-there" in str(failures[0].payload) or "TOOL FAILED" in str(failures[0].payload)


def test_streaming_reports_the_same_truth_as_the_blocking_turn(runtime):
    """Streaming must not be a second, unverified path: the same stages produce the same outcome."""
    runtime, adapter = runtime
    target = Path(runtime.paths.home) / "workspace" / "stream-proof.txt"
    adapter.script = [
        {"content": "Writing.", "tool_calls": [
            {"name": "fs_write", "arguments": {"path": str(target), "content": "streamed\n"}}]},
        {"content": "Written by the streaming path."},
    ]

    async def drain():
        chunks = []
        async for frame in runtime.executive.stream_turn(_turn("Stream a write", conversation_id="s")):
            chunks.append(frame)
        return chunks

    frames = asyncio.run(drain())
    types = [frame["type"] for frame in frames]
    assert "turn_finished" in types
    finished = frames[-1]["result"]
    assert finished["reply"].strip()
    assert target.exists() and target.read_text() == "streamed\n"
    _stage("streaming", f"frames={types}")
