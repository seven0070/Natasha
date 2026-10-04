"""Permission model: default deny, owner-only capabilities, worker containment, path escapes.

Each test maps to a threat in the master build order: an agent that talks itself into privilege,
a worker that widens its own scope, or a path/shell trick that escapes the sandbox.
"""

from __future__ import annotations

import pytest

from natasha.core import PolicyDenied
from natasha.security.policy import (
    OWNER_ONLY,
    WORKER_FORBIDDEN,
    Capability,
    Effect,
    PolicyRequest,
)


def _req(capability, actor="model:main", resource="", scope=None, **context):
    return PolicyRequest(capability=capability, actor=actor, resource=resource,
                         mission_scope=scope, context=dict(context))


@pytest.mark.security
@pytest.mark.parametrize("capability", sorted(OWNER_ONLY, key=lambda c: c.value))
def test_owner_only_capabilities_are_refused_to_agents(policy, capability):
    for actor in ("model:main", "worker:coder", "skill:helper", "mcp:server"):
        decision = policy.check(_req(capability, actor=actor, resource="credential://x",
                                    scope=[capability]))
        assert decision.effect is Effect.DENY, (capability, actor)
        assert decision.risk.name == "CRITICAL"


@pytest.mark.security
@pytest.mark.parametrize("capability", sorted(WORKER_FORBIDDEN, key=lambda c: c.value))
def test_workers_can_never_hold_governance_capabilities(policy, capability):
    decision = policy.check(_req(capability, actor="worker:coder", resource="credential://x",
                                 scope=[capability]))
    assert decision.effect is Effect.DENY
    assert decision.risk.name == "CRITICAL"


@pytest.mark.security
def test_a_worker_without_a_scope_is_inert(policy, home):
    """Legitimate, in-root resources - the refusal must come from the missing scope, not the path."""
    inside = str(home / "workspace" / "notes.md")
    for capability, resource in (
        (Capability.SHELL_EXEC, "ls"),
        (Capability.FS_WRITE, inside),
        (Capability.MEMORY_WRITE, "note"),
    ):
        decision = policy.check(_req(capability, actor="worker:coder", resource=resource))
        assert decision.effect is Effect.DENY, (capability, decision.reason)
        assert "mission scope" in decision.reason


@pytest.mark.security
def test_a_worker_cannot_widen_its_own_scope(policy):
    decision = policy.check(_req(Capability.SHELL_EXEC, actor="worker:coder", resource="ls",
                                scope=[Capability.FS_READ]))
    assert decision.effect is Effect.DENY
    assert "outside the mission scope" in decision.reason or "no grant" in decision.reason


@pytest.mark.security
def test_unknown_capabilities_are_a_hard_error(policy):
    with pytest.raises(PolicyDenied):
        policy.check(PolicyRequest(capability="root.everything", actor="model:main"))


@pytest.mark.security
def test_setuid_writes_are_refused(policy, home):
    decision = policy.check(_req(Capability.FS_WRITE, resource="/usr/bin/evil"))
    assert decision.effect is Effect.DENY
    assert "outside permitted roots" in decision.reason


@pytest.mark.security
def test_traversal_out_of_the_workspace_is_refused(policy, home):
    escape = str(home / "workspace" / ".." / ".." / "etc" / "passwd")
    decision = policy.check(_req(Capability.FS_WRITE, resource=escape))
    assert decision.effect is Effect.DENY
    assert "outside permitted roots" in decision.reason or "denied" in decision.reason


@pytest.mark.security
def test_symlink_escape_is_resolved_before_the_check(policy, home, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    link = home / "workspace" / "link.txt"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists():
        link.unlink()
    link.symlink_to(outside)
    # The engine never returns the link path: the caller gets the *real* target, so the check and
    # the action cannot disagree about which file is touched.
    assert str(policy.resolve_path(str(link))) == str(outside)
    decision = policy.check(_req(Capability.FS_WRITE, resource=str(link)))
    assert decision.effect is Effect.DENY
    assert "outside permitted roots" in decision.reason


@pytest.mark.security
def test_environment_and_key_material_are_denied_paths(policy, home):
    for target in ("/etc/shadow", str(home / ".ssh" / "id_rsa"), str(home / "secrets" / ".env")):
        decision = policy.check(_req(Capability.FS_READ, resource=target))
        assert decision.effect is Effect.DENY, target


@pytest.mark.security
def test_models_never_get_unguarded_high_risk_actions(policy):
    """No non-owner actor reaches a HIGH/CRITICAL action without an approval obligation."""
    for capability in (Capability.SHELL_EXEC, Capability.CODE_EXEC, Capability.INPUT_CONTROL):
        decision = policy.check(_req(capability, actor="model:main", resource="ls"))
        assert decision.effect is Effect.APPROVAL
        assert decision.risk.name in {"HIGH", "CRITICAL"}


@pytest.mark.security
def test_owner_authority_is_not_transferable_to_a_model_actor(policy):
    owner = policy.check(_req(Capability.UPGRADE_APPLY, actor="owner"))
    model = policy.check(_req(Capability.UPGRADE_APPLY, actor="model:main", scope=[Capability.UPGRADE_APPLY]))
    assert owner.effect is Effect.ALLOW
    assert model.effect is Effect.DENY
