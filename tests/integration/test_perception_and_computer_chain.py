"""Perception and computer use, verified against what this machine can actually do.

The point of these tests is honesty: an engine that cannot see, hear or click must say so, and the
gate in front of a desktop action must stop the action *before* the backend runs. Where the sandbox
genuinely has the capability (WAV parsing, voice-activity detection, image headers) the test asserts
the real result; where it does not (no vision model, no desktop, no audio device) it asserts the
honest refusal, never a fabricated success.
"""

from __future__ import annotations

import importlib.util
import io
import struct
import wave
from pathlib import Path

import pytest

from natasha.computer.controller import ScreenBackend

pytestmark = pytest.mark.integration


def _wav(samples: list[int], *, rate: int = 16_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", max(-32768, min(32767, value)))
                                    for value in samples))
    return buffer.getvalue()


def _silence(seconds: float, rate: int = 16_000) -> list[int]:
    return [0] * int(rate * seconds)


def _tone(seconds: float, amplitude: int = 12_000, rate: int = 16_000) -> list[int]:
    return [amplitude if index % 2 == 0 else -amplitude for index in range(int(rate * seconds))]


def _png(path: Path, width: int, height: int) -> bool:
    """Write a real PNG; False when Pillow is not installed (the caller skips honestly)."""
    try:
        from PIL import Image
    except Exception:
        return False
    Image.new("RGB", (width, height), (20, 60, 120)).save(path, format="PNG")
    return True


# --------------------------------------------------------------------------- vision

def test_vision_capabilities_match_the_desktop_backend(rt):
    capabilities = rt.vision.capabilities()
    desktop = rt.computer.capabilities()
    assert capabilities["screenshot"] == bool(desktop["actions"]["screenshot"]), (
        "vision analyses screenshots but does not take them: the capability must come from the "
        "controller that does")
    if not capabilities["available"]:
        assert capabilities["reason"], "an unavailable engine must explain itself"


def test_vision_reads_a_real_image_without_inventing_a_description(rt, home):
    target = Path(home) / "artifacts" / "probe.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not _png(target, 320, 240):
        pytest.skip("Pillow is not installed: no way to produce a real image fixture")

    import asyncio

    analysis = asyncio.run(rt.vision.analyze(str(target)))
    assert analysis.width == 320 and analysis.height == 240, (
        "the header parser must report the real dimensions")
    assert analysis.bytes == target.stat().st_size
    if analysis.ok:
        assert analysis.model, "a successful analysis must name the model that produced it"
        assert analysis.text.strip()
    else:
        assert analysis.error, "a failed analysis must say why"
        assert not analysis.text.strip(), "no text may be produced without a model or OCR"


def test_vision_refuses_a_file_that_is_not_an_image(rt, tmp_path):
    import asyncio

    fake = tmp_path / "not-really.png"
    fake.write_text("this is plain text wearing a .png extension", encoding="utf-8")
    analysis = asyncio.run(rt.vision.analyze(str(fake)))
    assert not analysis.ok
    assert analysis.error, "a mislabelled file must be rejected with a reason"


def test_ocr_is_only_claimed_when_the_tooling_is_present(rt):
    import importlib.util as util

    capabilities = rt.vision.capabilities()
    expected = util.find_spec("pytesseract") is not None and util.find_spec("PIL") is not None
    assert capabilities["ocr"] is expected


# --------------------------------------------------------------------------- hearing

def test_hearing_reports_its_engines_and_devices_honestly(rt):
    status = rt.hearing.status()
    assert set(status) >= {"local_model", "provider_audio", "microphone", "ready"}
    assert status["ready"] == (status["local_model"] or status["provider_audio"])
    assert rt.hearing.microphone_available() is (
        importlib.util.find_spec("sounddevice") is not None)


def test_voice_activity_detection_finds_the_speech_and_nothing_else(rt):
    audio = _wav(_silence(0.4) + _tone(0.6) + _silence(0.4))
    info = rt.hearing.wav_info(audio)
    assert info["sample_rate"] == 16_000
    assert abs(info["duration_seconds"] - 1.4) < 0.05

    segments = rt.hearing.energy_segments(audio)
    assert len(segments) == 1, f"one burst of speech, got {segments}"
    assert abs(segments[0].duration - 0.6) < 0.2, segments[0].to_dict()
    # Silence alone must produce no speech segment.
    assert rt.hearing.energy_segments(_wav(_silence(1.0))) == []


def test_transcription_without_an_engine_fails_with_an_actionable_reason(rt):
    import asyncio

    audio = _wav(_tone(0.5))
    result = asyncio.run(rt.hearing.transcribe(audio))
    if rt.hearing.status()["ready"]:
        # A real engine is installed here: then it must produce real text or a real error.
        assert result.ok or result.error
    else:
        assert not result.ok
        assert "install faster-whisper" in result.error or "configure" in result.error, result.error
        assert not result.text, "no text may be invented without a transcription engine"


def test_recording_without_an_audio_backend_is_a_configuration_error(rt):
    if importlib.util.find_spec("sounddevice") is not None:  # pragma: no cover - device present
        pytest.skip("an audio backend is installed in this environment")
    from natasha.core import ConfigurationError

    with pytest.raises(ConfigurationError) as excinfo:
        rt.hearing.record(0.1)
    assert "sounddevice" in str(excinfo.value)


# --------------------------------------------------------------------------- accessibility

def test_the_accessibility_tree_never_fakes_a_result():
    from natasha.computer.accessibility import get_accessibility_tree

    tree = get_accessibility_tree()
    assert set(tree) >= {"ok", "platform", "backend", "nodes", "count", "error"}
    if not tree["ok"]:
        assert tree["error"], "an unavailable tree must explain what to install"
        assert tree["nodes"] == []
    else:  # pragma: no cover - needs a desktop session
        assert tree["count"] >= 1, "a successful read must contain at least one node"
        assert tree["backend"]


# --------------------------------------------------------------------------- computer use

class _RecordingBackend(ScreenBackend):
    """A desktop backend that records calls instead of touching a screen that does not exist."""

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def available(self) -> bool:
        return True

    def size(self) -> tuple[int, int]:
        return (1920, 1080)

    def screenshot(self, path: str) -> str:
        self.calls.append(("screenshot", (path,)))
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")  # a header is enough for this test
        return path

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self.calls.append(("click", (x, y, button, clicks)))

    @property
    def supports_javascript(self) -> bool:
        return False

    def move(self, x: int, y: int) -> None:
        self.calls.append(("move", (x, y)))

    def type_text(self, text: str) -> None:
        self.calls.append(("type", (text,)))

    def press(self, key: str, *, presses: int = 1) -> None:
        self.calls.append(("press", (key, presses)))

    def scroll(self, amount: int) -> None:
        self.calls.append(("scroll", (amount,)))

    def clipboard_read(self) -> str:
        self.calls.append(("clipboard_read", ()))
        return ""

    def clipboard_write(self, text: str) -> None:
        self.calls.append(("clipboard_write", (text,)))

    def raise_window(self, title: str) -> None:
        self.calls.append(("raise", (title,)))

    def launch(self, command: list[str]) -> int:
        self.calls.append(("launch", tuple(command)))
        return 0


def test_the_controller_fails_honestly_when_its_backend_is_missing(rt):
    """The action must report itself as failed, name the backend problem, and produce no artifact."""
    capabilities = rt.computer.capabilities()
    if capabilities["available"]:  # pragma: no cover - needs a desktop
        pytest.skip("this environment has a working desktop backend")

    result = rt.computer.screenshot(actor="owner")
    assert result["ok"] is False
    assert not result["artifact"], "no artifact may be claimed when nothing was captured"
    assert "install" in result["error"], (
        "an unavailable backend must tell the operator how to fix it, not just 'failed'")
    assert "ComputerUnavailable" in result["error"] or "no desktop" in result["error"]


def test_a_desktop_action_by_a_model_is_gated_before_the_backend_runs(rt):
    """The choke point: policy first, backend never."""
    from natasha.computer.controller import ComputerController
    from natasha.core.errors import ApprovalRequired
    from natasha.core.risk import RiskLevel
    from natasha.security.policy import Capability, Effect, PolicyRequest

    backend = _RecordingBackend()
    controller = ComputerController(policy=rt.policy, approvals=rt.approvals, log=rt.log,
                                    artifacts=rt.paths.artifacts, backend=backend)

    decision = rt.policy.check(PolicyRequest(Capability.INPUT_CONTROL, "screen", actor="model:main",
                                             risk_hint=RiskLevel.HIGH))
    assert decision.effect in (Effect.APPROVAL, Effect.DENY), decision.reason

    with pytest.raises((ApprovalRequired, Exception)) as excinfo:
        controller.click(10, 10, actor="model:main")
    assert backend.calls == [], "no desktop action may run before policy and approval allow it"
    assert "approval" in str(excinfo.value).lower() or "denied" in str(excinfo.value).lower()

    # The owner acting directly is the authority - and the backend then really runs.
    controller.click(10, 10, actor="owner")
    assert backend.calls == [("click", (10, 10, "left", 1))]


def test_high_risk_actions_are_not_silently_auto_approved_for_workers(rt):
    from natasha.core.risk import RiskLevel
    from natasha.security.policy import Capability, Effect, PolicyRequest

    for actor in ("model:main", "worker:1", "skill:demo"):
        decision = rt.policy.check(PolicyRequest(Capability.INPUT_CONTROL, "screen",
                                                 actor=actor, risk_hint=RiskLevel.HIGH))
        # The model needs an approval; a worker without a mission scope is refused outright.
        # Either way it is never ALLOW: high-risk desktop input is never automatic.
        assert decision.effect in (Effect.APPROVAL, Effect.DENY), \
            f"{actor} must not control the desktop silently ({decision.effect}: {decision.reason})"


# --------------------------------------------------------------------------- browser

def test_the_browser_controller_does_not_claim_javascript_it_does_not_have(rt):
    capabilities = rt.browser.capabilities()
    playwright = importlib.util.find_spec("playwright") is not None
    assert capabilities["javascript"] is bool(playwright and capabilities["available"])
    assert capabilities["can"], "the controller must list what it can do"
    if not capabilities["javascript"]:
        assert "extract" in capabilities["can"] or "read" in capabilities["can"]


def test_the_browser_refuses_a_denied_host_before_any_request(rt):
    """The SSRF guard runs before the network call, so nothing is contacted."""
    from natasha.core.errors import AccessDenied, ApprovalRequired

    with pytest.raises((AccessDenied, ApprovalRequired)):
        rt.browser.open("http://169.254.169.254/latest/meta-data/", actor="model:main")
