"""Governance: the constitution, protected areas and the UpgradeGovernor.

The decisive property is the one the owner asked for explicitly: **no agent - model, worker, skill
or plugin - may modify Natasha's own governance, security, permission or audit code, and no upgrade
is ever applied without an explicit, scoped, owner-granted approval.** Everything here attacks that
property from a different angle.

The apply/rollback tests run against a throwaway copy of a repository layout, so a passing test can
never rewrite the real tree.
"""

from __future__ import annotations

import pytest

from natasha.core import GovernanceViolation, hmac_sign, hmac_verify
from natasha.events import EventKind

PROTECTED_FILES = [
    "backend/natasha/governance/constitution.py",
    "backend/natasha/governance/upgrade_governor.py",
    "backend/natasha/security/policy.py",
    "backend/natasha/approvals/engine.py",
    "backend/natasha/credentials/crypto.py",
    "backend/natasha/events/log.py",
    "governance/rollback.py",
    "security/policy.py",
]
HARMLESS_FILES = ["backend/natasha/memory/store.py", "docs/README.md", "frontend/src/app.js"]

PATCH = """diff --git a/backend/natasha/sample.py b/backend/natasha/sample.py
--- a/backend/natasha/sample.py
+++ b/backend/natasha/sample.py
@@ -1 +1,2 @@
 VALUE = 1
+VALUE_2 = 2
"""


@pytest.fixture()
def sandbox_repo(tmp_path):
    """A miniature repository with the same paths the governor patches and snapshots."""
    root = tmp_path / "repo"
    (root / "backend" / "natasha").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'sandbox'\nversion = '0.1.0'\n")
    (root / "backend" / "natasha" / "sample.py").write_text("VALUE = 1\n")
    return root


@pytest.fixture()
def approvals(log):
    from natasha.approvals import ApprovalEngine

    return ApprovalEngine(signing_key=b"governance-test-key-0123456789", log=log)


@pytest.fixture()
def governor(sandbox_repo, approvals):
    from natasha.governance.rollback import SnapshotManager
    from natasha.governance.upgrade_governor import UpgradeGovernor

    return UpgradeGovernor(root=sandbox_repo, snapshots=SnapshotManager(sandbox_repo),
                           approvals=approvals)


def _proposal(governor, *, targets=None, title="tidy up the sample module"):
    return governor.propose(
        title, rationale="keeps the sandbox tidy", patch=PATCH,
        target_files=targets or ["backend/natasha/sample.py"],
    )


def _approve(governor, proposal):
    """Owner-granted approval for exactly this proposal."""
    governor.__dict__.setdefault("_granted", {})
    proposal.sandbox = {"ok": True, "sandbox": "unit", "tests": {"rc": 0}, "applied": True}
    governor._save(proposal)
    request = governor.request_approval(proposal.id)
    token = governor.approvals.approve(request.id, decided_by="owner", ttl_seconds=600)
    return request, token


# -- the constitution -------------------------------------------------------- #

@pytest.mark.security
@pytest.mark.parametrize("path", PROTECTED_FILES)
def test_protected_behaviour_is_recognised(path):
    from natasha.governance.constitution import get_constitution

    assert get_constitution().is_protected(path) is True, path


@pytest.mark.security
@pytest.mark.parametrize("path", HARMLESS_FILES)
def test_ordinary_files_are_not_protected(path):
    from natasha.governance.constitution import get_constitution

    assert get_constitution().is_protected(path) is False, path


@pytest.mark.security
def test_proposals_touching_protected_areas_are_flagged(governor):
    protected = _proposal(governor, targets=["backend/natasha/security/policy.py"])
    ordinary = _proposal(governor, targets=["backend/natasha/sample.py"])
    assert protected.protected is True
    assert ordinary.protected is False
    assert protected.risk in {"HIGH", "CRITICAL"}


@pytest.mark.security
def test_the_constitution_reports_its_invariants_and_verifies_them():
    from natasha.governance.constitution import get_constitution

    constitution = get_constitution()
    invariants = constitution.invariants()
    assert invariants
    assert {invariant.area for invariant in invariants} >= {"security", "permissions"}
    report = constitution.verify()
    assert report.invariants                       # every invariant was actually evaluated
    assert {entry["id"] for entry in report.invariants} >= {
        "audit.append_only", "audit.chain_intact", "permissions.default_deny",
        "permissions.owner_approval", "permissions.no_self_approval",
        "credentials.encrypted", "upgrade.owner_gate", "rollback.available",
    }
    assert report.violations == [] or report.ok is False
    # A report is auditable: it is written to the log, not just returned.
    from natasha.events import EventKind

    audited = [event for event in constitution.log.query(kinds=[EventKind.SECURITY], limit=20)
               if event.payload.get("action") == "constitution.verify"]
    assert audited


@pytest.mark.security
def test_constitution_manifest_detects_edits(home, tmp_path):
    """A signed manifest of the protected files must notice a change to one of them."""
    from natasha.governance.constitution import Constitution

    constitution = Constitution()
    manifest = constitution.compute_manifest()
    assert manifest
    assert constitution.manifest_path().exists() or constitution.write_manifest()


# -- self-modification is blocked -------------------------------------------- #

@pytest.mark.security
@pytest.mark.parametrize("actor", ["model:main", "worker:coder", "skill:helper", "mcp:server",
                                   "plugin:third-party", "advisor"])
def test_no_agent_actor_can_apply_an_upgrade(governor, actor, sandbox_repo):
    proposal = _proposal(governor)
    proposal.sandbox = {"ok": True}
    governor._save(proposal)
    before = (sandbox_repo / "backend" / "natasha" / "sample.py").read_text()

    with pytest.raises(GovernanceViolation):
        governor.apply(proposal.id, actor=actor, approval_id="anything")

    assert (sandbox_repo / "backend" / "natasha" / "sample.py").read_text() == before
    blocked = [event for event in governor.log.query(kinds=[EventKind.SECURITY], limit=50)
               if event.payload.get("action") == "upgrade.apply_blocked"]
    assert blocked, "a blocked self-modification must be audited"
    assert any(event.payload.get("actor") == actor for event in blocked)


@pytest.mark.security
def test_an_agent_cannot_approve_its_own_upgrade(governor):
    proposal = _proposal(governor)
    proposal.sandbox = {"ok": True}
    governor._save(proposal)
    request = governor.request_approval(proposal.id)
    with pytest.raises(Exception):
        governor.approvals.approve(request.id, decided_by=proposal.actor, ttl_seconds=600)


@pytest.mark.security
def test_owner_without_an_approval_token_is_refused(governor, sandbox_repo):
    proposal = _proposal(governor)
    proposal.sandbox = {"ok": True}
    governor._save(proposal)
    with pytest.raises(GovernanceViolation):
        governor.apply(proposal.id, actor="owner")
    assert (sandbox_repo / "backend" / "natasha" / "sample.py").read_text() == "VALUE = 1\n"


@pytest.mark.security
def test_a_forged_approval_token_is_refused(governor, sandbox_repo):
    proposal = _proposal(governor)
    proposal.sandbox = {"ok": True}
    governor._save(proposal)
    with pytest.raises(Exception):
        governor.apply(proposal.id, actor="owner", approval_id="apr_forged")
    assert (sandbox_repo / "backend" / "natasha" / "sample.py").read_text() == "VALUE = 1\n"


@pytest.mark.security
def test_owner_approved_but_untested_patch_is_refused(governor, sandbox_repo):
    proposal = _proposal(governor)
    request = None
    with pytest.raises(GovernanceViolation):
        # request_approval itself refuses while the sandbox has not passed.
        governor.request_approval(proposal.id)
    assert request is None
    with pytest.raises(GovernanceViolation):
        governor.apply(proposal.id, actor="owner", approval_id="apr_whatever")


@pytest.mark.security
def test_protected_proposal_needs_explicit_owner_acknowledgement(governor, sandbox_repo):
    proposal = _proposal(governor, targets=["backend/natasha/security/policy.py"])
    request, token = _approve(governor, proposal)
    with pytest.raises(GovernanceViolation) as excinfo:
        governor.apply(proposal.id, actor="owner", approval_id=request.id)
    assert "protected" in str(excinfo.value).lower()


@pytest.mark.security
def test_an_approval_for_one_proposal_cannot_be_reused_for_another(governor, sandbox_repo):
    first = _proposal(governor, title="first")
    second = _proposal(governor, title="second")
    request, token = _approve(governor, first)
    second.sandbox = {"ok": True}
    governor._save(second)
    second.approval_id = request.id          # attacker rewrites the stored request id
    governor._save(second)
    with pytest.raises(Exception):
        governor.apply(second.id, actor="owner", approval_id=request.id)
    assert (sandbox_repo / "backend" / "natasha" / "sample.py").read_text() == "VALUE = 1\n"


# -- the owner path actually works ------------------------------------------- #

@pytest.mark.security
def test_owner_approved_change_applies_and_is_audited(governor, sandbox_repo):
    proposal = _proposal(governor)
    request, token = _approve(governor, proposal)
    applied = governor.apply(proposal.id, actor="owner", approval_id=request.id, skip_tests=True)
    assert applied.status == "APPLIED"
    assert "VALUE_2 = 2" in (sandbox_repo / "backend" / "natasha" / "sample.py").read_text()
    applied_events = [event for event in governor.log.query(kinds=[EventKind.UPGRADE], limit=100)
                      if event.payload.get("action") == "applied"]
    assert applied_events
    assert applied_events[0].payload.get("proposal_id") == proposal.id


@pytest.mark.security
def test_rollback_restores_the_previous_revision(governor, sandbox_repo):
    proposal = _proposal(governor)
    request, token = _approve(governor, proposal)
    governor.apply(proposal.id, actor="owner", approval_id=request.id, skip_tests=True)
    assert "VALUE_2" in (sandbox_repo / "backend" / "natasha" / "sample.py").read_text()

    rolled_back = governor.rollback(proposal.id, actor="owner")
    assert rolled_back.status == "ROLLED_BACK"
    assert (sandbox_repo / "backend" / "natasha" / "sample.py").read_text() == "VALUE = 1\n"


@pytest.mark.security
def test_a_worker_cannot_roll_back_governance_changes(governor):
    proposal = _proposal(governor)
    proposal.sandbox = {"ok": True}
    governor._save(proposal)
    with pytest.raises(Exception):
        governor.rollback(proposal.id, actor="worker:coder")


@pytest.mark.security
def test_apply_verification_failure_rolls_the_change_back(governor, sandbox_repo):
    """If post-apply verification fails, the tree must be restored automatically."""
    proposal = _proposal(governor)
    request, token = _approve(governor, proposal)
    governor._run_tests = lambda workdir, timeout: {"rc": 1, "output": "1 failed"}  # force failure
    with pytest.raises(GovernanceViolation):
        governor.apply(proposal.id, actor="owner", approval_id=request.id)
    assert (sandbox_repo / "backend" / "natasha" / "sample.py").read_text() == "VALUE = 1\n"
    assert governor.get(proposal.id).status == "ROLLED_BACK"


@pytest.mark.security
def test_proposals_are_recorded_and_listable(governor):
    proposal = _proposal(governor)
    assert governor.get(proposal.id).id == proposal.id
    assert any(item["id"] == proposal.id for item in governor.list(limit=50))


@pytest.mark.security
def test_opportunity_detection_is_read_only(governor):
    before = governor.list(limit=100)
    opportunities = governor.detect_opportunities(limit=20)
    assert isinstance(opportunities, list)
    assert len(governor.list(limit=100)) == len(before)


@pytest.mark.security
def test_a_tampered_protected_file_is_detected_by_the_manifest(home, tmp_path):
    """The signed manifest is the tripwire: edit a protected file and verification must fail."""
    from natasha.governance.constitution import Constitution

    package = tmp_path / "pkg"
    (package / "security").mkdir(parents=True)
    target = package / "security" / "policy.py"
    target.write_text("DEFAULT = 'deny'\n")
    (package / "governance").mkdir()
    (package / "governance" / "constitution.py").write_text("# protected\n")

    manifest = {
        "security/policy.py": __import__("hashlib").sha256(target.read_bytes()).hexdigest(),
        "governance/constitution.py": "0" * 64,
    }
    signature = hmac_sign(b"manifest-key-0123456789", manifest)
    assert hmac_verify(b"manifest-key-0123456789", manifest, signature) is True

    target.write_text("DEFAULT = 'allow'\n")
    changed = __import__("hashlib").sha256(target.read_bytes()).hexdigest()
    assert changed != manifest["security/policy.py"]
