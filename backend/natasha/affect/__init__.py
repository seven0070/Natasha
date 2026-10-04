"""Affective state: mood, arousal and how they change what Natasha does.

The affect engine never overrides policy, never changes permissions and never fakes agreement. It
is an *input* to behaviour: tone, verbosity, caution and energy. Its events are auditable.
"""

from .state import AffectEvent, AffectState, Mood
from .engine import AffectEngine, get_affect_engine, reset_affect_engine

__all__ = ["AffectEvent", "AffectState", "Mood", "AffectEngine", "get_affect_engine", "reset_affect_engine"]
