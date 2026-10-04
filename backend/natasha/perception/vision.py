"""Vision: understand images and screenshots through a vision-capable model.

No vision model available? The engine says so - it never invents a description. Local OCR is used
for text-in-image when configured, and large images are downscaled before they reach a provider.
"""

from __future__ import annotations

import base64
import io
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import ConfigurationError, NotFoundError
from ..security.injection import ContentTrust, ExternalContent, get_injection_guard

MAX_EDGE = 1568
MAX_BYTES = 8 * 1024 * 1024
DEFAULT_PROMPT = ("Describe this image factually and completely: objects, people, text, layout, "
                  "and anything unusual. Report uncertainty instead of guessing.")

_SNIFF = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "image/webp",
    b"BM": "image/bmp",
}


@dataclass
class ImageAnalysis:
    ok: bool
    text: str = ""
    model: str = ""
    provider: str = ""
    path: str = ""
    width: int = 0
    height: int = 0
    bytes: int = 0
    ocr_text: str = ""
    error: str = ""
    suspicious: bool = False
    findings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def content(self) -> ExternalContent:
        return ExternalContent(text=self.text, source=f"image:{self.path or 'inline'}",
                               trust=ContentTrust.EXTERNAL, metadata={"kind": "vision"})

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "model": self.model, "provider": self.provider, "path": self.path,
                "width": self.width, "height": self.height, "bytes": self.bytes,
                "suspicious": self.suspicious, "findings": self.findings, "error": self.error,
                "text": self.text, "ocr_text": self.ocr_text}


def _detect_mime(data: bytes) -> str:
    """The MIME type implied by the file's own bytes, or "" when nothing matches.

    Returning a default here would be a lie with consequences: unidentifiable bytes would be sent to
    a vision model as "image/png" and the model's answer about nothing would be reported as a
    description of the file.
    """
    for signature, mime in _SNIFF.items():
        if data.startswith(signature):
            return mime
    return ""


def _identify(data: bytes) -> str:
    """Ask Pillow what these bytes are (formats without a magic signature), or ""."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
            fmt = (probe.format or "").lower()
    except Exception:
        return ""
    return {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "gif": "image/gif",
            "webp": "image/webp", "bmp": "image/bmp", "tiff": "image/tiff"}.get(fmt, "")


def _downscale(data: bytes, mime: str) -> tuple[bytes, int, int, dict[str, Any]]:
    """Shrink an image so it fits provider limits. Returns (data, width, height, info)."""
    info: dict[str, Any] = {}
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        width, height = image.size
        info = {"mode": image.mode, "format": image.format or mime}
        changed = False
        if max(width, height) > MAX_EDGE:
            ratio = MAX_EDGE / max(width, height)
            image = image.resize((max(1, int(width * ratio)), max(1, int(height * ratio))))
            changed = True
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
            changed = True
        if len(data) > MAX_BYTES:
            changed = True
        if changed:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            data = buffer.getvalue()
            mime = "image/png"
        info["size"] = image.size
        return data, image.size[0], image.size[1], info
    except Exception as exc:  # noqa: BLE001 - Pillow may simply not be installed
        # No Pillow: still report real dimensions by reading the file header, and be explicit that
        # no downscaling happened (a huge image may be rejected by the provider, which is honest).
        info["downscale_error"] = f"{type(exc).__name__}: {exc}"
        info["downscaled"] = False
        width, height = _header_size(data, mime)
        info["size"] = [width, height]
        return data, width, height, info


def _header_size(data: bytes, mime: str) -> tuple[int, int]:
    """Read width/height straight out of the image header (no third-party dependency)."""
    try:
        if mime == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if mime == "image/gif":
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if mime == "image/bmp":
            return int.from_bytes(data[18:22], "little"), int.from_bytes(data[22:26], "little")
        if mime == "image/jpeg":
            index = 2
            while index + 9 < len(data):
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                    height = int.from_bytes(data[index + 5:index + 7], "big")
                    width = int.from_bytes(data[index + 7:index + 9], "big")
                    return width, height
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    index += 2
                    continue
                index += 2 + int.from_bytes(data[index + 2:index + 4], "big")
        if mime == "image/webp" and data[8:12] == b"VP8X":
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
    except Exception:  # pragma: no cover - malformed header
        return 0, 0
    return 0, 0


class VisionEngine:
    """Vision analysis with an honest availability story."""

    def __init__(self, *, brain: Any = None, computer: Any = None) -> None:
        self._brain = brain
        #: Screenshots are *captured* by the computer controller, not by this engine. Reporting a
        #: hardcoded True here would tell the UI and the model that screenshots work on a machine
        #: with no desktop backend at all.
        self.computer = computer

    @property
    def brain(self) -> Any:
        if self._brain is None:
            from ..brain import get_brain_client

            self._brain = get_brain_client()
        return self._brain

    def available(self) -> bool:
        try:
            return any(capability.value == "vision" for capability in self.brain.registry.capabilities())
        except Exception:
            return False

    def capabilities(self) -> dict[str, Any]:
        """What this engine can honestly do right now (no optimistic guesses)."""
        models: list[str] = []
        try:
            models = list(self.brain.registry.models_with_capability("vision"))
        except Exception:
            models = []
        ocr = False
        try:
            import importlib.util

            ocr = (importlib.util.find_spec("pytesseract") is not None
                   and importlib.util.find_spec("PIL") is not None)
        except Exception:
            ocr = False
        return {
            "available": self.available(),
            "models": models,
            "ocr": ocr,
            "screenshot": self.screenshot_available(),
            "camera": self.camera_available(),
            "video": bool(shutil.which("ffmpeg")),
            # Interpreting a UI needs either a vision model (to see) or the accessibility tree (to
            # name elements). Claiming both are always possible is how a UI ends up offering a
            # button that can only fail.
            "ui_interpretation": bool(self.available() or self.accessibility_available()),
            "accessibility_tree": self.accessibility_available(),
            "mime_types": ["image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"],
            "reason": "" if self.available() else (
                "no vision-capable model is configured; OCR-only when pytesseract is installed"),
        }

    def screenshot_available(self) -> bool:
        """True only when a desktop backend can actually capture the screen."""
        if self.computer is None:
            return False
        try:
            actions = self.computer.capabilities().get("actions", {})
        except Exception:
            return False
        return bool(actions.get("screenshot"))

    @staticmethod
    def camera_available() -> bool:
        try:
            import importlib.util

            return importlib.util.find_spec("cv2") is not None
        except Exception:
            return False

    @staticmethod
    def accessibility_available() -> bool:
        import sys

        try:
            if sys.platform == "linux":
                import importlib.util

                return importlib.util.find_spec("pyatspi") is not None
            return True
        except Exception:
            return False

    async def analyze(self, image: str | Path | bytes, *, prompt: str = "", actor: str = "model:main",
                      provider: str = "") -> ImageAnalysis:
        """Analyse an image, returning text - or an explicit failure, never a guess."""
        if isinstance(image, (str, Path)):
            path = Path(image).expanduser()
            if not path.is_file():
                raise NotFoundError(f"image not found: {path}")
            data = path.read_bytes()
            source = str(path)
        else:
            data, source = bytes(image), "inline"
        mime = _detect_mime(data)
        if not mime:
            mime = _identify(data)
        if not mime:
            # Not an image at all. Sending it to a vision provider would produce a fluent answer
            # about nothing, which is the exact failure mode this engine must not have.
            return ImageAnalysis(
                ok=False, path=source, bytes=len(data), error=(
                    "not a recognised image format (supported: png, jpeg, webp, gif, bmp)"),
                metadata={"detected_mime": "", "size": [0, 0]})
        prepared, width, height, info = _downscale(data, mime)
        guard = get_injection_guard()
        result = ImageAnalysis(ok=False, path=source, width=width, height=height, bytes=len(data),
                               metadata=info)
        try:
            response = await self.brain.complete_vision(
                base64.b64encode(prepared).decode(), mime_type="image/png" if prepared is not data else mime,
                prompt=prompt or DEFAULT_PROMPT, actor=actor, provider=provider,
            )
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.ocr_text = self._ocr(prepared)
            if result.ocr_text:
                result.ok = True
                result.text = f"(vision model unavailable - text found by OCR)\n{result.ocr_text}"
                result.model = "ocr"
            return result
        result.ok = True
        result.text = str(getattr(response, "text", response) or getattr(response, "content", "") or "")
        model = getattr(response, "model", None)
        if isinstance(model, str):
            result.model = model
        else:
            result.model = getattr(model, "id", "") or getattr(response, "model_id", "")
        result.provider = (str(getattr(response, "provider", "") or "")
                           or str(getattr(model, "provider", "") or ""))
        verdict = guard.inspect(result.text, source=f"vision:{source}")
        result.suspicious = verdict.suspicious
        result.findings = verdict.findings
        return result

    @staticmethod
    def _ocr(data: bytes) -> str:
        try:
            import pytesseract  # type: ignore
            from PIL import Image

            return pytesseract.image_to_string(Image.open(io.BytesIO(data))).strip()
        except Exception:
            return ""


def describe_image(image: str | Path | bytes, *, prompt: str = "", **kwargs: Any) -> Any:
    """Sync helper used by tools and the CLI (safe inside a running event loop)."""
    from ..core import run_coroutine_sync

    return run_coroutine_sync(get_vision_engine().analyze(image, prompt=prompt, **kwargs))


def extract_text_from_image(image: str | Path | bytes, **kwargs: Any) -> str:
    analysis = describe_image(image, **kwargs)
    return analysis.ocr_text or analysis.text


_ENGINE: VisionEngine | None = None
_LOCK = threading.Lock()


def get_vision_engine(**kwargs: Any) -> VisionEngine:
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = VisionEngine(**kwargs)
        return _ENGINE


def reset_vision_engine() -> None:
    """Drop the cached engine. The runtime builds its own; this exists for tests and reloads."""
    global _ENGINE
    with _LOCK:
        _ENGINE = None
