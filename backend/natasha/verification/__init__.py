"""Verification: nothing is reported as done until it has been checked.

The verification engine is what keeps Natasha honest. A plan declares how each claim will be checked;
the engine runs the checks, records evidence, and refuses to mark work complete when a check fails.
"Success" is a verdict of this engine - never of the model that produced the work.
"""

from .checks import (
    BehaviorCheck,
    CheckResult,
    CheckStatus,
    CommandCheck,
    CompositeCheck,
    FileExistsCheck,
    ImportCheck,
    OutputSchemaCheck,
    PythonSyntaxCheck,
    VerificationCheck,
    SideEffectCheck,
)
from .engine import VerificationEngine, VerificationReport, get_verification_engine

__all__ = [
    "BehaviorCheck",
    "CheckResult", "CheckStatus", "CommandCheck", "CompositeCheck", "FileExistsCheck", "ImportCheck",
    "OutputSchemaCheck", "PythonSyntaxCheck", "VerificationCheck", "SideEffectCheck",
    "VerificationEngine", "VerificationReport", "get_verification_engine",
]
