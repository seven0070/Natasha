"""The creation engine: jobs that produce artifacts and tell the truth about how."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import threading
import textwrap
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..core import NatashaError, new_id
from ..core.clock import iso
from ..core.hashing import sha256_file
from ..core.risk import RiskLevel
from ..events import EventKind, get_event_log
from .video import VideoPipeline

#: What the engine can be asked to make.
KINDS = ("document", "image", "audio", "video", "slides")

#: Leading bytes that identify a format. Anything declared as one of these kinds is checked against
#: them, so a truncated or mislabelled file is reported instead of being called a success.
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image": (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"<svg", b"<?xml"),
    "audio": (b"RIFF", b"ID3", b"\xff\xfb", b"\xff\xf3", b"OggS", b"fLaC", b"#!AMR"),
    "video": (b"\x1a\x45\xdf\xa3",),  # Matroska/WebM; MP4 is recognised by "ftyp" at offset 4.
}


def _looks_like(data: bytes, kind: str) -> bool:
    """True when the first bytes of the file are consistent with the declared kind."""
    if kind == "video":
        return data.startswith(_MAGIC["video"]) or data[4:8] == b"ftyp"
    prefixes = _MAGIC.get(kind)
    if prefixes is None:  # documents, slides, storyboards, narration scripts - text or JSON
        return True
    if kind == "image" and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return any(data.startswith(prefix) for prefix in prefixes)


def verify_artifact(path: str | Path, kind: str = "") -> dict[str, Any]:
    """Read an artifact back off disk and report what is actually there.

    The hash is the file's, not the producer's claim: a creation job that writes nothing, writes a
    different format than it announced, or whose output is truncated is reported as such.
    """
    target = Path(path)
    checked_at = iso()
    base = {"checked_at": checked_at, "path": str(target), "kind": kind}
    if not target.is_file():
        return {**base, "status": "missing", "method": "exists + sha256 on disk",
                "detail": "the file does not exist"}
    size = target.stat().st_size
    if size == 0:
        return {**base, "status": "empty", "method": "exists + sha256 on disk",
                "detail": "the file exists but is empty", "bytes": 0}
    with target.open("rb") as handle:
        head = handle.read(16)
    if not _looks_like(head, kind):
        return {**base, "status": "format-mismatch", "method": "format signature + sha256 on disk",
                "detail": f"declared {kind or 'file'} but the file starts with {head[:8]!r}",
                "bytes": size, "sha256": sha256_file(target)}
    if kind in ("document", "slides", "storyboard", "text"):
        try:
            target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            return {**base, "status": "unreadable", "method": "utf-8 decode + sha256 on disk",
                    "detail": f"declared text but the bytes are not valid UTF-8: {exc}",
                    "bytes": size, "sha256": sha256_file(target)}
    return {**base, "status": "verified", "method": "exists + format signature + sha256 on disk",
            "detail": f"{size} bytes read back from disk", "bytes": size,
            "sha256": sha256_file(target)}


class CreationStage(str, Enum):
    """A pipeline stage and whether it really ran."""

    PLANNED = "planned"
    RAN = "ran"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class CreationJob:
    """One creation request and everything that happened to it."""

    id: str = field(default_factory=lambda: new_id("job"))
    kind: str = ""
    brief: str = ""
    status: str = "pending"        # pending | partial | succeeded | failed
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    stages: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""
    started_at: str = field(default_factory=iso)
    finished_at: str = ""
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def stage(self, name: str, state: CreationStage, detail: str = "") -> None:
        self.stages.append({"stage": name, "state": state.value, "detail": detail[:300], "at": iso()})

    def artifact(self, path: str, *, kind: str = "", note: str = "", bytes_: int = 0,
                 produced_by: str = "") -> dict[str, Any]:
        """Record an artifact with everything needed to audit it later.

        id, type, MIME, size, sha256, provenance (which job and which generator produced it, and
        whether a model was involved) and a verification block that was computed by reading the
        file back - not by trusting the code that wrote it.
        """
        resolved = kind or self.kind
        target = Path(path)
        verification = verify_artifact(target, resolved)
        mime, _ = mimetypes.guess_type(target.name)
        record: dict[str, Any] = {
            "id": new_id("art"),
            "path": str(path),
            "name": target.name,
            "kind": resolved,
            "mime": mime or "application/octet-stream",
            "bytes": bytes_ or int(verification.get("bytes") or 0),
            "note": note,
            "sha256": verification.get("sha256", ""),
            "created_at": iso(),
            "provenance": {
                "job_id": self.id,
                "job_kind": self.kind,
                "brief": self.brief[:200],
                "produced_by": produced_by or note or "creation engine",
                # Only a generator that names itself "model:..." counts as model-generated. A local
                # composition or a template skeleton is never described as an AI output.
                "model_generated": (produced_by or "").startswith("model"),
                "actor": "owner",
            },
            "verification": verification,
        }
        self.artifacts.append(record)
        return record

    @property
    def unverified(self) -> list[dict[str, Any]]:
        """Artifacts whose bytes on disk do not match what the job says it produced."""
        return [item for item in self.artifacts
                if (item.get("verification") or {}).get("status") != "verified"]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "brief": self.brief, "status": self.status,
                "artifacts": self.artifacts, "stages": self.stages, "notes": self.notes,
                "error": self.error, "started_at": self.started_at, "finished_at": self.finished_at,
                "duration_ms": round(self.duration_ms, 2), "metadata": self.metadata}


class CreationEngine:
    """Text, images, audio, video - with per-stage honesty and no fabricated outputs."""

    def __init__(self, *, brain: Any = None, tools: Any = None, memory: Any = None,
                 artifacts: Path | None = None, log: Any = None, voice: Any = None,
                 broker: Any = None, settings: Any = None) -> None:
        self.brain = brain
        self.tools = tools
        self.memory = memory
        self.log = log or get_event_log()
        self.voice = voice
        self.broker = broker
        self.settings = settings
        if artifacts is None:
            from ..core import get_paths

            artifacts = get_paths().artifacts
        self.artifacts = Path(artifacts)
        self.video_pipeline = VideoPipeline(brain=brain, voice=voice, artifacts=self.artifacts / "video")
        self.jobs: dict[str, CreationJob] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ config
    def _creation_settings(self) -> Any:
        return getattr(self.settings, "creation", None) if self.settings is not None else None

    def _setting(self, name: str, default: Any = "") -> Any:
        source = self._creation_settings()
        if source is None:
            return default
        if isinstance(source, dict):
            return source.get(name, default)
        return getattr(source, name, default)

    def capabilities(self) -> dict[str, Any]:
        endpoint = self._setting("image_endpoint")
        return {
            "kinds": list(KINDS),
            "brain": self.brain is not None,
            "image_model": {"endpoint": endpoint or "", "configured": bool(endpoint)},
            "audio": self.voice.capabilities() if self.voice is not None else {"available": False,
                                                                              "reason": "no voice engine attached"},
            "video": self.video_pipeline.capabilities(),
            "artifacts_dir": str(self.artifacts),
            "honesty": "artifacts record which generator produced them; nothing is presented as "
                       "model-generated unless a model generated it",
        }

    # ------------------------------------------------------------------ dispatch
    async def create(self, kind: str, brief: str, **options: Any) -> CreationJob:
        """Create something, recording exactly which stages ran."""
        kind = (kind or "").strip().lower()
        if kind not in KINDS:
            raise NatashaError(f"unknown creation kind {kind!r}; known: {', '.join(KINDS)}")
        job = CreationJob(kind=kind, brief=brief, metadata=dict(options.get("metadata") or {}))
        with self._lock:
            self.jobs[job.id] = job
        started = time.perf_counter()
        try:
            if kind == "document":
                await self._document(job, brief, **options)
            elif kind == "image":
                await self._image(job, brief, **options)
            elif kind == "audio":
                await self._audio(job, brief, **options)
            elif kind == "video":
                await self._video(job, brief, **options)
            else:
                await self._slides(job, brief, **options)
        except Exception as exc:
            job.status = "failed" if not job.artifacts else "partial"
            job.error = f"{type(exc).__name__}: {exc}"
            job.stage("dispatch", CreationStage.FAILED, job.error)
        finally:
            job.duration_ms = (time.perf_counter() - started) * 1000
            job.finished_at = iso()
        broken = job.unverified
        if broken:
            detail = "; ".join(f"{Path(item['path']).name}: "
                               f"{(item.get('verification') or {}).get('status')}" for item in broken)
            job.notes.append(f"artifact verification failed - {detail}")
            if job.status in ("succeeded", "pending"):
                job.status = "failed" if not [item for item in job.artifacts if item not in broken] \
                    else "partial"
            if not job.error:
                job.error = f"artifact verification failed: {detail}"
            job.stage("verify", CreationStage.FAILED, detail)
        elif job.artifacts:
            job.stage("verify", CreationStage.RAN,
                      f"{len(job.artifacts)} artifact(s) hashed and read back from disk")
        self._audit(job)
        return job

    def _audit(self, job: CreationJob) -> None:
        self.log.append(EventKind.CREATION, {"action": "create", **job.to_dict()},
                        actor="owner", source="creation",
                        risk=RiskLevel.LOW if job.status == "succeeded" else RiskLevel.MEDIUM)
        if self.memory is not None and job.artifacts:
            try:
                for artifact in job.artifacts:
                    self.memory.note_artifact(artifact["path"], description=f"{job.kind}: {job.brief[:80]}")
            except Exception:
                pass

    # ------------------------------------------------------------------ documents
    async def _document(self, job: CreationJob, brief: str, *, title: str = "", words: int = 500,
                        filename: str = "", **_: Any) -> None:
        job.stage("plan", CreationStage.RAN, "outline the document")
        text = ""
        produced_by = "template:skeleton"
        if self.brain is not None:
            try:
                from ..brain import ChatMessage, CompletionRequest

                prompt = (f"Write a {words}-word document titled {title or brief[:60]!r}.\n"
                          f"Brief: {brief}\nUse plain language, mark anything uncertain, and do not "
                          "invent facts, sources or numbers.")
                response = await self.brain.complete(CompletionRequest(
                    messages=[ChatMessage.system("You are Natasha's writing engine. Never invent facts."),
                              ChatMessage.user(prompt)],
                    task="write", actor="owner", trace_id=job.id))
                text = (getattr(response, "text", "") or "").strip()
                model = getattr(response, "model", "") or getattr(response, "provider", "")
                produced_by = f"model:{model}" if model else "model"
                job.stage("draft", CreationStage.RAN, f"model produced {len(text)} chars")
            except Exception as exc:
                job.stage("draft", CreationStage.FAILED, f"{type(exc).__name__}: {exc}")
                job.notes.append("The model draft failed; writing a skeleton instead.")
        if not text:
            text = (f"# {title or brief[:60]}\n\n"
                    f"## Brief\n{brief}\n\n## Status\nNo model was available to draft this document. "
                    "The sections below are an empty skeleton to fill in.\n")
            job.stage("draft", CreationStage.SKIPPED, "no model available - skeleton written")
        if not text.lstrip().startswith("#"):
            text = f"# {title or brief[:60]}\n\n{text}\n"
        target = self.artifacts / "documents" / (filename or f"{_slug(title or brief)}.md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        job.stage("write", CreationStage.RAN, str(target))
        job.artifact(str(target), kind="document", note="markdown", bytes_=target.stat().st_size,
                     produced_by=produced_by)
        job.status = "succeeded"

    # ------------------------------------------------------------------ images
    async def _image(self, job: CreationJob, brief: str, *, count: int = 1, size: str = "1024x1024",
                     filename: str = "", **_: Any) -> None:
        count = max(1, min(8, int(count)))
        endpoint = self._setting("image_endpoint")
        made = 0
        for index in range(count):
            target = self.artifacts / "images" / (filename if count == 1 and filename
                                                  else f"{_slug(brief)}-{index + 1}.png")
            target.parent.mkdir(parents=True, exist_ok=True)
            if endpoint:
                try:
                    await self._image_remote(brief, endpoint, target, size=size)
                    job.stage(f"image[{index}]", CreationStage.RAN, f"image model -> {target}")
                    job.artifact(str(target), kind="image",
                                 note="generated by the configured image model",
                                 produced_by=f"model:{self._setting('image_model', 'image-endpoint')}")
                    made += 1
                    continue
                except Exception as exc:
                    job.stage(f"image[{index}]", CreationStage.FAILED, f"{type(exc).__name__}: {exc}")
                    job.notes.append("The image model call failed; a labelled local composition was "
                                     "produced instead.")
            try:
                self._image_local(brief, target, size=size, index=index)
                job.stage(f"image[{index}]", CreationStage.RAN, f"local composition -> {target}")
                job.artifact(str(target), kind="image",
                             note="LOCAL COMPOSITION - not model-generated; no image model configured",
                             produced_by="local:pillow")
                made += 1
            except Exception as exc:
                job.stage(f"image[{index}]", CreationStage.FAILED, f"{type(exc).__name__}: {exc}")
        if made == 0:
            job.status = "failed"
            job.error = "no image could be produced (no image model, and local composition failed)"
        else:
            job.status = "succeeded" if not job.notes else "partial"

    async def _image_remote(self, brief: str, endpoint: str, target: Path, *, size: str) -> None:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.broker is not None:
            handle = self.broker.issue("image_generation", purpose="image_generation")
            if handle is not None:
                headers.update(handle.headers())
        payload = {"model": self._setting("image_model", "gpt-image-1"), "prompt": brief,
                   "size": size, "n": 1}
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        b64 = ""
        items = data.get("data") or []
        if items and isinstance(items[0], dict):
            b64 = items[0].get("b64_json") or ""
            if not b64 and items[0].get("url"):
                async with httpx.AsyncClient(timeout=120) as client:
                    image_response = await client.get(items[0]["url"])
                    image_response.raise_for_status()
                    target.write_bytes(image_response.content)
                    return
        if not b64:
            raise NatashaError("the image endpoint returned no image data")
        import base64

        target.write_bytes(base64.b64decode(b64))

    def _image_local(self, brief: str, target: Path, *, size: str, index: int) -> None:
        """Deterministic local composition: a real PNG that is labelled as what it is."""
        from PIL import Image, ImageDraw

        width, height = _parse_size(size)
        digest = hashlib.sha256(f"{brief}:{index}".encode()).digest()
        base = (digest[0], digest[1], digest[2])
        image = Image.new("RGB", (width, height), base)
        draw = ImageDraw.Draw(image)
        for y in range(height):
            blend = y / max(1, height - 1)
            colour = tuple(int(channel * (1 - blend) + (255 - channel) * blend * 0.4)
                           for channel in base)
            draw.line([(0, y), (width, y)], fill=colour)
        margin = max(24, width // 16)
        wrapped = textwrap.wrap(brief, width=max(20, width // 14))[:12]
        line_height = max(18, width // 40)
        y = height // 3
        for line in wrapped:
            draw.text((margin, y), line, fill=(255, 255, 255))
            y += line_height
        draw.text((margin, height - margin - line_height),
                  "LOCAL COMPOSITION - no image model configured", fill=(255, 255, 255))
        image.save(target, format="PNG")

    # ------------------------------------------------------------------ audio
    async def _audio(self, job: CreationJob, brief: str, *, text: str = "", voice: str = "",
                     filename: str = "", **_: Any) -> None:
        script = (text or brief).strip()
        if self.voice is None:
            job.stage("speak", CreationStage.SKIPPED, "no voice engine attached")
            job.status = "failed"
            job.error = "no voice engine attached to the creation engine"
            return
        target = self.artifacts / "audio" / (filename or f"{_slug(brief)}.wav")
        target.parent.mkdir(parents=True, exist_ok=True)
        result = await asyncio.to_thread(self.voice.speak, script, voice=voice, out_path=str(target))
        if result.ok:
            job.stage("speak", CreationStage.RAN, f"{result.backend} -> {result.audio_path}")
            job.artifact(result.audio_path, kind="audio", note=f"spoken by {result.backend}",
                         bytes_=Path(result.audio_path).stat().st_size,
                         produced_by=f"voice:{result.backend}")
            if script != brief:
                script_path = target.with_suffix(".txt")
                script_path.write_text(script, encoding="utf-8")
                job.artifact(str(script_path), kind="text", note="narration script",
                             produced_by="local:script")
            job.status = "succeeded"
        else:
            job.stage("speak", CreationStage.FAILED, result.error)
            job.status = "failed"
            job.error = result.error

    # ------------------------------------------------------------------ video
    async def _video(self, job: CreationJob, brief: str, *, shots: int = 5, style: str = "",
                     title: str = "", render: bool = True, images: list[str] | None = None,
                     **_: Any) -> None:
        job.stage("storyboard", CreationStage.RAN, "planning shots")
        board = await self.video_pipeline.storyboard(brief, title=title, shots=shots, style=style)
        board_path = self.artifacts / "video" / f"{_slug(board.title)}-storyboard.json"
        board_path.parent.mkdir(parents=True, exist_ok=True)
        board_path.write_text(json.dumps(board.to_dict(), indent=2), encoding="utf-8")
        markdown_path = board_path.with_suffix(".md")
        markdown_path.write_text(board.to_markdown(), encoding="utf-8")
        planner = f"model:{board.source}" if board.source == "model" else f"local:{board.source}"
        job.artifact(str(board_path), kind="storyboard", note="shot list (JSON)", produced_by=planner)
        job.artifact(str(markdown_path), kind="storyboard", note="shot list (markdown)",
                     produced_by=planner)
        job.stage("storyboard", CreationStage.RAN, f"{len(board.shots)} shots, {board.duration_seconds}s")

        if images:
            self.video_pipeline.frames(board, images)
            job.stage("frames", CreationStage.RAN, f"{len(images)} supplied frames")
        else:
            # Generate one image per shot with the same honesty rules as _image().
            frame_job = CreationJob(kind="image", brief=f"{board.title} frames")
            made: list[str] = []
            for shot in board.shots:
                target = self.artifacts / "video" / "frames" / f"{_slug(board.title)}-{shot.index + 1}.png"
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self._image_local(shot.prompt or shot.description, target, size="1280x720",
                                      index=shot.index)
                    shot.image = str(target)
                    made.append(str(target))
                except Exception as exc:
                    frame_job.stage(f"frame[{shot.index}]", CreationStage.FAILED, str(exc))
            if made:
                job.stage("frames", CreationStage.RAN,
                          f"{len(made)} local frames (no image model configured)")
                job.notes.append("Frames are labelled local compositions, not model-generated images.")
                for frame in made:
                    job.artifact(frame, kind="image", note="LOCAL COMPOSITION frame",
                                 produced_by="local:pillow")

        narration = self.video_pipeline.narrate(board)
        if narration.get("ok"):
            job.stage("narration", CreationStage.RAN, narration["audio"])
            job.artifact(narration["audio"], kind="audio", note="narration",
                         produced_by=f"voice:{narration.get('backend', 'unknown')}")
        else:
            job.stage("narration", CreationStage.SKIPPED, narration.get("error", "no narration"))

        if not render:
            job.stage("render", CreationStage.SKIPPED, "render=False")
            job.status = "partial"
        else:
            outcome = self.video_pipeline.render(board, audio=narration.get("audio", ""))
            if outcome.get("ok"):
                job.stage("render", CreationStage.RAN, outcome["video"])
                job.artifact(outcome["video"], kind="video", note="rendered with ffmpeg",
                             bytes_=outcome.get("bytes", 0), produced_by="local:ffmpeg")
                job.status = "succeeded" if not job.notes else "partial"
            else:
                job.stage("render", CreationStage.SKIPPED, outcome.get("error", "render failed"))
                job.notes.append(outcome.get("error", ""))
                job.status = "partial"

    # ------------------------------------------------------------------ slides
    async def _slides(self, job: CreationJob, brief: str, *, slides: int = 6, title: str = "",
                      **_: Any) -> None:
        outline: list[str] = []
        produced_by = "template:outline"
        if self.brain is not None:
            try:
                from ..brain import ChatMessage, CompletionRequest

                response = await self.brain.complete(CompletionRequest(
                    messages=[ChatMessage.system("Reply with a JSON array of slide titles and bullets."),
                              ChatMessage.user(f"Create {slides} slides for: {brief}")],
                    task="write", actor="owner", trace_id=job.id))
                import re

                match = re.search(r"\[.*\]", getattr(response, "text", "") or "", flags=re.DOTALL)
                if match:
                    parsed = json.loads(match.group(0))
                    outline = [json.dumps(item, default=str) if not isinstance(item, str) else item
                               for item in parsed][:slides]
                    outline_model = getattr(response, "model", "") or getattr(response, "provider", "")
                    produced_by = f"model:{outline_model}" if outline_model else "model"
            except Exception as exc:
                job.stage("outline", CreationStage.FAILED, f"{type(exc).__name__}: {exc}")
        if not outline:
            outline = [f"Slide {index + 1}: {brief}" for index in range(max(1, slides))]
            job.stage("outline", CreationStage.SKIPPED, "no model - structural outline written")
        target = self.artifacts / "documents" / f"{_slug(title or brief)}-slides.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        body = [f"# {title or brief[:60]}", ""]
        for index, item in enumerate(outline, start=1):
            body += [f"## Slide {index}", "", f"{item}", ""]
        target.write_text("\n".join(body), encoding="utf-8")
        job.stage("write", CreationStage.RAN, str(target))
        job.artifact(str(target), kind="slides", note="markdown deck", bytes_=target.stat().st_size,
                     produced_by=produced_by)
        job.status = "succeeded"

    # ------------------------------------------------------------------ read
    def get(self, job_id: str) -> CreationJob:
        with self._lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise NatashaError(f"no creation job {job_id!r}")
        return job

    def history(self, *, limit: int = 50, kind: str = "") -> list[dict[str, Any]]:
        with self._lock:
            jobs = [job for job in self.jobs.values() if not kind or job.kind == kind]
        jobs.sort(key=lambda job: job.started_at, reverse=True)
        return [job.to_dict() for job in jobs[:limit]]


def _parse_size(size: str) -> tuple[int, int]:
    try:
        width, _, height = str(size).lower().partition("x")
        return max(64, min(4096, int(width))), max(64, min(4096, int(height)))
    except Exception:
        return 1024, 1024


def _slug(text: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", (text or "untitled").lower()).strip("-")
    return slug[:60] or "untitled"


_ENGINE: CreationEngine | None = None
_LOCK = threading.Lock()


def get_creation_engine(**kwargs: Any) -> CreationEngine:
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = CreationEngine(**kwargs)
        return _ENGINE


def reset_creation_engine() -> None:
    global _ENGINE
    with _LOCK:
        _ENGINE = None
