/* Projects: Neural Repositories, Workspaces, Artifacts and Associated Tasks.
   Obsidian Intelligence Stitch Design Implementation with real Natasha backend. */

import { api } from "../api.js";
import { el, clear, fmtTime, fmtBytes, toast, modal, kv } from "../ui.js";
import { section, table, json, stat, emptyState } from "../components.js";
import { register, go } from "../router.js";
import { store } from "../store.js";

export async function renderProjects(container, { arg } = {}) {
  clear(container);

  // Status banner (Offline / Live Sync)
  const syncBanner = el("div", {
    class: "row",
    style: "justify-content:space-between;align-items:center;background:var(--bg-elev);border:1px solid var(--border);border-radius:var(--radius);padding:0.65rem 1rem;margin-bottom:1.5rem",
  },
  el("div", { class: "row tight" },
    el("span", { class: "pulse-dot" }),
    el("span", { class: "bold small" }, "Workspace Telemetry Synchronized"),
    el("span", { class: "small muted" }, "• Real-time local state")),
  el("button", {
    class: "btn small btn--ghost",
    onclick: () => refresh(),
  }, el("span", { class: "material-symbols-outlined icon-sm" }, "sync"), "Re-sync"));

  // Top header with actions
  const header = el("div", {
    class: "row",
    style: "justify-content:space-between;align-items:flex-end;margin-bottom:1.5rem",
  },
  el("div", {},
    el("span", { class: "small muted bold", style: "letter-spacing:0.06em;text-transform:uppercase" }, "Workspace Management"),
    el("h1", { style: "margin:0.25rem 0 0" }, "Neural Repositories & Workspaces")),
  el("div", { class: "row tight" },
    el("button", {
      class: "btn btn--primary",
      onclick: () => createProjectModal(container),
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "add"), "New Project",
       el("kbd", { class: "small mono", style: "background:rgba(0,0,0,0.2);padding:1px 5px;border-radius:3px;margin-left:4px" }, "⌘K"))));

  // Search & Filter Bar
  const searchInput = el("input", {
    placeholder: "Search workspaces, artifacts, or tags (e.g. LLM, RAG)...",
    style: "max-width:420px",
    oninput: () => renderCards(),
  });

  let activeFilter = "all";
  let isGridView = true;

  const filterTabs = el("div", { class: "filter-tabs" });
  const allTab = el("button", { class: "filter-chip active", onclick: () => setFilter("all") }, "All");
  const activeTab = el("button", { class: "filter-chip", onclick: () => setFilter("active") }, "Active");
  const sharedTab = el("button", { class: "filter-chip", onclick: () => setFilter("shared") }, "Shared");
  const archivedTab = el("button", { class: "filter-chip", onclick: () => setFilter("archived") }, "Archived");

  const viewToggle = el("button", {
    class: "icon-btn small", title: "Toggle Grid/List view",
    onclick: () => {
      isGridView = !isGridView;
      viewToggle.querySelector(".material-symbols-outlined").textContent = isGridView ? "grid_view" : "list";
      renderCards();
    },
  }, el("span", { class: "material-symbols-outlined" }, "grid_view"));

  filterTabs.append(allTab, activeTab, sharedTab, archivedTab, viewToggle);

  const controlBar = el("div", {
    class: "row",
    style: "justify-content:space-between;align-items:center;margin-bottom:1.5rem",
  }, searchInput, filterTabs);

  const gridContainer = el("div", { class: "project-grid" });
  container.append(syncBanner, header, controlBar, gridContainer);

  let projectList = [];

  function setFilter(filter) {
    activeFilter = filter;
    [allTab, activeTab, sharedTab, archivedTab].forEach((t) => t.classList.remove("active"));
    if (filter === "all") allTab.classList.add("active");
    else if (filter === "active") activeTab.classList.add("active");
    else if (filter === "shared") sharedTab.classList.add("active");
    else if (filter === "archived") archivedTab.classList.add("active");
    renderCards();
  }

  function renderCards() {
    clear(gridContainer);
    gridContainer.style.gridTemplateColumns = isGridView ? "repeat(auto-fill, minmax(320px, 1fr))" : "1fr";

    const query = searchInput.value.trim().toLowerCase();
    const filtered = projectList.filter((proj) => {
      if (activeFilter === "active" && proj.status !== "active") return false;
      if (activeFilter === "archived" && proj.status !== "archived") return false;
      if (!query) return true;
      return (proj.name + " " + proj.description + " " + proj.tags.join(" ")).toLowerCase().includes(query);
    });

    if (!filtered.length) {
      gridContainer.append(emptyState("No projects match the specified filter or query."));
      return;
    }

    filtered.forEach((proj) => {
      const card = el("div", {
        class: "project-card tactile-card",
        data3dTilt: "true",
      },
      el("div", {},
        el("div", { class: "row", style: "justify-content:space-between;align-items:flex-start;margin-bottom:0.5rem" },
          el("div", { class: "project-card__title" }, proj.name),
          el("span", { class: `chip ${proj.status === "active" ? "chip--ok" : "chip--muted"}` }, proj.status)),
        el("p", { class: "project-card__desc" }, proj.description),
        el("div", { class: "row tight", style: "margin-top:0.6rem" },
          ...proj.tags.map((t) => el("span", { class: "tag" }, t)))),
      el("div", { class: "project-card__stats" },
        el("span", { class: "row tight" },
          el("span", { class: "material-symbols-outlined icon-sm" }, "draft"),
          `${proj.artifactCount} files`),
        el("span", { class: "row tight" },
          el("span", { class: "material-symbols-outlined icon-sm" }, "task_alt"),
          `${proj.taskCount} tasks`),
        el("span", { style: "margin-left:auto" }, fmtTime(proj.updatedAt))),
      el("div", { class: "row tight", style: "padding-top:0.75rem;border-top:1px solid var(--border-soft)" },
        el("button", {
          class: "btn small btn--primary",
          onclick: (e) => { e.stopPropagation(); openProjectDetail(proj); },
        }, "Open Workspace"),
        el("button", {
          class: "btn small",
          onclick: (e) => { e.stopPropagation(); go("chat"); },
        }, "Chat")));

      gridContainer.append(card);
    });
  }

  function openProjectDetail(proj) {
    modal({
      title: proj.name,
      body: el("div", {},
        el("p", { class: "muted" }, proj.description),
        kv([
          ["Scope", proj.scope],
          ["Status", proj.status],
          ["Files Generated", proj.artifactCount],
          ["Associated Tasks", proj.taskCount],
          ["Last Updated", fmtTime(proj.updatedAt)],
        ]),
        section("Project Artifacts",
          proj.artifacts && proj.artifacts.length
            ? table([
              { label: "File Name", value: "name" },
              { label: "Size", value: (row) => fmtBytes(row.bytes) },
              { label: "Modified", value: (row) => fmtTime(row.modified_at) },
              { label: "Download", value: (row) => el("a", {
                class: "btn small", target: "_blank",
                href: `/api/artifacts/download?path=${encodeURIComponent(row.path)}`,
              }, "Download") },
            ], proj.artifacts)
            : el("p", { class: "small muted" }, "No artifacts recorded for this workspace.")),
        el("div", { class: "row tight", style: "margin-top:1rem" },
          el("button", { class: "btn btn--primary", onclick: () => go("tasks") }, "View Tasks"),
          el("button", { class: "btn", onclick: () => go("artifacts") }, "All Artifacts"))),
      actions: [{ label: "Close" }],
    });
  }

  function createProjectModal(host) {
    const nameInput = el("input", { placeholder: "Project / Workspace name (e.g. Ingestion Pipeline)" });
    const descInput = el("textarea", { placeholder: "Brief description of the workspace and goals...", rows: "3" });
    const scopeInput = el("input", { placeholder: "Tags or scopes, comma separated (e.g. backend, rag, vision)" });

    modal({
      title: "New Neural Workspace",
      body: el("div", {},
        el("label", {}, "Workspace Title"), nameInput,
        el("label", { style: "margin-top:0.5rem" }, "Description"), descInput,
        el("label", { style: "margin-top:0.5rem" }, "Tags / Focus Scopes"), scopeInput),
      actions: [
        { label: "Cancel" },
        {
          label: "Initialize Workspace", kind: "primary",
          onClick: async () => {
            const name = nameInput.value.trim();
            if (!name) { toast("Workspace name is required", "error"); return true; }
            try {
              // Create an initial scoping mission in Natasha for this project
              const created = await api.post("/api/missions", {
                title: name,
                objective: descInput.value.trim() || `Workspace initialized for ${name}`,
                scope: scopeInput.value.split(",").map((s) => s.trim()).filter(Boolean),
                auto_run: false,
              });
              toast("Project workspace initialized", "ok");
              await refresh();
            } catch (error) { toast(error.message, "error"); }
          },
        },
      ],
    });
  }

  async function refresh() {
    try {
      const [artifactsPayload, missionsPayload, creationPayload] = await Promise.all([
        api.get("/api/artifacts", { limit: 200 }).catch(() => ({ artifacts: [] })),
        api.get("/api/missions", { limit: 100 }).catch(() => ({ missions: [] })),
        api.get("/api/creation/jobs", { limit: 50 }).catch(() => ({ jobs: [] })),
      ]);

      const artifacts = artifactsPayload.artifacts || [];
      const missions = missionsPayload.missions || [];
      const creationJobs = creationPayload.jobs || [];

      // Group missions and artifacts into logical projects
      const projectMap = new Map();

      // Add default primary project for Natasha Core
      projectMap.set("core", {
        id: "core",
        name: "Natasha Core Autonomous Workspace",
        description: "Primary local runtime workspace for autonomous task orchestration, agent tool execution, and local inference.",
        status: "active",
        tags: ["Core", "Runtime", "Autonomous"],
        artifacts: [],
        tasks: [],
        taskCount: 0,
        artifactCount: 0,
        scope: "Full System",
        updatedAt: new Date().toISOString(),
      });

      missions.forEach((m) => {
        const key = m.title ? m.title.toLowerCase().replace(/[^a-z0-9]+/g, "-") : `mission-${m.id.slice(0, 8)}`;
        if (!projectMap.has(key)) {
          projectMap.set(key, {
            id: key,
            name: m.title || `Workspace ${m.id.slice(0, 8)}`,
            description: m.objective || "Autonomous mission context",
            status: ["COMPLETED", "VERIFIED"].includes(String(m.state || "").toUpperCase()) ? "archived" : "active",
            tags: m.scope && m.scope.length ? m.scope : ["Mission", "Autonomous"],
            artifacts: [],
            tasks: [m],
            taskCount: 1,
            artifactCount: 0,
            scope: (m.scope || []).join(", ") || "General",
            updatedAt: m.updated_at || m.created_at || new Date().toISOString(),
          });
        } else {
          const entry = projectMap.get(key);
          entry.tasks.push(m);
          entry.taskCount += 1;
        }
      });

      artifacts.forEach((art) => {
        let placed = false;
        if (art.mission_id) {
          for (const proj of projectMap.values()) {
            if (proj.tasks.some((t) => t.id === art.mission_id)) {
              proj.artifacts.push(art);
              proj.artifactCount += 1;
              placed = true;
              break;
            }
          }
        }
        if (!placed) {
          const core = projectMap.get("core");
          core.artifacts.push(art);
          core.artifactCount += 1;
        }
      });

      projectList = Array.from(projectMap.values());
      renderCards();
    } catch (error) {
      clear(gridContainer).append(el("div", { class: "empty error" }, `Could not load projects: ${error.message || error}`));
    }
  }

  await refresh();
}

register({
  id: "projects", title: "Projects", icon: "folder",
  subtitle: "Neural repositories, workspaces and artifact bundles",
  render: renderProjects,
});
