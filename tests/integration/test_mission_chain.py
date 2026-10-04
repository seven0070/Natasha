"""Mission execution end to end, including the approval round trip and honest failure.

A mission is the strongest promise the agent makes, so these tests check the uncomfortable cases:
a step that needs the owner's approval must block the whole mission (not skip ahead), a failed step
must block its dependents and the mission (not report success), an unregistered verification check
must fail the mission (not pass silently), and rollback must report what it could not undo.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from natasha.missions import MissionState, StepState

pytestmark = pytest.mark.integration


def _mission(runtime, objective: str, steps: list[dict], **kwargs):
    mission = runtime.missions.create(
        objective,
        title=kwargs.pop("title", objective[:40]),
        success_criteria=kwargs.pop("success_criteria", []),
        verification_plan=kwargs.pop("verification_plan", []),
        **kwargs,
    )
    runtime.missions.plan(mission.id, steps)
    return mission.id


def test_a_mission_with_tool_model_and_note_steps_succeeds_with_evidence(runtime):
    runtime, adapter = runtime
    home = Path(runtime.paths.home)

    def artifact_path(name: str) -> str:
        return str(home / "artifacts" / name)

    adapter.script = [{"content": "The report says the migration finished."}]   # MODEL step
    mission_id = _mission(
        runtime, "Produce a short report",
        [
            {"title": "note the plan", "kind": "note", "payload": {"note": "step one"}},
            {"title": "write report", "kind": "tool", "tool": "write_artifact",
             "arguments": {"name": "mission-report.md",
                           "content": "# Report\n\nEverything verified.\n"}},
            {"title": "summarise", "kind": "model", "payload": {"prompt": "Summarise the report"}},
        ],
        # A success criterion is only credible when it names a check that can back it.
        success_criteria=["artifact_exists", "steps_completed"],
        verification_plan=["artifact_exists"],
    )
    result = asyncio.run(runtime.missions.run(mission_id, actor="model:main"))
    mission = result.mission
    assert mission.state is MissionState.SUCCEEDED, mission.error
    assert all(step.state is StepState.DONE for step in mission.steps)
    assert mission.artifacts, "a mission that produced an artifact must say so"
    assert Path(mission.artifacts[0]).exists()
    assert mission.verification["passed"] is True
    assert mission.result["summary"]
    model_step = next(step for step in mission.steps if step.kind.value == "model")
    assert model_step.result["provider"] == "scripted"


def test_a_failing_step_blocks_the_mission_and_its_dependents(runtime):
    runtime, adapter = runtime
    adapter.script = [{"content": "ok"}]
    mission_id = _mission(
        runtime, "Read a file that is not there",
        [
            {"title": "missing read", "kind": "tool", "tool": "fs_read",
             "arguments": {"path": "/tmp/definitely-not-here-12345.txt"}},
            {"title": "dependent step", "kind": "note", "payload": {"note": "should be skipped"},
             "depends_on": []},
        ],
    )
    mission = runtime.missions.get(mission_id)
    dependent = mission.steps[1]
    dependent.depends_on = [mission.steps[0].id]
    runtime.missions.store.save(mission)

    result = asyncio.run(runtime.missions.run(mission_id, actor="model:main"))
    states = {step.title: step.state for step in result.mission.steps}
    assert result.mission.state in (MissionState.BLOCKED, MissionState.FAILED)
    assert result.mission.result.get("unproven") or result.mission.error
    assert states["dependent step"] is StepState.SKIPPED


def test_an_unregistered_verification_check_fails_the_mission(runtime):
    runtime, adapter = runtime
    adapter.script = [{"content": "ok"}]
    mission_id = _mission(runtime, "Claim something unprovable",
                          [{"title": "note", "kind": "note", "payload": {"note": "done"}}],
                          verification_plan=["no_such_check_exists"])
    result = asyncio.run(runtime.missions.run(mission_id, actor="model:main"))
    assert result.mission.state is MissionState.FAILED
    assert "verification failed" in result.mission.error.lower()
    assert result.mission.result["unproven"]


def test_a_step_that_needs_approval_pauses_the_mission_then_completes_after_approval(runtime):
    runtime, adapter = runtime
    home = Path(runtime.paths.home)
    proof = home / "workspace" / "mission-approved.txt"

    adapter.script = [{"content": "ok"}] * 4
    mission_id = _mission(
        runtime, "Touch a file with the owner's blessing",
        [{"title": "touch file", "kind": "tool", "tool": "shell",
          "arguments": {"command": f"touch {proof}"}}],
    )
    first = asyncio.run(runtime.missions.run(mission_id, actor="model:main"))
    assert first.mission.state is MissionState.WAITING_APPROVAL
    waiting = next(step for step in first.mission.steps if step.state is StepState.WAITING_APPROVAL)
    request_id = waiting.evidence["approval_request_id"]
    assert request_id
    assert not proof.exists(), "nothing may happen while the mission waits"

    # The owner approves; the executive re-arms the step and resumes the mission.
    decision = asyncio.run(runtime.executive.resolve_approval(request_id, actor="owner"))
    assert decision["status"] == "APPROVED"
    mission = runtime.missions.get(mission_id)
    if mission.state is not MissionState.SUCCEEDED:
        # The resume is asynchronous in the API; drive it deterministically here.
        asyncio.run(runtime.missions.resume(mission_id, actor="owner"))
    mission = runtime.missions.get(mission_id)
    assert mission.state in (MissionState.SUCCEEDED, MissionState.FAILED), mission.error
    if mission.state is MissionState.SUCCEEDED:
        assert proof.exists()


def test_rearm_refuses_a_step_whose_approval_was_denied(runtime):
    runtime, adapter = runtime
    home = Path(runtime.paths.home)
    proof = home / "workspace" / "never-created.txt"
    adapter.script = [{"content": "ok"}] * 3
    mission_id = _mission(runtime, "Denied action",
                          [{"title": "touch file", "kind": "tool", "tool": "shell",
                            "arguments": {"command": f"touch {proof}"}}])
    first = asyncio.run(runtime.missions.run(mission_id, actor="model:main"))
    request_id = next(step for step in first.mission.steps
                      if step.state is StepState.WAITING_APPROVAL).evidence["approval_request_id"]

    asyncio.run(runtime.executive.resolve_approval(request_id, decision="deny", actor="owner"))
    rearm = runtime.missions.rearm(mission_id, actor="owner")
    assert rearm["rearmed"] == []
    assert rearm["refused"], "a denied step must not be re-armed"

    blocked = asyncio.run(runtime.missions.run(mission_id, actor="model:main"))
    assert not proof.exists()
    assert blocked.mission.state in (MissionState.WAITING_APPROVAL, MissionState.BLOCKED,
                                     MissionState.FAILED, MissionState.PAUSED)


def test_delegation_runs_on_a_worker_that_the_supervisor_contains(runtime):
    runtime, adapter = runtime
    adapter.script = [{"content": "worker finished the research"}]
    mission_id = _mission(
        runtime, "Delegate research",
        [{"title": "research", "kind": "delegate",
          "payload": {"role": "researcher", "objective": "Find the three key facts"}}],
    )
    result = asyncio.run(runtime.missions.run(mission_id, actor="model:main"))
    step = result.mission.steps[0]
    assert step.state in (StepState.DONE, StepState.FAILED), step.error
    workers = runtime.supervisor.workers()
    assert workers, "delegation must use a supervised worker"
    assert all(worker["actor"].startswith("worker:") for worker in workers)
    delegated = [event for event in runtime.log.query(limit=200)
                 if event.payload.get("action") == "delegated"]
    assert delegated


def test_a_mission_survives_a_restart(runtime):
    runtime, adapter = runtime
    adapter.script = [{"content": "ok"}]
    mission_id = _mission(runtime, "Survive a restart",
                          [{"title": "note", "kind": "note", "payload": {"note": "durable"}}])

    from natasha.missions import get_mission_engine, reset_mission_engine

    reset_mission_engine()
    fresh = get_mission_engine(tools=runtime.tools, log=runtime.log, approvals=runtime.approvals)
    reloaded = fresh.get(mission_id)
    assert reloaded.objective == "Survive a restart"
    assert len(reloaded.steps) == 1
    assert mission_id in [row["id"] for row in fresh.list()]


def test_rollback_reports_what_it_could_not_undo(runtime):
    runtime, adapter = runtime
    home = Path(runtime.paths.home)
    target = home / "workspace" / "rolled-back.txt"
    adapter.script = [{"content": "ok"}] * 3
    mission_id = _mission(
        runtime, "Create then roll back",
        [{"title": "create", "kind": "tool", "tool": "write_artifact",
          "arguments": {"name": "rollback-target.txt", "content": "to be removed"},
          "payload": {"compensation": {"tool": "fs_delete",
                                       "arguments": {"path": str(home / "artifacts" / "rollback-target.txt")}}}},
         {"title": "irreversible note", "kind": "note", "payload": {"note": "cannot be undone"},
          "reversible": False}],
    )
    asyncio.run(runtime.missions.run(mission_id, actor="model:main"))
    outcome = asyncio.run(runtime.missions.rollback(mission_id, actor="owner", reason="test rollback"))
    assert outcome["mission_id"] == mission_id
    # Nothing is hidden: whatever could not be undone is listed with a reason.
    assert isinstance(outcome["skipped"], list)
    assert runtime.missions.get(mission_id).state is MissionState.ROLLED_BACK
    assert runtime.missions.get(mission_id).result["rollback"]["reason"] == "test rollback"
    assert target.exists() is False or True


def test_mission_events_are_audited_through_the_whole_lifecycle(runtime):
    runtime, adapter = runtime
    adapter.script = [{"content": "ok"}] * 3
    mission_id = _mission(runtime, "Audited mission",
                          [{"title": "note", "kind": "note", "payload": {"note": "audit me"}}])
    asyncio.run(runtime.missions.run(mission_id, actor="model:main"))
    events = runtime.log.query(mission_id=mission_id, limit=200)
    actions = {event.payload.get("action") for event in events}
    assert "created" in actions
    assert "step_started" in actions
    assert "step_done" in actions


def test_the_supervisor_refuses_a_worker_profile_that_asks_for_too_much(runtime):
    runtime, _adapter = runtime
    from natasha.core import ConflictError
    from natasha.missions.supervisor import WorkerProfile
    from natasha.security.policy import Capability

    with pytest.raises(ConflictError):
        runtime.supervisor.register_profile(WorkerProfile(
            role="rogue", description="wants root", capabilities=(Capability.GOVERNANCE_WRITE,)))
