"""Vision: see images, screenshots, cameras, video and the accessibility tree.

Everything here funnels into the same `VisionEngine` the executive uses, so a screenshot the owner
uploads through the UI and a screenshot the agent takes during a mission are analysed identically -
and both are treated as untrusted external content by the injection guard.
"""

from __future__ import annotations

import base64
import binascii
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status

from ...core import run_coroutine_sync
from ...security.injection import ContentTrust, ExternalContent, get_injection_guard
from ..deps import audit, get_runtime, handle, rate_limit, require_owner
from ..models import CameraBody, VideoBody, VisionAnalyseBody, VisionOcrBody

router = APIRouter(prefix="/vision", tags=["vision"])

#: Cap on an uploaded frame - a 4K PNG screenshot is well under this.
MAX_IMAGE_BYTES = 24 * 1024 * 1024


def _engine(request: Request) -> Any:
    engine = get_runtime(request).vision
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the vision engine is unavailable")
    return engine


def _store_frame(request: Request, data: bytes, *, suffix: str = ".png") -> Path:
    """Write an uploaded frame into the uploads directory and return its path."""
    paths = get_runtime(request).paths
    uploads = Path(paths.uploads)
    uploads.mkdir(parents=True, exist_ok=True)
    from ...core import new_id

    target = uploads / f"vision-{new_id('img')}{suffix}"
    target.write_bytes(data)
    return target


def _decode_data_url(value: str) -> tuple[bytes, str]:
    """Accept `data:image/png;base64,...` or bare base64 and return (bytes, suffix)."""
    suffix = ".png"
    payload = value
    if value.startswith("data:"):
        header, _, payload = value.partition(",")
        if "jpeg" in header or "jpg" in header:
            suffix = ".jpg"
        elif "webp" in header:
            suffix = ".webp"
        elif "gif" in header:
            suffix = ".gif"
        elif "bmp" in header:
            suffix = ".bmp"
    try:
        return base64.b64decode(payload, validate=False), suffix
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid base64 image: {exc}") from exc


def _analyse(request: Request, source: str | Path | bytes, *, prompt: str, actor: str) -> dict[str, Any]:
    engine = _engine(request)
    result = run_coroutine_sync(engine.analyze(source, prompt=prompt, actor=actor))
    payload = result.to_dict()
    payload["content_trust"] = ContentTrust.EXTERNAL.value
    if isinstance(source, (str, Path)):
        payload["source"] = str(source)
    audit(get_runtime(request), "vision_analysed",
          {"ok": bool(payload.get("ok")), "model": payload.get("model", ""),
           "suspicious": bool(payload.get("suspicious"))},
          risk="LOW")  # type: ignore[arg-type]
    return payload


@router.get("/capabilities")
async def capabilities(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    engine = runtime.vision
    payload = engine.capabilities() if engine is not None else {"available": False}
    accessibility = _accessibility(request)
    payload["accessibility"] = {"available": accessibility.get("ok", False),
                                "detail": accessibility.get("error", "")}
    payload["document_reader"] = _document_reader_capabilities(request)
    return payload


def _document_reader_capabilities(request: Request) -> dict[str, Any]:
    from ...perception import get_document_reader

    reader = get_document_reader()
    return {"available": True, "formats": sorted(reader.supported_extensions)
            if hasattr(reader, "supported_extensions") else
            ["txt", "md", "csv", "json", "pdf", "docx", "xlsx", "pptx", "png", "jpg", "zip"]}


def _accessibility(request: Request) -> dict[str, Any]:
    """Read the operating system accessibility tree. Honest when the platform cannot provide it."""
    try:

        from ...computer import get_accessibility_tree

        return get_accessibility_tree()
    except Exception as exc:  # pragma: no cover - platform dependent
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "nodes": []}


@router.post("/analyse")
async def analyse(body: VisionAnalyseBody, request: Request,
                  actor: str = Depends(require_owner),
                  limited: None = Depends(rate_limit("vision"))) -> dict[str, Any]:
    """Analyse an image already on disk (inside the allowed roots)."""
    runtime = get_runtime(request)
    if body.data_url:
        data, suffix = _decode_data_url(body.data_url)
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "image is too large")
        path = _store_frame(request, data, suffix=suffix)
    elif body.path:
        path = Path(body.path).expanduser()
        from ...security.policy import Capability

        try:
            path = runtime.policy.resolve_path(str(path), must_exist=True)
            runtime.policy.require(Capability.FS_READ, str(path))
        except Exception as exc:
            raise handle(exc) from exc
        if not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"image not found: {path}")
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "provide either path or data_url")
    try:
        return _analyse(request, path, prompt=body.prompt, actor=actor)
    except HTTPException:
        raise
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/ocr")
async def ocr(body: VisionOcrBody, request: Request, actor: str = Depends(require_owner),
              limited: None = Depends(rate_limit("vision"))) -> dict[str, Any]:
    """Extract text from an image, screenshot or document without a vision model."""
    engine = _engine(request)
    runtime = get_runtime(request)
    text, source, meta = "", "", {}
    if body.path:
        target = Path(body.path).expanduser()
        if not target.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"file not found: {target}")
        source = str(target)
        if target.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
            text = engine._ocr(target.read_bytes())  # noqa: SLF001 - deliberate: OCR path is local-only
        else:
            from ...perception import get_document_reader

            extract = get_document_reader().read(target)
            text, meta = extract.text, extract.to_dict()
    elif body.data_url:
        data, _ = _decode_data_url(body.data_url)
        source = "inline"
        text = engine._ocr(data)  # noqa: SLF001
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "provide either path or data_url")
    if not text:
        return {"ok": False, "source": source, "text": "", "error":
                "no text extracted: install pytesseract + tesseract, or pass a document type "
                "supported by the document reader", "meta": meta}
    guard = get_injection_guard()
    verdict = guard.inspect(text, source=f"ocr:{source}")
    audit(runtime, "vision_ocr", {"source": source, "chars": len(text), "suspicious": verdict.suspicious},
          risk="LOW")  # type: ignore[arg-type]
    return {"ok": True, "source": source, "text": text, "chars": len(text),
            "suspicious": verdict.suspicious, "findings": verdict.findings, "meta": meta,
            "trust": ContentTrust.EXTERNAL.value}


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), prompt: str = "",
                 actor: str = Depends(require_owner),
                 limited: None = Depends(rate_limit("vision"))) -> dict[str, Any]:
    """Upload an image and analyse it in one step (what the UI uses)."""
    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty upload")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "image is too large")
    suffix = Path(file.filename or "image.png").suffix or ".png"
    path = _store_frame(request, data, suffix=suffix)
    try:
        payload = _analyse(request, path, prompt=prompt, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    payload["stored_path"] = str(path)
    return payload


@router.post("/screen")
async def screen(body: VisionAnalyseBody, request: Request,
                 actor: str = Depends(require_owner),
                 limited: None = Depends(rate_limit("vision"))) -> dict[str, Any]:
    """Screenshot the desktop (through the computer-use controller, so policy still applies) and look at it."""
    runtime = get_runtime(request)
    if runtime.computer is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "computer use is unavailable")
    try:
        capture = runtime.computer.screenshot(actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    path = capture.get("path") or capture.get("result", {}).get("path", "")
    if not path or not Path(path).is_file():
        return {"ok": False, "error": "screenshot failed", "capture": capture}
    payload = _analyse(request, path, prompt=body.prompt, actor=actor)
    payload["capture"] = capture
    return payload


@router.post("/camera")
async def camera(body: CameraBody, request: Request,
                 actor: str = Depends(require_owner),
                 limited: None = Depends(rate_limit("vision"))) -> dict[str, Any]:
    """Grab one frame from a webcam and analyse it. Requires OpenCV and a camera."""
    try:
        import cv2  # type: ignore
    except Exception as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED,
                            f"camera capture needs opencv-python ({type(exc).__name__})") from exc
    capture = cv2.VideoCapture(body.index)
    try:
        ok, frame = capture.read()
    finally:
        capture.release()
    if not ok:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"camera {body.index} produced no frame")
    ok, buffer = cv2.imencode(".png", frame)
    if not ok:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "could not encode the camera frame")
    path = _store_frame(request, bytes(buffer.tobytes()), suffix=".png")
    payload = _analyse(request, path, prompt=body.prompt, actor=actor)
    payload["stored_path"] = str(path)
    payload["camera"] = body.index
    return payload


@router.post("/video")
async def video(body: VideoBody, request: Request,
                actor: str = Depends(require_owner),
                limited: None = Depends(rate_limit("vision"))) -> dict[str, Any]:
    """Sample frames from a video with ffmpeg and analyse each one."""
    import shutil
    import subprocess

    runtime = get_runtime(request)
    target = Path(body.path).expanduser()
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"video not found: {target}")
    if shutil.which("ffmpeg") is None:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED,
                            "video frame extraction needs ffmpeg on PATH")
    frames = max(1, min(int(body.frames), 8))
    out_dir = Path(runtime.paths.uploads) / "video-frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for index in range(frames):
        target_frame = out_dir / f"{target.stem}-{index}.png"
        command = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(target), "-vf",
                   f"select=eq(n\\,{index * 30})", "-frames:v", "1", str(target_frame)]
        process = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        if process.returncode != 0 or not target_frame.exists():
            results.append({"ok": False, "frame": index, "error": process.stderr.strip()[:200]})
            continue
        analysis = _analyse(request, target_frame, prompt=body.prompt, actor=actor)
        analysis["frame"] = index
        results.append(analysis)
    ok = any(item.get("ok") for item in results)
    return {"ok": ok, "video": str(target), "frames": results, "count": len(results),
            "error": "" if ok else "no frame could be analysed"}


@router.get("/accessibility")
async def accessibility(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    """The OS accessibility tree: what the screen *means*, not just what it looks like."""
    tree = _accessibility(request)
    audit(get_runtime(request), "accessibility_read", {"ok": bool(tree.get("ok"))},
          risk="LOW")  # type: ignore[arg-type]
    return tree


@router.post("/ui")
async def ui_understanding(body: VisionAnalyseBody, request: Request,
                           actor: str = Depends(require_owner),
                           limited: None = Depends(rate_limit("vision"))) -> dict[str, Any]:
    """Interpret a UI: combine the screenshot with the accessibility tree into one untrusted block."""
    runtime = get_runtime(request)
    tree = _accessibility(request)
    visual: dict[str, Any] = {"ok": False, "error": "no image provided"}
    if body.path or body.data_url:
        visual = await analyse(body, request, actor)
    elements = tree.get("nodes", []) if isinstance(tree, dict) else []
    summary_lines = [f"- {item.get('role', 'element')}: {item.get('name', '')}".rstrip()
                     for item in elements[:120] if isinstance(item, dict)]
    text = (visual.get("text", "") or "").strip()
    if summary_lines:
        text += "\n\nAccessibility elements:\n" + "\n".join(summary_lines)
    guard = get_injection_guard()
    verdict = guard.inspect(text, source="ui:interpretation")
    audit(runtime, "ui_interpreted", {"ok": bool(text), "elements": len(summary_lines)},
          risk="LOW")  # type: ignore[arg-type]
    return {"ok": bool(text), "text": text, "visual": visual, "elements": summary_lines,
            "suspicious": verdict.suspicious, "findings": verdict.findings,
            "trust": ContentTrust.EXTERNAL.value, "source": "ui"}


@router.post("/documents")
async def documents(body: VisionOcrBody, request: Request,
                    actor: str = Depends(require_owner),
                    limited: None = Depends(rate_limit("vision"))) -> dict[str, Any]:
    """Read a document (pdf/docx/xlsx/pptx/csv/image/zip/text) as untrusted content."""
    from ...perception import get_document_reader

    target = Path(body.path).expanduser()
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"document not found: {target}")
    try:
        extract = get_document_reader().read(target)
    except Exception as exc:
        raise handle(exc) from exc
    guard = get_injection_guard()
    verdict = guard.inspect(extract.text, source=f"document:{target}")
    audit(get_runtime(request), "document_read",
          {"path": str(target), "chars": len(extract.text), "suspicious": verdict.suspicious},
          risk="LOW")  # type: ignore[arg-type]
    payload = extract.to_dict()
    payload["suspicious"] = verdict.suspicious
    payload["findings"] = verdict.findings
    payload["trust"] = ContentTrust.EXTERNAL.value
    return payload


# Keep a reference so `ExternalContent` imported above is used in type checking paths.
_ = ExternalContent
