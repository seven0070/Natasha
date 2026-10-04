"""Integration tools must be reachable *and* gated.

Two failures lived here until they were fixed, and both were invisible to the unit tests:

1. Every argument-driven connector tool (GitHub, Slack, email) was denied with
   ``cannot determine host for ''`` - the policy engine inspects the *resource*, and the registry
   never told it which host the connector would contact. The tools were registered but could never
   run, with a denial that blamed the wrong thing.
2. ``integration__email__send`` is HIGH risk yet declared ``requires_approval=False``. The policy
   engine still demanded an approval (so nothing was silently allowed), but the declaration the CLI,
   the UI and the mission planner read was a lie.

These tests hold the wiring to the policy: a dangerous tool must say it is dangerous, and a tool
that names a host must be judged on that host.
"""

from __future__ import annotations

import pytest

from natasha.core.risk import RiskLevel
from natasha.tools.base import ToolContext

pytestmark = pytest.mark.integration


def _integration_tools(rt) -> list[dict]:
    return [spec for spec in rt.tools.describe() if spec["name"].startswith("integration__")]


def test_no_dangerous_tool_hides_behind_requires_approval_false(rt):
    """The invariant behind the email defect, for every registered tool and connector."""
    liars = [(spec["name"], spec["risk"], spec["source"]) for spec in rt.tools.describe()
             if spec["risk"] in ("HIGH", "CRITICAL") and not spec["requires_approval"]]
    assert not liars, (
        "HIGH/CRITICAL tools must declare requires_approval: the policy engine is the real gate, "
        f"but this flag is what the UI, the CLI and the mission planner read: {liars}")


def test_the_email_send_tool_declares_the_risk_it_carries(rt):
    spec = rt.tools.spec("integration__email__send")
    assert spec.risk is RiskLevel.HIGH
    assert spec.requires_approval is True
    assert spec.capability.value == "net.socket"


def test_argument_driven_connectors_are_judged_on_their_host_not_on_an_empty_string(rt):
    """The exact regression: github/slack tools used to fail with 'cannot determine host'."""
    result = _run(rt, "integration__github__get_repo", {"repo": "seven0070/Natasha"})
    assert not result.ok
    assert "cannot determine host" not in result.error
    assert "no host to check" not in result.error
    # api.github.com is a real host that is simply not allowlisted: that is an approval, not a bug.
    assert "api.github.com" in result.error
    assert "approval" in result.error.lower()


def test_a_connector_that_cannot_name_its_host_is_denied_with_an_actionable_reason(rt):
    """An unconfigured SMTP server must say so - and must not blame the recipient's domain."""
    result = _run(rt, "integration__email__send",
                  {"to": "someone@example.com", "subject": "s", "body": "b"})
    assert not result.ok
    assert "no host to check" in result.error
    assert "example.com" not in result.error, "the recipient domain is not the host being contacted"
    assert "configure the connector's host" in result.error


def test_the_approval_for_a_send_is_bound_to_the_recipient_and_single_use(rt):
    rt.integrations.get("email").settings["smtp_host"] = "smtp.example.com"
    arguments = {"to": "owner@example.com", "subject": "hello", "body": "from the test"}

    blocked = _run(rt, "integration__email__send", arguments)
    assert not blocked.ok
    request_id = blocked.metadata.get("approval_request_id")
    assert request_id, blocked.to_dict()
    request = rt.approvals.get_request(request_id)
    assert request.operation == "tool.integration__email__send"
    assert request.arguments.get("to") == arguments["to"]

    rt.approvals.approve(request_id, decided_by="owner")

    # A different recipient is a different action: the grant must not carry over.
    other = _run(rt, "integration__email__send", {**arguments, "to": "attacker@example.com"},
                 approval_id=request_id)
    assert not other.ok
    assert "fingerprint" in other.error.lower() or "does not match" in other.error.lower()

    # The exact call passes policy and reaches the connector; offline the SMTP attempt fails with a
    # network error, which is proof that authorisation - not policy - is what stopped it before.
    granted = _run(rt, "integration__email__send", arguments, approval_id=request_id)
    assert "approval required" not in granted.error.lower()
    assert "policy denied" not in granted.error.lower()


def test_an_allowlisted_host_proceeds_without_an_approval(rt):
    """Owner-configured allowlists are honoured: the tool reaches the connector, not the gate."""
    rt.policy.settings.network_allow_domains = [
        *rt.policy.settings.network_allow_domains, "api.github.com"]
    rt.policy.__init__(rt.policy.settings)  # rebuild the compiled allow/deny tables
    result = _run(rt, "integration__github__get_repo", {"repo": "seven0070/Natasha"})
    assert "approval required" not in result.error.lower()
    assert "no host to check" not in result.error.lower()
    # Offline: the request itself fails (connection error), which is the connector, not the policy.
    assert not result.ok


def test_an_owner_acting_directly_is_the_authority_but_still_reaches_the_connector(rt):
    """Documented design: owner authority bypasses the *approval*, never the connector's own checks."""
    rt.integrations.get("email").settings["smtp_host"] = "smtp.example.com"
    result = _run(rt, "integration__email__send",
                  {"to": "owner@example.com", "subject": "s", "body": "b"}, actor="owner")
    assert "approval required" not in result.error.lower()
    events = [event for event in rt.log.query(limit=200)
              if (event.payload or {}).get("action") == "approval"
              and (event.payload or {}).get("outcome") == "owner_authority"]
    assert events, "an owner-authorised call must be audited as owner authority, not silently allowed"


# --------------------------------------------------------------------------- helpers

def _run(rt, name: str, arguments: dict, *, approval_id: str = "", actor: str = "model:main"):
    """Call a tool the way the executive does: a context with an actor, optionally an approval."""
    import asyncio

    async def call():
        return await rt.tools.execute(name, arguments, context=ToolContext(actor=actor),
                                      approval_id=approval_id)

    return asyncio.run(call())
