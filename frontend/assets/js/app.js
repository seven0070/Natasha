/* Bootstrap: owner gate, navigation, command palette, health chip, routing. */

import { api } from "./api.js";
import { store, startStatusPolling } from "./store.js";
import { el, clear, toast, fmtTime, titleCase } from "./ui.js";
import { start, routeList, go, currentRoute } from "./router.js";

import { bubbleCursor } from "./cursor.js";
import { desktop } from "./desktop.js";

import "./views/chat.js";
import "./views/projects.js";
import "./views/missions.js";
import "./views/agents.js";
import "./views/voice.js";
import "./views/settings.js";
import "./views/memory.js";
import "./views/approvals.js";
import "./views/activity.js";
import "./views/artifacts.js";
import "./views/vision.js";
import "./views/computer.js";
import "./views/skills.js";
import "./views/integrations.js";
import "./views/providers.js";
import "./views/security.js";
import "./views/evolution.js";
import "./views/tools.js";

const dom = {
  gate: document.getElementById("gate"),
  gateForm: document.getElementById("gate-form"),
  gatePass: document.getElementById("gate-pass"),
  gateSubmit: document.getElementById("gate-submit"),
  gateError: document.getElementById("gate-error"),
  gateSub: document.getElementById("gate-sub"),
  gateHint: document.getElementById("gate-hint"),
  shell: document.getElementById("shell"),
  nav: document.getElementById("nav"),
  view: document.getElementById("view"),
  viewSub: document.getElementById("view-sub"),
  health: document.getElementById("health-chip"),
  menu: document.getElementById("menu"),
  collapse: document.getElementById("collapse"),
  logout: document.getElementById("logout"),
  theme: document.getElementById("theme"),
  paletteOpen: document.getElementById("palette-open"),
  palette: document.getElementById("palette"),
  paletteInput: document.getElementById("palette-input"),
  paletteList: document.getElementById("palette-list"),
};

const PRIMARY_ORDER = ["chat", "projects", "missions", "agents", "voice", "settings"];

function buildNav() {
  clear(dom.nav);
  const allRoutes = routeList().filter((r) => r.id !== "tasks");
  allRoutes.sort((a, b) => {
    const ai = PRIMARY_ORDER.indexOf(a.id);
    const bi = PRIMARY_ORDER.indexOf(b.id);
    if (ai !== -1 && bi !== -1) return ai - bi;
    if (ai !== -1) return -1;
    if (bi !== -1) return 1;
    return a.title.localeCompare(b.title);
  });

  allRoutes.forEach((route) => {
    const isIconName = route.icon && route.icon.length > 2;
    const iconEl = isIconName
      ? el("span", { class: "nav__icon material-symbols-outlined" }, route.icon)
      : el("span", { class: "nav__icon" }, route.icon || "•");
    const link = el("a", {
      class: "nav__item", href: `#/${route.id}`, dataset: { route: route.id },
    },
      iconEl,
      el("span", { class: "nav__label" }, route.title),
      route.id === "approvals" ? el("span", { class: "nav__badge", dataset: { badge: "approvals" }, hidden: true }, "") : null);
    dom.nav.append(link);
  });
}

function updateBadges() {
  const badge = dom.nav.querySelector('[data-badge="approvals"]');
  if (!badge) return;
  if (store.pendingApprovals > 0) {
    badge.textContent = String(store.pendingApprovals);
    badge.hidden = false;
  } else badge.hidden = true;
}

function updateHealth() {
  const status = store.status;
  if (!status) { dom.health.textContent = "offline"; dom.health.className = "chip chip--danger"; return; }
  const degraded = (status.state && status.state.degraded) || [];
  dom.health.textContent = degraded.length ? `${degraded.length} degraded` : "healthy";
  dom.health.className = `chip ${degraded.length ? "chip--warn" : "chip--ok"}`;
  dom.health.title = degraded.length ? degraded.join("\n") : "All attached subsystems report healthy";
}

function showShell(visible) {
  dom.gate.hidden = visible;
  dom.shell.hidden = !visible;
}

function renderGate() {
  showShell(false);
  dom.gateSub.textContent = store.initialised
    ? `Welcome back${store.ownerId ? `, ${store.ownerId}` : ""}. Unlock to continue.`
    : "First run: create the owner passphrase. It is stored only on this machine.";
  dom.gateSubmit.textContent = store.initialised ? "Unlock" : "Create owner";
  dom.gateHint.textContent = store.initialised
    ? "Your passphrase unlocks the local session. It never leaves this machine."
    : "Choose at least 8 characters. This passphrase is the only key to your agent.";
  dom.gatePass.value = "";
  dom.gatePass.focus();
}

dom.gateForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const passphrase = dom.gatePass.value;
  dom.gateError.hidden = true;
  dom.gateSubmit.disabled = true;
  try {
    if (store.initialised) await store.login(passphrase);
    else await store.setup(passphrase, "owner");
    await enterApp();
  } catch (error) {
    dom.gateError.textContent = error.message || String(error);
    dom.gateError.hidden = false;
  } finally {
    dom.gateSubmit.disabled = false;
  }
});

dom.logout.addEventListener("click", async () => {
  await store.logout();
  renderGate();
});

/* ---------- command palette ---------- */
const paletteState = { items: [], index: 0 };

async function paletteItems(query) {
  const routes = routeList().map((route) => ({
    label: route.title, hint: route.subtitle || "", run: () => go(route.id),
  }));
  if (!query) return routes;
  const matches = routes.filter((route) => route.label.toLowerCase().includes(query.toLowerCase()));
  const extras = [];
  if (query.length >= 3) {
    const searchable = [
      ["missions", "/api/missions", (row) => row.title || row.objective || row.id, (row) => go("missions", row.id)],
      ["memories", "/api/memory", (row) => row.summary || row.content, () => go("memory")],
      ["skills", "/api/skills", (row) => row.name || row.id, () => go("skills")],
      ["tools", "/api/tools", (row) => row.name, () => go("tools")],
    ];
    await Promise.all(searchable.map(async ([kind, path, label, run]) => {
      try {
        const payload = await api.get(path, { limit: 50 });
        const rows = payload[kind] || payload.memories || payload.tools || [];
        rows.forEach((row) => {
          const text = String(label(row) || "");
          if (text.toLowerCase().includes(query.toLowerCase())) {
            extras.push({ label: text, hint: kind, run: () => run(row) });
          }
        });
      } catch { /* a failed lookup just contributes nothing */ }
    }));
  }
  return [...matches, ...extras].slice(0, 12);
}

async function renderPalette(query) {
  paletteState.items = await paletteItems(query);
  paletteState.index = 0;
  clear(dom.paletteList);
  if (!paletteState.items.length) {
    dom.paletteList.append(el("li", { class: "muted" }, "nothing matched"));
    return;
  }
  paletteState.items.forEach((item, index) => {
    dom.paletteList.append(el("li", {
      class: index === 0 ? "sel" : "",
      onclick: () => { closePalette(); item.run(); },
    }, item.label, item.hint ? el("span", { class: "small muted" }, `  ${item.hint}`) : null));
  });
}

function openPalette() {
  dom.palette.hidden = false;
  dom.paletteInput.value = "";
  renderPalette("");
  dom.paletteInput.focus();
}

function closePalette() { dom.palette.hidden = true; }

dom.paletteOpen.addEventListener("click", openPalette);
dom.paletteInput.addEventListener("input", () => renderPalette(dom.paletteInput.value));
dom.paletteInput.addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const delta = event.key === "ArrowDown" ? 1 : -1;
    paletteState.index = Math.max(0, Math.min(paletteState.items.length - 1, paletteState.index + delta));
    Array.from(dom.paletteList.children).forEach((node, index) =>
      node.classList.toggle("sel", index === paletteState.index));
  } else if (event.key === "Enter") {
    const item = paletteState.items[paletteState.index];
    closePalette();
    if (item) item.run();
  } else if (event.key === "Escape") closePalette();
});

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    dom.palette.hidden ? openPalette() : closePalette();
  } else if (event.key === "Escape" && !dom.palette.hidden) closePalette();
  if (dom.palette.hidden === false && event.key === "Escape") return;
});

document.addEventListener("click", (event) => {
  if (!dom.palette.hidden && !dom.palette.contains(event.target) && event.target !== dom.paletteOpen) {
    closePalette();
  }
});

function updateProfile() {
  const ownerEl = document.getElementById("sidebar-owner-name");
  if (ownerEl && store.ownerId) {
    ownerEl.textContent = store.ownerId;
  }
}

// Global ⌘N / New Chat trigger
const btnNewChat = document.getElementById("btn-sidebar-new");
if (btnNewChat) {
  btnNewChat.addEventListener("click", () => {
    go("chat");
    window.dispatchEvent(new CustomEvent("natasha:new-chat"));
  });
}

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "n") {
    event.preventDefault();
    go("chat");
    window.dispatchEvent(new CustomEvent("natasha:new-chat"));
  }
});

/* ---------- chrome ---------- */
dom.theme.addEventListener("click", () => store.toggleTheme());
dom.menu.addEventListener("click", () => {
  store.set({ sidebarOpen: !store.sidebarOpen });
  document.querySelector(".sidebar").classList.toggle("open", store.sidebarOpen);
});
dom.collapse.addEventListener("click", () => {
  dom.shell.classList.toggle("collapsed");
  dom.collapse.textContent = dom.shell.classList.contains("collapsed") ? "»" : "«";
});
document.querySelectorAll(".nav__item").forEach(() => {});

store.subscribe(() => {
  updateBadges();
  updateHealth();
  updateProfile();
});
setInterval(updateHealth, 20000);

/* ---------- boot ---------- */
async function enterApp() {
  showShell(true);
  buildNav();
  updateBadges();
  updateHealth();
  updateProfile();
  bubbleCursor.init();
  start((definition) => { dom.viewSub.textContent = definition.subtitle || ""; });
  startStatusPolling();
  const route = currentRoute();
  if (route) dom.viewSub.textContent = route.definition.subtitle || "";
}

async function boot() {
  bubbleCursor.init();

  const startupEl = document.getElementById("startup");
  const startupStatus = document.getElementById("startup-status");
  const startupError = document.getElementById("startup-error");
  const startupRetry = document.getElementById("startup-retry");
  const startupSpinner = document.getElementById("startup-spinner");

  const setStartupStatus = (msg) => {
    if (startupStatus) startupStatus.textContent = msg;
  };

  const showStartupFailure = (err) => {
    if (startupSpinner) startupSpinner.hidden = true;
    if (startupError) {
      startupError.hidden = false;
      startupError.textContent = `Backend connection failed: ${err.message || err}`;
    }
    if (startupRetry) {
      startupRetry.hidden = false;
      startupRetry.onclick = () => {
        startupError.hidden = true;
        startupRetry.hidden = true;
        if (startupSpinner) startupSpinner.hidden = false;
        boot();
      };
    }
  };

  // If running in Tauri desktop, wait for backend health check
  if (desktop.isTauri) {
    setStartupStatus("Starting or connecting to backend...");
    let retries = 0;
    const maxRetries = 20;
    let healthy = false;

    while (retries < maxRetries && !healthy) {
      try {
        healthy = await desktop.checkBackendHealth();
        if (healthy) break;
      } catch {
        // continue polling
      }
      retries++;
      await new Promise((r) => setTimeout(r, 1000));
    }

    if (!healthy) {
      showStartupFailure(new Error("Natasha backend could not be reached on http://127.0.0.1:8000."));
      return;
    }
  }

  setStartupStatus("Checking authentication...");
  let authenticated = false;
  try {
    authenticated = await store.bootstrap();
  } catch (err) {
    showStartupFailure(err);
    return;
  }

  // Hide startup screen
  if (startupEl) startupEl.hidden = true;

  if (authenticated) {
    await enterApp();
  } else {
    renderGate();
    // If the runtime is started without auth (loopback only), go straight in.
    if (store.info && store.info.auth_required === false) {
      try { await store.login(""); } catch { /* stays locked */ }
      if (store.authenticated) await enterApp();
    }
  }
}

boot().catch((error) => {
  document.body.append(el("div", { class: "empty error", style: "margin:2rem" },
    `Natasha could not start the console: ${error.message || error}`));
});

window.addEventListener("error", (event) => {
  if (event.message) toast(event.message, "error");
});
window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason && event.reason.message ? event.reason.message : String(event.reason);
  if (reason && !reason.includes("owner authentication required")) toast(reason, "error");
});

export { fmtTime, titleCase };
