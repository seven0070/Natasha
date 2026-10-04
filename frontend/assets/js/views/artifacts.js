/* Artifacts: everything Natasha produced, with previews, hashes and downloads. */

import { api } from "../api.js";
import { el, clear, fmtTime, fmtBytes, toast, modal, kv } from "../ui.js";
import { section, table, stat, artifactCard } from "../components.js";
import { register } from "../router.js";

export async function renderArtifacts(container) {
  const listHost = el("div", { class: "grid" });
  const statsHost = el("div", { class: "grid", style: "margin-bottom:.8rem" });
  const search = el("input", { placeholder: "filter by name, mime or mission…" });

  async function refresh() {
    const payload = await api.get("/api/artifacts");
    const all = payload.artifacts || [];
    const needle = search.value.trim().toLowerCase();
    const artifacts = needle
      ? all.filter((item) => JSON.stringify({ name: item.name, mime: item.mime, mission_id: item.mission_id })
        .toLowerCase().includes(needle))
      : all;
    clear(statsHost);
    const totalBytes = all.reduce((sum, item) => sum + Number(item.bytes || 0), 0);
    const byKind = all.reduce((acc, item) => { acc[item.kind] = (acc[item.kind] || 0) + 1; return acc; }, {});
    statsHost.append(stat("artifacts", all.length, fmtBytes(totalBytes)),
      ...Object.entries(byKind).slice(0, 4).map(([kind, count]) => stat(kind, count, "files")));
    clear(listHost);
    if (!artifacts.length) {
      listHost.append(el("div", { class: "empty" }, "Nothing created yet. Missions, creation jobs and tools write here."));
    }
    artifacts.forEach((artifact) => listHost.append(artifactCard(artifact, {
      onPreview: (item) => preview(item, container),
    })));

  }

  async function preview(artifact, host) {
    const path = artifact.path || "";
    let payload;
    try { payload = await api.get("/api/artifacts/content", { path }); }
    catch (error) { toast(error.message, "error"); return; }
    const view = modal({
      title: artifact.name || path.split("/").pop(),
      body: el("div", {},
        kv([["path", artifact.relative || path], ["mime", artifact.mime],
            ["bytes", fmtBytes(artifact.bytes)], ["sha256", artifact.sha256],
            ["mission", artifact.mission_id], ["modified", fmtTime(artifact.modified_at)]]),
        el("div", { style: "margin-top:.5rem" },
          payload.previewable
            ? el("pre", {}, (payload.text || "").slice(0, 200_000) + (payload.truncated ? "\n… (truncated)" : ""))
            : el("p", { class: "muted" }, "Binary or oversized artifact - download it to inspect it."))),
      actions: [
        { label: "Close" },
        { label: "Download", kind: "primary", onClick: () => {
          window.open(`/api/artifacts/download?path=${encodeURIComponent(path)}`, "_blank");
        } },
      ],
    });
    return view;
  }

  search.addEventListener("input", refresh);
  container.append(el("div", { class: "row tight", style: "margin-bottom:.6rem" },
    el("div", { style: "flex:1 1 20rem" }, search),
    el("button", { class: "btn", onclick: () => refresh() }, "Refresh")),
    statsHost, section("Produced artifacts", listHost));
  await refresh();
}

register({
  id: "artifacts", title: "Artifacts", icon: "📦",
  subtitle: "files, previews, hashes, provenance",
  render: renderArtifacts,
});

export { table };
