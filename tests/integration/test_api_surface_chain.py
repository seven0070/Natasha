"""The API surfaces added for the console: vision, voice, providers, artifacts, integrations.

These are the endpoints the web UI actually calls. Each one is exercised against the real runtime -
no mocks - so a shape change or a missing attribute fails here rather than in the browser.
"""

from __future__ import annotations

import base64

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _owner_token(owner):
    """Every endpoint in this file is owner-authorised, exactly as the console is."""
    return owner


@pytest.fixture()
def owner_client(owner):
    """An authenticated client (the owner fixture logs in and carries the token)."""
    return owner

PNG_1x1 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00"
    b"\x00IEND\xaeB`\x82").decode()


# ---------------------------------------------------------------- vision
def test_vision_capabilities_reports_what_exists(owner_client):
    payload = owner_client.get("/api/vision/capabilities").json()
    assert "available" in payload
    assert "image/png" in payload["mime_types"]
    assert isinstance(payload["models"], list)
    assert "accessibility" in payload and "document_reader" in payload
    # Screenshots are taken by the computer controller, so the two views must agree. This used to
    # be hardcoded True, which advertised screenshots on machines with no desktop backend at all.
    computer = owner_client.get("/api/computer/capabilities").json()
    assert payload["screenshot"] == bool(computer["computer"]["actions"]["screenshot"]), (
        "vision's screenshot support must match the controller that actually captures screens")


def test_ocr_says_so_when_there_is_no_engine(client):
    """No OCR install and no vision model: an honest failure, never a guess."""
    payload = client.post("/api/vision/ocr",
                          json={"data_url": f"data:image/png;base64,{PNG_1x1}"}).json()
    assert payload["ok"] is False
    assert payload["text"] == ""
    assert "pytesseract" in payload["error"] or "no text extracted" in payload["error"]


def test_analysis_is_either_a_real_answer_or_an_explicit_failure(client, rt):
    """Whatever the provider situation, the reply carries the truth: text, or the reason it failed."""
    image = rt.paths.uploads / "dot.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(base64.b64decode(PNG_1x1))
    payload = client.post("/api/vision/analyse", json={"path": str(image), "prompt": "what is this?"}).json()
    assert payload["width"] == 1 and payload["height"] == 1       # the header reader reports the truth
    if payload["ok"]:
        assert payload["text"], "an ok result must carry text"
        assert payload["model"], "an ok result must name the model that produced it"
    else:
        assert payload["error"], "a failed result must explain itself"
        assert payload["text"] == ""
        assert payload["ocr_text"] in ("", None) or "OCR" in payload["text"]


def test_analyse_rejects_a_path_outside_the_allowed_roots(client):
    response = client.post("/api/vision/analyse", json={"path": "/etc/passwd"})
    assert response.status_code in (403, 404)
    assert "outside" in response.json()["detail"] or "not found" in response.json()["detail"].lower()


def test_analyse_needs_a_source(client):
    assert client.post("/api/vision/analyse", json={}).status_code == 422
    assert client.post("/api/vision/ocr", json={}).status_code == 422


def test_document_reader_reads_a_text_file(client, rt):
    document = rt.paths.uploads / "notes.txt"
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text("Meeting notes: ship the release on Friday.")
    payload = client.post("/api/vision/documents", json={"path": str(document)}).json()
    assert payload["ok"] is True
    assert "Friday" in payload["preview"]
    assert payload["trust"] == "external"


def test_accessibility_is_honest_about_the_platform(client):
    payload = client.get("/api/vision/accessibility").json()
    assert set(payload) >= {"ok", "platform", "backend", "nodes", "count"}
    assert isinstance(payload["nodes"], list)
    if not payload["ok"]:
        assert payload["error"]


def test_ui_interpretation_combines_screen_and_tree(client, rt):
    image = rt.paths.uploads / "screen.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(base64.b64decode(PNG_1x1))
    payload = client.post("/api/vision/ui", json={"path": str(image)}).json()
    assert "elements" in payload and isinstance(payload["elements"], list)
    assert payload["trust"] == "external"


# ---------------------------------------------------------------- voice
def test_voice_capabilities_and_loop_are_wired(client):
    capabilities = client.get("/api/voice/capabilities").json()
    assert set(capabilities) >= {"voice", "hearing", "microphone", "wake_words", "pipeline"}
    assert capabilities["pipeline"][0] == "microphone"
    loop = client.get("/api/voice/loop").json()
    assert set(loop) >= {"capabilities", "turns"}
    assert loop["capabilities"]["pipeline"][-1] == "speaker"


def test_a_text_voice_turn_runs_the_real_executive(owner_client):
    client = owner_client
    payload = client.post("/api/voice/turn", json={"text": "hello natasha", "speak": False}).json()
    assert payload["transcript"] == "hello natasha"
    assert payload["stages"]["executive"]["turn_id"]
    # With no model configured the reply must be the honest offline placeholder, not silence.
    assert payload["reply"]
    assert payload["ok"] is True


def test_a_voice_turn_without_speech_is_recorded_as_such(owner_client):
    client = owner_client
    payload = client.post("/api/voice/turn", json={"text": "say something", "speak": True}).json()
    if not payload["audio_path"]:
        assert payload["stages"]["tts"]["ok"] is False
        assert payload["stages"]["tts"]["honest"]
    else:
        assert payload["speech_backend"]


def test_an_empty_voice_turn_fails_with_a_reason(owner_client):
    client = owner_client
    payload = client.post("/api/voice/turn", json={"text": "   ", "speak": False}).json()
    assert payload["ok"] is False
    assert payload["error"] == "nothing was said"


def test_interrupt_reports_whether_something_was_playing(owner_client):
    client = owner_client
    payload = client.post("/api/voice/interrupt", json={}).json()
    assert payload["ok"] is True
    assert isinstance(payload["stopped_playback"], bool)


def test_voice_turns_land_in_the_loop_history(client):
    client.post("/api/voice/turn", json={"text": "remember this turn", "speak": False})
    turns = client.get("/api/voice/loop").json()["turns"]
    assert any(turn["transcript"] == "remember this turn" for turn in turns)


# ---------------------------------------------------------------- providers
def test_provider_surface_lists_the_catalogue_and_the_live_runtime(client, rt):
    payload = client.get("/api/providers").json()
    names = {provider["name"] for provider in payload["providers"]}
    # The catalogue is what the runtime can be pointed at...
    catalogue = set(rt.settings.providers)
    assert {"ollama", "llamacpp", "lmstudio", "vllm", "openai", "anthropic", "echo"} <= catalogue
    # ...while the listing reports the adapters this runtime actually holds.
    assert "scripted" in names
    assert {"prefer_local", "routing_weights", "fallback_depth"} <= set(payload["routing"])
    assert isinstance(payload["models"], list) and payload["models"]


def test_provider_health_check_is_recorded(client):
    payload = client.post("/api/providers/scripted/check", json={}).json()
    assert payload["name"] == "scripted" and payload["provider"] == "scripted"
    assert payload["ok"] is True and payload["healthy"] is True
    listing = client.get("/api/providers").json()
    scripted = next(item for item in listing["providers"] if item["name"] == "scripted")
    assert scripted["health"]["checked_at"]


def test_discovery_of_an_unreachable_local_server_is_honest(client):
    """There is no Ollama in this environment, and the runtime says so instead of inventing models."""
    payload = client.post("/api/providers/ollama/discover", json={}).json()
    assert payload["provider"] == "ollama"
    if not payload["ok"]:
        assert payload["error"]
        assert payload["models"] == []


def test_routing_preferences_persist(client, settings):
    response = client.post("/api/providers/routing",
                           json={"prefer_local": False, "fallback_depth": 2, "stream": False})
    assert response.status_code == 200
    payload = client.get("/api/providers").json()["routing"]
    assert payload["prefer_local"] is False
    assert payload["fallback_depth"] == 2
    assert payload["stream"] is False
    # restore
    client.post("/api/providers/routing", json={"prefer_local": True, "fallback_depth": 3, "stream": True})


def test_enabling_and_disabling_a_provider_round_trips(client, rt):
    """A configurable provider can be switched off and back on; an unknown one is refused."""
    target = next((adapter for adapter in rt.brain.providers.all()
                   if getattr(adapter, "settings", None) is not None), None)
    if target is None:
        # Nothing in this runtime has settings: the route must refuse, never pretend or 500.
        response = client.post("/api/providers/echo/disable", json={})
        assert response.status_code == 409
        return
    disabled = client.post(f"/api/providers/{target.name}/disable", json={})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    assert client.post(f"/api/providers/{target.name}/enable", json={}).json()["enabled"] is True
    assert client.post("/api/providers/not-a-provider/disable", json={}).status_code == 404


# ---------------------------------------------------------------- artifacts
def test_artifact_listing_and_content_hosting(client, runtime):
    runtime, _adapter = runtime
    roots = runtime.paths
    artifact = roots.artifacts / "reports" / "summary.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("# Report\n\nAll checks passed.\n")

    listing = client.get("/api/artifacts").json()
    names = [item["relative"] for item in listing["artifacts"]]
    assert any(name.endswith("summary.md") for name in names)
    entry = next(item for item in listing["artifacts"] if item["relative"].endswith("summary.md"))
    assert entry["mime"] == "text/markdown"
    assert entry["sha256"]

    content = client.get("/api/artifacts/content", params={"path": entry["path"]}).json()
    assert content["previewable"] is True
    assert "All checks passed" in content["text"]

    download = client.get("/api/artifacts/download", params={"path": entry["path"]})
    assert download.status_code == 200
    assert b"All checks passed" in download.content


def test_artifact_content_refuses_paths_outside_the_artifacts_root(client):
    response = client.get("/api/artifacts/content", params={"path": "/etc/passwd"})
    assert response.status_code in (403, 404)


def test_artifact_traversal_is_refused(client, runtime):
    runtime, _adapter = runtime
    escape = str(runtime.paths.artifacts / ".." / ".." / "etc" / "passwd")
    assert client.get("/api/artifacts/content", params={"path": escape}).status_code in (403, 404)


def test_missing_artifact_is_a_404(client, runtime):
    runtime, _adapter = runtime
    assert client.get("/api/artifacts/content",
                      params={"path": str(runtime.paths.artifacts / "nope.txt")}).status_code == 404


# ---------------------------------------------------------------- memory correction
def test_memory_correction_supersedes_instead_of_overwriting(client, rt):
    created = client.post("/api/memory", json={
        "kind": "preference", "content": "The owner prefers tabs.", "summary": "indentation",
        "importance": 0.6, "source": "owner"}).json()
    memory_id = (created.get("memory") or created)["id"]
    payload = client.post("/api/memory/correct", json={
        "memory_id": memory_id, "content": "The owner prefers spaces.", "reason": "typo"}).json()
    assert payload["superseded"] == memory_id
    replacement = payload["replacement"]
    replacement_id = replacement["id"] if isinstance(replacement, dict) else replacement
    assert replacement_id and replacement_id != memory_id
    assert replacement["superseded_by"] == "" and replacement.get("content") == \
        "The owner prefers spaces."
    recalled = client.get("/api/memory/recall", params={"query": "indentation preference"}).json()
    hits = {hit["memory"]["id"]: hit["memory"] for hit in recalled["hits"]}
    assert replacement_id in hits
    assert hits[replacement_id]["content"] == "The owner prefers spaces."
    assert memory_id not in hits          # superseded, so retrieval stops returning it
    # The lineage is recorded on both sides: the new record names what it corrects, the old one
    # names its replacement - and the old record still exists for audit.
    assert hits[replacement_id]["metadata"]["corrects"] == memory_id
    old = rt.memory.get(memory_id, touch=False)
    assert old.superseded_by == replacement_id


def test_correcting_an_unknown_memory_is_404(client):
    response = client.post("/api/memory/correct",
                           json={"memory_id": "mem_does_not_exist", "content": "x"})
    assert response.status_code in (404, 422)


# ---------------------------------------------------------------- integrations
def test_connect_requires_a_credential_reference(client):
    connector = client.get("/api/integrations").json()["connectors"][0]["name"]
    response = client.post(f"/api/integrations/{connector}/connect",
                           json={"credential_ref": "not-a-ref"})
    assert response.status_code == 409
    assert "credential://" in response.json()["detail"]


def test_connect_and_disconnect_persist_references_only(client, rt):
    connector = client.get("/api/integrations").json()["connectors"][0]["name"]
    credential_name = f"{connector}-console"
    client.post("/api/security/credentials", json={
        "name": credential_name, "kind": "api_key", "secret": "super-secret-value", "metadata": {}})
    created = client.post(f"/api/integrations/{connector}/connect", json={
        "credential_ref": f"credential://{credential_name}",
        "settings": {"base_url": "https://example.invalid", "api_token": "should-not-be-stored"},
        "enabled": True})
    assert created.status_code == 200, created.text
    assert created.json()["connected"] is True
    stored = (rt.paths.config / "integrations.json").read_text()
    assert f"credential://{credential_name}" in stored
    assert "super-secret-value" not in stored
    assert "should-not-be-stored" not in stored  # secret-looking settings are stripped
    disconnected = client.post(f"/api/integrations/{connector}/disconnect", json={}).json()
    assert disconnected["connected"] is False


def test_an_unknown_integration_is_a_404(client):
    assert client.post("/api/integrations/not-connected/perform",
                       json={"action": "noop", "arguments": {}}).status_code == 404
    assert client.post("/api/integrations/not-connected/connect",
                       json={"credential_ref": "credential://x"}).status_code == 404
    assert client.post("/api/integrations/not-connected/disconnect", json={}).status_code == 404


# ---------------------------------------------------------------- the console itself
def test_the_console_is_served_and_assets_exist(ui_client):
    client = ui_client
    page = client.get("/")
    assert page.status_code == 200
    assert "/assets/js/app.js" in page.text
    for asset in ("/assets/app.css", "/assets/js/app.js", "/assets/logo.svg", "/manifest.webmanifest"):
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert response.content


def test_unknown_api_routes_stay_404_while_the_spa_falls_back(ui_client):
    client = ui_client
    assert client.get("/api/definitely-not-a-route").status_code == 404
    assert client.get("/some/deep/link").status_code == 200
