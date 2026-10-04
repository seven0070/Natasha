"""The world model: entities, relationships and beliefs with explicit confidence.

The world model is what Natasha *believes* about the environment - people, organisations, devices,
projects, services, places, events. Unlike memory (which stores what was said), the world model
stores typed structure that reasoning can traverse. Every belief carries provenance and confidence,
contradictions are surfaced rather than silently overwritten, and merging is auditable.
"""

from .models import Belief, Entity, EntityKind, Predicate, Relation, RelationKind
from .store import WorldModel, get_world_model, reset_world_model

__all__ = ["Belief", "Entity", "EntityKind", "Predicate", "Relation", "RelationKind",
           "WorldModel", "get_world_model", "reset_world_model"]
