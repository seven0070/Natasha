"""Universal credential system.

* Secrets are encrypted at rest (AES-256-GCM with HKDF-derived subkeys).
* The master key never lives in the database or in configuration.
* The model never sees the vault: tools receive short-lived, scoped handles from the broker.
* Every use, rotation and revocation is an audit event.
"""

from .broker import CredentialBroker, CredentialHandle, get_broker
from .crypto import SecretBox, get_master_key, rotate_master_key
from .manager import CredentialKind, UniversalCredentialManager, get_credential_manager
from .oauth import OAuth2Client, OAuthError, PKCEChallenge, TokenSet
from .vault import CredentialMetadata, CredentialVault

__all__ = [
    "CredentialBroker", "CredentialHandle", "get_broker", "SecretBox", "get_master_key", "rotate_master_key",
    "CredentialKind", "UniversalCredentialManager", "get_credential_manager", "OAuth2Client", "OAuthError",
    "PKCEChallenge", "TokenSet", "CredentialMetadata", "CredentialVault",
]
