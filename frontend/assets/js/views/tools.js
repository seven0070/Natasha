/* Tool registry and MCP servers: what can be run, by whom, and with which approval. */

import { api } from "../api.js";
import { el, clear, fmtTime, toast, modal, kv } from "../ui.js";
import { section, table, stat, json, riskBadge, emptyState } from "../components.js";
import { register } from "../router.js";

async function runTool(tool, refresh) {
  const payloadInput = el("textarea", { placeholder: "arguments (JSON)" });
  const approvalId = el("input", { placeholder: "approval id (required for HIGH/CRITICAL risk)" });
  const host = el("div", {});
  modal({
    title: `Run ${tool.name}`,
    body: el("div", {},
      kv([["capability", tool.capability], ["risk", tool.risk],
          ["requires approval", tool.requires_approval ? "yes" : "no"],
          ["reversible", tool.reversible ? "yes" : "no"], ["source", tool.source]]),
      el("label", {}, "Arguments"), payloadInput,
      el("label", {}, "Approval (if the policy demands one)"), approvalId,
      el("button", {
        class: "btn btn--primary", style: "margin-top:.5rem", onclick: async () => {
          let parsed = {};
          try { parsed = JSON.parse(payloadInput.value || "{}"); }
          catch { toast("Arguments must be valid JSON", "error"); return; }
          try {
            const result = await api.post(`/api/tools/${encodeURIComponent(tool.name)}`, {
              arguments: parsed, approval_id: approvalId.value.trim(), actor: "owner",
            });
            clear(host).append(json(result));
            refresh();
          } catch (error) {
            clear(host).append(el("div", { class: "empty error" }, error.message));
          }
        },
      }, "Execute"),
      host),
  });
}

export async function renderTools(container) {
  const [toolsPayload, mcpPayload] = await Promise.all([api.get("/api/tools"), api.get("/api/mcp")]);
  const tools = toolsPayload.tools || [];
  const servers = mcpPayload.servers || mcpPayload.mcp || [];

  const toolsHost = el("div", {});
  clear(toolsHost).append(table([
    { label: "tool", value: (row) => el("strong", {}, row.name) },
    { label: "capability", value: "capability" },
    { label: "risk", value: (row) => riskBadge(row.risk) },
    { label: "approval", value: (row) => (row.requires_approval ? "required" : "policy decides") },
    { label: "source", value: "source" },
    { label: "actions", value: (row) => el("div", { class: "row tight" },
      el("button", { class: "btn small", onclick: () => runTool(row, () => renderTools(container)) }, "Run"),
      el("button", {
        class: "btn small", onclick: () => modal({ title: row.name, body: json(row) }),
      }, "Schema")) },
  ], tools, { empty: "No tools registered." }));

  const mcpHost = el("div", {});
  function renderServers() {
    clear(mcpHost);
    if (!servers.length) {
      mcpHost.append(emptyState("No MCP servers registered. Servers are sandboxed and their tools are treated as untrusted."));
    }
    servers.forEach((server) => {
      const name = server.name;
      const card = el("div", { class: "card" },
        el("div", { class: "card__head" },
          el("span", { class: `chip ${server.enabled === false ? "" : "chip--ok"}` },
            server.enabled === false ? "disabled" : (server.status || "enabled")),
          el("strong", {}, name),
          el("span", { class: "chip" }, server.transport || "stdio"),
          server.tools_count ? el("span", { class: "small muted", style: "margin-left:auto" },
            `${server.tools_count} tools`) : null),
        kv([["command", (server.command || []).join(" ")], ["url", server.url],
            ["trust", server.trust || "untrusted"], ["added", fmtTime(server.added_at)]]),
        el("div", { class: "row tight", style: "margin-top:.4rem" },
          el("button", {
            class: "btn small", onclick: async () => {
              try {
                const payload = await api.get(`/api/mcp/${encodeURIComponent(name)}/health`);
                modal({ title: `${name} health`, body: json(payload) });
              } catch (error) { toast(error.message, "error"); }
            },
          }, "Health"),
          el("button", {
            class: "btn small", onclick: async () => {
              try {
                const payload = await api.get(`/api/mcp/${encodeURIComponent(name)}/tools`);
                modal({ title: `${name} tools`, body: json(payload) });
              } catch (error) { toast(error.message, "error"); }
            },
          }, "Tools"),
          el("button", {
            class: "btn small", onclick: async () => {
              try {
                await api.post(`/api/mcp/${encodeURIComponent(name)}/${server.enabled === false ? "enable" : "disable"}`, {});
                toast("Updated", "ok");
                renderTools(container);
              } catch (error) { toast(error.message, "error"); }
            },
          }, server.enabled === false ? "Enable" : "Disable")));
      if ((server.tools || []).length) {
        card.append(json(server.tools.slice(0, 30), { label: "advertised tools" }));
      }
      mcpHost.append(card);
    });
  }
  renderServers();

  const installName = el("input", { placeholder: "server name" });
  const installCommand = el("input", { placeholder: "command, e.g. python3 -m my_mcp_server" });
  const installUrl = el("input", { placeholder: "or a url (sse/http transport)" });
  const installTransport = el("select", {},
    el("option", { value: "stdio" }, "stdio"),
    el("option", { value: "sse" }, "sse"),
    el("option", { value: "http" }, "http"));

  container.append(el("div", { class: "grid", style: "margin-bottom:.8rem" },
    stat("tools", tools.length, "registered capabilities"),
    stat("approval-gated", tools.filter((tool) => tool.requires_approval).length, "always ask first"),
    stat("MCP servers", servers.length, "external tool providers")),
    section("Tools", toolsHost),
    section("Install an MCP server",
      el("div", { class: "row tight" }, installName, installCommand, installUrl, installTransport,
        el("button", {
          class: "btn btn--primary", onclick: async () => {
            const command = installCommand.value.trim().split(/\s+/).filter(Boolean);
            try {
              await api.post("/api/mcp", {
                name: installName.value.trim(), command, url: installUrl.value.trim(),
                transport: installTransport.value,
              });
              toast("Server registered. Its tools are untrusted until you enable it.", "ok");
              renderTools(container);
            } catch (error) { toast(error.message, "error"); }
          },
        }, "Install")),
      el("div", { class: "small muted" }, "External tools may be malicious: their output is data, never instructions.")),
    section("MCP servers", mcpHost));
}

register({
  id: "tools", title: "Tools & MCP", icon: "🧰",
  subtitle: "registry, risk, approvals, external servers",
  render: renderTools,
});
