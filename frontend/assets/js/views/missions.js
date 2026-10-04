/* Missions: objectives, plans, runs, verification, recovery and artifacts. */

import { api } from "../api.js";
import { el, clear, fmtTime, fmtDuration, toast, modal, kv, riskChip } from "../ui.js";
import { section, table, json, stat, markdownBlock } from "../components.js";
import { register, go } from "../router.js";
import { store } from "../store.js";

const NEW_STEP = { name: "", tool: "http.request", description: "", arguments: "{}" };

function stateChip(state) {
  const name = String(state || "").toUpperCase();
  const klass = ["COMPLETED", "VERIFIED"].includes(name) ? "chip--ok"
    : ["FAILED", "BLOCKED", "CANCELLED"].includes(name) ? "chip--danger"
      : ["RUNNING", "VERIFYING", "REPAIRING"].includes(name) ? "chip--warn" : "";
  return el("span", { class: `chip ${klass}` }, name.toLowerCase() || "unknown");
}

async function createMission(container) {
  const objective = el("textarea", { placeholder: "Objective: what should actually be true when this is done?" });
  const title = el("input", { placeholder: "short title (optional)" });
  const autoRun = el("input", { type: "checkbox" });
  const criteria = el("input", { placeholder: "success criteria, comma separated (optional)" });
  const stepList = el("div", { style: "display:flex;flex-direction:column;gap:.4rem" });
  const steps = [];

  const renderSteps = () => {
    clear(stepList);
    steps.forEach((step, index) => {
      const tool = el("input", { value: step.tool, placeholder: "tool name" ,
        onchange: () => { step.tool = tool.value.trim(); } });
      const name = el("input", { value: step.name, placeholder: "step name",
        onchange: () => { step.name = name.value.trim(); } });
      const args = el("input", { value: step.arguments, placeholder: "arguments (JSON)",
        onchange: () => { step.arguments = args.value; } });
      stepList.append(el("div", { class: "row" },
        el("div", { style: "flex:0 0 10rem" }, name),
        el("div", { style: "flex:0 0 12rem" }, tool),
        args,
        el("button", { class: "btn small", style: "flex:0 0 auto",
                      onclick: () => { steps.splice(index, 1); renderSteps(); } }, "Remove")));
    });
    if (!steps.length) stepList.append(el("div", { class: "small muted" }, "No steps: Natasha will plan the mission herself."));
  };
  renderSteps();

  modal({
    title: "New mission",
    body: el("div", {},
      el("label", {}, "Objective"), objective,
      el("label", {}, "Title"), title,
      el("label", {}, "Success criteria"), criteria,
      el("label", {}, "Steps (optional, planned immediately)"), stepList,
      el("button", {
        class: "btn small", onclick: () => { steps.push({ ...NEW_STEP }); renderSteps(); },
      }, "Add step"),
      el("label", { style: "display:flex;gap:.4rem;align-items:center;margin-top:.6rem" }, autoRun,
        "Run as soon as it is created")),
    actions: [
      { label: "Cancel" },
      {
        label: "Create", kind: "primary", onClick: async () => {
          const payload = {
            objective: objective.value.trim(),
            title: title.value.trim(),
            success_criteria: criteria.value.split(",").map((item) => item.trim()).filter(Boolean),
            auto_run: autoRun.checked,
            steps: steps.filter((step) => step.tool && step.name).map((step) => {
              let parsed = {};
              try { parsed = JSON.parse(step.arguments || "{}"); } catch { parsed = {}; }
              return { name: step.name, tool: step.tool, arguments: parsed, description: step.description };
            }),
          };
          if (!payload.objective) { toast("An objective is required", "error"); return true; }
          try {
            const created = await api.post("/api/missions", payload);
            toast(created.result ? `Mission finished: ${created.result.ok ? "verified" : "not verified"}` : "Mission created", "ok");
            const id = (created.mission || {}).id;
            clear(container);
            await renderMissions(container, { arg: id });
          } catch (error) { toast(error.message, "error"); }
        },
      },
    ],
  });
}

async function missionDetail(container, id) {
  const detail = container.querySelector("[data-detail]");
  const payload = await api.get(`/api/missions/${id}`);
  const mission = payload.mission || payload;
  const checks = mission.checks || [];
  const steps = mission.steps || [];
  const body = el("div", {},
    el("div", { class: "card" },
      el("div", { class: "card__head" }, el("h3", {}, mission.title || mission.objective), stateChip(mission.state),
        el("span", { class: "small muted", style: "margin-left:auto" }, fmtTime(mission.updated_at))),
      el("p", {}, mission.objective),
      kv([["id", mission.id], ["priority", mission.priority], ["attempts", mission.attempts],
          ["created", fmtTime(mission.created_at)], ["started", fmtTime(mission.started_at)],
          ["finished", fmtTime(mission.finished_at)], ["plan", mission.plan_summary],
          ["scope", (mission.scope || []).join(", ")], ["parent", mission.parent_id]])),
    el("div", { class: "row tight", style: "margin:.6rem 0" },
      el("button", { class: "btn btn--primary", onclick: () => run("run") }, "Run"),
      el("button", { class: "btn", onclick: () => run("verify") }, "Verify"),
      el("button", { class: "btn", onclick: () => run("pause") }, "Pause"),
      el("button", { class: "btn", onclick: () => run("resume") }, "Resume"),
      el("button", { class: "btn", onclick: () => run("rollback") }, "Roll back"),
      el("button", { class: "btn btn--danger", onclick: () => run("cancel") }, "Cancel")),
    steps.length ? section("Plan", table([
      { label: "#", value: (row, index) => String(steps.indexOf(row)) },
      { label: "step", value: "name" },
      { label: "tool", value: "tool" },
      { label: "state", value: (row) => stateChip(row.state || (row.ok ? "completed" : "pending")) },
      { label: "attempts", value: "attempts" },
      { label: "detail", value: (row) => row.error || row.summary || row.observation || "" },
    ], steps)) : null,
    checks.length ? section("Checks", table([
      { label: "check", value: "name" },
      { label: "status", value: (row) => el("span", { class: `chip ${String(row.status).toLowerCase() === "passed" ? "chip--ok" : "chip--danger"}` }, String(row.status || "")) },
      { label: "evidence", value: (row) => (row.evidence || row.detail || "").toString().slice(0, 160) },
    ], checks)) : null,
    (mission.artifacts || []).length ? section("Artifacts", el("div", { class: "row tight" },
      ...mission.artifacts.map((artifact) => el("button", {
        class: "btn small", onclick: () => go("artifacts"),
      }, String(artifact.name || artifact.path || artifact).split("/").pop())))) : null,
    section("Raw record", json(mission, { label: "full mission JSON" })));

  async function run(action) {
    try {
      const result = await api.post(`/api/missions/${id}/${action}`, {});
      toast(`${action}: ${result.ok === false ? "did not succeed" : "done"}`, result.ok === false ? "error" : "ok");
      clear(detail);
      await missionDetail(container, id);
      store.refreshStatus();
    } catch (error) { toast(error.message, "error"); }
  }

  clear(detail).append(body);
}

export async function renderMissions(container, { arg } = {}) {
  const list = el("div", { class: "list" });
  const detail = el("div", { "data-detail": "1" });
  container.append(el("div", { class: "row tight", style: "margin-bottom:.6rem" },
    el("button", { class: "btn btn--primary", onclick: () => createMission(container) }, "New mission"),
    el("button", { class: "btn", onclick: () => refresh() }, "Refresh")));
  const stats = el("div", { class: "grid", style: "margin-bottom:.8rem" });
  container.append(stats, el("div", { class: "split" }, el("div", {}, section("Missions", list)), detail));

  async function refresh() {
    const [payload, statsPayload] = await Promise.all([
      api.get("/api/missions", { limit: 100 }), api.get("/api/missions/stats"),
    ]);
    clear(stats);
    Object.entries(statsPayload.by_state || {}).forEach(([state, count]) => {
      stats.append(stat(state, count, "missions"));
    });
    if (!Object.keys(statsPayload.by_state || {}).length) stats.append(stat("missions", 0, "none yet"));
    const missions = payload.missions || [];
    clear(list);
    if (!missions.length) list.append(el("div", { class: "empty" }, "No missions yet."));
    missions.forEach((mission) => {
      list.append(el("div", {
        class: `list__item${mission.id === arg ? " active" : ""}`,
        onclick: () => { arg = mission.id; location.hash = `#/missions/${mission.id}`;
          missionDetail(container, mission.id); },
      }, el("div", { class: "row", style: "gap:.4rem;align-items:center" },
        el("strong", {}, mission.title || mission.objective || mission.id),
        stateChip(mission.state)),
        el("div", { class: "small muted" }, `${mission.id} - ${fmtTime(mission.updated_at)}`)));
    });
    if (arg) await missionDetail(container, arg);
    else {
      clear(detail).append(section("Mission detail", el("p", { class: "muted" },
        "Choose a mission to see its plan, checks, evidence and artifacts.")));
    }
  }
  await refresh();
}

register({
  id: "missions", title: "Missions", icon: "🎯",
  subtitle: "plan, run, verify, recover",
  render: renderMissions,
});
