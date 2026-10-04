"""Wake word detection over transcripts.

Local, deterministic and auditable: transcripts (from the hearing engine) are matched against the
configured wake phrases. No always-on cloud audio, no hidden listening - the microphone is opened only
by an explicit listen call, and every detection is logged.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

from ..core.clock import iso
from ..events import EventKind, get_event_log

DEFAULT_WAKE_PHRASES = ("hey natasha", "ok natasha", "natasha")


@dataclass
class WakeWordHit:
    phrase: str
    text: str
    remainder: str
    confidence: float
    at: str = field(default_factory=iso)

    def to_dict(self) -> dict[str, Any]:
        return {"phrase": self.phrase, "text": self.text, "remainder": self.remainder,
                "confidence": self.confidence, "at": self.at}


class WakeWordDetector:
    """Matches wake phrases and returns the command that followed them."""

    def __init__(self, *, phrases: tuple[str, ...] = DEFAULT_WAKE_PHRASES, fuzzy: int = 1,
                 log: Any = None) -> None:
        self.phrases = tuple(phrase.lower() for phrase in phrases)
        self.fuzzy = max(0, fuzzy)
        self.log = log or get_event_log()
        self.hits: list[WakeWordHit] = []
        self._lock = threading.RLock()

    def detect(self, transcript: str) -> WakeWordHit | None:
        text = (transcript or "").strip()
        if not text:
            return None
        lowered = re.sub(r"[^\w\s']", " ", text.lower())
        lowered = re.sub(r"\s+", " ", lowered).strip()
        for phrase in self.phrases:
            index = self._find(lowered, phrase)
            if index < 0:
                continue
            # Approximate confidence: exact match scores higher than a fuzzy one.
            confidence = 1.0 if phrase in lowered else 0.7
            remainder = text[index + len(phrase):].strip(" ,.!?-") if index >= 0 else ""
            hit = WakeWordHit(phrase=phrase, text=text, remainder=remainder, confidence=confidence)
            with self._lock:
                self.hits.append(hit)
                self.hits = self.hits[-200:]
            self.log.append(EventKind.PERCEPTION, {"action": "wake_word", **hit.to_dict()},
                            actor="owner", source="voice.wakeword")
            return hit
        return None

    def _find(self, haystack: str, needle: str) -> int:
        index = haystack.find(needle)
        if index >= 0 or self.fuzzy == 0:
            return index
        # Allow small transcription errors ("hey natash", "hey natasa").
        words = haystack.split()
        target = needle.split()
        for start in range(len(words) - len(target) + 1):
            window = words[start:start + len(target)]
            distance = sum(_edit(want, got) for want, got in zip(target, window))
            if distance <= self.fuzzy:
                return haystack.find(window[0])
        return -1

    def history(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [hit.to_dict() for hit in self.hits[-limit:]]


def _edit(left: str, right: str) -> int:
    if left == right:
        return 0
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, start=1):
        current = [index]
        for position, other in enumerate(right, start=1):
            current.append(min(previous[position] + 1, current[position - 1] + 1,
                               previous[position - 1] + (char != other)))
        previous = current
    return previous[-1]


_DETECTOR: WakeWordDetector | None = None
_LOCK = threading.Lock()


def get_wakeword_detector(**kwargs: Any) -> WakeWordDetector:
    global _DETECTOR
    with _LOCK:
        if _DETECTOR is None:
            _DETECTOR = WakeWordDetector(**kwargs)
        return _DETECTOR


def reset_wakeword_detector() -> None:
    """Drop the cached engine. The runtime builds its own; this exists for tests and reloads."""
    global _DETECTOR
    with _LOCK:
        _DETECTOR = None
