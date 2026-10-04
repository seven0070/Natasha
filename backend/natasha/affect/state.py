"""Affect data model."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ..core.clock import iso, parse_iso


class Mood(str, Enum):
    """Coarse mood labels derived from valence/arousal - never a hidden agenda."""

    CALM = "calm"
    FOCUSED = "focused"
    CURIOUS = "curious"
    CONCERNED = "concerned"
    FRUSTRATED = "frustrated"
    PLEASED = "pleased"
    TIRED = "tired"
    URGENT = "urgent"


#: How valence/arousal map onto a mood label.
_MOOD_RULES = (
    (lambda v, a: a > 0.6 and v < 0.0, Mood.URGENT),
    (lambda v, a: v < -0.4 and a > 0.4, Mood.FRUSTRATED),
    (lambda v, a: v < -0.2, Mood.CONCERNED),
    (lambda v, a: v > 0.4 and a < 0.4, Mood.PLEASED),
    (lambda v, a: a > 0.5, Mood.FOCUSED),
    (lambda v, a: v > 0.2, Mood.CURIOUS),
    (lambda v, a: a < 0.15, Mood.TIRED),
)


def mood_for(valence: float, arousal: float) -> Mood:
    for predicate, mood in _MOOD_RULES:
        if predicate(valence, arousal):
            return mood
    return Mood.CALM


@dataclass
class AffectEvent:
    """Something that happened and moved the state."""

    kind: str
    detail: str = ""
    valence_impact: float = 0.0
    arousal_impact: float = 0.0
    mission_id: str = ""
    at: str = field(default_factory=iso)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "valence_impact": self.valence_impact,
                "arousal_impact": self.arousal_impact, "mission_id": self.mission_id, "at": self.at}


@dataclass
class AffectState:
    """Valence (pleasant/unpleasant), arousal (calm/alert), confidence and energy."""

    valence: float = 0.1
    arousal: float = 0.3
    confidence: float = 0.6
    energy: float = 0.8
    mood: Mood = Mood.CALM
    updated_at: str = field(default_factory=iso)
    events: list[AffectEvent] = field(default_factory=list)

    def recompute(self) -> None:
        self.mood = mood_for(self.valence, self.arousal)

    def decay(self, *, half_life_seconds: float = 1800.0, now: str | None = None) -> None:
        """Return gradually to baseline - affect is a state, not an accumulating grudge."""
        moment = parse_iso(now) if now else datetime.now(timezone.utc)
        last = parse_iso(self.updated_at)
        elapsed = max(0.0, (moment - last).total_seconds())
        if elapsed <= 0:
            return
        factor = math.pow(0.5, elapsed / half_life_seconds)
        self.valence = self.valence * factor
        self.arousal = 0.3 + (self.arousal - 0.3) * factor
        self.energy = min(1.0, self.energy + (0.8 - self.energy) * (1 - factor) * 0.5)
        self.updated_at = iso(moment)
        self.recompute()

    def to_dict(self) -> dict[str, Any]:
        return {"valence": round(self.valence, 4), "arousal": round(self.arousal, 4),
                "confidence": round(self.confidence, 4), "energy": round(self.energy, 4),
                "mood": self.mood.value, "updated_at": self.updated_at,
                "recent_events": [event.to_dict() for event in self.events[-10:]]}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AffectState":
        state = cls(valence=float(payload.get("valence", 0.1)),
                    arousal=float(payload.get("arousal", 0.3)),
                    confidence=float(payload.get("confidence", 0.6)),
                    energy=float(payload.get("energy", 0.8)),
                    updated_at=payload.get("updated_at") or iso())
        state.mood = Mood(payload.get("mood", "calm")) if payload.get("mood") else mood_for(state.valence, state.arousal)
        return state
