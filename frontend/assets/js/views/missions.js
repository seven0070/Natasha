/* Tasks / Missions: objectives, plans, runs, verification, recovery and artifacts.
   Obsidian Intelligence Stitch Design Implementation with real Natasha backend. */

import { api } from "../api.js";
import { el, clear, fmtTime, fmtDuration, toast, modal, kv, riskChip } from "../ui.js";
import { section, table, json, stat, markdownBlock, emptyState } from "../components.js";
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
  const objective = el("textarea", {
    placeholder: "Objective: what should actually be true when this task is done?",
    rows: "3",
  });
  const title = el("input", { placeholder: "Short title (optional, e.g. 'Refactor auth middleware')" });
  const autoRun = el("input", { type: "checkbox", checked: true });
  const criteria = el("input", { placeholder: "Success criteria, comma separated (e.g. 'tests pass, zero warnings')" });
  const stepList = el("div", { style: "display:flex;flex-direction:column;gap:.4rem;margin:0.5rem 0" });
  const steps = [];

  const renderSteps = () => {
    clear(stepList);
    steps.forEach((step, index) => {
      const tool = el("input", {
        value: step.tool, placeholder: "Tool name (e.g. fs.write, http.request)",
        onchange: () => { step.tool = tool.value.trim(); },
      });
      const name = el("input", {
        value: step.name, placeholder: "Step name",
        onchange: () => { step.name = name.value.trim(); },
      });
      const args = el("input", {
        value: step.arguments, placeholder: "Arguments JSON",
        onchange: () => { step.arguments = args.value; },
      });
      stepList.append(el("div", { class: "row tight", style: "align-items:center" },
        el("div", { style: "flex:1 1 8rem" }, name),
        el("div", { style: "flex:1 1 10rem" }, tool),
        el("div", { style: "flex:2 1 12rem" }, args),
        el("button", {
          class: "btn small btn--danger",
          onclick: () => { steps.splice(index, 1); renderSteps(); },
        }, "Remove")));
    });
    if (!steps.length) {
      stepList.append(el("div", { class: "small muted" }, "No manual steps added: Natasha runtime will autonomously plan execution steps."));
    }
  };
  renderSteps();

  modal({
    title: "Run New Task",
    body: el("div", {},
      el("label", {}, "Task Objective"), objective,
      el("label", { style: "margin-top:0.5rem" }, "Title"), title,
      el("label", { style: "margin-top:0.5rem" }, "Success Criteria"), criteria,
      el("label", { style: "margin-top:0.5rem" }, "Execution Steps (Optional)"), stepList,
      el("button", {
        class: "btn small", type: "button",
        onclick: () => { steps.push({ ...NEW_STEP }); renderSteps(); },
      }, el("span", { class: "material-symbols-outlined icon-sm" }, "add"), "Add Step"),
      el("label", { style: "display:flex;gap:.5rem;align-items:center;margin-top:.85rem;cursor:pointer" },
        autoRun,
        el("span", { class: "bold" }, "Execute immediately after creation"))),
    actions: [
      { label: "Cancel" },
      {
        label: "Launch Task", kind: "primary", onClick: async () => {
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
          if (!payload.objective) { toast("A task objective is required", "error"); return true; }
          try {
            const created = await api.post("/api/missions", payload);
            toast(created.result ? `Task finished: ${created.result.ok ? "verified" : "completed with remarks"}` : "Task launched into queue", "ok");
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
  if (!detail) return;
  clear(detail).append(el("span", { class: "spinner" }));

  try {
    const payload = await api.get(`/api/missions/${id}`);
    const mission = payload.mission || payload;
    const checks = mission.checks || [];
    const steps = mission.steps || [];

    const totalSteps = steps.length || 1;
    const completedSteps = steps.filter((s) => s.state === "COMPLETED" || s.ok).length;
    const progressPercent = Math.round((completedSteps / totalSteps) * 100);

    const body = el("div", { class: "tactile-card", data3dTilt: "true" },
      el("div", { class: "card__head" },
        el("div", {},
          el("h3", {}, mission.title || mission.objective),
          el("span", { class: "small muted mono" }, `ID: ${mission.id}`)),
        stateChip(mission.state)),
      el("p", { style: "font-size:14px;line-height:1.6" }, mission.objective),

      el("div", { class: "task-progress-bar", style: "margin:0.75rem 0" },
        el("div", { class: "task-progress-bar__fill", style: `width:${progressPercent}%` })),
      el("div", { class: "row", style: "justify-content:space-between;margin-bottom:0.75rem" },
        el("span", { class: "small muted" }, `${completedSteps} of ${steps.length} steps verified`),
        el("span", { class: "small bold mono" }, `${progressPercent}% complete`)),

      kv([
        ["priority", mission.priority || "NORMAL"],
        ["attempts", mission.attempts || 0],
        ["created", fmtTime(mission.created_at)],
        ["started", fmtTime(mission.started_at)],
        ["finished", fmtTime(mission.finished_at)],
        ["plan", mission.plan_summary || "Autonomous execution plan"],
        ["scope", (mission.scope || []).join(", ") || "Full runtime context"],
      ]),

      el("div", { class: "row tight", style: "margin:1rem 0;padding-top:0.75rem;border-top:1px solid var(--border-soft)" },
        el("button", { class: "btn small btn--primary", onclick: () => run("run") },
          el("span", { class: "material-symbols-outlined icon-sm" }, "play_arrow"), "Run"),
        el("button", { class: "btn small", onclick: () => run("verify") },
          el("span", { class: "material-symbols-outlined icon-sm" }, "verified"), "Verify"),
        el("button", { class: "btn small", onclick: () => run("pause") },
          el("span", { class: "material-symbols-outlined icon-sm" }, "pause"), "Pause"),
        el("button", { class: "btn small", onclick: () => run("resume") },
          el("span", { class: "material-symbols-outlined icon-sm" }, "play_circle"), "Resume"),
        el("button", { class: "btn small", onclick: () => run("rollback") },
          el("span", { class: "material-symbols-outlined icon-sm" }, "undo"), "Roll Back"),
        el("button", { class: "btn small btn--danger", onclick: () => run("cancel") },
          el("span", { class: "material-symbols-outlined icon-sm" }, "cancel"), "Cancel")),

      steps.length ? section("Execution Plan", table([
        { label: "#", value: (row, index) => String(steps.indexOf(row) + 1) },
        { label: "Step", value: "name" },
        { label: "Tool", value: (row) => el("span", { class: "mono small" }, row.tool || "internal") },
        { label: "State", value: (row) => stateChip(row.state || (row.ok ? "completed" : "pending")) },
        { label: "Detail", value: (row) => el("span", { class: "small muted" }, row.error || row.summary || row.observation || "-") },
      ], steps)) : null,

      checks.length ? section("Verification Checks", table([
        { label: "Check", value: "name" },
        { label: "Status", value: (row) => el("span", { class: `chip ${String(row.status).toLowerCase() === "passed" ? "chip--ok" : "chip--danger"}` }, String(row.status || "")) },
        { label: "Evidence", value: (row) => el("span", { class: "mono small muted" }, (row.evidence || row.detail || "").toString().slice(0, 140)) },
      ], checks)) : null,

      (mission.artifacts || []).length ? section("Produced Artifacts", el("div", { class: "row tight" },
        ...mission.artifacts.map((artifact) => el("button", {
          class: "btn small", onclick: () => go("artifacts"),
        }, el("span", { class: "material-symbols-outlined icon-sm" }, "draft"),
           String(artifact.name || artifact.path || artifact).split("/").pop())))) : null,

      section("Raw Telemetry", json(mission, { label: "View Task JSON Record" })));

    clear(detail).append(body);
  } catch (error) {
    clear(detail).append(el("div", { class: "empty error" }, `Could not inspect task: ${error.message || error}`));
  }

  async function run(action) {
    try {
      const result = await api.post(`/api/missions/${id}/${action}`, {});
      toast(`${action}: ${result.ok === false ? "did not succeed" : "success"}`, result.ok === false ? "error" : "ok");
      await missionDetail(container, id);
      store.refreshStatus();
    } catch (error) { toast(error.message, "error"); }
  }
}

export async function renderMissions(container, { arg } = {}) {
  clear(container);

  // Top header with actions
  const header = el("div", { class: "row", style: "justify-content:space-between;align-items:center;margin-bottom:1.5rem" },
    el("div", {},
      el("h1", { style: "margin:0 0 4px" }, "Operations & Mission Orchestration"),
      el("p", { class: "muted small", style: "margin:0" }, "Live task queues, verification cycles, and rollback controls powered by Natasha runtime.")),
    el("div", { class: "row tight" },
      el("button", { class: "btn btn--ghost", onclick: () => refresh() },
        el("span", { class: "material-symbols-outlined icon-sm" }, "refresh"), "Refresh"),
      el("button", { class: "btn btn--primary", onclick: () => createMission(container) },
        el("span", { class: "material-symbols-outlined icon-sm" }, "play_arrow"), "Run New Task")));

  // Metrics tiles grid
  const metricsHost = el("div", { class: "task-metric-grid" });

  // Filter navigation tabs
  let currentFilter = "all";
  const filterTabs = el("div", { class: "filter-tabs", style: "margin-bottom:1.25rem" });
  const allTab = el("button", { class: "filter-chip active", onclick: () => setFilter("all") }, "All Tasks");
  const activeTab = el("button", { class: "filter-chip", onclick: () => setFilter("active") }, "In Progress");
  const completedTab = el("button", { class: "filter-chip", onclick: () => setFilter("completed") }, "Completed");
  const failedTab = el("button", { class: "filter-chip", onclick: () => setFilter("failed") }, "Failed / Stalled");
  filterTabs.append(allTab, activeTab, completedTab, failedTab);

  const list = el("div", { class: "task-list-stream" });
  const detail = el("div", { "data-detail": "1" });
  const splitLayout = el("div", { class: "tasks-split-layout" },
    el("div", {}, list),
    detail);

  container.append(header, metricsHost, filterTabs, splitLayout);

  let cachedMissions = [];

  function setFilter(filter) {
    currentFilter = filter;
    [allTab, activeTab, completedTab, failedTab].forEach((tab) => tab.classList.remove("active"));
    if (filter === "all") allTab.classList.add("active");
    else if (filter === "active") activeTab.classList.add("active");
    else if (filter === "completed") completedTab.classList.add("active");
    else if (filter === "failed") failedTab.classList.add("active");
    renderList();
  }

  function renderList() {
    clear(list);
    const filtered = cachedMissions.filter((mission) => {
      const s = String(mission.state || "").toUpperCase();
      if (currentFilter === "active") return ["RUNNING", "VERIFYING", "REPAIRING", "PLANNING"].includes(s);
      if (currentFilter === "completed") return ["COMPLETED", "VERIFIED"].includes(s);
      if (currentFilter === "failed") return ["FAILED", "BLOCKED", "CANCELLED"].includes(s);
      return true;
    });

    if (!filtered.length) {
      list.append(emptyState("No tasks found in this view."));
      return;
    }

    filtered.forEach((mission) => {
      const steps = mission.steps || [];
      const totalSteps = steps.length || 1;
      const completedSteps = steps.filter((s) => s.state === "COMPLETED" || s.ok).length;
      const pct = Math.round((completedSteps / totalSteps) * 100);

      const card = el("div", {
        class: `task-item-card tactile-card${mission.id === arg ? " selected" : ""}`,
        data3dTilt: "true",
        onclick: () => {
          arg = mission.id;
          location.hash = `#/tasks/${mission.id}`;
          Array.from(list.children).forEach((c) => c.classList.remove("selected"));
          card.classList.add("selected");
          missionDetail(container, mission.id);
        },
      },
      el("div", { class: "row", style: "justify-content:space-between;align-items:flex-start;margin-bottom:0.4rem" },
        el("strong", { style: "font-size:15px;color:var(--text)" }, mission.title || mission.objective || mission.id),
        stateChip(mission.state)),
      el("p", { class: "small muted", style: "margin:0 0 0.5rem;line-height:1.5" },
        (mission.objective || "").slice(0, 160) + ((mission.objective || "").length > 160 ? "…" : "")),
      el("div", { class: "task-progress-bar" },
        el("div", { class: "task-progress-bar__fill", style: `width:${pct}%` })),
      el("div", { class: "row", style: "justify-content:space-between;margin-top:0.4rem;font-size:12px;color:var(--text-faint)" },
        el("span", { class: "mono" }, `${completedSteps}/${steps.length || 0} steps`),
        el("span", {}, fmtTime(mission.updated_at))));

      list.append(card);
    });
  }

  async function refresh() {
    try {
      const [payload, statsPayload] = await Promise.all([
        api.get("/api/missions", { limit: 100 }),
        api.get("/api/missions/stats"),
      ]);

      cachedMissions = payload.missions || [];

      // Render metric tiles
      clear(metricsHost);
      const byState = statsPayload.by_state || {};
      const total = statsPayload.total_missions || cachedMissions.length;
      const active = (byState.RUNNING || 0) + (byState.VERIFYING || 0) + (byState.PLANNING || 0);
      const completed = (byState.COMPLETED || 0) + (byState.VERIFIED || 0);
      const failed = (byState.FAILED || 0) + (byState.BLOCKED || 0) + (byState.CANCELLED || 0);

      metricsHost.append(
        el("div", { class: "task-metric-card" },
          el("div", { class: "stat__label" }, "Total Tasks"),
          el("div", { class: "stat__value" }, String(total)),
          el("div", { class: "stat__meta" }, "Registered operations")),
        el("div", { class: "task-metric-card" },
          el("div", { class: "stat__label", style: "color:var(--ok)" },
            el("span", { class: "pulse-dot", style: "margin-right:4px" }), "In Progress"),
          el("div", { class: "stat__value", style: "color:var(--ok)" }, String(active)),
          el("div", { class: "stat__meta" }, "Actively running routines")),
        el("div", { class: "task-metric-card" },
          el("div", { class: "stat__label" }, "Completed"),
          el("div", { class: "stat__value" }, String(completed)),
          el("div", { class: "stat__meta" }, total ? `${Math.round((completed / total) * 100)}% verification rate` : "No runs")),
        el("div", { class: "task-metric-card" },
          el("div", { class: "stat__label" }, "Failed / Stalled"),
          el("div", { class: "stat__value", style: failed ? "color:var(--danger)" : "" }, String(failed)),
          el("div", { class: "stat__meta" }, failed ? "Requires inspection" : "Clean execution")));

      allTab.textContent = `All Tasks (${total})`;
      activeTab.textContent = `In Progress (${active})`;
      completedTab.textContent = `Completed (${completed})`;
      failedTab.textContent = `Failed (${failed})`;

      renderList();

      if (arg) {
        await missionDetail(container, arg);
      } else if (cachedMissions.length) {
        await missionDetail(container, cachedMissions[0].id);
      } else {
        clear(detail).append(section("Task Inspector", el("p", { class: "muted" },
          "Choose a task from the list or click 'Run New Task' to begin.")));
      }
    } catch (error) {
      clear(list).append(el("div", { class: "empty error" }, `Could not load tasks: ${error.message || error}`));
    }
  }

  await refresh();
}

register({
  id: "missions", title: "Tasks", icon: "check_box",
  subtitle: "Autonomous task queue, verification and execution logs",
  render: renderMissions,
});
register({
  id: "tasks", title: "Tasks", icon: "check_box",
  subtitle: "Autonomous task queue, verification and execution logs",
  render: renderMissions,
});

export { stateChip };
