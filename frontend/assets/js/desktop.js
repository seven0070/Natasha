/* Desktop Native Bridge for Natasha Tauri Application.
   Provides native window controls, system info, background process telemetry,
   and deep-link handling with safe fallbacks when running in a standard browser. */

import { toast } from "./ui.js";
import { go } from "./router.js";

class DesktopBridge {
  constructor() {
    this.isTauri = Boolean(window.__TAURI_INTERNALS__ || window.__TAURI__);
    this.listeners = new Map();
    this.init();
  }

  isDesktop() {
    return this.isTauri;
  }

  async invoke(command, args = {}) {
    if (!this.isTauri) {
      return null;
    }
    try {
      if (window.__TAURI_INTERNALS__ && typeof window.__TAURI_INTERNALS__.invoke === "function") {
        return await window.__TAURI_INTERNALS__.invoke(command, args);
      }
      if (window.__TAURI__ && window.__TAURI__.core && typeof window.__TAURI__.core.invoke === "function") {
        return await window.__TAURI__.core.invoke(command, args);
      }
    } catch (err) {
      console.warn(`[DesktopBridge] Command '${command}' error:`, err);
      throw err;
    }
    return null;
  }

  init() {
    // Listen for backend lifecycle events emitted from Rust
    window.addEventListener("natasha:backend-ready", (e) => {
      console.log("[DesktopBridge] Backend ready:", e.detail);
    });

    window.addEventListener("natasha:backend-error", (e) => {
      console.error("[DesktopBridge] Backend error:", e.detail);
      toast(`Desktop Backend Error: ${e.detail}`, "error");
    });

    // Listen for tray navigation events
    window.addEventListener("natasha:navigate", (e) => {
      const target = typeof e.detail === "string" ? e.detail : (e.detail && e.detail.view);
      if (target) {
        go(target);
      }
    });

    // Deep link handling (natasha://chat/<id>, natasha://project/<id>, natasha://task/<id>)
    window.addEventListener("natasha:deep-link", (e) => {
      this.handleDeepLink(e.detail);
    });
  }

  handleDeepLink(urlStr) {
    if (!urlStr || typeof urlStr !== "string") return;
    try {
      // e.g. natasha://chat/123 or natasha://project/demo
      const parsed = new URL(urlStr);
      if (parsed.protocol !== "natasha:") return;

      const host = parsed.hostname || parsed.pathname.replace(/^\/\//, "").split("/")[0];
      const segments = parsed.pathname.split("/").filter(Boolean);
      const arg = segments[0] || "";

      if (host === "chat") {
        go("chat", arg);
      } else if (host === "project" || host === "projects") {
        go("projects", arg);
      } else if (host === "task" || host === "tasks" || host === "mission" || host === "missions") {
        go("missions", arg);
      } else if (host === "agent" || host === "agents") {
        go("agents", arg);
      } else if (host === "voice") {
        go("voice");
      } else if (host === "settings") {
        go("settings");
      }
    } catch (err) {
      console.warn("[DesktopBridge] Invalid deep link:", urlStr, err);
    }
  }

  // --- Window Controls ---
  async minimizeWindow() {
    return this.invoke("window_minimize");
  }

  async maximizeWindow() {
    return this.invoke("window_maximize");
  }

  async toggleMaximize() {
    return this.invoke("window_toggle_maximize");
  }

  async closeWindow() {
    return this.invoke("window_close");
  }

  async setFullscreen(fullscreen) {
    return this.invoke("window_set_fullscreen", { fullscreen });
  }

  // --- Backend Process Lifecycle ---
  async getBackendStatus() {
    return this.invoke("get_backend_status");
  }

  async restartBackend() {
    return this.invoke("restart_backend");
  }

  async checkBackendHealth() {
    return this.invoke("check_backend_health");
  }

  // --- System Telemetry ---
  async getSystemInfo() {
    return this.invoke("get_system_info");
  }

  // --- Restricted Filesystem Operations ---
  async readFile(path) {
    return this.invoke("safe_read_file", { path });
  }

  async writeFile(path, content) {
    return this.invoke("safe_write_file", { path, content });
  }

  async listDir(path) {
    return this.invoke("safe_list_dir", { path });
  }

  // --- Desktop Notifications ---
  async sendNotification(title, body) {
    if (!this.isTauri) {
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification(title, { body, icon: "assets/logo.svg" });
      }
      return;
    }
    // Tauri notification plugin
    if (window.__TAURI__ && window.__TAURI__.notification) {
      try {
        await window.__TAURI__.notification.sendNotification({ title, body });
      } catch (err) {
        console.warn("[DesktopBridge] Notification error:", err);
      }
    }
  }
}

export const desktop = new DesktopBridge();
