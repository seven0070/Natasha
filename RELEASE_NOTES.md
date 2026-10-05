# Natasha Release Notes — Version 0.9.0

**Release Date:** October 5, 2026  
**Version:** `0.9.0`  
**Git Branch:** `arena/01a105f9-natasha`  
**Architecture:** Native Desktop Shell (Tauri v2) + Stitch Obsidian Intelligence UI + FastAPI Backend Authority  

---

## 1. Executive Summary

Natasha `0.9.0` is the inaugural native desktop release of the Natasha autonomous intelligence platform. This release wraps the completed, validated Natasha backend and the Stitch Obsidian Intelligence visual interface into a secure, native desktop application using **Tauri v2**.

The desktop shell acts strictly as a native capability layer and process supervisor without duplicating backend business logic or replacing existing APIs.

---

## 2. Major Features & Desktop Capabilities

### Native Desktop Shell (Tauri v2)
- **Automatic Backend Process Supervisor**: Spawns and manages `apps.server` on `127.0.0.1:8000` windowlessly (`CREATE_NO_WINDOW`). Reuses existing active instances (e.g. running in Docker or CLI) without spawning duplicate processes. Guarantees clean exit on application shutdown with zero zombie processes.
- **Crash Recovery**: Automatically detects backend crashes and attempts up to 3 restarts before presenting an interactive troubleshooting prompt.
- **Startup Gate & Health State Machine**: Displays an Obsidian-styled startup gate (`#startup`) that polls `/api/system/health` until the backend is fully initialized before unlocking the interface.
- **Native System Tray**: Menu with quick access to *Open Natasha*, *New Chat*, *Voice*, *Settings*, and *Quit*.
- **Native Window Controls**: Frameless and custom window actions: Minimize, Maximize, Toggle Maximize, Close, Fullscreen.
- **Deep Linking Protocol (`natasha://`)**: System-wide URI routing for `natasha://chat/<id>`, `natasha://project/<id>`, `natasha://task/<id>`, `natasha://voice`, and `natasha://settings`.
- **Desktop Paths & Native Dialogs**: Secure native file picker (`open_file_dialog`) and save file dialog (`save_file_dialog`) without exposing raw shell handles. Standard folder resolution for user documents, desktop, and app data.
- **System Telemetry**: Native CPU and memory monitoring via the `sysinfo` subsystem.
- **Desktop Notifications**: Native OS notifications for mission completions, approvals, and system alerts with web fallback.
- **Clipboard Management**: Seamless OS clipboard integration for copying and reading text.

### Visual Interface (Stitch Obsidian Intelligence)
- Complete Obsidian Intelligence design system with curated dark palette tokens, 3D tilt effects, and specular highlights.
- Interactive **3D Bubble Cursor** with spring physics and an accessibility toggle.
- 6 primary screens:
  - **Chat**: Real-time streaming conversations, tool execution indicators, and approval cards.
  - **Projects**: Project grid, status metrics, and workspace views.
  - **Missions/Tasks**: Multi-phase mission timeline, metric cards, and failure recovery controls.
  - **Agents**: Agent team roster, roles, and status monitor.
  - **Voice Arena**: Voice interaction visualizer and real-time audio loop controls.
  - **Settings**: 12-category desktop configuration panel.

---

## 3. Security & Governance

- **Least-Privilege Capability Model**: Defined in `src-tauri/capabilities/default.json`. Unrestricted shell execution (`shell:execute`) and raw filesystem access are strictly prohibited.
- **Path Traversal Protection**: Every filesystem command passes through `validate_safe_path`, which blocks `..` sequences and forbids access to system root directories (`C:\Windows`, `/etc`, `/sys`, `/proc`, `/dev`).
- **Credential Vault Isolation**: API keys, OAuth secrets, and credentials remain strictly inside the backend vault (`backend/core/credentials.py`). No secrets are exposed to the Tauri frontend layer or stored in desktop state.
- **Content Security Policy (CSP)**: Strict CSP enforcing local loopback communication (`tauri:`, `http://127.0.0.1:*`, `ws://127.0.0.1:*`).

---

## 4. Platform Verification Status

| Category | Capability / Platform | Status | Evidence / Notes |
| :--- | :--- | :--- | :--- |
| **Desktop** | Windows 10/11 Desktop Shell | **VERIFIED** | Rust toolchain, Cargo compilation, IPC bridge, integration tests passing. Binary built at `src-tauri/target/release/natasha.exe` (35.06 MiB). |
| **Desktop** | Windows NSIS Installer | **VERIFIED** | Built at `src-tauri/target/release/bundle/nsis/Natasha_0.9.0_x64-setup.exe` (7.00 MiB). |
| **Desktop** | Windows MSI Package | **VERIFIED** | Built at `src-tauri/target/release/bundle/msi/Natasha_0.9.0_x64_en-US.msi` (10.50 MiB). |
| **Security** | Least-privilege Permissions | **VERIFIED** | `tests/unit/test_desktop_integration.py` verifies shell execution blocked and paths restricted. |
| **Security** | Path Traversal Prevention | **VERIFIED** | Built-in Rust unit tests and Python integration tests verify traversal rejection. |
| **Backend** | REST & WebSocket Integration | **VERIFIED** | `api.js` dynamic loopback resolution verified against FastAPI endpoints. |
| **Hardware** | Voice Arena Microphone/STT | **UNVERIFIED — HARDWARE NOT AVAILABLE** | SAPI TTS backend detected; local STT model / mic unverified in headless environment. |
| **Signing** | Windows Code Signing | **UNVERIFIED — PRODUCTION SIGNING CREDENTIALS NOT CONFIGURED** | Authenticode signing ready in CI when `TAURI_SIGNING_PRIVATE_KEY` is added to GitHub Secrets. |

---

## 5. Installation & Upgrades

### System Requirements
- **Operating System**: Windows 10 (Version 2004+ / Build 19041+) or Windows 11 (64-bit x86_64).
- **Runtime**: Microsoft Edge WebView2 runtime (preinstalled on modern Windows 10/11).
- **Hardware**: Minimum 4 GB RAM, 500 MB disk space.

### Clean Installation
1. Download `Natasha_0.9.0_x64-setup.exe` (Windows NSIS) or `Natasha_0.9.0_x64.msi`.
2. Follow the setup wizard to install into `%LOCALAPPDATA%\Natasha`.
3. Launch Natasha. The desktop shell will automatically initialize the local backend engine and present the Obsidian interface.

### Data Preservation & Uninstallation
- All user memories, SQLite databases, and project configurations are stored under `%USERPROFILE%\.natasha` (Windows), `~/.natasha` (Linux/macOS).
- Running the uninstaller removes application binaries and desktop shortcuts while **preserving your user data** unless explicit removal is selected.

---

## 6. Known Limitations
1. **Cloud API Credentials**: When running fully offline without cloud API keys, external model providers will honestly report as unavailable. Local models (Ollama/llama.cpp) can be toggled via Settings.
2. **Code Signing Secrets**: Official signed production releases require adding `TAURI_SIGNING_PRIVATE_KEY` and Apple Developer certificates to GitHub Actions Secrets.
