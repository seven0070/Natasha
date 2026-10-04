/* Approvals: the owner's gate. Nothing dangerous runs without a decision here. */

import { api } from "../api.js";
import { el, clear, fmtTime, toast } from "../ui.js";
import { section, approvalCard, table, stat, emptyState } from "../components.js";
import { register } from "../router.js";
import { store } from "../store.js";

export async function renderApprovals(container) {
  const pendingHost = el("div", { class: "grid" });
  const historyHost = el("div", {});
  const actions = el("div", { class: "row tight", style: "margin-bottom:.7rem" },
    el("button", { class: "btn", onclick: () => refresh() }, "Refresh"),
    el("button", {
      class: "btn", onclick: async () => {
        try {
          const result = await api.post("/api/approvals/expire", {});
          toast(`${result.expired ?? 0} expired`, "ok");
          refresh();
        } catch (error) { toast(error.message, "error"); }
      },
    }, "Expire stale requests"));

  async function refresh() {
    const [pending, history] = await Promise.all([
      api.get("/api/approvals"), api.get("/api/approvals/history"),
    ]);
    const requests = pending.pending || [];
    clear(pendingHost);
    if (!requests.length) {
      pendingHost.append(emptyState("Nothing is waiting. Dangerous actions queue here instead of running."));
    }
    requests.forEach((request) => pendingHost.append(approvalCard(request, {
      onDecided: () => { refresh(); store.refreshStatus(); },
    })));
    const rows = (history.history || []).map((item) => ({
      id: item.request_id || item.id,
      operation: item.operation,
      status: item.status,
      risk: item.risk,
      decided_by: item.decided_by || item.actor,
      at: item.decided_at || item.created_at,
    }));
    clear(historyHost).append(table([
      { label: "request", value: "id" },
      { label: "operation", value: "operation" },
      { label: "status", value: "status" },
      { label: "risk", value: "risk" },
      { label: "decided by", value: "decided_by" },
      { label: "when", value: (row) => fmtTime(row.at) },
    ], rows, { empty: "No approval decisions recorded yet." }));
  }

  container.append(el("div", { class: "grid", style: "margin-bottom:.8rem" },
    stat("pending", store.pendingApprovals, "awaiting the owner")),
    actions,
    section("Waiting for you", pendingHost),
    section("Decision history", historyHost));
  await refresh();
}

register({
  id: "approvals", title: "Approvals", icon: "✋",
  subtitle: "explicit, scoped, expiring authorisation",
  render: renderApprovals,
});
