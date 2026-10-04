/* Computer use: OBSERVE -> PLAN -> PERMISSION -> ACT -> OBSERVE -> VERIFY. */

import { api } from "../api.js";
import { el, clear, fmtTime, toast, kv, riskChip } from "../ui.js";
import { section, table, stat, json, riskBadge } from "../components.js";
import { register } from "../router.js";

export async function renderComputer(container) {
  const capabilities = await api.get("/api/computer/capabilities");
  const computer = capabilities.computer || {};
  const browser = capabilities.browser || {};
  const result = el("div", {});
  const history = el("div", { "data-history": "1" });

  const x = el("input", { type: "number", placeholder: "x", value: "100" });
  const y = el("input", { type: "number", placeholder: "y", value: "100" });
  const text = el("input", { placeholder: "text to type or key to press (enter, tab, ctrl+s)" });
  const command = el("input", { placeholder: "program to launch (e.g. xdg-open https://example.com)" });
  const url = el("input", { placeholder: "https://…" });
  const selector = el("input", { placeholder: "css selector (for click / fill)" });

  function show(payload, label) {
    clear(result).append(section(label, payload.ok === false
      ? el("div", { class: "empty error" }, payload.error || "the action did not complete")
      : null, json(payload)));

    if (payload && payload.approval_request_id) {
      clear(result).append(section(label, el("div", { class: "approval-card" },
        el("strong", {}, "This action needs your approval before it can touch the desktop"),
        el("div", { class: "small" }, `risk ${payload.risk || "HIGH"} - approval ${payload.approval_request_id}`),
        el("div", { class: "small muted" }, "Open Approvals, decide, then run the action again with the approval id."))));
    }
  }

  async function act(action, payload, label) {
    try { show(await api.post(action, payload), label || action); }
    catch (error) { clear(result).append(section(label || action, el("div", { class: "empty error" }, error.message))); }
    refreshHistory();
  }

  async function refreshHistory() {
    try {
      const payload = await api.get("/api/computer/history", { limit: 40 });
      clear(history).append(table([
        { label: "when", value: (row) => fmtTime(row.at || row.ts) },
        { label: "action", value: "action" },
        { label: "target", value: (row) => row.target || row.url || row.text || "" },
        { label: "ok", value: (row) => (row.ok ? "yes" : `no: ${row.error || ""}`) },
        { label: "artifact", value: (row) => String(row.path || "").split("/").pop() },
      ], payload.actions || [], { empty: "No computer actions recorded yet." }));
    } catch (error) { clear(history).append(el("div", { class: "small error" }, error.message)); }
  }

  container.append(
    el("div", { class: "grid", style: "margin-bottom:.8rem" },
      stat("screen backend", computer.backend || "none", computer.available ? "available" : computer.reason || "unavailable"),
      stat("browser backend", browser.backend || "none", browser.available ? "available" : "not available"),
      stat("mouse + keyboard", computer.actions ? Object.entries(computer.actions).filter(([, value]) => value).length + " actions" : "unavailable"),
      stat("javascript", browser.javascript ? "available" : "not available", (browser.can || []).join(", "))),
    el("div", { class: "split" },
      el("div", {},
        section("Observe",
          el("div", { class: "row tight" },
            el("button", { class: "btn", onclick: () => act("/api/computer/screenshot", {}, "screenshot") }, "Screenshot"),
            el("button", { class: "btn", onclick: async () => {
              try { const payload = await api.get("/api/computer/screen/size"); show(payload, "screen size"); }
              catch (error) { toast(error.message, "error"); }
            } }, "Screen size"),
            el("button", { class: "btn", onclick: async () => {
              try { show(await api.get("/api/computer/clipboard"), "clipboard"); }
              catch (error) { toast(error.message, "error"); }
            } }, "Read clipboard"))),
        section("Act (needs approval above MEDIUM risk)",
          el("div", { class: "row tight" }, x, y,
            el("button", { class: "btn", onclick: () => act("/api/computer/click",
              { x: Number(x.value), y: Number(y.value) }, "click") }, "Click")),
          el("div", { class: "row tight", style: "margin-top:.4rem" }, text,
            el("button", { class: "btn", onclick: () => act("/api/computer/type", { text: text.value }, "type") }, "Type"),
            el("button", { class: "btn", onclick: () => act("/api/computer/key", { key: text.value }, "key") }, "Press key")),
          el("div", { class: "row tight", style: "margin-top:.4rem" }, command,
            el("button", { class: "btn", onclick: () => act("/api/computer/launch",
              { command: command.value.split(/\s+/).filter(Boolean) }, "launch") }, "Launch application"))),
        section("Browser",
          el("div", { class: "row tight" }, url,
            el("button", { class: "btn", onclick: () => act("/api/computer/browser/open", { url: url.value }, "open") }, "Open"),
            el("button", { class: "btn", onclick: () => act("/api/computer/browser/read",
              { url: url.value, max_chars: 20000 }, "read") }, "Read"),
            el("button", { class: "btn", onclick: () => act("/api/computer/browser/screenshot",
              { url: url.value }, "browser screenshot") }, "Screenshot")),
          el("div", { class: "row tight", style: "margin-top:.4rem" }, selector,
            el("button", { class: "btn", onclick: () => act("/api/computer/browser/click",
              { url: url.value, selector: selector.value }, "browser click") }, "Click element")))),
      el("div", {}, section("Result", result), section("Action log", history))));
  await refreshHistory();
}

register({
  id: "computer", title: "Computer", icon: "🖥",
  subtitle: "observe, plan, permission, act, verify",
  render: renderComputer,
});

export { riskChip, riskBadge, kv };
