"""Text-to-speech with honest backend detection."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import NatashaError, new_id
from ..core.clock import iso
from ..core.risk import RiskLevel
from ..events import EventKind, get_event_log
#: Where spoken output goes when the caller does not choose a file.
SPEECH_SUFFIXES = ("wav", "mp3", "ogg")


@dataclass
class VoiceResult:
    """The outcome of a speech attempt - including the reason it failed."""

    id: str = field(default_factory=lambda: new_id("spk"))
    text: str = ""
    audio_path: str = ""
    backend: str = ""
    ok: bool = False
    error: str = ""
    duration_ms: float = 0.0
    metered_chars: int = 0
    at: str = field(default_factory=iso)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text_chars": len(self.text), "audio_path": self.audio_path,
                "backend": self.backend, "ok": self.ok, "error": self.error,
                "duration_ms": round(self.duration_ms, 2), "at": self.at}


class VoiceEngine:
    """Speaks text with whatever the host can actually provide."""

    def __init__(self, *, brain: Any = None, hearing: Any = None, log: Any = None,
                 broker: Any = None, approvals: Any = None, artifacts: Any = None,
                 out_dir: str | Path | None = None, settings: Any = None) -> None:
        self.brain = brain
        self.hearing = hearing
        self.log = log or get_event_log()
        self.broker = broker
        self.approvals = approvals
        self.settings = settings
        self.out_dir = Path(out_dir) if out_dir else self._default_out_dir(artifacts)
        self.backends = self._detect_backends()
        self.history: list[VoiceResult] = []
        self._playback: subprocess.Popen | None = None
        self._lock = threading.RLock()

    @staticmethod
    def _default_out_dir(artifacts: Any) -> Path:
        if artifacts is not None:
            return Path(artifacts) / "speech"
        from ..core import get_paths

        return get_paths().artifacts / "speech"

    def _detect_backends(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for name, binary in (("piper", "piper"), ("espeak-ng", "espeak-ng"), ("espeak", "espeak"),
                             ("say", "say")):
            path = shutil.which(binary)
            if path:
                found[name] = path
        if os.name == "nt" and shutil.which("powershell"):
            found["sapi"] = shutil.which("powershell") or "powershell"
        endpoint = self._tts_endpoint()
        if endpoint:
            found["endpoint"] = endpoint
        return found

    def _tts_endpoint(self) -> str:
        settings = self.settings
        if settings is None:
            return ""
        creation = getattr(settings, "creation", None)
        for source in (creation, getattr(settings, "voice", None), settings):
            if source is None:
                continue
            value = getattr(source, "tts_endpoint", "") if not isinstance(source, dict) else source.get("tts_endpoint", "")
            if value:
                return str(value)
        return ""

    # ------------------------------------------------------------------ capabilities
    def capabilities(self) -> dict[str, Any]:
        return {"backends": sorted(self.backends), "available": bool(self.backends),
                "out_dir": str(self.out_dir),
                "speech_to_text": bool(self.hearing is not None),
                "reason": "" if self.backends else
                "no speech backend found: install espeak-ng (apt install espeak-ng) or piper, "
                "or configure a TTS endpoint"}

    def list_voices(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if "espeak-ng" in self.backends or "espeak" in self.backends:
            binary = self.backends.get("espeak-ng") or self.backends["espeak"]
            result = subprocess.run([binary, "--voices"], capture_output=True, text=True, timeout=30, check=False)
            voices = []
            for line in result.stdout.splitlines()[1:limit + 1]:
                parts = line.split()
                if len(parts) >= 4:
                    voices.append({"language": parts[1], "name": parts[3], "backend": Path(binary).name})
            return voices
        if "say" in self.backends:
            return [{"name": "default", "language": "system", "backend": "say"}]
        return []

    # ------------------------------------------------------------------ speak
    def speak(self, text: str, *, voice: str = "", speed: int = 175, out_path: str = "",
              actor: str = "owner", play: bool = False, language: str = "en") -> VoiceResult:
        """Render text to an audio file (never silently returns an empty result)."""
        text = (text or "").strip()
        result = VoiceResult(text=text, metered_chars=len(text))
        if not text:
            result.error = "nothing to speak"
            return result
        self.out_dir.mkdir(parents=True, exist_ok=True)
        target = Path(out_path) if out_path else self.out_dir / f"{result.id}.wav"
        started = time.perf_counter()
        try:
            result.backend, result.audio_path = self._render(text, target, voice=voice, speed=speed,
                                                             language=language)
            result.ok = Path(result.audio_path).exists() and Path(result.audio_path).stat().st_size > 0
            if not result.ok:
                result.error = "speech backend produced no audio"
            elif play:
                self._play(result.audio_path)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            result.duration_ms = (time.perf_counter() - started) * 1000
        with self._lock:
            self.history.append(result)
            self.history = self.history[-200:]
        self.log.append(EventKind.VOICE, {"action": "speak", **result.to_dict()},
                        actor=actor, source="voice",
                        risk=RiskLevel.LOW if result.ok else RiskLevel.MEDIUM)
        return result

    def _render(self, text: str, target: Path, *, voice: str, speed: int, language: str) -> tuple[str, str]:
        if "piper" in self.backends and voice:
            # piper needs an explicit ONNX voice model; the caller names it with `voice`.
            process = subprocess.run([self.backends["piper"], "--model", voice, "--output_file", str(target)],
                                     input=text, capture_output=True, text=True, timeout=120, check=False)
            if process.returncode == 0 and target.exists():
                return "piper", str(target)
            raise NatashaError(f"piper failed: {process.stderr.strip()[:200]}")
        if "espeak-ng" in self.backends or "espeak" in self.backends:
            binary = self.backends.get("espeak-ng") or self.backends["espeak"]
            arguments = [binary, "-s", str(speed), "-w", str(target)]
            if voice:
                arguments += ["-v", voice]
            process = subprocess.run(arguments, input=text, capture_output=True, text=True, timeout=120,
                                     check=False)
            if process.returncode != 0:
                raise NatashaError(f"espeak failed: {process.stderr.strip()[:200]}")
            return Path(binary).name, str(target)
        if "say" in self.backends:
            process = subprocess.run([self.backends["say"], "-o", str(target), text],
                                     capture_output=True, text=True, timeout=120, check=False)
            if process.returncode != 0:
                raise NatashaError(f"say failed: {process.stderr.strip()[:200]}")
            return "say", str(target)
        if "sapi" in self.backends:
            script = ("Add-Type -AssemblyName System.Speech; "
                      f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                      f"$s.SetOutputToWaveFile('{target}'); $s.Speak('{text.replace(chr(39), chr(39) + chr(39))}')")
            process = subprocess.run([self.backends["sapi"], "-NoProfile", "-Command", script],
                                     capture_output=True, text=True, timeout=120, check=False)
            if process.returncode != 0:
                raise NatashaError(f"SAPI failed: {process.stderr.strip()[:200]}")
            return "sapi", str(target)
        if "endpoint" in self.backends:
            return "endpoint", self._render_remote(text, target, voice=voice)
        raise NatashaError("no speech backend available: install espeak-ng or piper, "
                           "or configure a TTS endpoint")

    def _render_remote(self, text: str, target: Path, *, voice: str) -> str:
        """OpenAI-compatible ``/audio/speech`` via the broker - credentials never touch the caller."""
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.broker is not None:
            handle = self.broker.issue("tts", purpose="text_to_speech")
            if handle is not None:
                headers.update(handle.headers())
        payload = {"model": voice or "tts-1", "input": text, "voice": "alloy"}
        with httpx.Client(timeout=120) as client:
            response = client.post(self.backends["endpoint"], json=payload, headers=headers)
            response.raise_for_status()
            target.write_bytes(response.content)
        return str(target)

    def _play(self, path: str) -> None:
        for player in ("aplay", "paplay", "afplay", "ffplay"):
            binary = shutil.which(player)
            if binary:
                self.stop_speaking()
                with self._lock:
                    self._playback = subprocess.Popen([binary, path], stdout=subprocess.DEVNULL,
                                                      stderr=subprocess.DEVNULL)
                return
        raise NatashaError("no audio player found (aplay/paplay/afplay/ffplay)")

    @property
    def speaking(self) -> bool:
        """True while audio is actually coming out of the speaker."""
        with self._lock:
            process = self._playback
        return bool(process is not None and process.poll() is None)

    def stop_speaking(self) -> bool:
        """Barge-in: stop playback now. Returns True when something was actually playing."""
        with self._lock:
            process, self._playback = self._playback, None
        if process is None or process.poll() is not None:
            return False
        try:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn player
                process.kill()
                process.wait(timeout=3)
        except Exception:
            return False
        self.log.append(EventKind.VOICE, {"action": "speech_interrupted", "pid": process.pid},
                        actor="owner", source="voice", risk=RiskLevel.LOW)
        return True

    # ------------------------------------------------------------------ listen
    def transcribe(self, path: str, *, language: str = "en", actor: str = "owner") -> dict[str, Any]:
        """Speech-to-text via the hearing engine (which is async internally)."""
        if self.hearing is None:
            return {"ok": False, "error": "no hearing engine attached", "text": ""}
        from ..core import run_coroutine_sync

        try:
            result = run_coroutine_sync(self.hearing.transcribe(path, language=language, actor=actor))
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "text": ""}
        return result.to_dict() if hasattr(result, "to_dict") else dict(result)

    @staticmethod
    def _recorder() -> str:
        for candidate in ("arecord", "sox", "ffmpeg"):
            if shutil.which(candidate):
                return candidate
        return ""

    def record(self, seconds: float = 5.0) -> tuple[bytes, str]:
        """Capture raw audio from the default input device. Returns ``(bytes, error)``."""
        recorder = self._recorder()
        if not recorder:
            return b"", "no audio recorder found (install alsa-utils for arecord)"
        target = Path(tempfile.gettempdir()) / f"natasha-listen-{new_id('rec')}.wav"
        if recorder == "arecord":
            command = ["arecord", "-q", "-d", str(int(seconds)), "-f", "S16_LE", "-r", "16000", str(target)]
        elif recorder == "sox":
            command = ["sox", "-d", "-q", str(target), "trim", "0", str(seconds)]
        else:
            command = ["ffmpeg", "-y", "-loglevel", "error", "-f", "alsa", "-i", "default", "-t",
                       str(seconds), str(target)]
        try:
            process = subprocess.run(command, capture_output=True, text=True, timeout=seconds + 30,
                                     check=False)
        except Exception as exc:
            return b"", f"{recorder} failed: {type(exc).__name__}: {exc}"
        if process.returncode != 0 or not target.exists():
            return b"", f"{recorder} failed: {process.stderr.strip()[:200]}"
        data = target.read_bytes()
        try:
            target.unlink()
        except OSError:  # pragma: no cover - best effort cleanup
            pass
        return data, ""

    def listen_once(self, *, seconds: float = 5.0, language: str = "en") -> dict[str, Any]:
        """Record from the microphone and transcribe. Honest when there is no recorder."""
        recorder = None
        for candidate in ("arecord", "sox", "ffmpeg"):
            if shutil.which(candidate):
                recorder = candidate
                break
        if recorder is None:
            return {"ok": False, "error": "no audio recorder found (install alsa-utils for arecord)"}
        data, error = self.record(seconds)
        if error or not data:
            return {"ok": False, "error": error or "the microphone returned no audio"}
        return self.transcribe(data, language=language)

    def history_dicts(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [item.to_dict() for item in self.history[-limit:]]


_ENGINE: VoiceEngine | None = None
_LOCK = threading.Lock()


def get_voice_engine(**kwargs: Any) -> VoiceEngine:
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = VoiceEngine(**kwargs)
        return _ENGINE


def reset_voice_engine() -> None:
    global _ENGINE
    with _LOCK:
        _ENGINE = None
