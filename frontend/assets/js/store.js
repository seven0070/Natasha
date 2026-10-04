/* Runtime state: one small observable store. No framework, no build step. */

import { api } from "./api.js";

const listeners = new Set();

export const store = {
  ready: false,
  authenticated: false,
  initialised: false,
  ownerId: "",
  info: null,
  status: null,
  doctor: null,
  paletteOpen: false,
  sidebarOpen: false,
  pendingApprovals: 0,
  theme: localStorage.getItem("natasha.theme") || "dark",
  route: "chat",

  subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  emit() { for (const fn of listeners) { try { fn(this); } catch (error) { console.error(error); } } },
  set(patch) { Object.assign(this, patch); this.emit(); },

  async bootstrap() {
    try { this.info = await api.get("/api/info"); } catch { this.info = null; }
    try {
      const auth = await api.get("/api/auth/status");
      this.initialised = Boolean(auth.initialised);
      this.authenticated = Boolean(auth.authenticated);
      this.ownerId = auth.owner_id || this.ownerId;
    } catch {
      this.authenticated = false;
    }
    if (this.authenticated) await this.refreshStatus();
    this.applyTheme();
    this.ready = true;
    this.emit();
    return this.authenticated;
  },

  async setup(passphrase, ownerId) {
    const result = await api.post("/api/auth/setup", { passphrase, owner_id: ownerId });
    api.setToken(result.token);
    this.authenticated = true;
    this.initialised = true;
    this.ownerId = result.owner_id || ownerId;
    await this.refreshStatus();
    this.emit();
    return result;
  },

  async login(passphrase) {
    const result = await api.post("/api/auth/login", { passphrase, client: "web" });
    api.setToken(result.token);
    this.authenticated = true;
    this.ownerId = result.owner_id || "";
    await this.refreshStatus();
    this.emit();
    return result;
  },

  async logout() {
    try { await api.post("/api/auth/logout", {}); } catch { /* the token is being discarded anyway */ }
    api.setToken("");
    this.authenticated = false;
    this.status = null;
    this.emit();
  },

  async refreshStatus() {
    try {
      this.status = await api.get("/api/status");
      const approvals = await api.get("/api/approvals");
      this.pendingApprovals = (approvals.pending || []).length;
    } catch (error) {
      if (error.status === 401) this.authenticated = false;
    }
    this.emit();
  },

  applyTheme() {
    document.documentElement.dataset.theme = this.theme;
  },

  toggleTheme() {
    this.theme = this.theme === "dark" ? "light" : "dark";
    localStorage.setItem("natasha.theme", this.theme);
    this.applyTheme();
    this.emit();
  },
};

/* A slow poll keeps the sidebar health chip and the approval badge honest. */
export function startStatusPolling(intervalMs = 15000) {
  const tick = () => { if (store.authenticated) store.refreshStatus(); };
  setInterval(tick, intervalMs);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
}
