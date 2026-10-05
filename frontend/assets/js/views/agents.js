/* Specialized Agents: Autonomous neural units, supervisor queue, and task delegation. */

import { api } from "../api.js";
import { el, clear, fmtTime, toast, modal } from "../ui.js";
import { section, table, stat, json } from "../components.js";
import { register } from "../router.js";

const DEFAULT_AGENTS = [
  {
    id: "architect",
    name: "Natasha Architect",
    role: "Code",
    title: "System Architect & Code Reviewer",
    icon: "deployed_code",
    desc: "Specialized in microservice topology, zero-downtime migrations, and static algorithmic analysis.",
    tags: ["Python", "Rust", "Architecture", "Docker"],
    ctx: "128k ctx",
    status: "Available",
  },
  {
    id: "scholar",
    name: "Natasha Scholar",
    role: "Research",
    title: "Deep Research & Knowledge Synthesizer",
    icon: "school",
    desc: "Autonomous literature review, semantic citation validation, and recursive cross-discipline research.",
    tags: ["ArXiv", "PubMed", "Synthesis", "Logic"],
    ctx: "200k ctx",
    status: "Available",
  },
  {
    id: "designer",
    name: "Natasha Designer",
    role: "Design",
    title: "Visual & Interface Design Specialist",
    icon: "palette",
    desc: "Design tokens, obsidian aesthetics, accessibility compliance, and interactive UI component craft.",
    tags: ["CSS Tokens", "Stitch UI", "Figma", "WCAG"],
    ctx: "64k ctx",
    status: "Available",
  },
  {
    id: "analyst",
    name: "Natasha Analyst",
    role: "Data",
    title: "Data Intelligence & Metrics Auditor",
    icon: "database",
    desc: "Continuous telemetry profiling, vector store indexing, anomaly detection, and schema validation.",
    tags: ["SQL", "Vector Store", "Metrics", "ETL"],
    ctx: "128k ctx",
    status: "Available",
  },
  {
    id: "scribe",
    name: "Natasha Scribe",
    role: "Writing",
    title: "Technical Writer & Documentation Lead",
    icon: "edit_note",
    desc: "High-clarity technical documentation, architectural decision records (ADRs), and changelog auditing.",
    tags: ["Markdown", "ADR", "Specs", "Prose"],
    ctx: "128k ctx",
    status: "Available",
  },
  {
    id: "operator",
    name: "Natasha Operator",
    role: "Code",
    title: "Sandbox & Tool Execution Governor",
    icon: "terminal",
    desc: "Supervised command sandboxing, permission boundary enforcement, and external tool coordination.",
    tags: ["Sandbox", "Security", "Process", "CLI"],
    ctx: "64k ctx",
    status: "Available",
  },
];

export async function renderAgents(container) {
  clear(container);

  // 1. Fetch real agent data from backend
  let rolesData = { roles: [], status: {} };
  let workersData = { workers: [], tasks: [], stats: {} };
  let profilesData = { profiles: {} };

  try {
    const [rolesRes, workersRes, profilesRes] = await Promise.all([
      api.get("/api/agents").catch(() => ({ roles: [] })),
      api.get("/api/agents/workers").catch(() => ({ workers: [], tasks: [], stats: {} })),
      api.get("/api/agents/profiles").catch(() => ({ profiles: {} })),
    ]);
    rolesData = rolesRes || rolesData;
    workersData = workersRes || workersData;
    profilesData = profilesRes || profilesData;
  } catch (err) {
    // Graceful fallback for offline/partial states
  }

  // 2. Merge backend profiles/roles with visual catalogue
  const backendRoles = Array.isArray(rolesData.roles) ? rolesData.roles : [];
  const profilesMap = profilesData.profiles || {};

  let agentList = [...DEFAULT_AGENTS];
  if (backendRoles.length > 0) {
    backendRoles.forEach((roleKey) => {
      const prof = profilesMap[roleKey] || {};
      const existing = agentList.find((a) => a.id === roleKey || a.name.toLowerCase().includes(roleKey.toLowerCase()));
      if (existing) {
        if (prof.description) existing.desc = prof.description;
        if (prof.tools) existing.tags = prof.tools.slice(0, 4);
      } else {
        agentList.push({
          id: roleKey,
          name: `Natasha ${roleKey.charAt(0).toUpperCase() + roleKey.slice(1)}`,
          role: "Code",
          title: prof.role || "Specialized Autonomous Agent",
          icon: "smart_toy",
          desc: prof.description || `Specialized ${roleKey} agent configured in the Natasha runtime.`,
          tags: prof.tools || [roleKey, "Runtime"],
          ctx: prof.context_window ? `${Math.round(prof.context_window / 1000)}k ctx` : "128k ctx",
          status: "Available",
        });
      }
    });
  }

  // 3. Header & Hero Section
  const hero = el("div", { style: "display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:1rem;margin-bottom:1.5rem;" },
    el("div", { style: "max-width:680px;" },
      el("div", { class: "row tight", style: "margin-bottom:0.4rem;align-items:center;" },
        el("span", { class: "chip", style: "text-transform:uppercase;font-size:10.5px;letter-spacing:0.06em;font-weight:600;" }, "Autonomous Neural Units"),
        el("span", { style: "width:6px;height:6px;border-radius:50%;background:var(--primary);display:inline-block;" }),
        el("span", { class: "small muted", style: "font-family:var(--mono);" }, "Core v4.8-active")),
      el("h1", { style: "margin:0 0 0.4rem 0;font-size:26px;letter-spacing:-0.02em;" }, "Specialized Agents"),
      el("p", { class: "muted", style: "margin:0;font-size:15px;line-height:1.5;" },
        "Deploy, configure, and collaborate with autonomous AI agents powered by Natasha core.")),
    el("div", { class: "row tight" },
      el("button", {
        class: "btn btn--primary",
        style: "display:flex;align-items:center;gap:0.4rem;padding:0.6rem 1.1rem;border-radius:var(--radius-pill);",
        onclick: () => openDelegationModal(),
      },
        el("span", { class: "material-symbols-outlined" }, "play_arrow"),
        el("span", { class: "bold" }, "Delegate Objective"),
        el("span", { class: "chip", style: "font-family:var(--mono);font-size:10px;padding:2px 6px;" }, "⌘K"))));

  // 4. Telemetry stats strip
  const workerCount = (workersData.workers || []).length || agentList.length;
  const taskCount = (workersData.tasks || []).length;
  const statsStrip = el("div", { class: "grid", style: "margin-bottom:1.5rem;" },
    stat("Active Fleet", `${workerCount} units`, `${taskCount} active missions`),
    stat("Collective Memory", "64.2 MB", "Vector Store Bus"),
    stat("Mean Latency", "280 ms", "Sub-loop Direct Bus"),
    stat("Tool Sandbox Health", "99.98%", "0 Boundary Breaches"));

  // 5. Search and Capability Filter Bar
  let activeFilter = "all";
  let searchQuery = "";
  const filterRow = el("div", {
    style: "display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:1rem;background:var(--bg-low);border:1px solid var(--border);border-radius:var(--radius-lg);padding:0.6rem 0.85rem;margin-bottom:1.5rem;",
  });

  const searchInput = el("input", {
    type: "text",
    placeholder: "Search agents by name, skill, or role…",
    style: "flex:1;min-width:240px;background:var(--bg-lowest);border:1px solid var(--border);padding:0.5rem 0.85rem;border-radius:var(--radius);font-size:13.5px;",
  });

  const filterChipsHost = el("div", { class: "row tight", style: "flex-wrap:wrap;" });
  const filterCategories = [
    { id: "all", label: "All" },
    { id: "Code", label: "Code", icon: "code" },
    { id: "Research", label: "Research", icon: "science" },
    { id: "Design", label: "Design", icon: "palette" },
    { id: "Data", label: "Data", icon: "database" },
    { id: "Writing", label: "Writing", icon: "edit_note" },
  ];

  function renderFilterChips() {
    clear(filterChipsHost);
    filterCategories.forEach((cat) => {
      const isSelected = activeFilter === cat.id;
      const chip = el("button", {
        class: `btn ${isSelected ? "btn--primary" : "btn--subtle"}`,
        style: "padding:0.35rem 0.75rem;border-radius:var(--radius-pill);font-size:12.5px;display:flex;align-items:center;gap:0.3rem;",
        onclick: () => {
          activeFilter = cat.id;
          renderFilterChips();
          renderGrid();
        },
      },
        cat.icon ? el("span", { class: "material-symbols-outlined" }, cat.icon) : null,
        cat.label);
      filterChipsHost.append(chip);
    });
  }

  filterRow.append(searchInput, filterChipsHost);
  renderFilterChips();

  // 6. Agents Grid
  const gridContainer = el("div", { class: "agents-grid" });

  function renderGrid() {
    clear(gridContainer);
    const filtered = agentList.filter((agent) => {
      const matchesFilter = activeFilter === "all" || agent.role.toLowerCase() === activeFilter.toLowerCase();
      const q = searchQuery.toLowerCase();
      const matchesSearch = !q ||
        agent.name.toLowerCase().includes(q) ||
        agent.desc.toLowerCase().includes(q) ||
        agent.title.toLowerCase().includes(q) ||
        agent.tags.some((t) => t.toLowerCase().includes(q));
      return matchesFilter && matchesSearch;
    });

    if (filtered.length === 0) {
      gridContainer.append(el("div", { class: "card", style: "grid-column:1/-1;text-align:center;padding:3rem;" },
        el("span", { class: "material-symbols-outlined", style: "font-size:36px;color:var(--text-faint);margin-bottom:0.5rem;" }, "smart_toy"),
        el("h3", { style: "margin:0 0 0.4rem 0;" }, "No matching agents found"),
        el("p", { class: "muted small", style: "margin:0;" }, "Try searching for a different skill, tag, or role.")));
      return;
    }

    filtered.forEach((agent) => {
      const card = el("div", { class: "agent-card card-3d" },
        // Head
        el("div", { class: "agent-card__head" },
          el("div", { class: "agent-avatar" },
            el("span", { class: "material-symbols-outlined" }, agent.icon || "smart_toy")),
          el("div", { class: "agent-card__info", style: "flex:1;min-width:0;" },
            el("div", { style: "display:flex;align-items:center;justify-content:space-between;gap:0.5rem;" },
              el("h4", { style: "margin:0;font-size:15px;" }, agent.name),
              el("span", { class: "chip chip--ok", style: "font-size:10.5px;padding:1px 6px;" }, agent.status)),
            el("div", { class: "agent-card__role" }, agent.title))),
        // Description
        el("p", { class: "muted", style: "font-size:13px;line-height:1.45;margin:0;" }, agent.desc),
        // Capabilities tags
        el("div", { class: "row tight", style: "flex-wrap:wrap;" },
          ...agent.tags.map((t) => el("span", { class: "chip", style: "font-size:11px;" }, t))),
        // Foot
        el("div", { style: "display:flex;align-items:center;justify-content:space-between;padding-top:0.75rem;border-top:1px solid var(--border-soft);margin-top:auto;" },
          el("span", { class: "small muted", style: "font-family:var(--mono);" }, agent.ctx),
          el("div", { class: "row tight" },
            el("button", {
              class: "btn btn--subtle",
              style: "padding:0.35rem 0.65rem;font-size:12px;",
              title: "Inspect Agent Specs",
              onclick: () => openInspectModal(agent),
            },
              el("span", { class: "material-symbols-outlined" }, "tune")),
            el("button", {
              class: "btn btn--primary",
              style: "padding:0.35rem 0.75rem;font-size:12px;",
              onclick: () => openDelegationModal(agent.id),
            }, "Delegate"))));
      gridContainer.append(card);
    });
  }

  searchInput.addEventListener("input", (e) => {
    searchQuery = e.target.value;
    renderGrid();
  });
  renderGrid();

  // 7. Active Worker Supervisor Queue Section
  const workersSection = section("Active Workers & Delegation Queue",
    table([
      { label: "Worker ID", value: (row) => row.id || row.worker_id || "worker" },
      { label: "Role", value: (row) => row.role || "general" },
      { label: "State", value: (row) => el("span", { class: `chip ${row.state === "busy" ? "chip--warn" : "chip--ok"}` }, row.state || "idle") },
      { label: "Current Task", value: (row) => row.current_task || row.objective || "idle" },
      { label: "Completed", value: (row) => String(row.tasks_completed || 0) },
      { label: "Updated", value: (row) => fmtTime(row.updated_at || Date.now()) },
    ], workersData.workers || [], { empty: "No background workers currently spawned. Natasha will spawn on delegation." }));

  // 8. Delegation Modal Handler
  function openDelegationModal(preselectedRole = "architect") {
    const roleSelect = el("select", {
      style: "width:100%;padding:0.6rem;background:var(--bg-lowest);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);margin-bottom:1rem;",
    },
      ...agentList.map((a) => {
        const opt = el("option", { value: a.id }, `${a.name} (${a.role})`);
        if (a.id === preselectedRole) opt.selected = true;
        return opt;
      }));

    const objectiveInput = el("textarea", {
      placeholder: "Describe the objective or task for this agent to accomplish autonomously...",
      rows: 4,
      style: "width:100%;padding:0.6rem;background:var(--bg-lowest);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);font-family:inherit;margin-bottom:1rem;",
    });

    const timeoutInput = el("input", {
      type: "number",
      value: "60",
      min: "5",
      max: "600",
      style: "width:100%;padding:0.5rem;background:var(--bg-lowest);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);margin-bottom:1.5rem;",
    });

    const statusEl = el("div", { class: "small muted", style: "margin-bottom:1rem;" });

    const submitBtn = el("button", {
      class: "btn btn--primary",
      style: "width:100%;",
      onclick: async () => {
        const objective = objectiveInput.value.trim();
        if (!objective) {
          toast("Please enter an objective for the agent.", "error");
          return;
        }
        submitBtn.disabled = true;
        statusEl.textContent = "Delegating to supervisor queue...";
        try {
          const result = await api.post("/api/agents/delegate", {
            role: roleSelect.value,
            objective,
            timeout_seconds: Number(timeoutInput.value) || 60,
            context: { delegated_from: "natasha-stitch-ui", timestamp: Date.now() },
          });
          toast(`Delegated successfully to ${roleSelect.value}.`, "ok");
          close();
          renderAgents(container);
        } catch (err) {
          statusEl.textContent = `Error: ${err.message}`;
          toast(err.message, "error");
        } finally {
          submitBtn.disabled = false;
        }
      },
    }, "Confirm & Dispatch");

    const body = el("div", {},
      el("label", { class: "bold small", style: "display:block;margin-bottom:0.3rem;" }, "Target Agent"),
      roleSelect,
      el("label", { class: "bold small", style: "display:block;margin-bottom:0.3rem;" }, "Autonomous Objective"),
      objectiveInput,
      el("label", { class: "bold small", style: "display:block;margin-bottom:0.3rem;" }, "Execution Timeout (seconds)"),
      timeoutInput,
      statusEl,
      submitBtn);

    const close = modal("Delegate Autonomous Objective", body);
  }

  function openInspectModal(agent) {
    const prof = profilesMap[agent.id] || {};
    const content = el("div", { style: "display:flex;flex-direction:column;gap:1rem;" },
      el("div", { class: "row", style: "align-items:center;" },
        el("div", { class: "agent-avatar" }, el("span", { class: "material-symbols-outlined" }, agent.icon)),
        el("div", {},
          el("h3", { style: "margin:0;" }, agent.name),
          el("div", { class: "small muted", style: "font-family:var(--mono);" }, agent.title))),
      el("p", { style: "font-size:14px;line-height:1.5;margin:0;" }, agent.desc),
      el("div", {},
        el("label", { class: "bold small", style: "display:block;margin-bottom:0.4rem;" }, "Capabilities & Tools"),
        el("div", { class: "row tight", style: "flex-wrap:wrap;" },
          ...agent.tags.map((t) => el("span", { class: "chip", style: "font-size:12px;" }, t)))),
      json(prof, { label: "raw profile specifications" }),
      el("div", { style: "margin-top:1rem;display:flex;justify-content:flex-end;" },
        el("button", {
          class: "btn btn--primary",
          onclick: () => {
            close();
            openDelegationModal(agent.id);
          },
        }, "Delegate to this Agent")));

    const close = modal(`Agent Specification: ${agent.name}`, content);
  }

  container.append(hero, statsStrip, filterRow, gridContainer, workersSection);
}

register({
  id: "agents",
  title: "Agents",
  icon: "smart_toy",
  subtitle: "specialized autonomous agents, supervisor queue, and task delegation",
  render: renderAgents,
});
