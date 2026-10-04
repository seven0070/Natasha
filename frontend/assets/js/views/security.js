/* Security: credentials (never revealed), sessions, the broker, and the security audit. */

import { api } from "../api.js";
import { el, clear, fmtTime, toast, confirmDialog } from "../ui.js";
import { section, table, stat, json } from "../components.js";
import { register } from "../router.js";

export async function renderSecurity(container) {
  const statsHost = el("div", { class: "grid", style: "margin-bottom:.8rem" });
  const credentialsHost = el("div", {});
  const sessionsHost = el("div", {});
  const brokerHost = el("div", {});
  const auditHost = el("div", {});

  const name = el("input", { placeholder: "credential name (e.g. openai)" });
  const kind = el("select", {}, ...[
    "api_key", "token", "password", "oauth", "ssh_key", "basic", "cookie", "service_account",
  ].map((value) => el("option", { value }, value)));
  const secret = el("input", { type: "password", placeholder: "secret value (encrypted at rest)" });
  const notes = el("input", { placeholder: "notes (stored with the credential, optional)" });

  async function refresh() {
    const [payload, sessions, broker] = await Promise.all([
      api.get("/api/security/credentials"),
      api.get("/api/security/sessions"),
      api.get("/api/security/broker"),
    ]);
    const stats = payload.stats || {};
    clear(statsHost).append(
      stat("credentials", stats.total ?? 0, `${stats.active ?? 0} active, ${stats.revoked ?? 0} revoked`),
      stat("encryption", stats.algorithm || "unknown", "secrets are never returned to a model"),
      stat("sessions", (sessions.sessions || []).length, `ttl ${sessions.ttl_minutes || "?"} minutes`),
      stat("broker", broker.active_ttl_seconds ? `${broker.active_ttl_seconds}s grants` : "idle",
        broker.note || "short-lived handles handed to tools"));
    clear(credentialsHost).append(table([
      { label: "name", value: "name" },
      { label: "kind", value: "kind" },
      { label: "status", value: (row) => el("span", { class: `chip ${row.revoked ? "chip--danger" : "chip--ok"}` },
        row.revoked ? "revoked" : "active") },
      { label: "created", value: (row) => fmtTime(row.created_at) },
      { label: "used", value: (row) => (row.use_count === undefined ? "" : `${row.use_count}x`) },
      { label: "actions", value: (row) => el("div", { class: "row tight" },
        el("button", { class: "btn small", onclick: () => act(row.name, "test") }, "Test"),
        el("button", { class: "btn small", onclick: () => act(row.name, "health") }, "Health"),
        el("button", { class: "btn small", onclick: () => act(row.name, "rotate") }, "Rotate"),
        el("button", { class: "btn small btn--danger", onclick: () => revoke(row.name) }, "Revoke"),
        el("button", { class: "btn small btn--danger", onclick: () => remove(row.name) }, "Delete")) },
    ], payload.credentials || [], { empty: "No credentials stored." }),
      el("div", { class: "small muted", style: "margin-top:.4rem" },
        "Values are deliberately absent from this table - the broker hands tools a short-lived handle instead."));

    clear(sessionsHost).append(table([
      { label: "client", value: "client" },
      { label: "created", value: (row) => fmtTime(row.created_at) },
      { label: "expires", value: (row) => fmtTime(row.expires_at) },
      { label: "last seen", value: (row) => fmtTime(row.last_seen_at) },
      { label: "actions", value: (row) => el("button", {
        class: "btn small btn--danger", onclick: async () => {
          try {
            await api.post("/api/auth/sessions/revoke", { session_id: row.id });
            toast("Session revoked", "ok");
            refresh();
          } catch (error) { toast(error.message, "error"); }
        },
      }, "Revoke") },
    ], sessions.sessions || [], { empty: "No active sessions." }));
    clear(brokerHost).append(json(broker, { label: "broker configuration" }));

    try {
      const audit = await api.get("/api/security/audit", { limit: 60 });
      clear(auditHost).append(table([
        { label: "when", value: (row) => fmtTime(row.ts) },
        { label: "kind", value: "kind" },
        { label: "actor", value: "actor" },
        { label: "risk", value: "risk" },
        { label: "detail", value: (row) => JSON.stringify(row.payload || {}).slice(0, 140) },
      ], audit.events || [], { empty: "No security events recorded." }));
    } catch (error) {
      clear(auditHost).append(el("div", { class: "small error" }, error.message));
    }
  }

  async function act(credentialName, action) {
    try {
      const payload = await api.post(`/api/security/credentials/${encodeURIComponent(credentialName)}/${action}`, {});
      toast(`${action}: ${payload.ok === false ? (payload.error || "failed") : "done"}`, payload.ok === false ? "error" : "ok");
      refresh();
    } catch (error) { toast(error.message, "error"); }
  }

  async function revoke(credentialName) {
    if (!(await confirmDialog(`Revoke ${credentialName}?`, "Tools lose access immediately; the audit trail keeps the history.", { confirmLabel: "Revoke" }))) return;
    try {
      await api.post(`/api/security/credentials/${encodeURIComponent(credentialName)}/revoke`, {});
      toast("Revoked", "ok");
      refresh();
    } catch (error) { toast(error.message, "error"); }
  }

  async function remove(credentialName) {
    if (!(await confirmDialog(`Delete ${credentialName}?`, "The encrypted material is destroyed. This cannot be undone.", { confirmLabel: "Delete" }))) return;
    try {
      await api.del(`/api/security/credentials/${encodeURIComponent(credentialName)}`);
      toast("Deleted", "ok");
      refresh();
    } catch (error) { toast(error.message, "error"); }
  }

  container.append(statsHost,
    section("Add a credential",
      el("div", { class: "row tight" }, name, kind,
        el("div", { style: "flex:1 1 16rem" }, secret),
        notes,
        el("button", {
          class: "btn btn--primary", onclick: async () => {
            if (!name.value.trim() || !secret.value) { toast("Name and secret are required", "error"); return; }
            try {
              await api.post("/api/security/credentials", {
                name: name.value.trim(), kind: kind.value, secret: secret.value,
                metadata: notes.value ? { notes: notes.value } : {},
              });
              secret.value = ""; notes.value = "";
              toast("Stored encrypted", "ok");
              refresh();
            } catch (error) { toast(error.message, "error"); }
          },
        }, "Store"))),
    section("Credentials", credentialsHost),
    section("Browser sessions", sessionsHost),
    section("Credential broker", brokerHost),
    section("Security audit", auditHost));
  await refresh();
}

register({
  id: "security", title: "Security", icon: "🔒",
  subtitle: "credentials, sessions, broker, audit",
  render: renderSecurity,
});
