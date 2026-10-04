/* Integrations: connectors that use credentials the broker holds, never the browser. */

import { api } from "../api.js";
import { el, clear, toast, kv } from "../ui.js";
import { section, table, json } from "../components.js";
import { register } from "../router.js";

export async function renderIntegrations(container) {
  const host = el("div", { class: "grid" });
  const resultHost = el("div", {});

  async function refresh() {
    const payload = await api.get("/api/integrations");
    const connectors = payload.connectors || [];
    clear(host);
    if (!connectors.length) {
      host.append(el("div", { class: "empty" }, "No connectors are registered."));
      return;
    }
    for (const connector of connectors) {
      const reference = el("input", { placeholder: "credential://name (stored in the vault)",
        value: connector.credential_ref || "" });
      const settings = el("textarea", { placeholder: '{"base_url": "https://…"}' });
      const enabled = el("input", { type: "checkbox", checked: connector.enabled === false ? "" : "checked" });
      const actions = connector.actions || [];
      const actionSelect = el("select", {}, ...actions.map((action) =>
        el("option", { value: action.name || action }, action.name || action)));
      const actionPayload = el("textarea", { placeholder: "arguments (JSON)" });

      const card = el("div", { class: "card" },
        el("div", { class: "card__head" },
          el("span", { class: `chip ${connector.connected ? "chip--ok" : ""}` },
            connector.connected ? "connected" : "not connected"),
          el("strong", {}, connector.name),
          el("span", { class: "small muted", style: "margin-left:auto" },
            actions.length ? `${actions.length} actions` : "")),
        el("p", { class: "small muted" }, connector.description || ""),
        el("label", {}, "Credential reference"), reference,
        el("label", {}, "Settings"), settings,
        el("label", { style: "display:flex;gap:.4rem;align-items:center" }, enabled, "enabled"),
        el("div", { class: "row tight", style: "margin-top:.5rem" },
          el("button", {
            class: "btn", onclick: async () => {
              let parsed = {};
              try { parsed = settings.value.trim() ? JSON.parse(settings.value) : {}; }
              catch { toast("Settings must be valid JSON", "error"); return; }
              try {
                const payload2 = await api.post(`/api/integrations/${encodeURIComponent(connector.name)}/connect`,
                  { credential_ref: reference.value.trim(), settings: parsed, enabled: enabled.checked });
                clear(resultHost).append(section(`${connector.name} connected`, json(payload2)));
                toast("Connected. Secrets stay in the vault.", "ok");
                refresh();
              } catch (error) { toast(error.message, "error"); }
            },
          }, "Connect"),
          el("button", {
            class: "btn", onclick: async () => {
              try {
                await api.post(`/api/integrations/${encodeURIComponent(connector.name)}/disconnect`, {});
                toast("Disconnected", "ok");
                refresh();
              } catch (error) { toast(error.message, "error"); }
            },
          }, "Disconnect")),
        actions.length ? el("div", {},
          el("label", {}, "Perform an action"), actionSelect, actionPayload,
          el("button", {
            class: "btn", style: "margin-top:.4rem", onclick: async () => {
              let parsed = {};
              try { parsed = actionPayload.value.trim() ? JSON.parse(actionPayload.value) : {}; }
              catch { toast("Arguments must be valid JSON", "error"); return; }
              try {
                const payload2 = await api.post(`/api/integrations/${encodeURIComponent(connector.name)}/perform`,
                  { action: actionSelect.value, arguments: parsed });
                clear(resultHost).append(section(`${connector.name}.${actionSelect.value}`, json(payload2)));
              } catch (error) { toast(error.message, "error"); }
            },
          }, "Perform")) : null,
        json(connector, { label: "connector detail" }));
      host.append(card);
    }
  }

  container.append(section("Connectors",
    el("div", { class: "small muted" }, "Connectors reference credentials; they never receive raw secrets from this page."),
    host), section("Result", resultHost));
  await refresh();
}

register({
  id: "integrations", title: "Integrations", icon: "🔌",
  subtitle: "connectors, credentials by reference",
  render: renderIntegrations,
});

export { table, kv };
