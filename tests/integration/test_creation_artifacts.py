"""Creation jobs must describe their output truthfully - and prove it from the bytes on disk.

A creation engine is easy to fake: write *something*, call it a success, and let the UI show a card.
These tests hold it to the opposite standard. Every artifact carries an id, a MIME type, a size, a
sha256, a provenance block naming the generator, and a verification block that was computed by
reading the file back. When a producer mislabels its output, the job must not report success.

No test here needs a model provider: the engine's offline paths (deterministic compositions,
templates) are exactly the paths that must never pretend to be model output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from natasha.core.hashing import sha256_file
from natasha.creation.engine import KINDS, verify_artifact

pytestmark = pytest.mark.integration

#: The keys every artifact record must carry for the UI, the API and an auditor to trust it.
REQUIRED_KEYS = ("id", "path", "name", "kind", "mime", "bytes", "sha256", "created_at",
                 "provenance", "verification")


def test_the_engine_advertises_exactly_the_kinds_it_can_dispatch(rt):
    capabilities = rt.creation.capabilities()
    assert capabilities["kinds"] == list(KINDS)
    assert set(KINDS) == {"document", "image", "audio", "video", "slides"}
    assert capabilities["artifacts_dir"]


async def test_a_creation_job_for_an_unknown_kind_is_rejected(rt):
    from natasha.core import NatashaError

    with pytest.raises(NatashaError):
        await rt.creation.create("hologram", "make me a hologram")


async def test_a_created_document_is_hashed_and_verified_on_disk(rt):
    job = await rt.creation.create("document", "why evidence matters", title="Evidence")
    assert job.status == "succeeded", job.to_dict()
    assert len(job.artifacts) == 1

    artifact = job.artifacts[0]
    for key in REQUIRED_KEYS:
        assert artifact.get(key) not in (None, ""), f"{key} missing from {artifact}"

    path = Path(artifact["path"])
    assert path.is_file(), artifact
    assert artifact["kind"] == "document"
    assert artifact["mime"] == "text/markdown"
    assert artifact["bytes"] == path.stat().st_size
    assert artifact["sha256"] == sha256_file(path), "the recorded hash must be the file's hash"
    assert artifact["verification"]["status"] == "verified"
    assert artifact["provenance"]["job_id"] == job.id
    assert artifact["provenance"]["actor"] == "owner"
    # The stage log must show that verification ran, not just that writing ran.
    assert any(stage["stage"] == "verify" and stage["state"] == "ran" for stage in job.stages)


async def test_a_local_composition_is_never_labelled_model_generated(rt):
    """With no image model configured the engine draws a deterministic PNG and says so."""
    job = await rt.creation.create("image", "a calm blue square", size="256x256")
    if job.status != "succeeded":
        # Honest failure is acceptable (no Pillow); a fabricated success is not.
        assert job.artifacts == [] or all(
            item["verification"]["status"] != "verified" for item in job.artifacts)
        assert job.error, "a failed image job must say why"
        pytest.skip(f"local composition unavailable here: {job.error}")

    artifact = job.artifacts[0]
    assert artifact["mime"] == "image/png"
    assert artifact["verification"]["status"] == "verified"
    assert artifact["provenance"]["model_generated"] is False, artifact["provenance"]
    assert "LOCAL COMPOSITION" in artifact["note"] or "no image model" in artifact["note"]


async def test_an_audio_job_never_reports_success_without_a_verified_track(rt):
    """Voice may be unavailable in this environment; the job must fail, not invent a file."""
    job = await rt.creation.create("audio", "say something short", text="one two three")
    if job.status == "succeeded":
        assert job.artifacts, "success with no artifact is a lie"
        for artifact in job.artifacts:
            assert artifact["verification"]["status"] == "verified", artifact
            assert Path(artifact["path"]).stat().st_size > 0
    else:
        assert job.artifacts == [], f"a failed job must not leave phantom artifacts: {job.artifacts}"
        assert job.error, "a failed audio job must explain itself"


async def test_a_video_job_reports_the_storyboard_when_rendering_is_impossible(rt):
    """Without ffmpeg the pipeline still produces a readable shot list - and calls itself partial."""
    job = await rt.creation.create("video", "a short clip about verification",
                                        shots=2, render=True)
    kinds = {artifact["kind"] for artifact in job.artifacts}
    assert "storyboard" in kinds, job.to_dict()
    for artifact in job.artifacts:
        assert artifact["verification"]["status"] == "verified", artifact
    if "video" not in kinds:
        assert job.status == "partial", "no rendered video means the job is not a full success"
        assert any("ffmpeg" in note.lower() for note in job.notes), job.notes


def test_the_verifier_reports_what_is_actually_on_disk(tmp_path):
    """The pure verification function: missing, empty, wrong format, and genuinely fine."""
    missing = verify_artifact(tmp_path / "nope.png", "image")
    assert missing["status"] == "missing"
    assert not missing.get("sha256"), "a missing file has no hash to report"

    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    assert verify_artifact(empty, "image")["status"] == "empty"

    mislabelled = tmp_path / "actually-text.png"
    mislabelled.write_text("this is not a PNG", encoding="utf-8")
    result = verify_artifact(mislabelled, "image")
    assert result["status"] == "format-mismatch"
    assert "PNG" not in result["detail"] or "starts with" in result["detail"]
    assert result["sha256"] == sha256_file(mislabelled)

    document = tmp_path / "notes.md"
    document.write_text("# Hello\n", encoding="utf-8")
    assert verify_artifact(document, "document")["status"] == "verified"


async def test_a_producer_that_mislabels_its_output_cannot_report_success(rt, monkeypatch):
    """The scenario that matters: code says PNG, disk says text. The job must fail the check."""
    engine = rt.creation

    def liar(brief, target, *, size, index):  # noqa: ANN001, ARG001 - mirrors the real signature
        Path(target).write_text("not an image at all", encoding="utf-8")

    monkeypatch.setattr(engine, "_image_local", liar)
    job = await engine.create("image", "anything")

    assert job.status == "failed", job.to_dict()
    assert job.unverified, job.to_dict()
    assert job.unverified[0]["verification"]["status"] == "format-mismatch"
    assert "verification" in job.error.lower()
    assert any(stage["stage"] == "verify" and stage["state"] == "failed" for stage in job.stages)
    assert "artifact verification failed" in " ".join(job.notes)
