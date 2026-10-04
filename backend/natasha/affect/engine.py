"""The affect engine: bounded, auditable mood that modulates style but never authority."""

from __future__ import annotations

import json
import threading
from typing import Any

from ..core import get_paths
from ..events import EventKind, get_event_log
from .state import AffectEvent, AffectState, Mood

#: Named events and how they move the state. Values are small and bounded on purpose.
EVENT_EFFECTS: dict[str, tuple[float, float]] = {
    "turn_completed": (0.06, -0.05),
    "mission_succeeded": (0.20, 0.05),
    "mission_failed": (-0.18, 0.15),
    "step_failed": (-0.08, 0.10),
    "tool_failed": (-0.06, 0.08),
    "tool_succeeded": (0.04, 0.0),
    "verification_failed": (-0.14, 0.12),
    "approval_denied": (-0.10, 0.05),
    "approval_granted": (0.08, 0.0),
    "security_alert": (-0.25, 0.30),
    "injection_attempt": (-0.15, 0.20),
    "owner_praise": (0.25, 0.05),
    "owner_correction": (-0.12, 0.10),
    "idle": (0.0, -0.05),
}
#: Ceilings: affect can bias behaviour, never disable caution or invent confidence.
MAX_CONFIDENCE = 0.95
MIN_CONFIDENCE = 0.15


class AffectEngine:
    """Stateful affective model with persistence and audit."""

    def __init__(self, *, log: Any = None, state: AffectState | None = None) -> None:
        self.log = log or get_event_log()
        self._state = state or self._load() or AffectState()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ persistence
    def _path(self) -> Any:
        return get_paths().db_path("affect")

    def _load(self) -> AffectState | None:
        import sqlite3

        path = self._path()
        if not path or not path.exists():
            return None
        try:
            with sqlite3.connect(str(path)) as connection:
                row = connection.execute("SELECT payload FROM affect_state WHERE id = 1").fetchone()
            if not row:
                return None
            return AffectState.from_dict(json.loads(row[0]))
        except Exception:
            return None

    def _save(self) -> None:
        import sqlite3

        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(path)) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS affect_state (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)")
                connection.execute("INSERT INTO affect_state (id, payload) VALUES (1, ?) "
                                   "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                                   (json.dumps(self._state.to_dict(), default=str),))
                connection.commit()
        except Exception as exc:
            self.log.append(EventKind.FAILURE, {"action": "affect_persist_failed", "error": str(exc)},
                            actor="system", source="affect")

    # ------------------------------------------------------------------ events
    def note_event(self, kind: str, *, detail: str = "", mission_id: str = "") -> dict[str, Any]:
        """Record an event and move the state. Unknown events move nothing."""
        with self._lock:
            self._state.decay()
            valence_delta, arousal_delta = EVENT_EFFECTS.get(kind, (0.0, 0.0))
            self._state.valence = _clamp(self._state.valence + valence_delta, -1.0, 1.0)
            self._state.arousal = _clamp(self._state.arousal + arousal_delta, 0.0, 1.0)
            if valence_delta or arousal_delta:
                self._state.confidence = _clamp(self._state.confidence + valence_delta * 0.4,
                                                MIN_CONFIDENCE, MAX_CONFIDENCE)
            event = AffectEvent(kind=kind, detail=detail[:200], valence_impact=valence_delta,
                                arousal_impact=arousal_delta, mission_id=mission_id)
            self._state.events.append(event)
            self._state.events = self._state.events[-200:]
            self._state.recompute()
            self._save()
        self.log.append(EventKind.SYSTEM, {"action": "affect_event", "kind": kind, "mood": self._state.mood.value,
                                           "valence": round(self._state.valence, 3),
                                           "arousal": round(self._state.arousal, 3)},
                        actor="system", source="affect", mission_id=mission_id)
        return self._state.to_dict()

    def set_baseline(self, *, valence: float | None = None, arousal: float | None = None) -> dict[str, Any]:
        with self._lock:
            if valence is not None:
                self._state.valence = _clamp(valence, -1.0, 1.0)
            if arousal is not None:
                self._state.arousal = _clamp(arousal, 0.0, 1.0)
            self._state.recompute()
            self._save()
        return self._state.to_dict()

    # ------------------------------------------------------------------ read
    @property
    def state(self) -> AffectState:
        with self._lock:
            self._state.decay()
            return self._state

    def snapshot(self) -> dict[str, Any]:
        return self.state.to_dict()

    def style(self) -> dict[str, Any]:
        """Concrete behavioural adjustments. Bounded, and never about permissions."""
        state = self.state
        verbosity = "concise" if state.energy < 0.4 or state.arousal > 0.7 else "normal"
        if state.mood == Mood.FRUSTRATED:
            tone = "steady"
        elif state.mood == Mood.PLEASED:
            tone = "warm"
        elif state.mood == Mood.URGENT:
            tone = "brisk"
        elif state.mood == Mood.CONCERNED:
            tone = "careful"
        else:
            tone = "neutral"
        return {
            "mood": state.mood.value,
            "tone": tone,
            "verbosity": verbosity,
            "suggest_caution": state.valence < -0.2 or state.arousal > 0.7,
            "offer_help_proactively": state.energy > 0.5 and state.valence > 0.0,
            "confidence_bias": round((state.confidence - 0.6) * 0.2, 4),
            "note": "affect modulates tone only; it never grants permission or raises authority",
        }

    def prompt_fragment(self) -> str:
        style = self.style()
        return ("Current affective state (style guidance only - it does not change your authority or "
                f"permissions): mood={style['mood']}, tone={style['tone']}, verbosity={style['verbosity']}" +
                (", be noticeably careful and double-check facts." if style["suggest_caution"] else "."))

    def events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [event.to_dict() for event in self._state.events[-limit:]]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


_ENGINE: AffectEngine | None = None
_LOCK = threading.Lock()


def get_affect_engine(**kwargs: Any) -> AffectEngine:
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = AffectEngine(**kwargs)
        return _ENGINE


def reset_affect_engine() -> None:
    global _ENGINE
    with _LOCK:
        _ENGINE = None
