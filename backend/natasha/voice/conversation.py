"""The voice loop: microphone -> VAD -> speech-to-text -> Natasha -> text-to-speech -> speaker.

The loop is deliberately thin: it *perceives* audio with the hearing engine, hands the transcript to
the same executive every other channel uses, and speaks the reply with the voice engine. Authority
never comes from audio - the executive turn runs as the model actor, exactly like a typed message,
so a spoken sentence can never authorise a dangerous action by itself.

Barge-in: `interrupt()` stops playback mid-sentence and records the utterance as interrupted, so the
owner can talk over Natasha the way they would over a person.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import NatashaError, new_id, run_coroutine_sync
from ..core.clock import iso
from ..core.risk import RiskLevel
from ..events import EventKind, get_event_log

#: Audio formats the hearing engine accepts.
SUPPORTED_AUDIO = {".wav", ".mp3", ".ogg", ".opus", ".flac", ".m4a", ".aac", ".webm"}


@dataclass
class VoiceTurn:
    """One trip around the loop, with the reason for every failed stage."""

    id: str = field(default_factory=lambda: new_id("vturn"))
    transcript: str = ""
    reply: str = ""
    audio_path: str = ""
    speech_backend: str = ""
    segments: int = 0
    interrupted: bool = False
    ok: bool = False
    error: str = ""
    stages: dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=iso)
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "transcript": self.transcript, "reply": self.reply,
                "audio_path": self.audio_path, "speech_backend": self.speech_backend,
                "segments": self.segments, "interrupted": self.interrupted, "ok": self.ok,
                "error": self.error, "stages": self.stages, "started_at": self.started_at,
                "latency_ms": round(self.latency_ms, 2)}


class VoiceLoop:
    """A conversational voice channel over the existing cognitive runtime."""

    def __init__(self, *, voice: Any = None, hearing: Any = None, executive: Any = None,
                 computer: Any = None, log: Any = None, wakewords: Any = None) -> None:
        self.voice = voice
        self.hearing = hearing
        self.executive = executive
        self.computer = computer
        self.log = log or get_event_log()
        self.wakewords = wakewords
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.turns: list[VoiceTurn] = []

    # ------------------------------------------------------------------ capability
    def capabilities(self) -> dict[str, Any]:
        voice_caps: dict[str, Any] = {"available": False}
        if self.voice is not None:
            method = getattr(self.voice, "capabilities", None)
            if callable(method):
                try:
                    voice_caps = method()
                except Exception as exc:  # pragma: no cover - defensive
                    voice_caps = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        hearing_caps: dict[str, Any] = {"available": False}
        if self.hearing is not None:
            status = getattr(self.hearing, "status", None)
            try:
                hearing_caps = status() if callable(status) else {"available": True}
            except Exception as exc:  # pragma: no cover - defensive
                hearing_caps = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "pipeline": ["microphone", "vad", "speech-to-text", "executive", "text-to-speech",
                         "speaker"],
            "speech_to_text": bool(hearing_caps.get("ready") or hearing_caps.get("local_model")
                                   or hearing_caps.get("provider_audio")),
            "text_to_speech": bool(voice_caps.get("available")),
            "microphone": bool(getattr(self.hearing, "microphone_available", lambda: False)())
            if self.hearing is not None else False,
            "barge_in": bool(voice_caps.get("available")),
            "hearing": hearing_caps,
            "voice": voice_caps,
            "wake_words": getattr(self.wakewords, "phrases", []) if self.wakewords else [],
        }

    # ------------------------------------------------------------------ audio in
    def record(self, seconds: float = 5.0) -> tuple[bytes, str]:
        """Record from the default input device. Returns ``(bytes, error)``."""
        if self.voice is not None and hasattr(self.voice, "record"):
            return self.voice.record(seconds)
        return b"", "no voice engine is attached"

    def vad_segments(self, audio: bytes) -> list[dict[str, Any]]:
        """Split audio into speech segments using the hearing engine's energy VAD."""
        if self.hearing is None:
            return []
        energy = getattr(self.hearing, "energy_segments", None)
        if not callable(energy):
            return []
        try:
            segments = energy(audio)
        except Exception:
            return []
        result = []
        for segment in segments:
            to_dict = getattr(segment, "to_dict", None)
            result.append(to_dict() if callable(to_dict) else dict(segment))
        return result

    def transcribe(self, audio: str | Path | bytes, *, language: str = "") -> dict[str, Any]:
        if self.hearing is None:
            return {"ok": False, "text": "", "error": "no hearing engine attached"}
        try:
            result = run_coroutine_sync(self.hearing.transcribe(audio, language=language))
        except Exception as exc:
            return {"ok": False, "text": "", "error": f"{type(exc).__name__}: {exc}"}
        return result.to_dict() if hasattr(result, "to_dict") else dict(result)

    # ------------------------------------------------------------------ audio out
    def speak(self, text: str, *, play: bool = True, actor: str = "voice:owner") -> dict[str, Any]:
        if self.voice is None:
            return {"ok": False, "error": "no voice engine attached", "audio_path": ""}
        try:
            result = self.voice.speak(text, play=play, actor=actor)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "audio_path": ""}
        return result.to_dict() if hasattr(result, "to_dict") else dict(result)

    def interrupt(self) -> dict[str, Any]:
        """Barge-in: stop the current playback (the owner is talking now)."""
        stopped = False
        if self.voice is not None:
            method = getattr(self.voice, "stop_speaking", None)
            if callable(method):
                try:
                    stopped = bool(method())
                except Exception:
                    stopped = False
        self._stop.set()
        with self._lock:
            for turn in reversed(self.turns):
                if turn.audio_path and not turn.interrupted:
                    turn.interrupted = True
                    break
        self.log.append(EventKind.VOICE, {"action": "interrupted", "stopped_playback": stopped},
                        actor="voice:owner", source="voice", risk=RiskLevel.LOW)
        return {"ok": True, "stopped_playback": stopped}

    # ------------------------------------------------------------------ the turn
    def turn(self, *, audio: bytes | None = None, text: str = "", path: str = "",
             language: str = "", speak: bool = True, seconds: float = 5.0,
             conversation_id: str = "", mission_id: str = "") -> VoiceTurn:
        """Run one full loop iteration. Every stage records what it did, including failures."""
        started = time.perf_counter()
        with self._lock:
            self._stop.clear()
        turn = VoiceTurn()
        try:
            segments: list[dict[str, Any]] = []
            if text:
                turn.transcript = text.strip()
                turn.stages["input"] = "text"
            else:
                source: Any = audio
                if source is None and path:
                    target = Path(path).expanduser()
                    if not target.is_file():
                        raise NatashaError(f"audio file not found: {target}")
                    if target.suffix.lower() not in SUPPORTED_AUDIO:
                        raise NatashaError(f"unsupported audio format {target.suffix!r}")
                    source = target
                    turn.stages["input"] = str(target)
                if source is None:
                    raw, error = self.record(seconds)
                    if error or not raw:
                        turn.error = error or "the microphone returned no audio"
                        turn.stages["input"] = "microphone"
                        turn.stages["record"] = {"ok": False, "error": turn.error}
                        return self._finish(turn, started, failed=True)
                    source = raw
                    turn.stages["input"] = "microphone"
                turn.stages["vad"] = {"applied": isinstance(source, (bytes, bytearray))}
                if isinstance(source, (bytes, bytearray)):
                    segments = self.vad_segments(bytes(source))
                    turn.segments = len(segments)
                    turn.stages["vad"] = {"segments": turn.segments, "detail": segments[:20]}
                    if segments and not any(item.get("speech", True) for item in segments):
                        turn.error = "no speech detected in the audio"
                        return self._finish(turn, started, failed=True)
                result = self.transcribe(source, language=language)
                turn.stages["stt"] = {key: result.get(key) for key in
                                      ("ok", "text", "model", "provider", "language", "error")}
                if not result.get("ok") or not str(result.get("text", "")).strip():
                    turn.error = result.get("error") or "speech-to-text returned no text"
                    return self._finish(turn, started, failed=True)
                turn.transcript = str(result["text"]).strip()
            if not turn.transcript:
                turn.error = "nothing was said"
                return self._finish(turn, started, failed=True)
            if self.wakewords is not None and turn.stages.get("input") == "microphone":
                detect = getattr(self.wakewords, "detect", None)
                hit = detect(turn.transcript) if callable(detect) else None
                if hit is not None:
                    remembered = turn.transcript
                    turn.transcript = (getattr(hit, "remainder", "") or "").strip() or remembered
                    turn.stages["wake_word"] = {"hit": True,
                                                "phrase": getattr(hit, "phrase", ""),
                                                "confidence": getattr(hit, "confidence", 0.0)}
                else:
                    turn.stages["wake_word"] = {"hit": False}
            if self.executive is None:
                raise NatashaError("the executive is not attached to the voice loop")

            from ..executive import Turn

            reply = self.executive.run_turn(Turn(
                message=turn.transcript, conversation_id=conversation_id or f"voice-{turn.id}",
                actor="model:main", mission_id=mission_id,
                metadata={"channel": "voice", "principal": "owner", "voice_turn": turn.id}))
            if hasattr(reply, "__await__"):
                reply = run_coroutine_sync(reply)
            payload = reply.to_dict() if hasattr(reply, "to_dict") else {}
            turn.reply = str(getattr(reply, "reply", "") or payload.get("reply") or
                             getattr(reply, "text", "") or "")
            turn.stages["executive"] = {"ok": not bool(payload.get("error")),
                                        "offline_placeholder": bool(payload.get("offline_placeholder")),
                                        "tool_calls": len(getattr(reply, "tool_calls", []) or []),
                                        "approvals": len(getattr(reply, "approvals_requested", []) or []),
                                        "turn_id": payload.get("turn_id", ""),
                                        "error": payload.get("error", "")}
            turn.ok = bool(turn.reply)
            if speak and turn.reply and not self._stop.is_set():
                speech = self.speak(turn.reply)
                turn.audio_path = str(speech.get("audio_path", ""))
                turn.speech_backend = str(speech.get("backend", ""))
                turn.stages["tts"] = {"ok": bool(speech.get("ok")), "backend": turn.speech_backend,
                                      "error": speech.get("error", "")}
                if not speech.get("ok"):
                    turn.stages["tts"]["honest"] = ("the reply is text only; no speech backend "
                                                    "produced audio")
            elif self._stop.is_set():
                turn.interrupted = True
                turn.stages["tts"] = {"ok": False, "error": "interrupted before speaking"}
            return self._finish(turn, started)
        except Exception as exc:
            turn.error = f"{type(exc).__name__}: {exc}"
            turn.stages.setdefault("error", {}).update({"type": type(exc).__name__})
            return self._finish(turn, started, failed=True)

    def _finish(self, turn: VoiceTurn, started: float, *, failed: bool = False) -> VoiceTurn:
        turn.latency_ms = (time.perf_counter() - started) * 1000
        with self._lock:
            self.turns.append(turn)
            self.turns = self.turns[-100:]
        self.log.append(EventKind.VOICE,
                        {"action": "voice_turn", "ok": turn.ok, "failed": failed,
                         "transcript_chars": len(turn.transcript), "reply_chars": len(turn.reply),
                         "interrupted": turn.interrupted, "error": turn.error},
                        actor="voice:owner", source="voice",
                        risk=RiskLevel.LOW if turn.ok else RiskLevel.MEDIUM)
        return turn

    def history(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [turn.to_dict() for turn in self.turns[-limit:]]


_LOOP: VoiceLoop | None = None
_LOCK = threading.Lock()


def get_voice_loop(**kwargs: Any) -> VoiceLoop:
    global _LOOP
    with _LOCK:
        if _LOOP is None:
            _LOOP = VoiceLoop(**kwargs)
    return _LOOP


def reset_voice_loop() -> None:
    global _LOOP
    with _LOCK:
        _LOOP = None
