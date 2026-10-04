"""The video pipeline: script -> storyboard -> frames -> narration -> render.

Each stage is a separate method so the engine can report exactly which ones ran. Rendering needs
ffmpeg; without it the pipeline produces the storyboard and stops, with the reason recorded.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import NatashaError
from ..core.clock import iso


@dataclass
class StoryboardShot:
    """One shot: what the viewer sees and hears."""

    index: int
    description: str
    narration: str = ""
    seconds: float = 4.0
    image: str = ""
    prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "description": self.description, "narration": self.narration,
                "seconds": self.seconds, "image": self.image, "prompt": self.prompt}


@dataclass
class Storyboard:
    """A shot list that can be rendered, edited or simply read."""

    title: str
    brief: str
    shots: list[StoryboardShot] = field(default_factory=list)
    style: str = ""
    #: Which planner produced the shots: "model", "template" (deterministic), or "" (unknown).
    source: str = ""
    fps: int = 24
    resolution: str = "1280x720"
    created_at: str = field(default_factory=iso)

    @property
    def duration_seconds(self) -> float:
        return round(sum(shot.seconds for shot in self.shots), 2)

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "brief": self.brief, "style": self.style, "fps": self.fps,
                "resolution": self.resolution, "duration_seconds": self.duration_seconds,
                "source": self.source, "shots": [shot.to_dict() for shot in self.shots],
                "created_at": self.created_at}

    def to_markdown(self) -> str:
        lines = [f"# Storyboard: {self.title}", "",
                 f"**Planner:** {self.source or 'unknown'}"
                 + (" (deterministic template - no model was available)"
                    if self.source == "template" else ""),
                 f"**Brief:** {self.brief}", "",
                 f"**Style:** {self.style or 'unspecified'}  |  **Duration:** {self.duration_seconds}s  |  "
                 f"**Resolution:** {self.resolution} @ {self.fps}fps", ""]
        for shot in self.shots:
            lines += [f"## Shot {shot.index + 1} ({shot.seconds:g}s)",
                      f"- Visual: {shot.description}",
                      f"- Narration: {shot.narration or '(none)'}"]
            if shot.prompt:
                lines.append(f"- Image prompt: {shot.prompt}")
            lines.append("")
        return "\n".join(lines)


class VideoPipeline:
    """Builds videos from a storyboard, with per-stage honesty."""

    def __init__(self, *, brain: Any = None, voice: Any = None, artifacts: Path | None = None) -> None:
        self.brain = brain
        self.voice = voice
        self.artifacts = Path(artifacts) if artifacts else Path(tempfile.gettempdir()) / "natasha-video"

    # ------------------------------------------------------------------ capability
    @staticmethod
    def ffmpeg() -> str:
        return shutil.which("ffmpeg") or ""

    def capabilities(self) -> dict[str, Any]:
        ffmpeg = self.ffmpeg()
        return {"ffmpeg": ffmpeg, "can_render": bool(ffmpeg),
                "can_speak": bool(self.voice is not None and getattr(self.voice, "backends", {})),
                "can_plan": self.brain is not None,
                "reason": "" if ffmpeg else "ffmpeg is not installed: apt install ffmpeg"}

    # ------------------------------------------------------------------ stages
    async def storyboard(self, brief: str, *, title: str = "", shots: int = 5,
                         style: str = "", seconds_per_shot: float = 4.0) -> Storyboard:
        """Turn a brief into a shot list. Uses the model when available, a template otherwise."""
        board = Storyboard(title=title or brief[:60] or "Untitled", brief=brief, style=style,
                           shots=[])
        generated = await self._ask_model(brief, shots=shots, style=style)
        board.source = "model" if generated else "template"
        if generated:
            for index, item in enumerate(generated):
                board.shots.append(StoryboardShot(
                    index=index, description=str(item.get("description", "")).strip(),
                    narration=str(item.get("narration", "")).strip(),
                    seconds=float(item.get("seconds", seconds_per_shot) or seconds_per_shot),
                    prompt=str(item.get("prompt") or item.get("description", "")).strip(),
                ))
        else:
            # Deterministic fallback: an honest structural draft, clearly marked.
            beats = ["Establish the setting", "Introduce the problem", "Show the work",
                     "Reveal the result", "Close with the takeaway"]
            for index in range(max(1, shots)):
                beat = beats[index] if index < len(beats) else f"Beat {index + 1}"
                board.shots.append(StoryboardShot(
                    index=index, description=f"{beat}: {brief}", narration="",
                    seconds=seconds_per_shot, prompt=f"{beat} - {brief}"))
        return board

    async def _ask_model(self, brief: str, *, shots: int, style: str) -> list[dict[str, Any]]:
        if self.brain is None:
            return []
        try:
            from ..brain import ChatMessage, CompletionRequest

            prompt = (f"Turn this brief into a {shots}-shot video storyboard.\nBrief: {brief}\n"
                      f"Style: {style or 'unspecified'}\n"
                      "Return ONLY a JSON array; each item has keys description, narration, seconds, prompt.")
            response = await self.brain.complete(CompletionRequest(
                messages=[ChatMessage.system("You are a storyboard artist. Reply with JSON only."),
                          ChatMessage.user(prompt)],
                task="reason", actor="system", trace_id="creation.video"))
            import json
            import re

            text = getattr(response, "text", "") or ""
            match = re.search(r"\[.*\]", text, flags=re.DOTALL)
            if not match:
                return []
            data = json.loads(match.group(0))
            return [item for item in data if isinstance(item, dict)][:shots]
        except Exception:
            return []

    def frames(self, board: Storyboard, images: list[str] | None = None) -> list[str]:
        """Fill each shot's image path (from a provided list) and report which shots lack one."""
        images = list(images or [])
        for index, shot in enumerate(board.shots):
            if index < len(images):
                shot.image = images[index]
        return [shot.image for shot in board.shots if shot.image]

    def narrate(self, board: Storyboard, *, out_path: str = "") -> dict[str, Any]:
        """Render the storyboard's narration to one audio track (or explain why not)."""
        if self.voice is None:
            return {"ok": False, "error": "no voice engine attached", "audio": ""}
        script = " ".join(shot.narration for shot in board.shots if shot.narration).strip()
        if not script:
            return {"ok": False, "error": "the storyboard has no narration to speak", "audio": ""}
        target = out_path or str(self.artifacts / f"{_slug(board.title)}-narration.wav")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        result = self.voice.speak(script, out_path=target)
        return {"ok": result.ok, "error": result.error, "audio": result.audio_path, "backend": result.backend}

    def render(self, board: Storyboard, *, out_path: str = "", audio: str = "",
               fallback_image: str = "") -> dict[str, Any]:
        """Render the storyboard to an MP4. Requires ffmpeg and at least one frame."""
        ffmpeg = self.ffmpeg()
        if not ffmpeg:
            return {"ok": False, "error": "ffmpeg is not installed - the storyboard was produced instead "
                                          "(apt install ffmpeg)", "video": "", "rendered": False}
        images = self.frames(board, [fallback_image] if fallback_image else None)
        images = [shot.image for shot in board.shots if shot.image]
        if not images:
            return {"ok": False, "error": "no frames available to render; generate images first",
                    "video": "", "rendered": False}

        target = Path(out_path) if out_path else self.artifacts / f"{_slug(board.title)}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as workdir:
            concat = Path(workdir) / "frames.txt"
            lines: list[str] = []
            for shot in board.shots:
                if not shot.image:
                    continue
                lines.append(f"file '{Path(shot.image).resolve()}'")
                lines.append(f"duration {shot.seconds:g}")
            if lines:
                lines.append(lines[-2] if lines[-2].startswith("file") else "")
            concat.write_text("\n".join(line for line in lines if line))
            arguments = [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                         "-i", str(concat)]
            if audio and Path(audio).exists():
                arguments += ["-i", audio, "-shortest"]
            arguments += ["-vsync", "vfr", "-pix_fmt", "yuv420p", "-r", str(board.fps), str(target)]
            process = subprocess.run(arguments, capture_output=True, text=True, timeout=900, check=False)
        if process.returncode != 0 or not target.exists():
            return {"ok": False, "error": f"ffmpeg failed: {process.stderr.strip()[:300]}",
                    "video": "", "rendered": False}
        return {"ok": True, "error": "", "video": str(target), "rendered": True,
                "bytes": target.stat().st_size, "duration_seconds": board.duration_seconds}


def _slug(text: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", (text or "untitled").lower()).strip("-")
    return slug[:60] or "untitled"
