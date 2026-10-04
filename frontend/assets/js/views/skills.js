/* Skills and the marketplace: create -> validate -> scan -> test -> approve -> install -> run. */

import { api } from "../api.js";
import { el, clear, fmtTime, toast, modal, kv } from "../ui.js";
import { section, table, stat, json, emptyState } from "../components.js";
import { register } from "../router.js";

function skillCard(skill, refresh) {
  const id = skill.id || skill.name;
  const node = el("div", { class: "card" },
    el("div", { class: "card__head" },
      el("span", { class: `chip ${skill.enabled === false ? "" : "chip--ok"}` },
        skill.enabled === false ? "disabled" : (skill.state || "active")),
      el("strong", {}, skill.name || id),
      skill.version ? el("span", { class: "chip" }, `v${skill.version}`) : null,
      skill.risk ? el("span", { class: "chip chip--warn" }, skill.risk) : null),
    el("p", { class: "small muted" }, skill.description || ""),
    kv([["id", id], ["source", skill.source], ["author", skill.author],
        ["installed", fmtTime(skill.installed_at || skill.created_at)],
        ["capabilities", (skill.capabilities || skill.permissions || []).join(", ")]]),
    el("div", { class: "row tight", style: "margin-top:.5rem" },
      el("button", { class: "btn small", onclick: () => run() }, "Run"),
      el("button", { class: "btn small", onclick: () => act("test") }, "Test"),
      el("button", { class: "btn small", onclick: () => act("scan") }, "Scan"),
      el("button", { class: "btn small", onclick: () => act(skill.enabled === false ? "enable" : "disable") },
        skill.enabled === false ? "Enable" : "Disable"),
      el("button", { class: "btn small", onclick: () => act("rollback") }, "Roll back")),
    json(skill, { label: "full manifest & state" }));

  async function act(action) {
    try {
      const payload = await api.post(`/api/skills/${encodeURIComponent(id)}/${action}`, {});
      toast(`${action}: ${payload.ok === false ? (payload.error || "failed") : "done"}`,
        payload.ok === false ? "error" : "ok");
      refresh();
    } catch (error) { toast(error.message, "error"); }
  }

  async function run() {
    const payloadInput = el("textarea", { placeholder: '{"input": "value"}' });
    modal({
      title: `Run ${skill.name || id}`,
      body: el("div", {}, el("label", {}, "Payload (JSON)"), payloadInput,
        el("div", { class: "small muted" }, "A skill runs with its declared capabilities only; anything above its grant needs your approval.")),
      actions: [
        { label: "Cancel" },
        { label: "Run", kind: "primary", onClick: async () => {
          let parsed = {};
          try { parsed = JSON.parse(payloadInput.value || "{}"); }
          catch { toast("Payload must be valid JSON", "error"); return true; }
          try {
            const result = await api.post(`/api/skills/${encodeURIComponent(id)}/run`, { payload: parsed });
            modal({ title: `Result of ${id}`, body: json(result) });
          } catch (error) { toast(error.message, "error"); }
        } },
      ],
    });
  }
  return node;
}

export async function renderSkills(container) {
  const listHost = el("div", { class: "grid" });
  const installPath = el("input", { placeholder: "path to a skill directory or archive on this machine" });
  const installName = el("input", { placeholder: "or a marketplace name" });

  async function refresh() {
    const payload = await api.get("/api/skills");
    const skills = payload.skills || [];
    clear(listHost);
    if (!skills.length) {
      listHost.append(emptyState("No skills installed. Skills are sandboxed, scanned and capability-scoped."));
      return;
    }
    skills.forEach((skill) => listHost.append(skillCard(skill, refresh)));
  }

  container.append(el("div", { class: "grid", style: "margin-bottom:.8rem" },
    stat("skills", "see list", "installed and sandboxed")),
    section("Install a skill",
      el("div", { class: "row tight" }, installPath, installName,
        el("button", {
          class: "btn btn--primary", onclick: async () => {
            try {
              const result = await api.post("/api/skills/install",
                { path: installPath.value.trim(), name: installName.value.trim() });
              toast(`Installed ${result.skill ? (result.skill.name || result.skill.id) : "skill"}`, "ok");
              refresh();
            } catch (error) { toast(error.message, "error"); }
          },
        }, "Install")),
      el("div", { class: "small muted" }, "Installation validates the manifest, scans for dangerous patterns, runs its tests and records every step in the audit log.")),
    section("Installed skills", listHost));
  await refresh();
}

/* ---------- marketplace ---------- */
export async function renderMarketplace(container) {
  const installedHost = el("div", {});
  const sourcesHost = el("div", {});
  const statsHost = el("div", { class: "grid", style: "margin-bottom:.8rem" });
  const nameInput = el("input", { placeholder: "package name" });
  const sourceInput = el("input", { placeholder: "source url or registry" });
  const versionInput = el("input", { placeholder: "version (optional)" });

  async function refresh() {
    const [payload, sources] = await Promise.all([
      api.get("/api/marketplace"), api.get("/api/marketplace/sources"),
    ]);
    const stats = payload.stats || {};
    clear(statsHost).append(stat("installed", stats.installed ?? 0, "packages"),
      stat("sources", stats.sources ?? 0, "registries"),
      stat("pinned", stats.pinned ?? 0, "locked versions"));
    clear(installedHost).append(table([
      { label: "package", value: (row) => row.name || row.id },
      { label: "version", value: "version" },
      { label: "kind", value: "kind" },
      { label: "pinned", value: (row) => (row.pinned ? "yes" : "no") },
      { label: "installed", value: (row) => fmtTime(row.installed_at) },
      { label: "actions", value: (row) => el("div", { class: "row tight" },
        el("button", { class: "btn small", onclick: () => act("review", row) }, "Review"),
        el("button", { class: "btn small", onclick: () => act("rollback", row) }, "Roll back"),
        el("button", { class: "btn small btn--danger", onclick: () => act("uninstall", row) }, "Uninstall")) },
    ], payload.installed || [], { empty: "Nothing installed from the marketplace yet." }));
    clear(sourcesHost).append(table([
      { label: "name", value: (row) => row.name || row.url },
      { label: "url", value: "url" },
      { label: "trust", value: "trust" },
      { label: "added", value: (row) => fmtTime(row.added_at) },
    ], sources.sources || [], { empty: "No extra sources configured: only the built-in catalogue is used." }),
      el("div", { class: "row tight", style: "margin-top:.5rem" },
        (() => {
          const url = el("input", { placeholder: "https://…/registry.json" });
          const name = el("input", { placeholder: "short name" });
          return el("div", { class: "row tight", style: "width:100%" }, name, url,
            el("button", {
              class: "btn", onclick: async () => {
                try {
                  await api.post("/api/marketplace/sources", { name: name.value.trim(), url: url.value.trim() });
                  toast("Source registered", "ok");
                  refresh();
                } catch (error) { toast(error.message, "error"); }
              },
            }, "Add source"));
        })()));
  }

  async function act(action, row) {
    const name = row.name || row.id;
    try {
      const payload = await api.post(`/api/marketplace/${action}`, { name, version: row.version || "" });
      toast(`${action}: ${payload.ok === false ? (payload.error || "refused") : "done"}`,
        payload.ok === false ? "error" : "ok");
      refresh();
    } catch (error) { toast(error.message, "error"); }
  }

  container.append(statsHost,
    section("Install from the marketplace",
      el("div", { class: "row tight" }, nameInput, versionInput, sourceInput,
        el("button", {
          class: "btn btn--primary", onclick: async () => {
            try {
              const result = await api.post("/api/marketplace/install", {
                name: nameInput.value.trim(), version: versionInput.value.trim(),
                source: sourceInput.value.trim(),
              });
              toast("Installed. Review the scan result before enabling.", "ok");
              if (result.review) modal({ title: "Review", body: json(result.review) });
              refresh();
            } catch (error) { toast(error.message, "error"); }
          },
        }, "Install"))),
    section("Installed packages", installedHost),
    section("Sources", sourcesHost));
  await refresh();
}

register({ id: "skills", title: "Skills", icon: "🧩", subtitle: "sandboxed, scanned, capability-scoped", render: renderSkills });
register({ id: "marketplace", title: "Marketplace", icon: "🛍", subtitle: "install, review, roll back, uninstall", render: renderMarketplace });
