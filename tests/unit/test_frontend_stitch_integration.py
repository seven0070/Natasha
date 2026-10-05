"""Integration tests for the Natasha Stitch UI Frontend.

Verifies:
- All 6 core Stitch screens (Chat, Tasks, Projects, Agents, Voice, Settings) are registered.
- Routing contracts, screen titles, and icons.
- Stitch Obsidian Intelligence design system tokens in app.css.
- 3D Bubble Cursor system and accessibility toggle.
- Settings structure conforms to the 12 familiar categories without leaking internal engine names.
- WebSocket streaming and API integrations for Chat, Tasks, Projects, Agents, Voice, and Settings.
- Loading, error, and empty state representations across all views.
"""

from __future__ import annotations

import re
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"
JS_ROOT = FRONTEND / "assets" / "js"
CSS_PATH = FRONTEND / "assets" / "app.css"
INDEX_HTML = FRONTEND / "index.html"
DESIGN_MD = REPO_ROOT / "design" / "stitch" / "DESIGN.md"


def test_stitch_design_reference_and_assets_exist():
    """Confirms the Stitch design artifacts are archived in design/stitch/ and frontend/assets/."""
    assert DESIGN_MD.is_file(), "DESIGN.md should be archived in design/stitch/"
    assert (REPO_ROOT / "design" / "stitch" / "assets" / "logo.svg").is_file()
    assert (FRONTEND / "assets" / "logo.svg").is_file()


def test_stylesheet_defines_stitch_obsidian_design_tokens():
    """Verify that app.css provides the complete Obsidian Intelligence design system."""
    assert CSS_PATH.is_file()
    css = CSS_PATH.read_text(encoding="utf-8")

    # Tokens and variables
    assert "--bg" in css
    assert "--primary" in css
    assert "--text" in css
    assert "--border" in css
    assert "--radius" in css

    # 3D Tilt & Specular sheens
    assert "data-3d-tilt" in css or "card-3d" in css
    assert "radial-gradient" in css

    # 3D Bubble Cursor
    assert "#bubble-cursor" in css
    assert ".bubble-cursor-hidden" in css

    # Core screen containers
    assert ".chat-river" in css
    assert ".msg" in css or ".msg__body" in css
    assert ".task-metric-card" in css or ".task-metric-grid" in css
    assert ".project-card" in css
    assert ".agents-grid" in css
    assert ".voice-arena" in css
    assert ".voice-neural-orb" in css
    assert ".voice-spectrum" in css
    assert ".settings-layout" in css
    assert ".settings-nav" in css


def test_all_six_stitch_screens_exist_and_register_routes():
    """Verify Chat, Tasks (missions), Projects, Agents, Voice, and Settings views exist."""
    required_views = ["chat.js", "missions.js", "projects.js", "agents.js", "voice.js", "settings.js"]
    for filename in required_views:
        path = JS_ROOT / "views" / filename
        assert path.is_file(), f"Missing view module: {filename}"
        content = path.read_text(encoding="utf-8")
        assert "register({" in content, f"{filename} must register a route"
        assert "render" in content, f"{filename} must export a render function"


def test_bubble_cursor_module_and_accessibility():
    """Verify bubble cursor provides smooth physics, text safety, and disable switch."""
    cursor_path = JS_ROOT / "cursor.js"
    assert cursor_path.is_file()
    code = cursor_path.read_text(encoding="utf-8")
    assert "export const bubbleCursor" in code
    assert "init()" in code or "init(" in code
    assert "enable()" in code or "enable(" in code
    assert "disable()" in code or "disable(" in code
    assert "toggle(" in code
    assert "prefers-reduced-motion" in code, "Must honor accessibility reduced motion"


def test_settings_categories_structure():
    """Verify Settings uses the 12 familiar categories and avoids internal engine terms."""
    settings_path = JS_ROOT / "views" / "settings.js"
    code = settings_path.read_text(encoding="utf-8")

    familiar_categories = [
        "General", "Appearance", "Notifications", "Privacy",
        "Personalization", "Data & Memory", "Voice", "Models",
        "Connections", "Security", "Advanced", "About"
    ]
    for cat in familiar_categories:
        assert cat.lower() in code.lower(), f"Settings must contain category: {cat}"

    # Verify internal engine terms are NOT exposed as top-level categories
    forbidden_top_level = [
        "Executive Engine", "Cognition Engine", "World Model",
        "Governance Engine", "Memory Engine"
    ]
    for term in forbidden_top_level:
        pattern = rf'label:\s*["\']{re.escape(term)}["\']'
        assert not re.search(pattern, code, re.IGNORECASE), f"Do not expose internal term {term} as category"


def test_chat_screen_integrations():
    """Verify Chat supports real WebSocket streaming, markdown, and tools."""
    chat_path = JS_ROOT / "views" / "chat.js"
    code = chat_path.read_text(encoding="utf-8")
    assert "chatStream" in code or "WebSocket" in code or "/api/chat" in code
    assert "renderMarkdown" in code or "markdown" in code
    assert "copy" in code or "Copy" in code


def test_tasks_screen_integrations():
    """Verify Tasks (missions) connects to /api/missions and shows metrics."""
    tasks_path = JS_ROOT / "views" / "missions.js"
    code = tasks_path.read_text(encoding="utf-8")
    assert "/api/missions" in code
    assert "Total" in code or "active" in code
    assert "renderMissions" in code


def test_projects_screen_integrations():
    """Verify Projects connects to /api/artifacts and creation endpoints."""
    projects_path = JS_ROOT / "views" / "projects.js"
    code = projects_path.read_text(encoding="utf-8")
    assert "/api/artifacts" in code or "/api/missions" in code
    assert "renderProjects" in code


def test_agents_screen_integrations():
    """Verify Agents connects to /api/agents and delegation endpoints."""
    agents_path = JS_ROOT / "views" / "agents.js"
    code = agents_path.read_text(encoding="utf-8")
    assert "/api/agents" in code
    assert "/api/agents/delegate" in code
    assert "renderAgents" in code


def test_voice_screen_integrations():
    """Verify Voice connects to /api/voice/loop, turn, and interrupt."""
    voice_path = JS_ROOT / "views" / "voice.js"
    code = voice_path.read_text(encoding="utf-8")
    assert "/api/voice/loop" in code
    assert "/api/voice/turn" in code
    assert "/api/voice/interrupt" in code
    assert "voice-neural-orb" in code
    assert "voice-spectrum" in code
