/* Shared building blocks used by more than one screen. */

import { el, clear, kv, markdown, titleCase, fmtTime, fmtBytes, riskChip, toast, confirmDialog } from "./ui.js";
import { api } from "./api.js";

export function section(title, ...children) {
  return el("div", { class: "card" },
    title ? el("div", { class: "card__head" }, el("h3", {}, title)) : null,
    ...children);
}

export function table(columns, rows, { empty = "Nothing here yet." } = {}) {
  if (!rows.length) return el("div", { class: "empty" }, empty);
  const head = el("tr", {}, ...columns.map((column) => el("th", { style: "text-align:left" }, column.label)));
  const body = rows.map((row) => el("tr", {},
    ...columns.map((column) => {
      const value = typeof column.value === "function" ? column.value(row) : row[column.value];
      return el("td", { style: "padding:.35rem .5rem;vertical-align:top;border-top:1px solid var(--border-soft)" },
        value instanceof Node ? value : (value === undefined || value === null ? "" : String(value)));
    })));
  return el("div", { style: "overflow:auto" },
    el("table", { style: "width:100%;border-collapse:collapse;font-size:.88rem" },
      el("thead", {}, head), el("tbody", {}, ...body)));
}

export function json(value, { label = "details" } = {}) {
  const node = el("details", {}, el("summary", { class: "small muted" }, label));
  node.append(el("pre", {}, JSON.stringify(value, null, 2)));
  return node;
}

export function chipRow(...values) {
  return el("div", { class: "row tight", style: "gap:.3rem" }, ...values.filter(Boolean));
}

export function riskBadge(risk) {
  return riskChip(risk || "LOW");
}

/** The approval card: what is being asked, why, what it touches, and the three honest answers. */
export function approvalCard(request, { onDecided } = {}) {
  const details = request.arguments || request.metadata || {};
  const resources = request.resources || [];
  const permissions = request.permissions || [];
  const node = el("div", { class: "approval-card" },
    el("div", { class: "row", style: "align-items:center;gap:.5rem" },
      el("strong", {}, request.operation || "action"),
      riskBadge(request.risk),
      request.mission_id ? el("span", { class: "chip" }, `mission ${String(request.mission_id).slice(0, 8)}`) : null,
      el("span", { class: "small muted", style: "margin-left:auto" }, fmtTime(request.created_at))),
    el("p", { class: "small", style: "margin:.4rem 0" }, request.reason || "no reason recorded"),
    permissions.length ? el("div", { class: "small muted" }, `permissions: ${permissions.join(", ")}`) : null,
    resources.length ? el("div", { class: "small muted" }, `resources: ${resources.join(", ")}`) : null,
    Object.keys(details).length ? json(details, { label: "arguments" }) : null,
    el("div", { class: "row tight", style: "margin-top:.5rem" },
      el("button", {
        class: "btn btn--primary", onclick: async (event) => {
          event.target.disabled = true;
          try {
            const scope = window.prompt("Scope for this approval (blank = exactly this request)",
              `${request.operation} ${resources.join(" ")}`.trim());
            const result = await api.post(`/api/approvals/${request.id}/approve`,
              { note: "approved in the web console", scope: scope || "" });
            toast("Approved. The owner alone can trigger it.", "ok");
            onDecided && onDecided(result);
          } catch (error) { toast(error.message, "error"); event.target.disabled = false; }
        },
      }, "Approve once"),
      el("button", {
        class: "btn btn--danger", onclick: async () => {
          try {
            await api.post(`/api/approvals/${request.id}/deny`, { note: "denied in the web console" });
            toast("Denied.", "ok");
            onDecided && onDecided();
          } catch (error) { toast(error.message, "error"); }
        },
      }, "Deny"),
      el("button", {
        class: "btn", onclick: async () => {
          if (!(await confirmDialog("Revoke this approval?", "Any pending authorisation for this request is withdrawn.", { confirmLabel: "Revoke" }))) return;
          try {
            await api.post(`/api/approvals/${request.id}/revoke`, { note: "revoked in the web console" });
            toast("Revoked.", "ok");
            onDecided && onDecided();
          } catch (error) { toast(error.message, "error"); }
        },
      }, "Revoke")));
  return node;
}

export function toolCallCard(call, { onOpenArtifact } = {}) {
  const ok = Boolean(call.ok);
  const node = el("div", { class: "tool-card" },
    el("div", { class: "tool-card__head" },
      el("span", { class: `chip ${ok ? "chip--ok" : "chip--danger"}` }, ok ? "tool ok" : "tool failed"),
      el("strong", {}, call.tool || call.name || "tool"),
      call.risk ? riskBadge(call.risk) : null,
      el("span", { class: "small muted", style: "margin-left:auto" },
        call.duration_ms ? `${Math.round(call.duration_ms)} ms` : "")),
    call.error ? el("p", { class: "small error" }, call.error) : null,
    (call.artifacts || []).length ? el("div", { class: "row tight", style: "margin-top:.3rem" },
      ...(call.artifacts || []).map((artifact) => el("button", {
        class: "btn small", onclick: () => onOpenArtifact && onOpenArtifact(artifact),
      }, `artifact ${String(artifact.name || artifact.path || "").split("/").pop()}`))) : null,
    el("details", {}, el("summary", { class: "small muted" }, "arguments & output"),
      el("pre", {}, JSON.stringify({ arguments: call.arguments, result: call.result }, null, 2))));
  return node;
}

export function artifactCard(artifact, { onPreview } = {}) {
  return el("div", { class: "card" },
    el("div", { class: "card__head" },
      el("span", { class: "chip" }, artifact.kind || "file"),
      el("strong", {}, artifact.name || String(artifact.path || "").split("/").pop()),
      el("span", { class: "small muted", style: "margin-left:auto" }, fmtBytes(artifact.bytes))),
    el("div", { class: "small muted mono" }, artifact.relative || artifact.path || ""),
    kv([["mime", artifact.mime], ["modified", fmtTime(artifact.modified_at)],
        ["mission", artifact.mission_id], ["sha256", String(artifact.sha256 || "").slice(0, 16)]]),
    el("div", { class: "row tight", style: "margin-top:.4rem" },
      onPreview ? el("button", { class: "btn small", onclick: () => onPreview(artifact) }, "Preview") : null,
      el("a", { class: "btn small", href: `/api/artifacts/download?path=${encodeURIComponent(artifact.path || "")}`,
                target: "_blank" }, "Download")));
}

export function emptyState(message, action) {
  return el("div", { class: "empty" }, message, action || null);
}

export function loadingBlock(label = "Loading…") {
  return el("div", { class: "empty" }, el("span", { class: "spinner" }), " ", label);
}

export async function withLoading(container, loader, renderer) {
  clear(container).append(loadingBlock());
  try {
    const data = await loader();
    clear(container);
    renderer(data);
    return data;
  } catch (error) {
    clear(container).append(el("div", { class: "empty error" }, error.message || String(error)));
    return null;
  }
}

export function markdownBlock(text) {
  const node = el("div", { class: "markdown" });
  node.innerHTML = markdown(text || "");
  return node;
}

export function stat(label, value, hint = "") {
  return el("div", { class: "card" },
    el("div", { class: "small muted" }, titleCase(label)),
    el("div", { style: "font-size:1.4rem;font-weight:650" }, String(value ?? "—")),
    hint ? el("div", { class: "small muted" }, hint) : null);
}

export function labelledInput(label, input) {
  return el("div", {}, el("label", {}, label), input);
}
