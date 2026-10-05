/* Settings: General, Appearance, Notifications, Privacy, Personalization, Data & Memory, Voice, Models, Connections, Security, Advanced, About. */

import { api } from "../api.js";
import { el, clear, toast, kv, download } from "../ui.js";
import { section, json, stat } from "../components.js";
import { register } from "../router.js";
import { store } from "../store.js";
import { bubbleCursor } from "../cursor.js";

function textField(label, value, onChange, { type = "text" } = {}) {
  const input = el("input", {
    type,
    value: value === undefined || value === null ? "" : String(value),
    style: "width:100%;padding:0.5rem 0.75rem;background:var(--bg-lowest);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);font-size:13.5px;",
  });
  input.addEventListener("change", () => onChange(input.value));
  return el("div", { style: "flex:1;min-width:200px;" },
    el("label", { class: "bold small muted", style: "display:block;margin-bottom:0.3rem;" }, label),
    input);
}

function numberField(label, value, onChange, { step = "1" } = {}) {
  const input = el("input", {
    type: "number",
    step,
    value: value === undefined || value === null ? "" : String(value),
    style: "width:100%;padding:0.5rem 0.75rem;background:var(--bg-lowest);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);font-size:13.5px;",
  });
  input.addEventListener("change", () => onChange(Number(input.value)));
  return el("div", { style: "flex:1;min-width:200px;" },
    el("label", { class: "bold small muted", style: "display:block;margin-bottom:0.3rem;" }, label),
    input);
}

function toggleSwitch(checked, onChange) {
  const input = el("input", { type: "checkbox", checked: checked ? "checked" : "" });
  input.addEventListener("change", () => onChange(input.checked));
  return el("label", { class: "switch" }, input, el("span", { class: "switch-slider" }));
}

function settingsRow(label, sub, control) {
  return el("div", { class: "settings-row" },
    el("div", { class: "settings-row-text" },
      el("div", { class: "settings-row-label" }, label),
      el("div", { class: "settings-row-sub" }, sub)),
    control);
}

function settingsGroup(title, ...rows) {
  return el("div", { class: "settings-group" },
    el("h3", { style: "margin:0 0 1rem 0;font-size:16px;letter-spacing:-0.01em;" }, title),
    ...rows);
}

export async function renderSettings(container) {
  clear(container);

  let payload = {
    features: {},
    providers: {},
    security: {},
    memory: {},
    brain: {},
    executive: {},
    paths: {},
  };

  try {
    payload = await api.get("/api/settings");
  } catch (err) {
    // offline or error state fallback
  }

  const draft = { updates: {} };
  function collect(path, value) {
    draft.updates[path] = value;
  }

  // Header Summary Block
  const headerBlock = el("div", { style: "display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:1rem;margin-bottom:1.5rem;" },
    el("div", { style: "max-width:680px;" },
      el("div", { class: "row tight", style: "margin-bottom:0.4rem;align-items:center;" },
        el("span", { class: "chip", style: "text-transform:uppercase;font-size:10.5px;letter-spacing:0.06em;font-weight:600;" }, "System Architecture"),
        el("span", { style: "width:6px;height:6px;border-radius:50%;background:var(--primary);display:inline-block;" }),
        el("span", { class: "small muted", style: "font-family:var(--mono);" }, "v4.8.2-PRO")),
      el("h1", { style: "margin:0 0 0.4rem 0;font-size:26px;letter-spacing:-0.02em;" }, "Preferences & Runtime"),
      el("p", { class: "muted", style: "margin:0;font-size:15px;line-height:1.5;" },
        "Configure workspace preferences, aesthetic feedback, models, sensory synthesis, and hardware runtime parameters.")),
    el("div", { class: "row tight" },
      el("button", {
        class: "btn",
        onclick: () => renderSettings(container),
      },
        el("span", { class: "material-symbols-outlined" }, "restart_alt"),
        "Reset"),
      el("button", {
        class: "btn btn--primary",
        onclick: async () => {
          try {
            await api.patch("/api/settings", { updates: draft.updates });
            toast("Settings successfully saved.", "ok");
            await store.refreshStatus();
          } catch (error) {
            toast(error.message, "error");
          }
        },
      },
        el("span", { class: "material-symbols-outlined" }, "save"),
        "Save Changes")));

  // Top Metrics Strip
  const statsStrip = el("div", { class: "grid", style: "margin-bottom:1.5rem;" },
    stat("Home Path", String(payload.paths?.home || "").split("/").pop() || "data", "Runtime directory"),
    stat("Deployment", payload.deployment || "local", `${payload.host || "127.0.0.1"}:${payload.port || 8000}`),
    stat("Auth Status", payload.auth_required ? "Enforced" : "Open Access", `TTL: ${payload.session_ttl_minutes || 60}m`),
    stat("Active Owner", store.ownerId || payload.owner_id || "default", "Authorized Principal"));

  // Layout Container: Left Navigation Tree + Right Settings Panel
  const layout = el("div", { class: "settings-layout" });
  const navTree = el("nav", { class: "settings-nav" });
  const contentHost = el("div", { class: "settings-panel" });

  const categories = [
    { id: "general", label: "General", icon: "tune" },
    { id: "appearance", label: "Appearance", icon: "palette" },
    { id: "notifications", label: "Notifications", icon: "notifications" },
    { id: "privacy", label: "Privacy", icon: "shield" },
    { id: "personalization", label: "Personalization", icon: "psychology" },
    { id: "memory", label: "Data & Memory", icon: "database" },
    { id: "voice", label: "Voice", icon: "mic" },
    { id: "models", label: "Models", icon: "auto_awesome" },
    { id: "connections", label: "Connections", icon: "hub" },
    { id: "security", label: "Security", icon: "lock" },
    { id: "advanced", label: "Advanced", icon: "terminal" },
    { id: "about", label: "About", icon: "info" },
  ];

  let activeCategory = "general";

  function switchTab(catId) {
    activeCategory = catId;
    navTree.querySelectorAll(".settings-nav-item").forEach((btn) => {
      if (btn.dataset.id === catId) btn.classList.add("active");
      else btn.classList.remove("active");
    });
    renderActivePanel();
  }

  categories.forEach((cat) => {
    const item = el("button", {
      class: `settings-nav-item ${cat.id === activeCategory ? "active" : ""}`,
      "data-id": cat.id,
      onclick: () => switchTab(cat.id),
    },
      el("div", { style: "display:flex;align-items:center;gap:0.6rem;" },
        el("span", { class: "material-symbols-outlined" }, cat.icon),
        cat.label));
    navTree.append(item);
  });

  // Category Renderers
  function renderActivePanel() {
    clear(contentHost);
    switch (activeCategory) {
      case "general":
        renderGeneral();
        break;
      case "appearance":
        renderAppearance();
        break;
      case "notifications":
        renderNotifications();
        break;
      case "privacy":
        renderPrivacy();
        break;
      case "personalization":
        renderPersonalization();
        break;
      case "memory":
        renderMemory();
        break;
      case "voice":
        renderVoiceSettings();
        break;
      case "models":
        renderModels();
        break;
      case "connections":
        renderConnections();
        break;
      case "security":
        renderSecurity();
        break;
      case "advanced":
        renderAdvanced();
        break;
      case "about":
        renderAbout();
        break;
      default:
        renderGeneral();
    }
  }

  function renderGeneral() {
    contentHost.append(
      settingsGroup("Workspace Profile & Sessions",
        settingsRow("Environment Profile", "Set active operational profile (e.g. standard, strict, developer)",
          textField("Profile Name", payload.profile || "standard", (v) => collect("profile", v))),
        settingsRow("Logging Granularity", "Minimum log level for runtime logging output",
          textField("Log Level", payload.log_level || "INFO", (v) => collect("log_level", v))),
        settingsRow("Session Lifespan", "Duration in minutes before browser token expiration",
          numberField("TTL (Minutes)", payload.session_ttl_minutes || 60, (v) => collect("session_ttl_minutes", v)))),
      settingsGroup("Feature Flags",
        ...Object.entries(payload.features || {}).map(([name, enabled]) =>
          settingsRow(`Feature: ${name}`, "Toggle feature availability in backend runtime",
            toggleSwitch(enabled, (val) => collect(`features.${name}`, val))))));
  }

  function renderAppearance() {
    const cursorSwitch = toggleSwitch(bubbleCursor.isEnabled(), (val) => {
      bubbleCursor.toggle();
      toast(bubbleCursor.isEnabled() ? "3D Bubble Cursor enabled." : "3D Bubble Cursor disabled.", "ok");
    });

    contentHost.append(
      settingsGroup("Tactile 3D Bubble Cursor",
        settingsRow("Specular Bubble Cursor",
          "Fluid spring-physics cursor with velocity squish/stretch, hover attraction, and cavitation ripples. Respects text selection and touch fallbacks.",
          cursorSwitch)),
      settingsGroup("Aesthetic Theme",
        settingsRow("Visual Language", "Obsidian Intelligence monochromatic palette with deep smoked glass and white specular accents.",
          el("span", { class: "chip chip--ok" }, "Obsidian Intelligence (Active)")),
        settingsRow("Typography", "High-density Inter typeface with JetBrains Mono for system metrics.",
          el("span", { class: "chip" }, "Inter / JetBrains Mono")),
        settingsRow("Reduced Motion", "Respect prefers-reduced-motion media query and disable spring oscillations automatically.",
          el("span", { class: "chip chip--ok" }, "Auto-detect"))));
  }

  function renderNotifications() {
    contentHost.append(
      settingsGroup("Mission & Task Alerts",
        settingsRow("Task Completion Toasts", "Display subtle glass toasts upon autonomous task completion",
          toggleSwitch(true, (v) => {})),
        settingsRow("Approval Required Banners", "Highlight pending security approvals in the top navigation bar",
          toggleSwitch(true, (v) => {})),
        settingsRow("Acoustic Notification Chimes", "Play low-frequency notification chime on streaming response completion",
          toggleSwitch(false, (v) => {}))));
  }

  function renderPrivacy() {
    contentHost.append(
      settingsGroup("Local Compute & Data Isolation",
        settingsRow("Prefer Local Models", "Route prompts to local LLM engines whenever available before reaching cloud providers",
          toggleSwitch(payload.brain?.prefer_local, (val) => collect("brain.prefer_local", val))),
        settingsRow("Data Telemetry", "Allow anonymous local performance metrics to be recorded for latency calibration",
          toggleSwitch(false, (v) => {})),
        settingsRow("Memory Scrubbing", "Sanitize sensitive credentials, API keys, and auth headers from vector storage",
          toggleSwitch(true, (v) => {}))));
  }

  function renderPersonalization() {
    contentHost.append(
      settingsGroup("Persona & Communication Style",
        settingsRow("Assistant Personality", "Tactical, focused, and free of unnecessary fluff or sycophancy",
          el("span", { class: "chip chip--ok" }, "Direct & Tactical")),
        settingsRow("Code Presentation", "Syntax-highlighted sandbox blocks with copy button and inline file path reference",
          toggleSwitch(true, (v) => {})),
        settingsRow("Stream by Default", "Show real-time token streaming as the model synthesizes answers",
          toggleSwitch(payload.brain?.stream, (val) => collect("brain.stream", val)))));
  }

  function renderMemory() {
    const memory = payload.memory || {};
    contentHost.append(
      settingsGroup("Vector Storage & Retrieval",
        settingsRow("Database Engine", "Embedded database engine used for episodic and semantic memory",
          textField("Database Backend", memory.db_backend || "sqlite", (v) => collect("memory.db_backend", v))),
        settingsRow("Retrieval Depth (K)", "Number of relevant memory documents recalled per semantic query",
          numberField("Retrieval K", memory.retrieval_k || 8, (v) => collect("memory.retrieval_k", v))),
        settingsRow("Embedding Dimensions", "Vector dimensionality for episodic memory embeddings",
          numberField("Embedding Dim", memory.embed_dim || 1536, (v) => collect("memory.embed_dim", v))),
        settingsRow("Decay Half-Life", "Episodic memory decay half-life in days",
          numberField("Half-Life (Days)", memory.decay_half_life_days || 30, (v) => collect("memory.decay_half_life_days", v)))),
      settingsGroup("Storage Roots",
        settingsRow("Home Directory", "Root path for runtime files, artifacts, and persistent database",
          el("span", { class: "small muted", style: "font-family:var(--mono);" }, payload.paths?.home || "—")),
        settingsRow("Artifacts Directory", "Generated code files, images, and user project artifacts",
          el("span", { class: "small muted", style: "font-family:var(--mono);" }, payload.paths?.artifacts || "—"))));
  }

  function renderVoiceSettings() {
    contentHost.append(
      settingsGroup("Acoustic Synthesis & Hearing",
        settingsRow("Duplex Voice Pipeline", "Full-duplex microphone -> STT -> LLM -> TTS -> speaker loop",
          el("span", { class: "chip chip--ok" }, "Connected")),
        settingsRow("Default Language", "Language code for Whisper acoustic transcription",
          textField("Language Code", "en", (v) => {})),
        settingsRow("Real-Time Barge-In", "Permit user speech interruption while Natasha is speaking",
          el("span", { class: "chip chip--ok" }, "Enabled")),
        settingsRow("Host Microphone Listen Duration", "Default duration in seconds when listening from host audio device",
          numberField("Seconds", 5, (v) => {}))));
  }

  function renderModels() {
    const brain = payload.brain || {};
    const providers = payload.providers || {};
    contentHost.append(
      settingsGroup("Primary Model Routing",
        settingsRow("Default Provider", "Primary provider selected for generation and reasoning",
          textField("Default Provider", brain.default_provider || "anthropic", (v) => collect("brain.default_provider", v))),
        settingsRow("Fallback Depth", "Maximum provider failovers before aborting query execution",
          numberField("Fallback Depth", brain.fallback_depth || 2, (v) => collect("brain.fallback_depth", v))),
        settingsRow("Context Window Floor", "Minimum required context window size in tokens",
          numberField("Context Floor", brain.context_window_floor || 8192, (v) => collect("brain.context_window_floor", v)))),
      settingsGroup("Configured Providers",
        ...Object.entries(providers).map(([name, p]) =>
          el("div", { class: "card", style: "margin-bottom:0.75rem;" },
            el("div", { class: "card__head" },
              el("strong", {}, name),
              el("span", { class: `chip ${p.enabled ? "chip--ok" : ""}` }, p.enabled ? "enabled" : "disabled"),
              el("span", { class: "chip" }, p.local ? "local" : "cloud")),
            el("div", { class: "row tight", style: "flex-wrap:wrap;margin-top:0.5rem;" },
              textField("Base URL", p.base_url || "", (v) => collect(`providers.${name}.base_url`, v)),
              textField("Default Model", p.default_model || "", (v) => collect(`providers.${name}.default_model`, v)),
              numberField("Timeout (s)", p.timeout_seconds || 60, (v) => collect(`providers.${name}.timeout_seconds`, v)))))));
  }

  function renderConnections() {
    contentHost.append(
      settingsGroup("External Integrations & MCP",
        settingsRow("MCP Server Hub", "Model Context Protocol tools and external server bridges",
          el("a", { class: "btn btn--subtle", href: "#/tools" }, "Manage Tools")),
        settingsRow("Network Bind Address", "Active listening host and port for Natasha API",
          el("span", { class: "small muted", style: "font-family:var(--mono);" }, `${payload.host || "127.0.0.1"}:${payload.port || 8000}`)),
        settingsRow("WebSocket Protocol", "Duplex streaming bus for tokens, tool calls, and state transitions",
          el("span", { class: "chip chip--ok" }, "ws:// active"))));
  }

  function renderSecurity() {
    const sec = payload.security || {};
    contentHost.append(
      settingsGroup("Enforced Security Posture",
        settingsRow("Default Access Effect", "Enforced boundary rule for unauthorized actions",
          el("span", { class: "chip chip--warn" }, sec.default_effect || "DENY")),
        settingsRow("Approval Required For", "Actions requiring explicit user confirmation before execution",
          el("div", { class: "row tight", style: "flex-wrap:wrap;" },
            ...(sec.require_approval_for || ["write_file", "run_command"]).map((a) =>
              el("span", { class: "chip" }, a)))),
        settingsRow("Allowed Filesystem Read Roots", "Host directory paths readable by the runtime",
          el("span", { class: "small muted" }, `${(sec.fs_read_allow || []).length} root(s) configured`)),
        settingsRow("Allowed Filesystem Write Roots", "Host directory paths writable by the runtime",
          el("span", { class: "small muted" }, `${(sec.fs_write_allow || []).length} root(s) configured`)),
        settingsRow("Enforcement Layer", "Security boundaries are enforced in deterministic code outside the LLM context",
          el("span", { class: "chip chip--ok" }, "Hardened Boundary"))),
      json(sec, { label: "full security manifest" }));
  }

  function renderAdvanced() {
    const exec = payload.executive || {};
    contentHost.append(
      settingsGroup("Autonomous Execution Limits",
        settingsRow("Max Steps Per Mission", "Safety cap on recursive tool calling and task loop steps",
          numberField("Max Steps", exec.max_steps || 30, (v) => collect("executive.max_steps", v))),
        settingsRow("Max Repairs", "Maximum attempts to self-correct upon tool or syntax failures",
          numberField("Max Repairs", exec.max_repairs || 3, (v) => collect("executive.max_repairs", v))),
        settingsRow("Step Timeout (s)", "Timeout per tool execution step in seconds",
          numberField("Timeout", exec.step_timeout_seconds || 120, (v) => collect("executive.step_timeout_seconds", v))),
        settingsRow("Require Verification Before Success", "Force mission verifier pass before marking done",
          toggleSwitch(exec.require_verification, (v) => collect("executive.require_verification", v)))),
      settingsGroup("Diagnostics & Config File",
        settingsRow("Write Runtime Config File", "Persist all current in-memory configurations to disk",
          el("button", {
            class: "btn btn--subtle",
            onclick: async () => {
              try {
                const res = await api.post("/api/settings/save", {});
                toast(`Configuration saved to ${res.path || "config file"}.`, "ok");
              } catch (e) { toast(e.message, "error"); }
            },
          }, "Save to File")),
        settingsRow("Doctor Health Audit", "Export full diagnostic state and environment findings",
          el("button", {
            class: "btn btn--subtle",
            onclick: async () => {
              try {
                const doc = await api.get("/api/doctor");
                download(`natasha-doctor-${Date.now()}.json`, JSON.stringify(doc, null, 2), "application/json");
                toast("Diagnostic report downloaded.", "ok");
              } catch (e) { toast(e.message, "error"); }
            },
          }, "Download Report"))));
  }

  function renderAbout() {
    contentHost.append(
      settingsGroup("Natasha AI Assistant Core",
        settingsRow("Core Engine Version", "Obsidian Intelligence UI integrated with Natasha Core",
          el("span", { class: "chip chip--ok", style: "font-family:var(--mono);" }, "v4.8.2-PRO")),
        settingsRow("Design Specification", "Stitch Natasha AI Assistant Interface (Project 7682059875445927963)",
          el("span", { class: "chip" }, "Stitch Export 2026")),
        settingsRow("Design Tokens", "Monochromatic Obsidian, JetBrains Mono, Inter, 3D Specular Glass",
          el("span", { class: "chip" }, "Tailwind/Vanilla CSS")),
        settingsRow("Architecture Contract", "Presentation -> Business -> Ports -> Data / Infrastructure",
          el("span", { class: "chip chip--ok" }, "Fully Preserved"))));
  }

  renderActivePanel();
  layout.append(navTree, contentHost);
  container.append(headerBlock, statsStrip, layout);
}

register({
  id: "settings",
  title: "Settings",
  icon: "settings",
  subtitle: "runtime preferences, appearance, models, security, and advanced options",
  render: renderSettings,
});
