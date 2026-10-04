"""Governance: the protected constitutional core and controlled evolution.

These rules are enforced by *code* - the model can read them and can propose changes to them, but
it cannot apply changes to them, cannot disable the audit trail, cannot weaken permissions and
cannot self-approve. Anything that tries is refused and recorded as a CRITICAL security event.
"""

from .constitution import (
    PROTECTED_AREAS,
    PROTECTED_PATHS,
    Constitution,
    GovernanceReport,
    Invariant,
    get_constitution,
)
from .identity import Identity, IdentityStore, get_identity_store
from .rollback import Snapshot, SnapshotManager, get_snapshot_manager
from .upgrade_governor import UpgradeGovernor, UpgradeProposal, get_upgrade_governor

__all__ = [
    "PROTECTED_AREAS", "PROTECTED_PATHS", "Constitution", "GovernanceReport", "Invariant",
    "get_constitution", "Identity", "IdentityStore", "get_identity_store", "Snapshot",
    "SnapshotManager", "get_snapshot_manager", "UpgradeGovernor", "UpgradeProposal", "get_upgrade_governor",
]
