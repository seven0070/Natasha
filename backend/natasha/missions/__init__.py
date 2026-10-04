"""Missions: durable, resumable goals with verification gates.

A mission is a promise Natasha made to the owner. It survives restarts, records every step and its
evidence, stops for approval when it must, repairs bounded failures, and cannot report success until
its verification plan passes. Nothing here trusts the model's own opinion of its progress.
"""

from .models import Mission, MissionState, MissionStep, Priority, StepKind, StepState
from .store import MissionStore, get_mission_store, reset_mission_store
from .checks import DEFAULT_CHECKS, register_default_checks
from .engine import MissionEngine, MissionResult, get_mission_engine, reset_mission_engine

__all__ = [
    "DEFAULT_CHECKS", "register_default_checks",
    "Mission", "MissionState", "MissionStep", "Priority", "StepState", "StepKind",
    "MissionStore", "get_mission_store", "reset_mission_store",
    "MissionEngine", "MissionResult", "get_mission_engine", "reset_mission_engine",
]
