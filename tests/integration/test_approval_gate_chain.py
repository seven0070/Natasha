"""Approval gating, end to end through the real runtime.

The point of these tests is not that an approval request exists - it is that *nothing happens* until
the owner says yes, that a yes is scoped to exactly one action and consumed once, that the owner's
refusal really blocks the action, and that nobody but the owner can decide.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from natasha.approvals import ApprovalStatus
from natasha.core import ApprovalDenied
from natasha.events import EventKind
from natasha.executive import Turn

pytestmark = pytest.mark.integration


def _turn(message: str, *, actor: str = "model:main") -> Turn:
    return Turn(message=message, actor=actor)


def _script_call(adapter, command: str, closing: str = "Finished.") -> None:
    adapter.script = [
        {"content": "Working on it.", "tool_calls": [{"name": "shell", "arguments": {"command": command}}]},
        {"content": closing},
    ]


@pytest.fixture()
def gate(runtime, home):
    """A workspace path the shell tool is allowed to touch, plus the runtime under test."""
    runtime, adapter = runtime
    proof = Path(home) / "workspace" / "approval-proof.txt"
    return runtime, adapter, proof


def test_a_dangerous_call_does_nothing_until_the_owner_approves(gate):
    runtime, adapter, proof = gate
    _script_call(adapter, f"touch {proof}")
    result = asyncio.run(runtime.executive.run_turn(_turn("Create the proof file")))

    records = result.tool_calls
    assert records and records[0]["ok"] is False
    assert "approval" in records[0]["error"].lower()
    assert not proof.exists(), "the side effect must not happen before approval"

    request_id = result.approvals_requested[0]["request_id"]
    request = runtime.approvals.get_request(request_id)
    assert request.operation == "tool.shell"                 # scoped to the tool, not "everything"
    assert request.arguments.get("command") == f"touch {proof}"
    assert request.reversibility == "irreversible"            # the owner is told the truth
    assert runtime.approvals.status(request_id) is ApprovalStatus.PENDING


def test_an_approved_call_runs_once_and_the_grant_is_consumed(gate):
    runtime, adapter, proof = gate
    _script_call(adapter, f"touch {proof}")
    first = asyncio.run(runtime.executive.run_turn(_turn("Create the proof file")))
    request_id = first.approvals_requested[0]["request_id"]

    decision = asyncio.run(runtime.executive.resolve_approval(request_id, actor="owner", note="looks fine"))
    assert decision["status"] == ApprovalStatus.APPROVED.value
    assert not proof.exists(), "approving is not the same as doing"

    _script_call(adapter, f"touch {proof}")
    second = asyncio.run(runtime.executive.run_turn(_turn("Create the proof file")))
    assert second.tool_calls[0]["ok"] is True
    assert proof.exists(), "an approved action must actually happen"

    # A grant is single-use: the same action again needs a fresh decision from the owner.
    _script_call(adapter, f"touch {proof}")
    third = asyncio.run(runtime.executive.run_turn(_turn("Create it again")))
    assert third.tool_calls[0]["ok"] is False
    assert third.approvals_requested
    assert third.approvals_requested[0]["request_id"] != request_id


def test_a_denied_request_really_blocks_the_action(gate):
    runtime, adapter, proof = gate
    _script_call(adapter, f"touch {proof}")
    first = asyncio.run(runtime.executive.run_turn(_turn("Create the proof file")))
    request_id = first.approvals_requested[0]["request_id"]

    decision = asyncio.run(runtime.executive.resolve_approval(request_id, decision="deny", actor="owner"))
    assert decision["status"] == ApprovalStatus.DENIED.value

    _script_call(adapter, f"touch {proof}")
    second = asyncio.run(runtime.executive.run_turn(_turn("Create the proof file")))
    assert second.tool_calls[0]["ok"] is False
    assert not proof.exists()
    with pytest.raises(ApprovalDenied):
        runtime.approvals.consume(request_id, "tool.shell", {"command": f"touch {proof}"}, actor="model:main")


def test_only_the_owner_may_approve(gate):
    runtime, adapter, proof = gate
    _script_call(adapter, f"touch {proof}")
    result = asyncio.run(runtime.executive.run_turn(_turn("Create the proof file")))
    request_id = result.approvals_requested[0]["request_id"]

    with pytest.raises(ApprovalDenied):
        runtime.approvals.approve(request_id, decided_by="model:main")
    assert runtime.approvals.status(request_id) is ApprovalStatus.PENDING

    # ... and the attempt itself is a security event, not a silent failure.
    security = runtime.log.query(kinds=[EventKind.SECURITY], limit=50)
    assert any(event.payload.get("action") == "approval.self_approval_attempt" for event in security)


def test_an_approval_is_bound_to_the_exact_command_it_was_granted_for(gate, home):
    runtime, adapter, proof = gate
    other = Path(home) / "workspace" / "a-different-file.txt"
    _script_call(adapter, f"touch {proof}")
    first = asyncio.run(runtime.executive.run_turn(_turn("Create the proof file")))
    request_id = first.approvals_requested[0]["request_id"]
    asyncio.run(runtime.executive.resolve_approval(request_id, actor="owner"))

    # A *different* command may not ride on the same approval.
    _script_call(adapter, f"touch {other}")
    second = asyncio.run(runtime.executive.run_turn(_turn("Create a different file")))
    assert second.tool_calls[0]["ok"] is False
    assert not other.exists()
    assert second.approvals_requested

    # ... and the original approval still works for the original command.
    _script_call(adapter, f"touch {proof}")
    third = asyncio.run(runtime.executive.run_turn(_turn("Create the proof file")))
    assert third.tool_calls[0]["ok"] is True
    assert proof.exists()


def test_a_revoked_grant_cannot_be_used(gate):
    runtime, adapter, proof = gate
    _script_call(adapter, f"touch {proof}")
    first = asyncio.run(runtime.executive.run_turn(_turn("Create the proof file")))
    request_id = first.approvals_requested[0]["request_id"]
    asyncio.run(runtime.executive.resolve_approval(request_id, actor="owner"))

    revoked = runtime.approvals.revoke(request_id, actor="owner")
    assert revoked >= 0
    with pytest.raises(ApprovalDenied):
        runtime.approvals.consume(request_id, "tool.shell", {"command": f"touch {proof}"}, actor="model:main")

    _script_call(adapter, f"touch {proof}")
    second = asyncio.run(runtime.executive.run_turn(_turn("Create the proof file")))
    assert second.tool_calls[0]["ok"] is False
    assert not proof.exists()


def test_the_owner_acting_directly_is_the_authority(gate):
    runtime, adapter, proof = gate
    _script_call(adapter, f"touch {proof}")
    result = asyncio.run(runtime.executive.run_turn(_turn("Create the proof file", actor="owner")))
    assert result.tool_calls[0]["ok"] is True
    assert proof.exists()
    assert not result.approvals_requested, "the owner does not need to approve their own action"
    events = runtime.log.query(kinds=[EventKind.APPROVAL], limit=50)
    assert any(event.payload.get("obligation") == "owner_direct_action" for event in events)


def test_the_decision_is_audited_with_who_when_and_why(gate):
    runtime, adapter, proof = gate
    _script_call(adapter, f"touch {proof}")
    result = asyncio.run(runtime.executive.run_turn(_turn("Create the proof file")))
    request_id = result.approvals_requested[0]["request_id"]
    asyncio.run(runtime.executive.resolve_approval(request_id, actor="owner", note="verified by the owner"))

    events = runtime.log.query(kinds=[EventKind.APPROVAL], limit=100)
    actions = [event.payload.get("action") for event in events]
    assert "requested" in actions
    assert "approved" in actions
    approved = next(event for event in events if event.payload.get("action") == "approved")
    assert approved.payload["request_id"] == request_id
    assert approved.actor.startswith("owner")


def test_a_pending_request_can_be_listed_for_the_ui(gate):
    runtime, adapter, proof = gate
    _script_call(adapter, f"touch {proof}")
    asyncio.run(runtime.executive.run_turn(_turn("Create the proof file")))
    pending = runtime.approvals.pending()
    assert pending and pending[0].operation == "tool.shell"
    assert pending[0].risk.name == "HIGH"
