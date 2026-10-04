"""Hearing: transcribe speech from a microphone or an audio file.

Local-first order: a local whisper-style model if installed, otherwise a provider that supports
audio input, otherwise an explicit "no transcription engine available" - never a fabricated script.
"""

from __future__ import annotations

import io
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import ConfigurationError, NotFoundError
from ..security.injection import ContentTrust, ExternalContent, get_injection_guard

SUPPORTED_AUDIO = {".wav", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".aac", ".webm"}


@dataclass
class SpeechSegment:
    start_seconds: float
    end_seconds: float
    text: str = ""
    confidence: float = 1.0

    @property
    def duration(self) -> float:
        return round(self.end_seconds - self.start_seconds, 3)

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start_seconds, "end": self.end_seconds, "duration": self.duration,
                "text": self.text, "confidence": self.confidence}


@dataclass
class AudioTranscription:
    ok: bool
    text: str = ""
    segments: list[SpeechSegment] = field(default_factory=list)
    language: str = ""
    duration_seconds: float = 0.0
    engine: str = ""
    path: str = ""
    error: str = ""
    suspicious: bool = False

    def content(self) -> ExternalContent:
        return ExternalContent(text=self.text, source=f"audio:{self.path or 'microphone'}",
                               trust=ContentTrust.OWNER if not self.path else ContentTrust.EXTERNAL,
                               metadata={"kind": "transcript"})

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "text": self.text, "language": self.language, "engine": self.engine,
                "duration_seconds": self.duration_seconds, "path": self.path, "error": self.error,
                "suspicious": self.suspicious, "segments": [segment.to_dict() for segment in self.segments]}


class HearingEngine:
    """Speech-to-text with local-first fallback and honest availability."""

    def __init__(self, *, brain: Any = None, default_language: str = "en") -> None:
        self._brain = brain
        self.default_language = default_language

    @property
    def brain(self) -> Any:
        if self._brain is None:
            from ..brain import get_brain_client

            self._brain = get_brain_client()
        return self._brain

    # ------------------------------------------------------------------ capabilities
    def local_model_available(self) -> bool:
        import importlib.util

        return any(importlib.util.find_spec(name) is not None
                   for name in ("faster_whisper", "whisper", "whispercpp", "vosk"))

    def provider_audio_available(self) -> bool:
        try:
            return bool(self.brain.registry.models_with_capability("audio"))
        except Exception:
            return False

    def status(self) -> dict[str, Any]:
        return {
            "local_model": self.local_model_available(),
            "provider_audio": self.provider_audio_available(),
            "microphone": self.microphone_available(),
            "ready": self.local_model_available() or self.provider_audio_available(),
        }

    @staticmethod
    def microphone_available() -> bool:
        import importlib.util

        return importlib.util.find_spec("sounddevice") is not None

    # ------------------------------------------------------------------ transcription
    async def transcribe(self, audio: str | Path | bytes, *, language: str = "",
                         actor: str = "model:main", prompt: str = "") -> AudioTranscription:
        """Transcribe audio from a file or raw bytes."""
        data, source = b"", ""
        if isinstance(audio, (str, Path)):
            path = Path(audio).expanduser()
            if not path.is_file():
                raise NotFoundError(f"audio file not found: {path}")
            if path.suffix.lower() not in SUPPORTED_AUDIO:
                return AudioTranscription(False, path=str(path),
                                          error=f"unsupported audio format {path.suffix!r}")
            data, source = path.read_bytes(), str(path)
        else:
            data, source = bytes(audio), "microphone"

        if self.local_model_available():
            local = self._transcribe_local(data, language or self.default_language)
            if local.ok:
                return self._finish(local, source)
        if not self.provider_audio_available():
            # A text-only provider cannot transcribe. Asking it anyway returns a confident answer
            # about audio it never heard, which would be recorded as a transcription.
            return AudioTranscription(
                False, path=source,
                error="no transcription engine available; install faster-whisper (pip install "
                      "faster-whisper) or configure an audio-capable provider")
        try:
            result = await self._transcribe_provider(data, language or self.default_language, actor, prompt)
        except Exception as exc:
            return AudioTranscription(False, path=source,
                                      error=f"no transcription engine available ({type(exc).__name__}: {exc}); "
                                            "install faster-whisper or configure an audio-capable provider")
        return self._finish(result, source)

    def _transcribe_local(self, data: bytes, language: str) -> AudioTranscription:
        try:
            from faster_whisper import WhisperModel  # type: ignore

            model = WhisperModel("base")
            segments, info = model.transcribe(io.BytesIO(data), language=language or None)
            pieces = [SpeechSegment(float(segment.start), float(segment.end), segment.text.strip(),
                                    float(getattr(segment, "avg_logprob", 0.0) or 0.0))
                      for segment in segments]
            return AudioTranscription(True, text=" ".join(piece.text for piece in pieces).strip(),
                                      segments=pieces, language=getattr(info, "language", language),
                                      duration_seconds=float(getattr(info, "duration", 0.0)),
                                      engine="faster-whisper")
        except Exception as exc:
            return AudioTranscription(False, error=f"local model failed: {exc}", engine="faster-whisper")

    async def _transcribe_provider(self, data: bytes, language: str, actor: str,
                                   prompt: str) -> AudioTranscription:
        import base64

        response = await self.brain.complete_audio(
            base64.b64encode(data).decode(), mime_type="audio/wav", prompt=prompt,
            language=language, actor=actor,
        )
        text = str(getattr(response, "text", response)).strip()
        model = getattr(getattr(response, "model", None), "id", "") or ""
        return AudioTranscription(True, text=text, language=language, engine=f"provider:{model}")

    @staticmethod
    def _finish(result: AudioTranscription, source: str) -> AudioTranscription:
        result.path = result.path or source
        guard = get_injection_guard()
        verdict = guard.inspect(result.text, source=f"audio:{source}")
        result.suspicious = verdict.suspicious
        return result

    # ------------------------------------------------------------------ microphone
    def record(self, seconds: float = 5.0, *, sample_rate: int = 16_000) -> bytes:
        """Capture from the microphone. Raises when no audio backend is installed."""
        try:
            import sounddevice as sd  # type: ignore
        except Exception as exc:
            raise ConfigurationError(
                "microphone capture needs the optional 'sounddevice' package",
                hint="pip install sounddevice",
            ) from exc
        frames = sd.rec(int(seconds * sample_rate), samplerate=sample_rate, channels=1, dtype="int16")
        sd.wait()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(frames.tobytes())
        return buffer.getvalue()

    @staticmethod
    def wav_info(data: bytes) -> dict[str, Any]:
        """Duration/sample info for a WAV payload (stdlib only)."""
        try:
            with wave.open(io.BytesIO(data), "rb") as handle:
                frames, rate = handle.getnframes(), handle.getframerate()
                return {"frames": frames, "sample_rate": rate,
                        "duration_seconds": round(frames / rate, 3) if rate else 0.0,
                        "channels": handle.getnchannels(), "width": handle.getsampwidth()}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def energy_segments(self, data: bytes, *, threshold: float = 0.02, min_seconds: float = 0.25) -> list[SpeechSegment]:
        """Cheap voice-activity detection on 16-bit PCM WAV using the standard library."""
        try:
            import audioop  # type: ignore
        except Exception:
            audioop = None  # type: ignore
        try:
            with wave.open(io.BytesIO(data), "rb") as handle:
                rate = handle.getframerate()
                width = handle.getsampwidth()
                raw = handle.readframes(handle.getnframes())
        except Exception:
            return []
        if width != 2 or audioop is None:
            return []
        window = int(rate * 0.05)
        segments: list[SpeechSegment] = []
        active_start: float | None = None
        for index in range(0, len(raw), window * 2):
            chunk = raw[index:index + window * 2]
            if not chunk:
                break
            rms = audioop.rms(chunk, 2)
            loud = (rms / 32768.0) > threshold
            position = index / 2 / rate
            if loud and active_start is None:
                active_start = position
            elif not loud and active_start is not None:
                if position - active_start >= min_seconds:
                    segments.append(SpeechSegment(active_start, position))
                active_start = None
        if active_start is not None:
            segments.append(SpeechSegment(active_start, len(raw) / 2 / rate))
        return segments


_ENGINE: HearingEngine | None = None
_LOCK = threading.Lock()


def get_hearing_engine(**kwargs: Any) -> HearingEngine:
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = HearingEngine(**kwargs)
        return _ENGINE


def reset_hearing_engine() -> None:
    """Drop the cached engine. The runtime builds its own; this exists for tests and reloads."""
    global _ENGINE
    with _LOCK:
        _ENGINE = None
