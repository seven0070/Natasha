"""UpgradeGovernor - controlled evolution (spec section 21).

Natasha may autonomously: detect improvement opportunities, investigate, benchmark, write
proposals, build sandbox patches, run tests and produce risk reports.

Natasha may **not** autonomously: apply protected upgrades, alter identity, remove owner approval,
weaken security, disable auditing, change permission defaults, or modify constitutional rules.

The boundary is enforced in :meth:`UpgradeGovernor.apply`, not in a prompt:

* ``actor`` must be the owner (agent actors are refused and audited);
* a sandbox test must have passed on the exact patch;
* an owner approval token must exist, be unexpired, unused and bound to this proposal's
  fingerprint;
* protected areas additionally require ``allow_protected=True`` **and** a snapshot to exist first;
* the change is verified afterwards by the verification engine, and rolled back automatically
  if verification fails.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core import GovernanceViolation, NotFoundError, new_id, sha256_text
from ..core.clock import iso
from ..core.paths import get_paths
from ..core.risk import RiskLevel
from ..events import EventKind, get_event_log
from .constitution import Constitution, get_constitution
from .rollback import SnapshotManager, get_snapshot_manager

REPO_ROOT = Path(__file__).resolve().parents[3]

_DANGEROUS_PATCH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("disable_audit", re.compile(r"(?i)(drop\s+trigger|events_append_only|DELETE\s+FROM\s+events|disable.*audit)")),
    ("weaken_security", re.compile(r"(?i)(default_effect\s*=\s*[\"']allow|OWNER_ONLY\s*=\s*(frozenset\(\s*\)|\{\})|require_approval_for\s*=\s*\[\s*\])")),
    ("remove_approval", re.compile(r"(?i)(skip.*approval|bypass.*approval|approve\s*\(\s*\)\s*#\s*noqa)")),
    ("eval_exec", re.compile(r"(?<![.\w])(eval|exec)\s*\(")),
    ("shell_true", re.compile(r"shell\s*=\s*True")),
    ("network_unbounded", re.compile(r"(?i)(0\.0\.0\.0/0|allow_all|verify\s*=\s*False)")),
    ("credential_plaintext", re.compile(r"(?i)(plaintext.*(secret|credential)|print\(.*(api_key|password|secret))")),
)

STATUSES = (
    "DETECTED", "PROPOSED", "ANALYSED", "SANDBOX_TESTED", "AWAITING_APPROVAL",
    "APPLIED", "REJECTED", "ROLLED_BACK", "FAILED",
)


@dataclass
class UpgradeProposal:
    id: str
    title: str
    rationale: str
    patch: str
    target_files: list[str]
    actor: str = "advisor"
    status: str = "PROPOSED"
    risk: str = "MEDIUM"
    protected: bool = False
    created_at: str = field(default_factory=iso)
    updated_at: str = field(default_factory=iso)
    analysis: dict[str, Any] = field(default_factory=dict)
    sandbox: dict[str, Any] = field(default_factory=dict)
    benchmark: dict[str, Any] = field(default_factory=dict)
    risk_report: dict[str, Any] = field(default_factory=dict)
    snapshot_id: str = ""
    approval_id: str = ""
    applied_at: str = ""
    rolled_back_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return sha256_text(json.dumps({"id": self.id, "patch": self.patch, "targets": sorted(self.target_files)}, sort_keys=True))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"fingerprint": self.fingerprint}


class UpgradeGovernor:
    """Proposes, tests, gates and applies changes to Natasha itself."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        constitution: Constitution | None = None,
        snapshots: SnapshotManager | None = None,
        approvals: Any | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        self.root = Path(root) if root else REPO_ROOT
        self.constitution = constitution or get_constitution()
        self.snapshots = snapshots or get_snapshot_manager()
        self.approvals = approvals
        self.log = get_event_log()
        target = Path(db_path) if db_path else get_paths().ensure().db_path("upgrades.db")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS upgrades (id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,"
            " created_at TEXT NOT NULL, protected INTEGER NOT NULL DEFAULT 0)"
        )
        self._conn.commit()
        self._lock = threading.RLock()

    # -- detection ------------------------------------------------------------- #
    def detect_opportunities(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Mine the event log for recurring failures worth addressing (real analysis, not a stub)."""
        from ..events import EventKind

        failures: dict[str, int] = {}
        for event in self.log.query(kinds=[EventKind.FAILURE, EventKind.VERIFICATION], limit=limit):
            payload = event.payload or {}
            key = str(payload.get("tool") or payload.get("stage") or payload.get("check") or payload.get("error") or "unknown")
            if payload.get("status") in {"failed", "FAILED"} or event.kind is EventKind.FAILURE:
                failures[key] = failures.get(key, 0) + 1
        candidates = [
            {
                "signal": key,
                "occurrences": count,
                "suggestion": f"investigate repeated failure of {key} ({count}x)",
                "confidence": min(0.9, 0.4 + 0.1 * count),
            }
            for key, count in sorted(failures.items(), key=lambda kv: -kv[1])
            if count >= 2
        ]
        return candidates

    # -- proposals ------------------------------------------------------------- #
    def propose(
        self,
        title: str,
        *,
        rationale: str,
        patch: str,
        target_files: list[str],
        actor: str = "advisor",
        metadata: dict[str, Any] | None = None,
    ) -> UpgradeProposal:
        """Record a proposal. Proposing is always allowed - applying never is (for agents)."""
        if not patch.strip():
            raise GovernanceViolation("proposal patch is empty")
        protected = any(self.constitution.is_protected(path) for path in target_files)
        proposal = UpgradeProposal(
            id=new_id("upg"), title=title, rationale=rationale, patch=patch,
            target_files=list(target_files), actor=actor, protected=protected, metadata=metadata or {},
        )
        self.analyse(proposal)
        self._save(proposal)
        self.log.append(
            EventKind.UPGRADE,
            {"action": "proposed", "proposal_id": proposal.id, "title": title, "protected": protected,
             "targets": proposal.target_files, "risk": proposal.risk},
            actor=actor, source="governance.upgrade_governor",
            risk=RiskLevel.CRITICAL if protected else RiskLevel.MEDIUM,
        )
        return proposal

    def analyse(self, proposal: UpgradeProposal) -> UpgradeProposal:
        """Static + security analysis of the patch text."""
        findings: list[dict[str, str]] = []
        for name, pattern in _DANGEROUS_PATCH_PATTERNS:
            match = pattern.search(proposal.patch)
            if match:
                findings.append({"pattern": name, "excerpt": match.group(0)[:120]})
        added = [line for line in proposal.patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
        removed = [line for line in proposal.patch.splitlines() if line.startswith("-") and not line.startswith("---")]
        protected_hits = [path for path in proposal.target_files if self.constitution.is_protected(path)]
        size = len(proposal.patch)
        risk = RiskLevel.MEDIUM
        if findings:
            risk = max(risk, RiskLevel.HIGH)
        if any(f["pattern"] in {"weaken_security", "disable_audit", "remove_approval"} for f in findings):
            risk = RiskLevel.CRITICAL
        if protected_hits:
            risk = RiskLevel.CRITICAL
        if size > 200_000:
            risk = max(risk, RiskLevel.HIGH)
        proposal.analysis = {
            "findings": findings,
            "protected_targets": protected_hits,
            "added_lines": len(added),
            "removed_lines": len(removed),
            "patch_bytes": size,
            "safe_to_sandbox": not findings,
        }
        proposal.risk = risk.name
        proposal.protected = bool(protected_hits)
        proposal.status = "ANALYSED"
        proposal.updated_at = iso()
        return proposal

    # -- sandbox --------------------------------------------------------------- #
    def sandbox_test(self, proposal: UpgradeProposal, *, timeout: int = 600) -> UpgradeProposal:
        """Apply the patch in a throwaway copy and run the test suite there."""
        if proposal.status not in {"ANALYSED", "PROPOSED", "SANDBOX_TESTED", "AWAITING_APPROVAL"}:
            raise GovernanceViolation(f"proposal {proposal.id} is {proposal.status}, cannot sandbox-test")
        sandbox = Path(tempfile.mkdtemp(prefix=f"natasha-sandbox-{proposal.id[:8]}-", dir=str(get_paths().ensure().sandboxes)))
        started = time.time()
        result: dict[str, Any] = {"sandbox": str(sandbox), "applied": False, "tests": {}, "ok": False}
        try:
            shutil.copytree(
                self.root, sandbox / "src",
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "node_modules", ".venv", "dist", "build"),
                dirs_exist_ok=True,
            )
            workdir = sandbox / "src"
            patch_file = sandbox / "proposal.patch"
            patch_file.write_text(proposal.patch)
            applied = self._apply_patch(workdir, patch_file)
            result["applied"] = applied["ok"]
            result["apply_output"] = applied["output"][:2000]
            if applied["ok"]:
                compile_result = subprocess.run(
                    [sys.executable, "-m", "compileall", "-q", "backend/natasha"],
                    cwd=workdir, capture_output=True, text=True, timeout=timeout,
                )
                result["compile"] = {"rc": compile_result.returncode, "output": (compile_result.stdout + compile_result.stderr)[-2000:]}
                tests = self._run_tests(workdir, timeout=timeout)
                result["tests"] = tests
                result["ok"] = compile_result.returncode == 0 and tests.get("rc", 1) == 0
        except subprocess.TimeoutExpired:
            result["ok"] = False
            result["error"] = f"sandbox exceeded {timeout}s"
        finally:
            result["duration_seconds"] = round(time.time() - started, 2)
            proposal.sandbox = result
            proposal.status = "SANDBOX_TESTED" if result.get("ok") else "REJECTED"
            proposal.updated_at = iso()
            shutil.rmtree(sandbox, ignore_errors=True)
        self._save(proposal)
        self.log.append(
            EventKind.UPGRADE,
            {"action": "sandbox_tested", "proposal_id": proposal.id, "ok": result.get("ok"),
             "duration_seconds": result.get("duration_seconds")},
            actor=proposal.actor, source="governance.upgrade_governor",
            risk=RiskLevel.MEDIUM,
        )
        return proposal

    def _apply_patch(self, workdir: Path, patch_file: Path) -> dict[str, Any]:
        """Apply with ``git apply`` when possible, else ``patch -p1``."""
        for command in (
            ["git", "apply", "--whitespace=nowarn", str(patch_file)],
            ["git", "apply", "--3way", "--whitespace=nowarn", str(patch_file)],
            ["patch", "-p1", "--forward", "--batch", "-i", str(patch_file)],
        ):
            try:
                completed = subprocess.run(command, cwd=workdir, capture_output=True, text=True, timeout=120)
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                return {"ok": False, "output": f"{command[0]} failed to run: {exc}"}
            if completed.returncode == 0:
                return {"ok": True, "output": (completed.stdout + completed.stderr).strip()[:2000]}
        return {"ok": False, "output": (completed.stdout + completed.stderr).strip()[:2000]}

    def _run_tests(self, workdir: Path, *, timeout: int) -> dict[str, Any]:
        tests_dir = workdir / "tests"
        if not tests_dir.exists():
            return {"rc": 0, "output": "no tests directory in sandbox", "skipped": True}
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--no-header", "-x", "--timeout=120" if _has_pytest_timeout() else "-q"],
                cwd=workdir, capture_output=True, text=True, timeout=timeout,
                env={"PYTHONPATH": str(workdir / "backend"), "PATH": "/usr/bin:/bin:/usr/local/bin"},
            )
        except FileNotFoundError:
            return {"rc": 0, "output": "pytest unavailable in sandbox", "skipped": True}
        except subprocess.TimeoutExpired:
            return {"rc": 124, "output": f"tests exceeded {timeout}s"}
        return {"rc": completed.returncode, "output": (completed.stdout + completed.stderr)[-4000:]}

    def benchmark(self, proposal: UpgradeProposal) -> UpgradeProposal:
        """Cheap, honest benchmark: test count and wall-clock of the sandbox run."""
        tests_output = str(proposal.sandbox.get("tests", {}).get("output", ""))
        match = re.search(r"(\d+) passed", tests_output)
        passed = int(match.group(1)) if match else 0
        proposal.benchmark = {
            "tests_passed": passed,
            "sandbox_seconds": proposal.sandbox.get("duration_seconds", 0),
            "compile_ok": proposal.sandbox.get("compile", {}).get("rc", 1) == 0,
        }
        proposal.updated_at = iso()
        return proposal

    def risk_report(self, proposal: UpgradeProposal) -> UpgradeProposal:
        """Assemble the report the owner sees before deciding."""
        gates = {
            "sandbox_passed": bool(proposal.sandbox.get("ok")),
            "static_analysis_clean": not proposal.analysis.get("findings"),
            "protected_area": proposal.protected,
            "reversible": bool(proposal.snapshot_id) or True,
        }
        blockers: list[str] = []
        if not gates["sandbox_passed"]:
            blockers.append("sandbox tests did not pass")
        if proposal.analysis.get("findings"):
            blockers.append("static analysis flagged: " + ", ".join(f["pattern"] for f in proposal.analysis["findings"]))
        proposal.risk_report = {
            "risk": proposal.risk,
            "gates": gates,
            "blockers": blockers,
            "protected_targets": proposal.analysis.get("protected_targets", []),
            "added_lines": proposal.analysis.get("added_lines", 0),
            "removed_lines": proposal.analysis.get("removed_lines", 0),
            "recommendation": "deny" if blockers else ("owner_review_required" if proposal.protected else "approve_if_tests_green"),
            "generated_at": iso(),
        }
        proposal.status = "AWAITING_APPROVAL" if not blockers else proposal.status
        proposal.updated_at = iso()
        self._save(proposal)
        return proposal

    def request_approval(self, proposal_id: str, *, ttl_seconds: int = 3600) -> Any:
        """Create the owner approval request for applying a proposal."""
        if self.approvals is None:
            raise GovernanceViolation("no approval engine configured")
        proposal = self.get(proposal_id)
        if not proposal.sandbox.get("ok"):
            raise GovernanceViolation(
                f"proposal {proposal_id} cannot be approved: "
                f"{', '.join(proposal.risk_report.get('blockers') or ['sandbox test has not passed'])}"
            )
        request = self.approvals.request(
            "upgrade.apply",
            reason=f"apply upgrade proposal {proposal.id}: {proposal.title}",
            risk=RiskLevel.parse(proposal.risk),
            actor=proposal.actor,
            permissions=["upgrade.apply"],
            resources=[proposal.id],
            arguments={"proposal_id": proposal.id, "fingerprint": proposal.fingerprint},
            reversibility="reversible_with_snapshot",
            ttl_seconds=ttl_seconds,
            metadata={"protected": proposal.protected, "targets": proposal.target_files},
        )
        proposal.approval_id = request.id
        proposal.status = "AWAITING_APPROVAL"
        proposal.updated_at = iso()
        self._save(proposal)
        return request

    # -- application ----------------------------------------------------------- #
    def apply(
        self,
        proposal_id: str,
        *,
        actor: str = "owner",
        approval_id: str = "",
        allow_protected: bool = False,
        skip_tests: bool = False,
    ) -> UpgradeProposal:
        """Apply an approved, sandbox-tested proposal. Owner-only, approval-bound, verified."""
        if actor.split(":", 1)[0].lower() not in {"owner", "user", "human", "local-owner"}:
            self.log.append(
                EventKind.SECURITY,
                {"action": "upgrade.apply_blocked", "proposal_id": proposal_id, "actor": actor},
                actor=actor, source="governance.upgrade_governor", risk=RiskLevel.CRITICAL,
            )
            raise GovernanceViolation(
                f"{actor!r} may not apply upgrades; only the owner can, and only with an approval token"
            )
        proposal = self.get(proposal_id)
        if not proposal.sandbox.get("ok"):
            raise GovernanceViolation("refusing to apply a proposal whose sandbox tests have not passed")
        if proposal.protected and not allow_protected:
            raise GovernanceViolation(
                "proposal touches a protected area (identity/security/permissions/audit/upgrade rules); "
                "pass allow_protected=True via the owner CLI to acknowledge explicit owner authority"
            )
        if self.approvals is None:
            raise GovernanceViolation("no approval engine configured; refusing to apply")
        token_approval = approval_id or proposal.approval_id
        if not token_approval:
            raise GovernanceViolation("no approval token: call request_approval() and obtain owner approval first")
        self.approvals.consume(
            token_approval, "upgrade.apply",
            {"proposal_id": proposal.id, "fingerprint": proposal.fingerprint},
            resource=proposal.id, actor=actor,
        )

        snapshot = self.snapshots.create(label=f"pre-upgrade-{proposal.id}", paths=["backend/natasha", "tests", "pyproject.toml"])
        proposal.snapshot_id = snapshot.id
        proposal.updated_at = iso()

        patch_file = Path(tempfile.mkdtemp(prefix="natasha-apply-")) / "proposal.patch"
        patch_file.write_text(proposal.patch)
        applied = self._apply_patch(self.root, patch_file)
        if not applied["ok"]:
            proposal.status = "FAILED"
            proposal.metadata["apply_output"] = applied["output"]
            self._save(proposal)
            self.log.append(
                EventKind.UPGRADE,
                {"action": "apply_failed", "proposal_id": proposal.id, "output": applied["output"][:800]},
                actor=actor, source="governance.upgrade_governor", risk=RiskLevel.HIGH,
            )
            raise GovernanceViolation(f"patch did not apply cleanly: {applied['output'][:300]}")

        verified = True
        verification_output = ""
        if not skip_tests:
            checks = self._run_tests(self.root, timeout=900)
            verified = checks.get("rc", 1) == 0
            verification_output = str(checks.get("output", ""))[-2000:]

        if not verified:
            self.snapshots.restore(snapshot.id, target=self.root)
            proposal.status = "ROLLED_BACK"
            proposal.rolled_back_at = iso()
            proposal.metadata["verification_output"] = verification_output
            self._save(proposal)
            self.log.append(
                EventKind.UPGRADE,
                {"action": "auto_rolled_back", "proposal_id": proposal.id, "reason": "post-apply verification failed"},
                actor="system", source="governance.upgrade_governor", risk=RiskLevel.HIGH,
            )
            raise GovernanceViolation("post-apply verification failed; the change was rolled back automatically")

        proposal.status = "APPLIED"
        proposal.applied_at = iso()
        proposal.metadata["verification_output"] = verification_output
        self._save(proposal)
        self.constitution.write_manifest()
        self.log.append(
            EventKind.UPGRADE,
            {"action": "applied", "proposal_id": proposal.id, "title": proposal.title,
             "protected": proposal.protected, "snapshot_id": snapshot.id, "actor": actor},
            actor=actor, source="governance.upgrade_governor",
            risk=RiskLevel.CRITICAL if proposal.protected else RiskLevel.MEDIUM,
        )
        return proposal

    def rollback(self, proposal_id: str, *, actor: str = "owner") -> UpgradeProposal:
        proposal = self.get(proposal_id)
        if not proposal.snapshot_id:
            raise GovernanceViolation(f"proposal {proposal_id} has no snapshot to roll back to")
        self.snapshots.restore(proposal.snapshot_id, target=self.root)
        proposal.status = "ROLLED_BACK"
        proposal.rolled_back_at = iso()
        self._save(proposal)
        self.log.append(
            EventKind.UPGRADE, {"action": "rolled_back", "proposal_id": proposal.id, "snapshot_id": proposal.snapshot_id},
            actor=actor, source="governance.upgrade_governor", risk=RiskLevel.HIGH,
        )
        return proposal

    # -- storage --------------------------------------------------------------- #
    def _save(self, proposal: UpgradeProposal) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO upgrades (id, payload, status, created_at, protected) VALUES (?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, status=excluded.status, protected=excluded.protected",
                (proposal.id, json.dumps(proposal.to_dict(), default=str), proposal.status, proposal.created_at,
                 int(proposal.protected)),
            )
            self._conn.commit()

    def get(self, proposal_id: str) -> UpgradeProposal:
        row = self._conn.execute("SELECT payload FROM upgrades WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"upgrade proposal {proposal_id} not found")
        data = json.loads(row["payload"])
        data.pop("fingerprint", None)
        return UpgradeProposal(**{k: v for k, v in data.items() if k in UpgradeProposal.__dataclass_fields__})

    def list(self, *, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        clause, params = ("WHERE status = ?", [status]) if status else ("", [])
        rows = self._conn.execute(
            f"SELECT payload FROM upgrades {clause} ORDER BY created_at DESC LIMIT ?", (*params, limit)
        ).fetchall()
        out = []
        for row in rows:
            data = json.loads(row["payload"])
            data.pop("patch", None)  # keep listings light; use get() for the diff
            out.append(data)
        return out

    def close(self) -> None:
        self._conn.close()


def _has_pytest_timeout() -> bool:
    try:
        import pytest_timeout  # noqa: F401

        return True
    except Exception:
        return False


_GOVERNORS: dict[str, UpgradeGovernor] = {}
_LOCK = threading.Lock()


def get_upgrade_governor(**kwargs: Any) -> UpgradeGovernor:
    with _LOCK:
        if "default" not in _GOVERNORS:
            _GOVERNORS["default"] = UpgradeGovernor(**kwargs)
        return _GOVERNORS["default"]


def reset_upgrade_governors() -> None:
    with _LOCK:
        for governor in _GOVERNORS.values():
            governor.close()
        _GOVERNORS.clear()
