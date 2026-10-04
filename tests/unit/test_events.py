"""The append-only event log: sealing, verification, tamper detection, sanitisation."""

from __future__ import annotations

import json
import sqlite3

import pytest

from natasha.core.risk import RiskLevel


def test_events_append_and_query(log):
    from natasha.events import EventKind

    first = log.append(EventKind.MESSAGE, {"action": "hello"}, actor="owner", source="test")
    log.append(EventKind.TOOL_REQUEST, {"tool": "fs_read"}, actor="model:main", mission_id="msn_1")
    assert first.seq >= 1
    assert log.count() >= 2
    assert log.query(kinds=["tool_request"])[0].payload["tool"] == "fs_read"
    assert len(log.query(mission_id="msn_1")) == 1
    assert log.query(search="fs_read")


def test_chain_verifies_when_untouched(log):
    from natasha.events import EventKind

    for index in range(5):
        log.append(EventKind.SYSTEM, {"index": index})
    ok, detail = log.verify_chain()
    assert ok and detail["checked"] >= 5 and detail["head_hash"]


def test_sql_triggers_reject_update_and_delete(log):
    """First line of defence: the database itself refuses to rewrite history."""
    from natasha.events import EventKind

    log.append(EventKind.SECURITY, {"action": "immutable"})
    connection = sqlite3.connect(str(log.path))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE events SET payload = '{}' WHERE seq = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM events WHERE seq = 1")
    connection.close()


def test_tampering_with_a_payload_is_detected(home):
    """Second line of defence: an attacker who drops the trigger still breaks the hash chain."""
    from natasha.events import EventKind, get_event_log

    log = get_event_log()
    for index in range(4):
        log.append(EventKind.SECURITY, {"action": f"event-{index}"})
    log.close()

    connection = sqlite3.connect(str(home / "db" / "events.db"))
    connection.execute("DROP TRIGGER events_append_only_update")
    connection.execute("UPDATE events SET payload = ? WHERE seq = 2", (json.dumps({"action": "tampered"}),))
    connection.commit()
    connection.close()

    from natasha.events.log import EventLog

    reopened = EventLog(home / "db" / "events.db")
    ok, detail = reopened.verify_chain()
    assert not ok
    assert detail.get("broken_at") == 2
    assert "hash mismatch" in detail["reason"]
    reopened.close()


def test_deleting_an_event_is_detected(home):
    from natasha.events import EventKind, get_event_log

    log = get_event_log()
    for index in range(4):
        log.append(EventKind.SYSTEM, {"index": index})
    log.close()

    connection = sqlite3.connect(str(home / "db" / "events.db"))
    connection.execute("DROP TRIGGER events_append_only_delete")
    connection.execute("DELETE FROM events WHERE seq = 3")
    connection.commit()
    connection.close()

    from natasha.events.log import EventLog

    reopened = EventLog(home / "db" / "events.db")
    ok, detail = reopened.verify_chain()
    assert not ok
    # The gap is noticed at the *next* event, whose prev_hash no longer matches the chain.
    assert detail.get("broken_at") == 4
    assert "prev_hash mismatch" in detail["reason"]
    reopened.close()


def test_secrets_never_reach_the_log(log):
    """The sanitiser must redact obvious secret shapes before they are stored."""
    from natasha.events import EventKind

    secret = "sk-" + "a" * 40
    event = log.append(EventKind.TOOL_REQUEST, {"header": f"Authorization: Bearer {secret}",
                                                "api_key": secret}, actor="model:main")
    rendered = json.dumps(event.to_dict())
    assert secret not in rendered
    assert "redact" in rendered.lower()


def test_export_is_jsonl_and_verifiable(home, log):
    from natasha.events import EventKind

    log.append(EventKind.SYSTEM, {"action": "export-me"})
    target = home / "reports" / "events.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    log.export_jsonl(target, verify=True)
    lines = [json.loads(line) for line in target.read_text().splitlines() if line.strip()]
    assert lines and any(line["payload"].get("action") == "export-me" for line in lines)


def test_risk_filtering_works(log):
    from natasha.events import EventKind

    log.append(EventKind.SECURITY, {"action": "low"}, risk=RiskLevel.LOW)
    log.append(EventKind.SECURITY, {"action": "critical"}, risk=RiskLevel.CRITICAL)
    critical = log.query(min_risk=RiskLevel.CRITICAL)
    assert all(event.risk is RiskLevel.CRITICAL for event in critical)
    assert any(event.payload["action"] == "critical" for event in critical)
