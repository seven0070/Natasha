"""The skills REST surface: installing and listing through the API, not through the classes.

The unit of work here is the endpoint, not the lifecycle: a regression in the response wiring (a
stale attribute, an audit call after the response is built, a missing owner check) only shows up when
a real HTTP request goes through the app. Installing a skill is one of the few write endpoints that
touches the filesystem and the database at once, so it is worth driving end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from natasha.events import EventKind

pytestmark = pytest.mark.integration

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_skill"
#: Read from the fixture instead of hard-coding it, so renaming the skill cannot silently pass.
SKILL_ID = json.loads((SAMPLE / "skill.json").read_text(encoding="utf-8"))["id"]


def test_installing_a_skill_through_the_api_returns_the_record(owner):
    response = owner.post("/api/skills/install", params={"path": str(SAMPLE), "activate": True})
    assert response.status_code in (200, 201), response.text
    record = response.json()
    assert record["id"] == SKILL_ID
    assert record["state"] in {"ACTIVE", "SkillState.ACTIVE", "active"}
    assert record["path"]
    assert record["checksum"], "the install must record the package checksum"


def test_the_installed_skill_is_listed_and_readable(owner):
    owner.post("/api/skills/install", params={"path": str(SAMPLE), "activate": True})
    listing = owner.get("/api/skills")
    assert listing.status_code == 200
    payload = listing.json()
    skills = payload.get("skills", payload) if isinstance(payload, dict) else payload
    assert any(item["id"] == SKILL_ID for item in skills), skills

    detail = owner.get(f"/api/skills/{SKILL_ID}")
    assert detail.status_code == 200
    assert detail.json()["id"] == SKILL_ID


def test_the_install_is_audited_through_the_api(owner):
    """The endpoint must leave an audit event with the skill id, not fail after installing.

    The audit call runs after the install, so a stale attribute there would return a 500 *and* leave
    a half-visible install: exactly the kind of bug a class-level test cannot see.
    """
    response = owner.post("/api/skills/install", params={"path": str(SAMPLE), "activate": True})
    assert response.status_code in (200, 201), response.text
    events = owner.app.state.runtime.log.query(kinds=[EventKind.SKILL], limit=100)
    joined = json.dumps([event.payload for event in events])
    assert SKILL_ID in joined, joined


def test_installing_needs_the_owner(client):
    """No token: the endpoint must refuse before touching the filesystem."""
    response = client.post("/api/skills/install", params={"path": str(SAMPLE)})
    assert response.status_code in (401, 403), response.text


def test_installing_a_skill_that_does_not_exist_fails_honestly(owner):
    response = owner.post("/api/skills/install", params={"path": "/nope/not-a-skill"})
    assert response.status_code >= 400
    assert "not-a-skill" in response.text or "skill" in response.text.lower()
