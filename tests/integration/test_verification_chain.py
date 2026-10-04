"""Verification: nothing is "done" without evidence, and failures are never smoothed over.

These tests drive the real checks against a real filesystem and real subprocesses. They are the
counterweight to the model's optimism: a missing file, a broken syntax tree, a command that exits
non-zero, a fabricated side effect or a schema violation must all come back as *unproven*.
"""

from __future__ import annotations

import hashlib
import sys

import pytest

from natasha.core import VerificationFailed
from natasha.events import EventKind
from natasha.verification import (
    BehaviorCheck,
    CheckResult,
    CheckStatus,
    CommandCheck,
    CompositeCheck,
    FileExistsCheck,
    ImportCheck,
    OutputSchemaCheck,
    PythonSyntaxCheck,
    SideEffectCheck,
    VerificationCheck,
    VerificationEngine,
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def engine(log):
    return VerificationEngine(log=log)


def _write(path, text: str):
    path.write_text(text, encoding="utf-8")
    return path


async def test_a_passing_report_says_what_it_proved(engine, tmp_path):
    artifact = _write(tmp_path / "report.md", "# Report\n\nProof lives here.\n")
    report = await engine.verify("report-artifact", [
        FileExistsCheck("artifact_exists", str(artifact), min_bytes=10),
        BehaviorCheck("has_heading", lambda _: ("# Report" in artifact.read_text(), "heading found")),
    ])
    assert report.passed is True
    assert report.unproven == []
    assert report.summary == "2/2 checks passed"
    assert report.to_dict()["passed"] is True


async def test_a_missing_file_cannot_be_claimed_as_written(engine, tmp_path):
    report = await engine.verify("missing-artifact", [
        FileExistsCheck("artifact_exists", str(tmp_path / "not-there.md")),
    ])
    assert report.passed is False
    assert [result.name for result in report.unproven] == ["artifact_exists"]
    assert "missing" in report.results[0].detail
    with pytest.raises(VerificationFailed) as excinfo:
        VerificationEngine.require(report)
    assert "artifact_exists" in str(excinfo.value)


async def test_a_truncated_file_fails_the_minimum_size_check(engine, tmp_path):
    tiny = _write(tmp_path / "tiny.txt", "x")
    report = await engine.verify("tiny", [FileExistsCheck("sized", str(tiny), min_bytes=1024)])
    assert report.passed is False


async def test_a_checksum_mismatch_is_reported_with_both_hashes(engine, tmp_path):
    artifact = _write(tmp_path / "payload.bin", "the real payload")
    wrong = hashlib.sha256(b"something else").hexdigest()
    report = await engine.verify("payload", [FileExistsCheck("checksum", str(artifact), sha256=wrong)])
    assert report.passed is False
    assert report.results[0].evidence["sha256"] != wrong
    assert report.results[0].detail == "checksum mismatch"


async def test_a_real_checksum_passes(engine, tmp_path):
    artifact = _write(tmp_path / "payload.bin", "the real payload")
    good = hashlib.sha256(artifact.read_bytes()).hexdigest()
    report = await engine.verify("payload", [FileExistsCheck("checksum", str(artifact), sha256=good)])
    assert report.passed is True


async def test_python_syntax_check_catches_a_broken_file(engine, tmp_path):
    tree = tmp_path / "code"
    tree.mkdir()
    _write(tree / "good.py", "def ok():\n    return 1\n")
    clean = await engine.verify("code", [PythonSyntaxCheck("syntax", root=str(tree))])
    assert clean.passed is True

    _write(tree / "broken.py", "def nope(:\n    pass\n")
    report = await engine.verify("code", [PythonSyntaxCheck("syntax", root=str(tree))])
    assert report.passed is False
    assert "broken.py" in report.results[0].detail


async def test_import_check_proves_a_module_really_loads(engine):
    report = await engine.verify("imports", [ImportCheck("engine_importable", "natasha.verification")])
    assert report.passed is True

    missing = await engine.verify("imports", [ImportCheck("ghost", "natasha.definitely_not_a_module")])
    assert missing.passed is False
    assert missing.results[0].status in {CheckStatus.FAILED, CheckStatus.ERROR}


async def test_a_command_that_fails_is_evidence_of_failure(engine):
    ok = await engine.verify("command-ok", [
        CommandCheck("true_cmd", [sys.executable, "-c", "print('hello from a real process')"]),
    ])
    assert ok.passed is True
    assert "hello from a real process" in ok.results[0].evidence["stdout_tail"]

    bad = await engine.verify("command-bad", [
        CommandCheck("false_cmd", [sys.executable, "-c",
                                   "import sys; sys.stderr.write('boom'); sys.exit(3)"]),
    ])
    assert bad.passed is False
    assert bad.results[0].evidence["returncode"] == 3
    assert "boom" in bad.results[0].evidence["stderr_tail"]


async def test_a_hanging_command_is_killed_and_reported(engine):
    report = await engine.verify("command-timeout", [
        CommandCheck("sleepy", [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.4),
    ])
    assert report.passed is False
    assert "timed out" in report.results[0].detail


async def test_structured_output_must_match_its_schema(engine):
    schema = {"type": "object", "required": ["title", "pages"], "properties": {
        "title": {"type": "string"},
        "pages": {"type": "integer", "minimum": 1},
    }}
    good = await engine.verify("mission-output", [
        OutputSchemaCheck("output_shape", schema, value={"title": "Report", "pages": 4}),
    ])
    assert good.passed is True

    bad = await engine.verify("mission-output", [
        OutputSchemaCheck("output_shape", schema, value={"title": "Report", "pages": 0}),
    ])
    assert bad.passed is False
    assert "schema violation" in bad.results[0].detail
    assert bad.results[0].evidence["issues"]


async def test_output_that_is_not_json_is_rejected(engine):
    report = await engine.verify("mission-output", [
        OutputSchemaCheck("output_shape", {"type": "object"}, value="{not json"),
    ])
    assert report.passed is False
    assert "not JSON" in report.results[0].detail


async def test_an_invalid_schema_is_an_error_not_a_pass(engine):
    report = await engine.verify("mission-output", [
        OutputSchemaCheck("output_shape", {"type": "object", "surprise": True}, value={"a": 1}),
    ])
    assert report.passed is False
    assert report.results[0].status is CheckStatus.ERROR
    assert "invalid schema" in report.results[0].detail


async def test_a_side_effect_that_did_not_happen_is_a_failure(engine):
    """The anti-fabrication check: the file was never written, so 'done' is not allowed."""
    report = await engine.verify("side-effect", [
        SideEffectCheck("file_created", expectation="report.md exists", actual="does-not-exist"),
    ])
    assert report.passed is False
    assert "expected" in report.results[0].detail

    honest = await engine.verify("side-effect", [
        SideEffectCheck("file_created", expectation="report.md exists", actual="report.md exists"),
    ])
    assert honest.passed is True


async def test_a_side_effect_predicate_can_check_the_real_thing(engine, tmp_path):
    artifact = _write(tmp_path / "output.txt", "written for real")
    report = await engine.verify("side-effect", [
        SideEffectCheck("file_grew", expectation="file has content",
                        predicate=lambda _: artifact.stat().st_size > 0),
    ])
    assert report.passed is True


async def test_a_broken_predicate_is_an_error_not_a_pass(engine):
    report = await engine.verify("predicate-error", [BehaviorCheck("explodes", lambda _: 1 / 0)])
    assert report.passed is False
    assert report.results[0].status is CheckStatus.ERROR


async def test_composite_checks_combine_sub_verdicts(engine, tmp_path):
    present = _write(tmp_path / "a.txt", "a")
    all_required = await engine.verify("composite", [
        CompositeCheck("bundle", [
            FileExistsCheck("present", str(present)),
            FileExistsCheck("absent", str(tmp_path / "b.txt")),
        ]),
    ])
    assert all_required.passed is False
    assert "1/2 sub-checks passed" in all_required.results[0].detail

    any_of = await engine.verify("composite", [
        CompositeCheck("either", [
            FileExistsCheck("present", str(present)),
            FileExistsCheck("absent", str(tmp_path / "b.txt")),
        ], require_all=False),
    ])
    assert any_of.passed is True


async def test_an_optional_check_does_not_block_success(engine, tmp_path):
    report = await engine.verify("optional", [
        FileExistsCheck("required_thing", str(_write(tmp_path / "yes.txt", "yes"))),
        FileExistsCheck("nice_to_have", str(tmp_path / "no.txt"), required=False),
    ])
    assert report.passed is True
    assert len(report.results) == 2


async def test_the_any_contract_passes_on_one_required_check(engine, tmp_path):
    report = await engine.verify("either-provider", [
        FileExistsCheck("local_model", str(tmp_path / "missing-local")),
        FileExistsCheck("cloud_model", str(_write(tmp_path / "cloud.txt", "cloud"))),
    ], contract="any")
    assert report.passed is True


async def test_evidence_from_passing_checks_flows_to_later_ones(engine, tmp_path):
    """A check can publish `output_*` evidence that a later check consumes - the mission handoff."""

    class Producer(VerificationCheck):
        name = "produce"

        async def check(self, _context):
            return CheckResult(self.name, CheckStatus.PASSED, "produced",
                               {"output_path": str(tmp_path / "x")})

    class Consumer(VerificationCheck):
        name = "consume"

        async def check(self, context):
            seen = context.get("output_path", "")
            return CheckResult(self.name, CheckStatus.PASSED if seen else CheckStatus.FAILED, f"saw {seen!r}")

    report = await engine.verify("handoff", [Producer(), Consumer()])
    assert report.passed is True
    assert any(result.name == "consume" and result.ok for result in report.results)


async def test_every_verdict_is_audited(engine, log, tmp_path):
    artifact = _write(tmp_path / "audited.txt", "audited")
    await engine.verify("audited-subject", [FileExistsCheck("exists", str(artifact))],
                        mission_id="mis_test", actor="worker:writer")
    events = log.query(kinds=[EventKind.VERIFICATION], limit=50)
    assert any(event.payload.get("subject") == "audited-subject" and event.payload.get("check") == "exists"
               for event in events)
    assert any(getattr(event, "mission_id", "") == "mis_test" for event in events)


async def test_verification_is_honest_about_a_failed_provider_call(engine):
    report = await engine.verify("provider-call", [
        SideEffectCheck("call_succeeded", expectation="200 OK", actual="503 Service Unavailable"),
    ])
    assert report.passed is False
    assert "503" in report.results[0].detail
    assert report.to_dict()["unproven"] == ["call_succeeded"]
