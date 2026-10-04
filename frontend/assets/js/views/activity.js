/* Activity: the append-only event log, with chain verification and honest counts. */

import { api } from "../api.js";
import { el, clear, fmtTime, toast, download } from "../ui.js";
import { section, table, stat, json } from "../components.js";
import { register } from "../router.js";

export async function renderActivity(container) {
  const kind = el("input", { placeholder: "kind filter (e.g. TOOL_EXECUTION)" });
  const missionId = el("input", { placeholder: "mission id" });
  const rows = el("div", {});
  const statsHost = el("div", { class: "grid", style: "margin-bottom:.8rem" });
  const limit = el("input", { type: "number", value: "100", min: "1", max: "2000" });

  async function refresh() {
    const [events, stats, verdict] = await Promise.all([
      api.get("/api/activity", { limit: limit.value, kind: kind.value.trim(), mission_id: missionId.value.trim() }),
      api.get("/api/activity/stats"),
      api.get("/api/activity/verify"),
    ]);
    clear(statsHost);
    statsHost.append(
      stat("events", stats.total, `head seq ${stats.head_seq}`),
      stat("chain", verdict.ok ? "valid" : "broken", `${verdict.detail?.checked ?? 0} links checked`),
      stat("head hash", String(stats.head_hash || "").slice(0, 12) + "…", "tamper-evident"),
      stat("by risk", Object.entries(stats.by_risk || {}).map(([key, value]) => `${key}:${value}`).join(" ")),
    );
    const list = events.events || [];
    clear(rows).append(table([
      { label: "seq", value: "seq" },
      { label: "when", value: (row) => fmtTime(row.ts) },
      { label: "kind", value: "kind" },
      { label: "actor", value: "actor" },
      { label: "risk", value: "risk" },
      { label: "mission", value: (row) => String(row.mission_id || "").slice(0, 8) },
      { label: "detail", value: (row) => {
        const node = el("div", { style: "max-width:34rem" });
        node.append(el("div", {}, row.source || ""));
        node.append(json(row.payload, { label: "payload" }));
        return node;
      } },
    ], list, { empty: "No events match." }));
  }

  container.append(statsHost,
    el("div", { class: "row tight", style: "margin-bottom:.6rem" },
      el("div", { style: "flex:0 0 14rem" }, kind),
      el("div", { style: "flex:0 0 14rem" }, missionId),
      el("div", { style: "flex:0 0 6rem" }, limit),
      el("button", { class: "btn", onclick: () => refresh() }, "Filter"),
      el("button", {
        class: "btn", onclick: async () => {
          try {
            const payload = await api.get("/api/activity/export", { limit: limit.value });
            download(`natasha-events-${Date.now()}.json`, JSON.stringify(payload, null, 2), "application/json");
          } catch (error) { toast(error.message, "error"); }
        },
      }, "Export JSON")),
    section("Events", rows));
  await refresh();
}

register({
  id: "activity", title: "Activity", icon: "📜",
  subtitle: "append-only, hash-chained audit trail",
  render: renderActivity,
});
