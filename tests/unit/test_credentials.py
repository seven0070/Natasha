"""Credentials: encrypted at rest, brokered, scoped, revocable, never logged.

The secret never leaves the vault except through the broker, non-owner actors cannot pull plaintext
out of it, and nothing - not SQLite rows, not the audit log, not the metadata API - leaks the value.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3

import pytest

SECRET = "sk-super-secret-value-1234567890"
REF = "credential://openai"


@pytest.fixture()
def manager(home, log, policy):
    from natasha.credentials import UniversalCredentialManager

    return UniversalCredentialManager(policy=policy, log=log)


@pytest.fixture()
def broker(manager, policy, log):
    from natasha.credentials.broker import CredentialBroker

    return CredentialBroker(manager, policy=policy, log=log)


def test_store_and_read_back(manager):
    metadata = manager.store(REF, SECRET, kind="api_key", provider="openai", actor="owner")
    assert metadata.ref == REF
    assert metadata.fingerprint
    assert SECRET not in json.dumps(metadata.to_dict(), default=str)
    assert manager.resolve(REF, actor="owner")["value"] == SECRET
    assert manager.exists(REF) is True


def test_secret_is_encrypted_on_disk(home, manager):
    manager.store(REF, SECRET, kind="api_key", provider="openai", actor="owner")
    vault_file = home / "db" / "vault.db"
    assert SECRET.encode() not in vault_file.read_bytes()
    connection = sqlite3.connect(str(vault_file))
    rows = connection.execute("SELECT * FROM credentials").fetchall()
    assert rows and rows[0][0] == REF
    assert SECRET not in json.dumps([tuple(row) for row in rows], default=str)
    connection.close()


def test_the_vault_file_is_itself_a_denied_path(manager, policy):
    """Even with a policy ALLOW-shaped request, the vault file is unreadable through file tools."""
    from natasha.security.policy import Capability, Effect, PolicyRequest

    decision = policy.check(PolicyRequest(capability=Capability.FS_READ,
                                          resource=str(manager.vault.path), actor="owner"))
    assert decision.effect is Effect.DENY


def test_a_worker_cannot_administer_credentials(manager):
    with pytest.raises(Exception):
        manager.store(REF, SECRET, kind="api_key", actor="worker:researcher")
    with pytest.raises(Exception):
        manager.revoke(REF, actor="worker:researcher")


def test_a_model_actor_cannot_read_the_raw_secret_without_approval(manager):
    manager.store(REF, SECRET, kind="api_key", provider="openai", actor="owner")
    with pytest.raises(Exception):
        manager.resolve(REF, actor="model:main")


def test_broker_issues_short_lived_audience_scoped_handles(broker, manager):
    manager.store(REF, SECRET, kind="api_key", provider="openai", actor="owner")
    handle = broker.issue(REF, purpose="chat", actor="owner")
    assert handle.grant.expires_at
    assert handle.grant.scope == "chat"
    assert handle.headers()["Authorization"].endswith(SECRET)
    assert SECRET not in json.dumps(handle.quote(), default=str)


def test_a_handle_stops_working_once_its_context_exits(broker, manager):
    manager.store(REF, SECRET, kind="api_key", provider="openai", actor="owner")
    with broker.issue(REF, purpose="chat", actor="owner") as handle:
        assert handle.value == SECRET
    with pytest.raises(Exception):
        _ = handle.value


def test_broker_refuses_unknown_and_revoked_credentials(broker, manager):
    assert manager.exists("credential://does-not-exist") is False
    manager.store("credential://groq", SECRET, kind="api_key", provider="groq", actor="owner")
    manager.revoke("credential://groq", actor="owner")
    with pytest.raises(Exception):
        broker.issue("credential://groq", purpose="chat", actor="owner")


def test_rotation_changes_the_secret_and_bumps_the_version(manager):
    manager.store(REF, SECRET, kind="api_key", provider="openai", actor="owner")
    new_secret = "sk-rotated-0987654321fedcba"
    metadata = manager.rotate(REF, new_secret, actor="owner")
    assert manager.resolve(REF, actor="owner")["value"] == new_secret
    assert metadata.version >= 2

    # The previous value survives only as ciphertext in the version history.
    vault_file = manager.vault.path
    connection = sqlite3.connect(str(vault_file))
    history = connection.execute("SELECT * FROM credential_versions").fetchall()
    connection.close()
    rendered = json.dumps([tuple(row) for row in history], default=str)
    assert SECRET not in rendered and new_secret not in rendered


def test_secrets_are_absent_from_the_event_log(manager, log):
    manager.store(REF, SECRET, kind="api_key", provider="openai", actor="owner")
    manager.resolve(REF, actor="owner")
    manager.rotate(REF, "sk-rotated-abcdef123456", actor="owner")
    rendered = json.dumps([event.to_dict() for event in log.query(limit=300)], default=str)
    assert SECRET not in rendered


def test_metadata_and_stats_never_include_secrets(manager):
    manager.store(REF, SECRET, kind="api_key", provider="openai", actor="owner")
    payload = json.dumps(manager.stats(), default=str) + json.dumps(manager.list(), default=str)
    assert SECRET not in payload


def test_headers_for_produces_provider_headers(manager):
    manager.store(REF, SECRET, kind="api_key", provider="openai", actor="owner")
    headers = manager.headers_for(REF, actor="owner", purpose="chat")
    assert headers["Authorization"] == f"Bearer {SECRET}"


def test_admin_actions_are_audited(manager, log):
    from natasha.events import EventKind

    manager.store(REF, SECRET, kind="api_key", provider="openai", actor="owner")
    manager.revoke(REF, actor="owner")
    events = log.query(kinds=[EventKind.CREDENTIAL], limit=50)
    actions = {event.payload.get("action") for event in events}
    assert {"stored", "revoked"} <= actions


def test_pkce_challenge_is_a_real_derivation():
    from natasha.credentials.oauth import PKCEChallenge

    challenge = PKCEChallenge.generate()
    assert challenge.method == "S256"
    assert challenge.verifier != challenge.challenge
    digest = hashlib.sha256(challenge.verifier.encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge.challenge


def test_storing_a_structured_secret_keeps_its_fields(manager):
    manager.store(REF, {"value": SECRET, "organization": "org-123"}, kind="api_key",
                  provider="openai", actor="owner")
    stored = manager.resolve(REF, actor="owner")
    assert stored["value"] == SECRET
    assert stored["organization"] == "org-123"


def test_unknown_kinds_are_refused(manager):
    with pytest.raises(Exception):
        manager.store(REF, SECRET, kind="not-a-real-kind", actor="owner")
