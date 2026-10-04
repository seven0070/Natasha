"""Voice: speak, transcribe, listen once, and the wake-word detector."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status

from ...core import run_coroutine_sync
from ..deps import rate_limit, audit, get_runtime, handle, require_owner
from ..models import VoiceBody, VoiceTurnBody

router = APIRouter(prefix="/voice", tags=["voice"])


def _engine(request: Request) -> Any:
    engine = get_runtime(request).voice
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the voice engine is unavailable")
    return engine


def _capabilities(engine: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    """Ask an engine what it can do - engines are not required to implement `capabilities`."""
    if engine is None:
        return dict(fallback)
    method = getattr(engine, "capabilities", None)
    if callable(method):
        try:
            return method()
        except Exception as exc:
            return {**fallback, "error": f"{type(exc).__name__}: {exc}"}
    for probe in ("status", "available"):
        value = getattr(engine, probe, None)
        try:
            if callable(value):
                value = value()
        except Exception:
            value = None
        if isinstance(value, dict):
            return value
        if value is not None:
            return {**fallback, "available": bool(value)}
    return dict(fallback)


@router.get("/capabilities")
async def capabilities(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    payload = {
        "voice": _capabilities(runtime.voice, {"available": False, "backends": [],
                                               "reason": "the voice engine is not attached"}),
        "hearing": _capabilities(runtime.hearing, {"available": False, "ready": False,
                                                   "reason": "no hearing engine attached"}),
        "microphone": bool(getattr(runtime.hearing, "microphone_available", lambda: False)())
        if runtime.hearing is not None else False,
        "wake_words": getattr(runtime, "wakewords", []) or [],
        "pipeline": ["microphone", "vad", "speech-to-text", "executive", "text-to-speech", "speaker"],
    }
    return payload


@router.get("/voices")
async def voices(request: Request, limit: int = 200, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return {"voices": _engine(request).list_voices(limit=limit)}


@router.post("/speak")
async def speak(body: VoiceBody, request: Request, actor: str = Depends(require_owner),
                limited: None = Depends(rate_limit("voice"))) -> dict[str, Any]:
    try:
        result = _engine(request).speak(body.text, voice=body.voice, out_path=body.out_path,
                                        play=body.play, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "voice_spoken", {"ok": result.ok, "backend": result.backend},
          risk="LOW")  # type: ignore[arg-type]
    return result.to_dict()


@router.get("/history")
async def history(request: Request, limit: int = 50, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return {"utterances": _engine(request).history_dicts(limit=limit)}


@router.post("/transcribe")
async def transcribe(request: Request, file: UploadFile = File(...), language: str = "en",
                     actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Transcribe an uploaded audio file. The bytes are written to a temp file, then read."""
    import tempfile
    from pathlib import Path

    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    data = await file.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "audio file is too large")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle_file:
        handle_file.write(data)
        path = handle_file.name
    engine = _engine(request)
    try:
        return engine.transcribe(path, language=language, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/listen")
async def listen(request: Request, seconds: float = 5.0, language: str = "en",
                 actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Record from the microphone and transcribe. Requires a recorder and a microphone."""
    try:
        return _engine(request).listen_once(seconds=seconds, language=language)
    except Exception as exc:
        raise handle(exc) from exc


def _loop(request: Request) -> Any:
    loop = getattr(get_runtime(request), "voice_loop", None)
    if loop is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "the voice loop is unavailable (is the voice feature enabled?)")
    return loop


@router.get("/loop")
async def loop_status(request: Request, limit: int = 20, actor: str = Depends(require_owner)) -> dict[str, Any]:
    """The conversational loop: what it can do, what it did, and how it failed when it did."""
    loop = _loop(request)
    return {"capabilities": loop.capabilities(), "turns": loop.history(limit=limit)}


@router.post("/turn")
async def turn(body: VoiceTurnBody, request: Request, actor: str = Depends(require_owner),
               limited: None = Depends(rate_limit("voice"))) -> dict[str, Any]:
    """One voice turn: text in, or record from the microphone, then reply and speak it."""
    loop = _loop(request)
    runtime = get_runtime(request)
    try:
        result = loop.turn(text=body.text, speak=body.speak, language=body.language,
                           seconds=body.seconds, conversation_id=body.conversation_id,
                           mission_id=body.mission_id)
    except Exception as exc:
        raise handle(exc) from exc
    audit(runtime, "voice_turn", {"ok": result.ok, "input": "text" if body.text else "microphone",
                                  "interrupted": result.interrupted, "error": result.error},
          risk="LOW")  # type: ignore[arg-type]
    return result.to_dict()


@router.post("/turn/audio")
async def turn_audio(request: Request, file: UploadFile = File(...), language: str = "",
                     speak: bool = True, conversation_id: str = "", mission_id: str = "",
                     actor: str = Depends(require_owner)) -> dict[str, Any]:
    """One voice turn from an uploaded recording (what the browser and desktop shells send)."""
    loop = _loop(request)
    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty audio upload")
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "audio file is too large")
    result = loop.turn(audio=data, language=language, speak=speak,
                       conversation_id=conversation_id, mission_id=mission_id)
    audit(get_runtime(request), "voice_turn",
          {"ok": result.ok, "input": "upload", "error": result.error},
          risk="LOW")  # type: ignore[arg-type]
    return result.to_dict()


@router.post("/turn/file")
async def turn_file(body: VoiceTurnBody, request: Request,
                    actor: str = Depends(require_owner)) -> dict[str, Any]:
    """One voice turn from an audio file already on disk."""
    loop = _loop(request)
    result = loop.turn(path=body.path, language=body.language, speak=body.speak,
                       conversation_id=body.conversation_id, mission_id=body.mission_id)
    return result.to_dict()


@router.post("/interrupt")
async def interrupt(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Barge-in: stop speaking, the owner is talking."""
    loop = _loop(request)
    payload = loop.interrupt()
    audit(get_runtime(request), "voice_interrupt", payload, risk="LOW")  # type: ignore[arg-type]
    return payload
