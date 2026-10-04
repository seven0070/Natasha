/* Settings: features, security posture, brain defaults, paths - everything the runtime really uses. */

import { api } from "../api.js";
import { el, clear, toast, kv, download } from "../ui.js";
import { section, json, stat } from "../components.js";
import { register } from "../router.js";
import { store } from "../store.js";

function textField(label, value, onChange, { type = "text" } = {}) {
  const input = el("input", { type, value: value === undefined || value === null ? "" : String(value) });
  input.addEventListener("change", () => onChange(input.value));
  return el("div", {}, el("label", {}, label), input);
}

function numberField(label, value, onChange, { step = "1" } = {}) {
  const input = el("input", { type: "number", step, value: value === undefined || value === null ? "" : String(value) });
  input.addEventListener("change", () => onChange(Number(input.value)));
  return el("div", {}, el("label", {}, label), input);
}

function toggle(label, value, onChange) {
  const input = el("input", { type: "checkbox", checked: value ? "checked" : "" });
  input.addEventListener("change", () => onChange(input.checked));
  return el("label", { style: "display:flex;gap:.5rem;align-items:center;margin:.3rem 0" }, input, label);
}

export async function renderSettings(container) {
  const payload = await api.get("/api/settings");
  const draft = { updates: {} };
  const featureHost = el("div", {});
  const providerHost = el("div", {});

  function collect(path, value) {
    draft.updates[path] = value;
  }

  for (const [name, enabled] of Object.entries(payload.features || {})) {
    featureHost.append(toggle(name, enabled, (checked) => collect(`features.${name}`, checked)));
  }

  providerHost.append(el("div", { class: "small muted" },
    "Providers are configured on the Providers screen. These are the values the runtime reads from configuration:"),
    ...Object.entries(payload.providers || {}).map(([name, provider]) => el("div", { class: "card" },
      el("div", { class: "card__head" },
        el("strong", {}, name),
        el("span", { class: `chip ${provider.enabled ? "chip--ok" : ""}` }, provider.enabled ? "enabled" : "disabled"),
        el("span", { class: "chip" }, provider.local ? "local" : "cloud")),
      el("div", { class: "row" },
        textField("base url", provider.base_url, (value) => collect(`providers.${name}.base_url`, value)),
        textField("default model", provider.default_model, (value) => collect(`providers.${name}.default_model`, value)),
        numberField("timeout", provider.timeout_seconds, (value) => collect(`providers.${name}.timeout_seconds`, value)),
        textField("credential reference", provider.credential_ref, (value) => collect(`providers.${name}.credential_ref`, value))))));

  const security = payload.security || {};
  const memory = payload.memory || {};
  const brain = payload.brain || {};
  const executive = payload.executive || {};

  container.append(
    el("div", { class: "grid", style: "margin-bottom:.8rem" },
      stat("home", String(payload.paths?.home || "").split("/").pop(), "data directory"),
      stat("deployment", payload.deployment, `${payload.host}:${payload.port}`),
      stat("auth", payload.auth_required ? "required" : "open", `session ttl ${payload.session_ttl_minutes} min`),
      stat("owner", store.ownerId || payload.owner_id || "unset", "the only principal with full authority")),
    section("Runtime",
      el("div", { class: "row" },
        textField("profile", payload.profile, (value) => collect("profile", value)),
        textField("log level", payload.log_level, (value) => collect("log_level", value)),
        numberField("session ttl (minutes)", payload.session_ttl_minutes, (value) => collect("session_ttl_minutes", value)))),
    section("Features", featureHost),
    section("Brain & routing defaults",
      el("div", { class: "row" },
        textField("default provider", brain.default_provider, (value) => collect("brain.default_provider", value)),
        numberField("fallback depth", brain.fallback_depth, (value) => collect("brain.fallback_depth", value)),
        numberField("context window floor", brain.context_window_floor, (value) => collect("brain.context_window_floor", value))),
      toggle("prefer local models", brain.prefer_local, (value) => collect("brain.prefer_local", value)),
      toggle("stream by default", brain.stream, (value) => collect("brain.stream", value))),
    section("Executive limits",
      el("div", { class: "row" },
        numberField("max steps", executive.max_steps, (value) => collect("executive.max_steps", value)),
        numberField("max repairs", executive.max_repairs, (value) => collect("executive.max_repairs", value)),
        numberField("step timeout (s)", executive.step_timeout_seconds, (value) => collect("executive.step_timeout_seconds", value))),
      toggle("require verification before success", executive.require_verification,
        (value) => collect("executive.require_verification", value)),
      toggle("allow unverified success", executive.allow_unverified_success,
        (value) => collect("executive.allow_unverified_success", value))),
    section("Memory",
      el("div", { class: "row" },
        textField("database backend", memory.db_backend, (value) => collect("memory.db_backend", value)),
        numberField("retrieval k", memory.retrieval_k, (value) => collect("memory.retrieval_k", value)),
        numberField("embedding dimensions", memory.embed_dim, (value) => collect("memory.embed_dim", value)),
        numberField("decay half-life (days)", memory.decay_half_life_days, (value) => collect("memory.decay_half_life_days", value)))) ,
    section("Providers", providerHost),
    section("Security posture (read-only here)",
      kv([["default effect", security.default_effect],
          ["approval required for", (security.require_approval_for || []).join(", ")],
          ["filesystem read roots", (security.fs_read_allow || []).length],
          ["filesystem write roots", (security.fs_write_allow || []).length],
          ["denied globs", (security.fs_deny_globs || []).length],
          ["allowed domains", (security.network_allow_domains || []).length],
          ["command allow-list", (security.command_allowlist || []).slice(0, 8).join(" ")]]),
      el("div", { class: "small muted" },
        "These are enforced in code, outside the model. Edit configuration files to change them; the UI cannot loosen security."),
      json(security, { label: "full security configuration" })),
    section("Paths", json(payload.paths || {}, { label: "runtime directories" })),
    el("div", { class: "row tight", style: "margin-top:1rem" },
      el("button", {
        class: "btn btn--primary", onclick: async () => {
          try {
            await api.patch("/api/settings", { updates: draft.updates });
            toast("Settings saved", "ok");
            await store.refreshStatus();
          } catch (error) { toast(error.message, "error"); }
        },
      }, "Save changes"),
      el("button", {
        class: "btn", onclick: async () => {
          try {
            const result = await api.post("/api/settings/save", {});
            toast(`Written to ${result.path || "the config file"}`, "ok");
          } catch (error) { toast(error.message, "error"); }
        },
      }, "Write config file"),
      el("button", {
        class: "btn", onclick: async () => {
          const doctor = await api.get("/api/doctor");
          download(`natasha-doctor-${Date.now()}.json`, JSON.stringify(doctor, null, 2), "application/json");
        },
      }, "Download doctor report")));

  const doctorHost = el("div", {});
  container.append(section("Doctor", doctorHost));
  try {
    const doctor = await api.get("/api/doctor");
    clear(doctorHost).append(el("div", { class: "row tight" },
      el("span", { class: `chip ${doctor.overall === "ok" ? "chip--ok" : "chip--warn"}` }, doctor.overall),
      el("span", { class: "small muted" }, `home: ${doctor.home}`)),
      json(doctor, { label: "findings and capabilities" }));
  } catch (error) {
    clear(doctorHost).append(el("div", { class: "small error" }, error.message));
  }
}

register({
  id: "settings", title: "Settings", icon: "⚙️",
  subtitle: "runtime configuration, features, security posture",
  render: renderSettings,
});
