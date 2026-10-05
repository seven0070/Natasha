# Natasha Desktop Application

Natasha is packaged as a high-performance native desktop application using **Tauri v2**. It embeds the complete Natasha Obsidian Intelligence interface within an OS-native WebView while connecting to the existing Natasha Python backend as the core authority.

---

## 1. Architectural Model

```text
               ┌───────────────────────────────────────────────────┐
               │              NATASHA DESKTOP SHELL                │
               │                   (Tauri v2)                      │
               │                                                   │
               │  ┌────────────────────┐   ┌────────────────────┐  │
               │  │  System Tray Menu  │   │  Window Controls   │  │
               │  │  Notifications     │   │  Deep Link Handler │  │
               │  │  Safe Filesystem   │   │  Auto-start Config │  │
               │  └─────────┬──────────┘   └─────────┬──────────┘  │
               │            │                        │             │
               │            ▼                        ▼             │
               │       IPC Bridge (desktop.js / Tauri Invoke)      │
               │                                                   │
               │  ┌─────────────────────────────────────────────┐  │
               │  │        Obsidian Intelligence UI             │  │
               │  │       (HTML5 / CSS / Vanilla JS)            │  │
               │  └──────────────────────┬──────────────────────┘  │
               └─────────────────────────┼─────────────────────────┘
                                         │ REST / WebSocket
                                         ▼ (http://127.0.0.1:8000)
               ┌───────────────────────────────────────────────────┐
               │              NATASHA BACKEND ENGINE               │
               │                 (Python / FastAPI)                │
               │                                                   │
               │  ┌───────────────┐ ┌──────────────┐ ┌──────────┐  │
               │  │ Presentation  │ │ Core / Brain │ │ Security │  │
               │  └───────┬───────┘ └──────┬───────┘ └────┬─────┘  │
               │          ▼                ▼              ▼        │
               │  ┌─────────────────────────────────────────────┐  │
               │  │ Data Layer (SQLite / Migrations / Vectors)  │  │
               │  └─────────────────────────────────────────────┘  │
               └───────────────────────────────────────────────────┘
```

The Tauri layer functions purely as a native desktop shell:
- It **does not replace** the Natasha backend or replicate business logic.
- It manages the lifecycle of the local backend server process (spawning, health-checking, clean termination, and crash recovery).
- It provides native operating-system capabilities with least-privilege security controls.

---

## 2. Supported Platforms

| Platform | Architecture | Installer Formats | Minimum OS Version |
| :--- | :--- | :--- | :--- |
| **Windows** | x86_64, aarch64 | NSIS (`.exe`), MSI (`.msi`) | Windows 10 (Build 19041+) / 11 |
| **macOS** | Apple Silicon & Intel (Universal) | DMG (`.dmg`), App Bundle (`.app`) | macOS 10.15 Catalina+ |
| **Linux** | x86_64 | AppImage (`.AppImage`), Debian (`.deb`) | Ubuntu 20.04+, Fedora 34+, Arch |

---

## 3. Prerequisites & Development Setup

### System Prerequisites
1. **Rust Toolchain**: Rust 1.80+ (`rustup default stable`)
2. **Node.js**: Node 20 LTS (`node -v`, `npm -v`)
3. **Python Runtime**: Python 3.12+ managed via `uv`
4. **Platform-Specific SDKs**:
   - **Windows**: Microsoft Visual Studio C++ Build Tools & WebView2 Runtime (preinstalled on Windows 10/11)
   - **macOS**: Xcode Command Line Tools (`xcode-select --install`)
   - **Linux**: WebKit2GTK and build tools:
     ```bash
     sudo apt-get install -y libwebkit2gtk-4.1-dev build-essential curl wget file libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev
     ```

### Development Mode

Run the application in live desktop mode with auto-reload:

```bash
# 1. Start or verify the backend in your development environment
uv run -m apps.server --host 127.0.0.1 --port 8000

# 2. Launch Tauri Desktop in development mode
npm run tauri dev
# or
npx @tauri-apps/cli dev
```

The Tauri runtime launches the desktop window, embeds `frontend/`, connects to `http://127.0.0.1:8000`, and opens the interface with live reload enabled.

---

## 4. Build & Packaging Commands

To produce production installers and binaries for your operating system:

```bash
# Production desktop bundle build
npm run tauri build
# or
npx @tauri-apps/cli build
```

Generated bundles are placed in:
- **Windows**: `src-tauri/target/release/bundle/nsis/Natasha_0.9.0_x64-setup.exe` & `bundle/msi/Natasha_0.9.0_x64_en-US.msi`
- **macOS**: `src-tauri/target/release/bundle/dmg/Natasha_0.9.0_universal.dmg`
- **Linux**: `src-tauri/target/release/bundle/appimage/Natasha_0.9.0_amd64.AppImage`

---

## 5. Security Architecture & Least Privilege

The desktop shell enforces strict capability boundaries:

### 1. Granular Capability Model (`src-tauri/capabilities/default.json`)
The WebView is isolated from raw operating system resources:
- **No arbitrary shell execution**: The shell command plugin is restricted. Frontend JavaScript cannot execute arbitrary binaries or terminal commands.
- **Path Traversal Protection**: All filesystem commands (`safe_read_file`, `safe_write_file`, `safe_list_dir`) validate target paths. Sequences containing `..` and attempts to touch operating system directories (`C:\Windows`, `/etc`, `/sys`, `/proc`, `/dev`) are denied with explicit security error events.
- **Credential Isolation**: Secrets, provider tokens, and encryption keys remain exclusively inside the backend credential vault (`backend/core/credentials.py`). No raw credentials are exposed to or persisted inside the Tauri frontend layer.

### 2. Content Security Policy (CSP)
The WebView enforces a strict CSP in `src-tauri/tauri.conf.json`:
```text
default-src 'self' tauri: http://tauri.localhost http://127.0.0.1:* http://localhost:*;
connect-src 'self' tauri: ws://127.0.0.1:* ws://localhost:* http://127.0.0.1:* http://localhost:*;
img-src 'self' asset: tauri: data: blob: https:;
style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;
font-src 'self' https://fonts.gstatic.com;
script-src 'self' 'unsafe-inline';
```

---

## 6. Native Desktop Capabilities

### System Tray
A native system tray icon runs in the OS menu/taskbar with:
- **Open Natasha**: Brings the application to foreground, restores minimized/hidden windows.
- **New Chat**: Focuses the window and navigates immediately to a fresh chat session.
- **Voice**: Navigates to the Voice Arena mode.
- **Settings**: Navigates to the 12-category desktop configuration panel.
- **Quit**: Gracefully shuts down backend services and exits the process cleanly.

### Window Controls
Custom framing and native controls:
- `desktop.minimizeWindow()`: Minimizes the application window.
- `desktop.maximizeWindow()`: Maximizes the window.
- `desktop.toggleMaximize()`: Toggles between maximized and restored state.
- `desktop.closeWindow()`: Closes/hides the window based on user preferences.
- `desktop.setFullscreen(flag)`: Toggles true fullscreen presentation.

### Deep Linking (`natasha://`)
The application registers the custom protocol scheme `natasha://` for system-wide navigation:
- `natasha://chat/<id>`: Open conversation by ID.
- `natasha://project/<id>`: Open project canvas by ID.
- `natasha://task/<id>`: Open task details by ID.
- `natasha://voice`: Launch Voice Arena.
- `natasha://settings`: Open configuration.

All incoming deep link URIs are validated against allowlisted routes before triggering UI transitions.

### System Telemetry & Notifications
- Memory and CPU usage telemetry via the native Rust `sysinfo` subsystem (`get_system_info`).
- Native notifications for long-running task completions, approval requests, and mission milestones.

### Native File Dialogs & Desktop Paths
- `desktop.openFileDialog({ title })`: Opens the OS-native file chooser without exposing raw shell or broad filesystem handles.
- `desktop.saveFileDialog({ title, defaultName })`: Opens the OS-native save dialog for exporting artifacts, chat logs, or configurations.
- `desktop.getDesktopPaths()`: Safely returns standard user paths (`home`, `documents`, `desktop`, `downloads`, `app_data`) via Tauri's path resolver.

### Clipboard & External URLs
- `desktop.copyToClipboard(text)` / `desktop.readFromClipboard()`: Seamless system clipboard access with fallback to Web API.
- `desktop.openExternal(url)`: Opens external web links safely in the default system browser (`noopener,noreferrer`).

---

## 7. Application Startup Sequence & State Machine

Natasha implements a controlled startup sequence to ensure the backend authority is fully healthy before the user interface is exposed:

```text
Launch Natasha
      ↓
Initialize Tauri Shell & Native Plugins
      ↓
Probe & Start Backend (apps/server.py on 127.0.0.1:8000)
      ↓
Health Polling (/api/system/health, up to 20 attempts)
      ↓
Authenticate Session (store.bootstrap())
      ↓
Hide Startup Gate & Mount Natasha Interface
      ↓
Ready
```

- **Startup Gate UI (`#startup`)**: Displays branding, live initialization status messages, and animated indicator while services initialize.
- **Graceful Error Recovery**: If the backend fails to respond within 20 seconds, the startup screen halts, displays the exact failure reason, and provides an interactive "Retry Connection" button without crashing the app.

---

## 8. Backend Process Lifecycle Management

Natasha features a self-healing process manager in Rust (`src-tauri/src/backend.rs`):

```text
Desktop Startup
      │
      ├─► Probe http://127.0.0.1:8000/api/system/health
      │      ├── [Active]  ──► Connect directly (managed = false, no duplicate process)
      │      └── [Offline] ──► Locate Python / .venv
      │                            │
      │                            ▼ Spawn python -m apps.server
      │                            │ (managed = true, CREATE_NO_WINDOW)
      │                            ▼
      │                     Poll health endpoint (up to 30s)
      │                            │
      │                            ▼
      │                     Emit 'natasha:backend-ready'
      │
Shutdown / Exit
      │
      ▼
Terminate managed child process cleanly (no zombie processes)
```

- **Crash Detection & Auto-Recovery**: If a managed backend unexpectedly terminates, `BackendManager` attempts up to 3 restarts before surfacing a persistent troubleshooting prompt to prevent infinite restart loops.
- **Non-managed Mode**: If the user launched the backend externally (e.g., via Docker or terminal script), the desktop client attaches to it transparently without killing it when the desktop UI exits.

---

## 9. Code Signing & Release Pipeline

### GitHub Actions CI/CD (`.github/workflows/desktop-release.yml`)
The multi-platform release pipeline triggers on Git release tags (`v*`):
1. **Pre-flight verification**: Runs Python backend tests, frontend syntax tests, and desktop integration tests.
2. **Multi-platform build matrix**: Builds native installers on Windows, macOS, and Ubuntu runners.
3. **Signing & Notarization**:
      - **Windows**: Tagged releases require `WINDOWS_SIGNING_CERTIFICATE` (base64-encoded production PFX) and `WINDOWS_SIGNING_PASSWORD` as protected GitHub Actions secrets, plus the `WINDOWS_SIGNING_PUBLISHER` repository variable containing the exact certificate subject. CI validates the Code Signing EKU and trusted certificate chain, signs with SHA-256 and a trusted timestamp, then verifies the executable, NSIS/MSI installers, the downloaded GitHub Release assets, and the NSIS-installed executable. Non-release CI builds are unsigned; a missing or untrusted production certificate blocks tagged releases.
   - **macOS**: Apple Developer ID signing and Apple Notary API verification (`APPLE_CERTIFICATE`, `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID`).
   - **Linux**: Package validation and SHA-256 checksum generation.

---

## 10. Data Storage & Uninstallation

### Application Data Locations
Natasha stores local configuration, databases, and persistent logs in standard OS locations:
- **Windows**: `%LOCALAPPDATA%\Natasha\` and `%USERPROFILE%\.natasha\`
- **macOS**: `~/Library/Application Support/ai.natasha.desktop/` and `~/.natasha/`
- **Linux**: `~/.config/natasha/` and `~/.natasha/`

### Clean Uninstallation
- **Windows**: Uninstall via "Windows Settings > Apps & Features > Natasha". The NSIS uninstaller removes all application binaries and shortcuts. Local SQLite databases and configuration in `%USERPROFILE%\.natasha\` are preserved unless the user explicitly checks "Remove user data".
- **macOS**: Move `Natasha.app` from `/Applications` to the Trash. To remove local databases, delete `~/.natasha/`.
- **Linux**: Delete the AppImage file or run `sudo apt remove natasha` if installed via `.deb`.

---

## 11. Troubleshooting

| Issue | Cause | Resolution |
| :--- | :--- | :--- |
| **Backend Offline / Connection Refused** | Python runtime missing or port 8000 in use | Run `uv sync` to ensure `.venv` is built; check if another service occupies port 8000 via `netstat -ano \| findstr 8000`. |
| **WebView Blank / White Screen** | WebKit/WebView2 runtime not initialized | Ensure Microsoft Edge WebView2 runtime is installed on Windows. On Linux, ensure `libwebkit2gtk-4.1-dev` is installed. |
| **Deep Links Not Opening** | Protocol handler unregistered | Run the application once as Administrator on Windows to register the `natasha://` protocol in the Windows Registry. |
| **System Tray Icon Missing** | OS desktop environment lacks AppIndicator | On Linux (GNOME), install the `gnome-shell-extension-appindicator` package. |
