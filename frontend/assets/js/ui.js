/* DOM helpers, formatters and overlays. Small on purpose: no framework, no build step. */

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "value") node.value = value;
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

export function fmtBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

export function fmtTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return date.toLocaleString();
}

export function fmtDuration(ms) {
  const value = Number(ms || 0);
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(1)} s`;
  return `${Math.floor(value / 60_000)}m ${Math.round((value % 60_000) / 1000)}s`;
}

export function titleCase(text) {
  return String(text || "").replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function riskChip(risk) {
  const name = String(risk || "LOW").toUpperCase();
  const klass = { CRITICAL: "chip--critical", HIGH: "chip--danger", MEDIUM: "chip--warn" }[name] || "chip--ok";
  return el("span", { class: `chip ${klass}` }, name.toLowerCase());
}

export function statusChip(ok, okText = "healthy", badText = "degraded") {
  return el("span", { class: `chip ${ok ? "chip--ok" : "chip--danger"}` }, ok ? okText : badText);
}

/* ---------- toasts & modal ---------- */
export function toast(message, kind = "info", timeout = 4200) {
  const host = document.getElementById("toasts");
  const node = el("div", { class: `toast toast--${kind}`, role: "status" }, String(message));
  host.append(node);
  setTimeout(() => node.remove(), timeout);
}

export function modal({ title, body, actions = [], onClose }) {
  const root = document.getElementById("modal-root");
  const close = () => { clear(root); if (onClose) onClose(); };
  const box = el("div", { class: "modal__box", role: "dialog", "aria-modal": "true" },
    el("div", { class: "modal__head" },
      el("h3", {}, title || ""),
      el("button", { class: "icon-btn", title: "Close", onclick: close }, "✕")),
    body,
    actions.length ? el("div", { class: "row tight", style: "margin-top:.8rem;justify-content:flex-end" },
      ...actions.map((action) => el("button", {
        class: `btn ${action.kind === "primary" ? "btn--primary" : action.kind === "danger" ? "btn--danger" : ""}`,
        onclick: async () => {
          if (action.onClick) { const keep = await action.onClick(); if (keep === true) return; }
          close();
        },
      }, action.label))) : null);
  const overlay = el("div", { class: "modal", onclick: (event) => { if (event.target === overlay) close(); } }, box);
  clear(root).append(overlay);
  return { close };
}

export function confirmDialog(title, message, { confirmLabel = "Confirm", danger = true } = {}) {
  return new Promise((resolve) => {
    modal({
      title,
      body: el("p", {}, message),
      actions: [
        { label: "Cancel", onClick: () => resolve(false) },
        { label: confirmLabel, kind: danger ? "danger" : "primary", onClick: () => resolve(true) },
      ],
      onClose: () => resolve(false),
    });
  });
}

/* ---------- safe markdown ---------- */
export function markdown(text) {
  const source = String(text || "");
  const escape = (value) => value.replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const lines = escape(source).split("\n");
  let html = "";
  let inCode = false;
  let listOpen = null;
  const closeList = () => { if (listOpen) { html += `</${listOpen}>`; listOpen = null; } };
  for (const raw of lines) {
    const line = raw.replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    if (line.trim().startsWith("```")) {
      closeList();
      html += inCode ? "</pre>" : "<pre>";
      inCode = !inCode;
      continue;
    }
    if (inCode) { html += line + "\n"; continue; }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { closeList(); html += `<h${heading[1].length + 1}>${heading[2]}</h${heading[1].length + 1}>`; continue; }
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) {
      if (listOpen !== "ul") { closeList(); html += "<ul>"; listOpen = "ul"; }
      html += `<li>${bullet[1]}</li>`;
      continue;
    }
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (numbered) {
      if (listOpen !== "ol") { closeList(); html += "<ol>"; listOpen = "ol"; }
      html += `<li>${numbered[1]}</li>`;
      continue;
    }
    closeList();
    if (line.trim() === "") continue;
    if (line.startsWith("&gt; ")) { html += `<blockquote>${line.slice(5)}</blockquote>`; continue; }
    html += `<p>${line}</p>`;
  }
  closeList();
  if (inCode) html += "</pre>";
  return html;
}

/* ---------- common blocks ---------- */
export function loading(label = "Loading…") {
  return el("div", { class: "empty" }, el("span", { class: "spinner" }), " ", label);
}

export function errorBlock(error, retry) {
  return el("div", { class: "empty error" },
    String(error && error.message ? error.message : error),
    retry ? el("div", { style: "margin-top:.6rem" },
      el("button", { class: "btn", onclick: retry }, "Retry")) : null);
}

export function kv(pairs) {
  const dl = el("dl", { class: "kv" });
  for (const [key, value] of pairs) {
    if (value === undefined || value === null || value === "") continue;
    dl.append(el("dt", {}, titleCase(key)), el("dd", {}, value instanceof Node ? value : String(value)));
  }
  return dl;
}

export function tabs(definitions, { onChange } = {}) {
  const bar = el("div", { class: "tabs", role: "tablist" });
  let active = definitions[0]?.id;
  const render = () => {
    clear(bar);
    for (const tab of definitions) {
      bar.append(el("button", {
        class: tab.id === active ? "active" : "",
        role: "tab",
        "aria-selected": String(tab.id === active),
        onclick: () => { active = tab.id; render(); onChange && onChange(tab.id); },
      }, tab.label));
    }
  };
  render();
  return { node: bar, get value() { return active; }, set value(next) { active = next; render(); } };
}

export function download(name, text, mime = "text/plain") {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
