/* Memory: what Natasha remembers, why it was recalled, and how to correct or forget it. */

import { api } from "../api.js";
import { el, clear, fmtTime, toast, kv, confirmDialog, titleCase } from "../ui.js";
import { section, table, stat, json, emptyState } from "../components.js";
import { register } from "../router.js";

const KINDS = ["working", "episodic", "semantic", "procedural", "profile", "preference",
               "relationship", "task", "artifact", "world"];

function memoryRow(record, { onChanged } = {}) {
  const node = el("div", { class: "list__item" },
    el("div", { class: "row", style: "gap:.4rem;align-items:center" },
      el("span", { class: "chip" }, record.kind),
      record.pinned ? el("span", { class: "chip chip--warn" }, "pinned") : null,
      el("span", { class: "small muted", style: "margin-left:auto" },
        `importance ${record.importance} - confidence ${record.confidence}`)),
    el("div", { style: "margin:.35rem 0" }, record.summary || record.content),
    el("div", { class: "small muted" },
      `${record.id} - ${fmtTime(record.created_at)} - provenance: ${(record.provenance || {}).source || "unknown"}${record.access_count ? ` - used ${record.access_count}x` : ""}`),
    el("div", { class: "row tight", style: "margin-top:.4rem" },
      el("button", {
        class: "btn small", onclick: async () => {
          const content = window.prompt("Correct this memory (the old one is superseded, never deleted):",
            record.content);
          if (!content) return;
          try {
            const result = await api.post("/api/memory/correct",
              { memory_id: record.id, content, reason: "corrected in the console" });
            toast(`Superseded by ${result.replacement}`, "ok");
            onChanged && onChanged();
          } catch (error) { toast(error.message, "error"); }
        },
      }, "Correct"),
      el("button", {
        class: "btn small btn--danger", onclick: async () => {
          if (!(await confirmDialog("Forget this memory?",
            "The memory is removed from retrieval; the audit log still records that it existed.",
            { confirmLabel: "Forget" }))) return;
          try {
            await api.post("/api/memory/forget", { memory_id: record.id, hard: false });
            toast("Forgotten.", "ok");
            onChanged && onChanged();
          } catch (error) { toast(error.message, "error"); }
        },
      }, "Forget")));
  return node;
}

export async function renderMemory(container) {
  const kindFilter = el("select", {}, el("option", { value: "" }, "all kinds"),
    ...KINDS.map((kind) => el("option", { value: kind }, titleCase(kind))));
  const query = el("input", { placeholder: "search memory…" });
  const recallButton = el("button", { class: "btn" }, "Recall");
  const list = el("div", { class: "list" });
  const statsHost = el("div", { class: "grid", style: "margin-bottom:.8rem" });
  const provenanceHost = el("div", {});

  const newKind = el("select", {}, ...KINDS.map((kind) => el("option", { value: kind }, titleCase(kind))));
  const newContent = el("textarea", { placeholder: "something worth remembering" });
  const newSummary = el("input", { placeholder: "one-line summary" });
  const newImportance = el("input", { type: "number", min: "0", max: "1", step: "0.1", value: "0.6" });

  async function refresh() {
    const [payload, statsPayload] = await Promise.all([
      api.get("/api/memory", { limit: 60, kind: kindFilter.value }),
      api.get("/api/memory/stats"),
    ]);
    clear(statsHost);
    statsHost.append(stat("memories", statsPayload.total, `${statsPayload.embedder} embeddings`));
    Object.entries(statsPayload.by_kind || {}).slice(0, 6).forEach(([kind, count]) => {
      statsHost.append(stat(kind, count, "stored"));
    });
    statsHost.append(stat("events", statsPayload.events, `db: ${String(statsPayload.db_path).split("/").pop()}`));
    const memories = payload.memories || [];
    clear(list);
    if (!memories.length) list.append(emptyState("Nothing matches. Ask something in Chat, or add a memory below."));
    memories.forEach((record) => list.append(memoryRow(record, { onChanged: refresh })));
  }

  async function recall() {
    const text = query.value.trim();
    if (!text) return;
    clear(list).append(el("div", { class: "small muted" }, "recalling…"));
    try {
      const payload = await api.get("/api/memory/recall",
        { query: text, limit: 12, kinds: kindFilter.value });
      const hits = payload.hits || payload.memories || [];
      clear(list);
      clear(provenanceHost);
      if (!hits.length) list.append(emptyState("Nothing relevant was found. Natasha says so instead of guessing."));
      hits.forEach((hit) => {
        const record = hit.memory || hit.record || hit;
        list.append(el("div", {},
          el("div", { class: "row", style: "gap:.4rem" },
            el("span", { class: "chip chip--ok" }, `score ${Number(hit.score ?? 0).toFixed(3)}`),
            el("span", { class: "chip" }, record.kind || "")),
          memoryRow(record, { onChanged: recall }),
          hit.parts ? el("div", { class: "small muted" },
            "signals: " + Object.entries(hit.parts).map(([key, value]) =>
              `${key} ${Number(value).toFixed(2)}`).join(", ")) : null));
      });
      provenanceHost.append(el("button", {
        class: "btn small", onclick: () => { clear(provenanceHost); refresh(); },
      }, "Back to the full list"));
    } catch (error) {
      clear(list).append(el("div", { class: "empty error" }, error.message));
    }
  }

  recallButton.addEventListener("click", recall);
  query.addEventListener("keydown", (event) => { if (event.key === "Enter") recall(); });
  kindFilter.addEventListener("change", refresh);

  container.append(statsHost,
    el("div", { class: "row tight", style: "margin-bottom:.6rem" },
      el("div", { style: "flex:1 1 18rem;display:flex;gap:.4rem" }, query, recallButton),
      el("div", { style: "flex:0 0 10rem" }, kindFilter),
      el("button", { class: "btn", onclick: () => refresh() }, "Refresh")),
    el("div", { class: "split" },
      el("div", {}, section("Memories", list), provenanceHost),
      el("div", {},
        section("Remember something",
          el("label", {}, "Kind"), newKind,
          el("label", {}, "Content"), newContent,
          el("label", {}, "Summary"), newSummary,
          el("label", {}, "Importance"), newImportance,
          el("button", {
            class: "btn btn--primary", style: "margin-top:.5rem", onclick: async () => {
              if (!newContent.value.trim()) { toast("Content is required", "error"); return; }
              try {
                const result = await api.post("/api/memory", {
                  kind: newKind.value, content: newContent.value.trim(), summary: newSummary.value.trim(),
                  importance: Number(newImportance.value || 0.5), source: "owner",
                  provenance: "owner:web-console",
                });
                toast(`Stored as ${result.memory ? result.memory.id : "a new memory"}`, "ok");
                newContent.value = ""; newSummary.value = "";
                refresh();
              } catch (error) { toast(error.message, "error"); }
            },
          }, "Store")),
        section("World model", el("div", { "data-world": "1" }, el("div", { class: "spinner" }))),
        section("Working memory", el("div", { "data-working": "1" }, el("div", { class: "spinner" }))))));

  await refresh();
  try {
    const world = await api.get("/api/memory/world");
    const host = container.querySelector("[data-world]");
    clear(host).append(table([
      { label: "entity", value: "name" }, { label: "kind", value: "kind" },
      { label: "facts", value: (row) => (row.attributes ? Object.keys(row.attributes).length : 0) },
    ], world.entities || [], { empty: "No entities learned yet." }),
      el("div", { class: "small muted", style: "margin-top:.4rem" },
        `${(world.facts || []).length} facts`));
  } catch (error) {
    clear(container.querySelector("[data-world]")).append(el("div", { class: "small error" }, error.message));
  }
  try {
    const working = await api.get("/api/memory/working");
    clear(container.querySelector("[data-working]")).append(json(working, { label: "working set" }));
  } catch (error) {
    clear(container.querySelector("[data-working]")).append(el("div", { class: "small error" }, error.message));
  }
}

register({
  id: "memory", title: "Memory", icon: "🧠",
  subtitle: "store, recall, provenance, correction",
  render: renderMemory,
});

export { kv };
