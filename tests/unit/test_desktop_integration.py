"""Integration tests for the Natasha Tauri Desktop Application.

Verifies:
- Tauri v2 configuration (tauri.conf.json) adheres to application specifications.
- Deep link protocols (natasha://) are properly registered.
- Granular capability permissions (src-tauri/capabilities/default.json) follow least-privilege.
- Native Rust source structure, window control commands, safe filesystem validations, and backend manager.
- Frontend desktop bridge (frontend/assets/js/desktop.js) provides window, fs, system, and deep-link handlers.
- Dynamic API endpoint resolution (frontend/assets/js/api.js) targets the local backend authority inside WebView.
- Application icons are present in src-tauri/icons/.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TAURI_DIR = REPO_ROOT / "src-tauri"
FRONTEND_DIR = REPO_ROOT / "frontend"
JS_DIR = FRONTEND_DIR / "assets" / "js"


def test_tauri_configuration_file():
    """Verify tauri.conf.json exists and contains correct metadata and configuration."""
    conf_path = TAURI_DIR / "tauri.conf.json"
    assert conf_path.is_file(), "tauri.conf.json must exist in src-tauri/"

    with open(conf_path, "r", encoding="utf-8") as f:
        conf = json.load(f)

    # Core identity
    assert conf.get("productName") == "Natasha"
    assert conf.get("version") == "0.9.0"
    assert conf.get("identifier") == "ai.natasha.desktop"

    # Frontend dev & build distribution path
    build_conf = conf.get("build", {})
    assert build_conf.get("frontendDist") == "../frontend"

    # Window settings
    windows = conf.get("app", {}).get("windows", [])
    assert len(windows) > 0, "At least one main window must be configured"
    main_win = windows[0]
    assert main_win.get("title") == "Natasha"
    assert main_win.get("width") == 1240
    assert main_win.get("height") == 840
    assert main_win.get("minWidth") == 800
    assert main_win.get("minHeight") == 600
    assert main_win.get("resizable") is True

    # Security CSP
    security = conf.get("app", {}).get("security", {})
    csp = security.get("csp", "")
    assert "default-src" in csp
    assert "tauri:" in csp
    assert "ws:" in csp, "WebSocket must be allowed in CSP for streaming"

    # Deep links
    plugins = conf.get("plugins", {})
    deep_link = plugins.get("deep-link", {})
    schemes = deep_link.get("desktop", {}).get("schemes", [])
    assert "natasha" in schemes, "natasha:// deep-link scheme must be registered"

    # Bundle targets and configurations
    bundle = conf.get("bundle", {})
    assert bundle.get("active") is True
    assert bundle.get("targets") == "all" or "all" in bundle.get("targets", []) or "nsis" in bundle.get("targets", [])
    assert "windows" in bundle and "nsis" in bundle["windows"]
    assert "macOS" in bundle
    assert "linux" in bundle


def test_tauri_capabilities_least_privilege():
    """Verify capabilities file grants necessary permissions without exposing raw unrestricted shell."""
    cap_path = TAURI_DIR / "capabilities" / "default.json"
    assert cap_path.is_file(), "capabilities/default.json must exist"

    with open(cap_path, "r", encoding="utf-8") as f:
        cap = json.load(f)

    windows = cap.get("windows", [])
    assert "main" in windows

    permissions = cap.get("permissions", [])
    assert "core:default" in permissions
    assert "dialog:default" in permissions
    assert "notification:default" in permissions
    assert "clipboard-manager:default" in permissions
    assert "autostart:default" in permissions
    assert "deep-link:default" in permissions

    # Verify no raw shell execute access is granted to frontend
    assert "shell:execute" not in permissions


def test_native_rust_source_structure():
    """Verify Rust modules implement native requirements."""
    src_dir = TAURI_DIR / "src"
    assert (src_dir / "lib.rs").is_file()
    assert (src_dir / "main.rs").is_file()
    assert (src_dir / "backend.rs").is_file()
    assert (src_dir / "tray.rs").is_file()
    assert (src_dir / "commands" / "mod.rs").is_file()
    assert (src_dir / "commands" / "fs.rs").is_file()
    assert (src_dir / "commands" / "window.rs").is_file()
    assert (src_dir / "commands" / "system.rs").is_file()
    assert (src_dir / "commands" / "backend.rs").is_file()

    # Verify safe filesystem restrictions in fs.rs
    fs_code = (src_dir / "commands" / "fs.rs").read_text(encoding="utf-8")
    assert "validate_safe_path" in fs_code
    assert "safe_read_file" in fs_code
    assert "safe_write_file" in fs_code
    assert "safe_list_dir" in fs_code
    assert "path traversal" in fs_code

    # Verify backend manager in backend.rs
    backend_code = (src_dir / "backend.rs").read_text(encoding="utf-8")
    assert "BackendManager" in backend_code
    assert "MAX_RESTART_ATTEMPTS" in backend_code
    assert "check_health" in backend_code
    assert "api/health" in backend_code
    assert "find_python" in backend_code
    assert "stop" in backend_code

    # Verify system tray in tray.rs
    tray_code = (src_dir / "tray.rs").read_text(encoding="utf-8")
    assert "setup_tray" in tray_code
    assert "Open Natasha" in tray_code
    assert "New Chat" in tray_code
    assert "Voice" in tray_code
    assert "Settings" in tray_code
    assert "Quit" in tray_code

    # Verify native file dialogs and desktop paths in fs.rs
    assert "get_desktop_paths" in fs_code
    assert "open_file_dialog" in fs_code
    assert "save_file_dialog" in fs_code


def test_icons_present():
    """Verify application icons exist for multi-platform distribution."""
    icons_dir = TAURI_DIR / "icons"
    assert (icons_dir / "icon.png").is_file()
    assert (icons_dir / "icon.ico").is_file()
    assert (icons_dir / "icon.icns").is_file()
    assert (icons_dir / "128x128.png").is_file()
    assert (icons_dir / "128x128@2x.png").is_file()


def test_frontend_desktop_bridge_exists_and_exports_api():
    """Verify frontend/assets/js/desktop.js implements desktop bridge with graceful fallbacks."""
    desktop_js = JS_DIR / "desktop.js"
    assert desktop_js.is_file(), "desktop.js must exist in frontend/assets/js/"

    content = desktop_js.read_text(encoding="utf-8")
    assert "export const desktop" in content
    assert "isTauri" in content
    assert "minimizeWindow" in content
    assert "maximizeWindow" in content
    assert "closeWindow" in content
    assert "safe_read_file" in content
    assert "safe_write_file" in content
    assert "safe_list_dir" in content
    assert "getDesktopPaths" in content
    assert "openFileDialog" in content
    assert "saveFileDialog" in content
    assert "copyToClipboard" in content
    assert "readFromClipboard" in content
    assert "openExternal" in content
    assert "getBackendStatus" in content
    assert "restartBackend" in content
    assert "handleDeepLink" in content

    # Deep link route handling
    assert "chat" in content
    assert "project" in content
    assert "task" in content


def test_frontend_api_resolves_desktop_backend_url():
    """Verify api.js properly detects Tauri and points to backend authority."""
    api_js = JS_DIR / "api.js"
    assert api_js.is_file()

    content = api_js.read_text(encoding="utf-8")
    assert "resolveApiUrl" in content
    assert "127.0.0.1:8000" in content
    assert "tauri.localhost" in content or "__TAURI_INTERNALS__" in content


def test_frontend_app_integrates_desktop_module():
    """Verify frontend/assets/js/app.js imports and initializes the desktop bridge."""
    app_js = JS_DIR / "app.js"
    assert app_js.is_file()

    content = app_js.read_text(encoding="utf-8")
    assert "desktop.js" in content
    assert "desktop" in content


def test_application_startup_sequence_and_screen():
    """Verify startup screen exists and app.js executes controlled startup sequence."""
    index_html = FRONTEND_DIR / "index.html"
    assert index_html.is_file()
    html_content = index_html.read_text(encoding="utf-8")
    assert 'id="startup"' in html_content
    assert 'id="startup-status"' in html_content
    assert 'id="startup-error"' in html_content
    assert 'id="startup-retry"' in html_content

    app_js = JS_DIR / "app.js"
    js_content = app_js.read_text(encoding="utf-8")
    assert "startupStatus" in js_content
    assert "checkBackendHealth" in js_content
    assert "Starting or connecting to backend..." in js_content
