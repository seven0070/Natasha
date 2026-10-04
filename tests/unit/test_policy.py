"""The capability policy engine: default-deny, capability gates, worker limits, owner authority."""

from __future__ import annotations

import pytest

from natasha.security.policy import Capability, Effect, PolicyEngine, PolicyRequest


def _request(capability: Capability, *, actor: str = "model:main", resource: str = "test:resource",
             context: dict | None = None, risk=None, scope=None) -> PolicyRequest:
    """The command/URL/path under judgement goes in ``resource``; extra facts go in ``context``."""
    return PolicyRequest(capability=capability, actor=actor, resource=resource,
                         context=context or {}, risk_hint=risk, mission_scope=scope)


def test_default_deny_for_unknown_capability(policy):
    decision = policy.check(PolicyRequest(capability=Capability.GOVERNANCE_WRITE, actor="model:main",
                                          resource="governance:constitution"))
    assert decision.effect is Effect.DENY


def test_model_cannot_execute_shell_without_approval(policy):
    decision = policy.check(_request(Capability.SHELL_EXEC, resource="unlisted-binary --flag"))
    assert decision.effect is Effect.APPROVAL
    assert decision.risk >= decision.risk.HIGH


def test_owner_satisfies_the_approval_gate_directly(policy):
    decision = policy.check(_request(Capability.SHELL_EXEC, actor="owner", resource="unlisted-binary --flag"))
    assert decision.effect is Effect.ALLOW


def test_worker_actor_cannot_run_shell_outside_a_mission_scope(policy):
    decision = policy.check(_request(Capability.SHELL_EXEC, actor="worker:coder",
                                     resource="unlisted-binary --flag"))
    assert decision.effect is Effect.DENY
    assert "mission scope" in decision.reason

    scoped = policy.check(_request(Capability.SHELL_EXEC, actor="worker:coder",
                                   resource="unlisted-binary --flag", scope=[Capability.SHELL_EXEC]))
    assert scoped.effect is Effect.APPROVAL


def test_worker_cannot_hold_forbidden_capabilities(policy):
    for capability in (Capability.CREDENTIAL_ADMIN, Capability.GOVERNANCE_WRITE, Capability.IDENTITY_WRITE,
                       Capability.SYSTEM_CONFIG, Capability.UPGRADE_APPLY, Capability.MCP_INSTALL,
                       Capability.SKILL_INSTALL):
        decision = policy.check(_request(capability, actor="worker:coder",
                                         resource=f"{capability.value}:x"))
        assert decision.effect is Effect.DENY, capability


def test_worker_network_access_is_gated_by_host_and_scope(policy):
    """Workers never get silent network access: non-allowlisted hosts need an approval."""
    unscoped = policy.check(_request(Capability.NET_HTTP, actor="worker:researcher",
                                     resource="https://example.com"))
    assert unscoped.effect is Effect.APPROVAL
    assert "not allowlisted" in unscoped.reason

    allowlisted = policy.check(_request(Capability.NET_HTTP, actor="worker:researcher",
                                        resource="https://api.openai.com/v1/models",
                                        scope=[Capability.NET_HTTP]))
    assert allowlisted.effect is Effect.ALLOW


def test_allowlisted_hosts_are_the_only_silent_network_access(policy):
    decision = policy.check(_request(Capability.NET_HTTP, actor="model:main",
                                     resource="https://example.com:443/x",
                                     context={"allow_host_override": "example.com"}))
    assert decision.effect is Effect.ALLOW
    assert "no_credential_leakage_to_redirects" in decision.obligations


def test_file_writes_are_confined_to_the_configured_roots(policy, home):
    inside = policy.check(PolicyRequest(capability=Capability.FS_WRITE, actor="owner",
                                        resource=str(home / "workspace" / "note.txt")))
    assert inside.effect is Effect.ALLOW
    outside = policy.check(PolicyRequest(capability=Capability.FS_WRITE, actor="model:main",
                                         resource="/etc/passwd"))
    assert outside.effect is Effect.DENY
    assert "outside permitted roots" in outside.reason


def test_reads_outside_home_require_approval_or_deny(policy):
    decision = policy.check(PolicyRequest(capability=Capability.FS_READ, actor="model:main",
                                          resource="/etc/shadow"))
    assert decision.effect in (Effect.DENY, Effect.APPROVAL)


def test_shell_metacharacters_never_run_uninterpreted(policy):
    """Pipes and substitutions are never handed to a shell; the obligation records that."""
    model = policy.check(PolicyRequest(capability=Capability.SHELL_EXEC, actor="model:main",
                                       resource="ls | wc -l"))
    assert model.effect is Effect.APPROVAL
    assert "no_shell_interpretation" in model.obligations
    assert "audit_full_command" in model.obligations

    owner = policy.check(PolicyRequest(capability=Capability.SHELL_EXEC, actor="owner",
                                       resource="ls | wc -l"))
    assert owner.effect is Effect.ALLOW          # the owner is present and acting
    assert "no_shell_interpretation" in owner.obligations


def test_denylisted_commands_are_denied_outright(policy):
    decision = policy.check(PolicyRequest(capability=Capability.SHELL_EXEC, actor="owner",
                                          resource="rm -rf /"))
    assert decision.effect is Effect.DENY


def test_critical_risk_always_requires_the_owner(policy):
    from natasha.core.risk import RiskLevel

    decision = policy.check(PolicyRequest(capability=Capability.DATA_QUERY, actor="model:main",
                                          resource="data:query", risk_hint=RiskLevel.CRITICAL))
    assert decision.effect is Effect.APPROVAL
    assert decision.risk is RiskLevel.CRITICAL


def test_decisions_explain_themselves(policy):
    decision = policy.check(_request(Capability.GOVERNANCE_WRITE, actor="worker:coder"))
    payload = decision.to_dict()
    assert payload["effect"] == "deny"
    assert payload["capability"] == "governance.write"
    assert payload["reason"]
