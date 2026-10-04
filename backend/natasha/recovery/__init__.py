"""Failure recovery: bounded repair that never pretends, never loops and never replays the dangerous.

Rules enforced here, not by the model:
* a repair is always bounded by an attempt budget and a deadline;
* irreversible actions are never replayed - they are compensated or escalated;
* a repair that fails is reported as a failure, with the original error attached;
* every attempt is audited so the owner can see exactly what Natasha retried.
"""

from .classifier import FailureClass, FailureRecord, classify
from .recovery import (
    RecoveryEngine,
    RecoveryOutcome,
    RecoveryPlan,
    RepairAction,
    get_recovery_engine,
    reset_recovery_engine,
)

__all__ = [
    "FailureClass", "FailureRecord", "classify", "RecoveryEngine", "RecoveryOutcome", "RecoveryPlan",
    "RepairAction", "get_recovery_engine", "reset_recovery_engine",
]
