/* Evolution: the constitution, protected areas, and the upgrade governor. */

import { api } from "../api.js";
import { el, clear, fmtTime, toast, modal } from "../ui.js";
import { section, table, stat, json } from "../components.js";
import { register } from "../router.js";

export async function renderEvolution(container) {
  const constitutionHost = el("div", {});
  const invariantsHost = el("div", {});
  const proposalsHost = el("div", {});
  const opportunitiesHost = el("div", {});
  const statsHost = el("div", { class: "grid", style: "margin-bottom:.8rem" });

  const summary = el("textarea", { placeholder: "what should change, and why is it safe?" });
  const target = el("input", { placeholder: "target file or area (e.g. backend/natasha/memory/retrieval.py)" });
  const change = el("textarea", { placeholder: "the change: a short description, or a unified diff" });

  async function refresh() {
    const [constitution, invariants, proposals, opportunities] = await Promise.all([
      api.get("/api/governance/constitution"),
      api.get("/api/governance/invariants"),
      api.get("/api/governance/upgrades"),
      api.get("/api/governance/upgrades/opportunities"),
    ]);
    const report = constitution.report || {};
    clear(statsHost).append(
      stat("constitution", report.ok ? "intact" : "violated", `${(report.invariants || []).length} invariants`),
      stat("manifest", report.manifest_ok ? "unchanged" : "changed", (report.manifest_changes || []).length ? `${report.manifest_changes.length} file(s) changed` : ""),
      stat("proposals", (proposals.proposals || []).length, "awaiting analysis, tests, approval"),
      stat("opportunities", (opportunities.opportunities || []).length, "detected improvements"));
    clear(constitutionHost).append(json({ protected_areas: constitution.protected_areas, report },
      { label: "constitution report" }),
      report.violations && report.violations.length
        ? el("div", { class: "empty error" }, `violations: ${report.violations.join(", ")}`) : null);
    clear(invariantsHost).append(table([
      { label: "id", value: "id" },
      { label: "area", value: "area" },
      { label: "invariant", value: "statement" },
      { label: "enforced by", value: "enforced_by" },
    ], invariants.invariants || []));
    clear(proposalsHost).append(table([
      { label: "proposal", value: (row) => row.summary || row.title || row.id },
      { label: "state", value: "state" },
      { label: "protected", value: (row) => (row.protected ? "yes - needs explicit owner acknowledgement" : "no") },
      { label: "tests", value: (row) => (row.tests ? (row.tests.ok ? "passed" : "failed") : "not run") },
      { label: "sandbox", value: (row) => (row.sandbox ? (row.sandbox.ok ? "passed" : "failed") : "not run") },
      { label: "created", value: (row) => fmtTime(row.created_at) },
      { label: "actions", value: (row) => el("div", { class: "row tight" },
        el("button", { class: "btn small", onclick: () => act(row.id, "analyse") }, "Analyse"),
        el("button", { class: "btn small", onclick: () => act(row.id, "test") }, "Test"),
        el("button", { class: "btn small", onclick: () => act(row.id, "request-approval") }, "Ask owner"),
        el("button", {
          class: "btn small btn--primary", onclick: async () => {
            const approvalId = window.prompt("Approval id (from the Approvals screen). Leave blank to be refused.");
            if (approvalId === null) return;
            try {
              const payload = await api.post(`/api/governance/upgrades/${row.id}/apply`,
                { approval_id: approvalId.trim() });
              modal({ title: `Apply ${row.id}`, body: json(payload) });
              refresh();
            } catch (error) { toast(error.message, "error"); }
          },
        }, "Apply"),
        el("button", {
          class: "btn small btn--danger", onclick: async () => {
            try {
              const payload = await api.post(`/api/governance/upgrades/${row.id}/rollback`, {});
              toast(payload.ok === false ? (payload.error || "rollback refused") : "Rolled back",
                payload.ok === false ? "error" : "ok");
              refresh();
            } catch (error) { toast(error.message, "error"); }
          },
        }, "Roll back")) },
    ], proposals.proposals || [], { empty: "No upgrade proposals. Natasha proposes, you decide." }));
    clear(opportunitiesHost).append(table([
      { label: "area", value: "area" },
      { label: "opportunity", value: (row) => row.summary || row.detail || row.reason },
      { label: "confidence", value: (row) => row.confidence ?? "" },
    ], opportunities.opportunities || [], { empty: "Nothing detected right now." }));
  }

  async function act(proposalId, action) {
    try {
      const payload = await api.post(`/api/governance/upgrades/${proposalId}/${action}`, {});
      toast(`${action}: ${payload.ok === false ? (payload.error || "failed") : "done"}`,
        payload.ok === false ? "error" : "ok");
      if (payload.approval_request_id || payload.approval) {
        modal({ title: "Approval requested", body: json(payload) });
      }
      refresh();
    } catch (error) { toast(error.message, "error"); }
  }

  container.append(statsHost,
    section("Constitution", constitutionHost),
    section("Invariants", invariantsHost),
    section("Propose an upgrade",
      el("div", {}, el("label", {}, "Summary"), summary,
        el("label", {}, "Target"), target,
        el("label", {}, "Change"), change,
        el("button", {
          class: "btn btn--primary", style: "margin-top:.5rem", onclick: async () => {
            if (!summary.value.trim()) { toast("A summary is required", "error"); return; }
            try {
              const payload = await api.post("/api/governance/upgrades", {
                summary: summary.value.trim(),
                changes: [{ path: target.value.trim(), description: change.value.trim() }],
              });
              toast("Proposal recorded. It still needs tests, sandbox and your approval.", "ok");
              summary.value = ""; change.value = "";
              refresh();
            } catch (error) { toast(error.message, "error"); }
          },
        }, "Propose")),
      el("div", { class: "small muted", style: "margin-top:.4rem" },
        "Natasha can propose but never approve: applying requires a fresh owner approval token bound to the proposal fingerprint.")),
    section("Proposals", proposalsHost),
    section("Detected opportunities", opportunitiesHost));
  await refresh();
}

register({
  id: "evolution", title: "Evolution", icon: "🌱",
  subtitle: "constitution, invariants, governor",
  render: renderEvolution,
});
