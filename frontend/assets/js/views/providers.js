/* Providers & models: discovery, health, routing, local-first preferences. */

import { api } from "../api.js";
import { el, clear, fmtTime, toast, kv } from "../ui.js";
import { section, table, stat, json } from "../components.js";
import { register } from "../router.js";

const LOCAL_HINT = {
  ollama: "ollama serve",
  llamacpp: "llama.cpp server (GGUF)",
  lmstudio: "LM Studio local server",
  vllm: "vLLM OpenAI-compatible server",
  openai_compatible: "any OpenAI-compatible local endpoint",
};

function providerCard(provider, refresh) {
  const health = provider.health || {};
  const healthy = health.ok !== false && health.healthy !== false;
  return el("div", { class: "card" },
    el("div", { class: "card__head" },
      el("span", { class: `chip ${provider.enabled ? (healthy ? "chip--ok" : "chip--danger") : ""}` },
        provider.enabled ? (healthy ? "enabled" : "unhealthy") : "disabled"),
      el("strong", {}, provider.name),
      provider.local ? el("span", { class: "chip chip--warn" }, "local") : el("span", { class: "chip" }, "cloud"),
      el("span", { class: "chip" }, provider.privacy_tier || "unknown")),
    kv([["base url", provider.base_url], ["credential", provider.has_credential ? "stored in the vault" : "not set"],
        ["tools", provider.supports_tools ? "yes" : "no"], ["streaming", provider.supports_streaming ? "yes" : "no"],
        ["vision", provider.supports_vision ? "yes" : "no"], ["embeddings", provider.supports_embeddings ? "yes" : "no"],
        ["checked", health.checked_at ? fmtTime(health.checked_at) : ""],
        ["detail", health.detail || health.error || ""],
        ["models", (provider.models || []).map((model) => model.id || model.name || model).join(", ")]]),
    el("div", { class: "row tight", style: "margin-top:.5rem" },
      el("button", { class: "btn small", onclick: () => act("check") }, "Health check"),
      el("button", { class: "btn small", onclick: () => act("discover") }, "Discover models"),
      el("button", { class: "btn small", onclick: () => act(provider.enabled ? "disable" : "enable") },
        provider.enabled ? "Disable" : "Enable"),
      LOCAL_HINT[provider.name] ? el("span", { class: "small muted" }, LOCAL_HINT[provider.name]) : null),
    json(provider, { label: "raw provider state" }));

  async function act(action) {
    try {
      const payload = await api.post(`/api/providers/${encodeURIComponent(provider.name)}/${action}`, {});
      toast(`${provider.name}: ${action} ${payload.ok === false ? (payload.error || "failed") : "done"}`,
        payload.ok === false ? "error" : "ok");
      refresh();
    } catch (error) { toast(error.message, "error"); }
  }
}

export async function renderProviders(container) {
  const cards = el("div", { class: "grid" });
  const stats = el("div", { class: "grid", style: "margin-bottom:.8rem" });
  const routingHost = el("div", {});

  async function refresh() {
    const payload = await api.get("/api/providers");
    const providers = payload.providers || [];
    const routing = payload.routing || {};
    clear(stats).append(
      stat("brain", payload.brain_available ? "available" : "offline placeholder",
        `${(payload.models || []).length} models across ${providers.length} providers`),
      stat("prefer local", routing.prefer_local ? "yes" : "no", `fallback depth ${routing.fallback_depth}`),
      stat("default provider", routing.default_provider || "auto", "chosen by routing weights"),
      stat("usage (recent)", Object.keys(payload.recent_usage || {}).length, "providers with recorded usage"));
    clear(cards);
    providers.forEach((provider) => cards.append(providerCard(provider, refresh)));

    const preferLocal = el("input", { type: "checkbox", checked: routing.prefer_local ? "checked" : "" });
    const defaultProvider = el("input", { value: routing.default_provider || "", placeholder: "auto" });
    const weights = routing.routing_weights || {};
    const weightInputs = {};
    Object.entries(weights).forEach(([key, value]) => {
      weightInputs[key] = el("input", { type: "number", step: "0.05", min: "0", max: "1", value: String(value) });
    });
    const stream = el("input", { type: "checkbox", checked: routing.stream === false ? "" : "checked" });
    const fallback = el("input", { type: "number", min: "0", max: "6", value: String(routing.fallback_depth ?? 3) });
    clear(routingHost).append(
      el("div", { class: "row tight" },
        el("label", { style: "display:flex;gap:.4rem;align-items:center" }, preferLocal, "prefer local models"),
        el("div", { style: "flex:0 0 12rem" }, el("label", {}, "default provider"), defaultProvider),
        el("label", { style: "display:flex;gap:.4rem;align-items:center" }, stream, "stream replies"),
        el("div", { style: "flex:0 0 8rem" }, el("label", {}, "fallback depth"), fallback)),
      el("div", { class: "row tight", style: "margin-top:.5rem" },
        ...Object.entries(weightInputs).map(([key, input]) =>
          el("div", { style: "flex:0 0 7rem" }, el("label", {}, `${key} weight`), input))),
      el("button", {
        class: "btn btn--primary", style: "margin-top:.5rem", onclick: async () => {
          const payload2 = { prefer_local: preferLocal.checked, default_provider: defaultProvider.value.trim(),
                            stream: stream.checked, fallback_depth: Number(fallback.value || 3),
                            routing_weights: Object.fromEntries(Object.entries(weightInputs)
                              .map(([key, input]) => [key, Number(input.value)])) };
          try {
            await api.post("/api/providers/routing", payload2);
            toast("Routing updated", "ok");
            refresh();
          } catch (error) { toast(error.message, "error"); }
        },
      }, "Save routing"));
  }

  container.append(stats, cards, section("Routing & local-first policy", routingHost));
  await refresh();
}

register({
  id: "providers", title: "Providers & Models", icon: "🧠",
  subtitle: "local models, cloud providers, routing, health",
  render: renderProviders,
});

export { table };
